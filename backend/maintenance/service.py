import json

from contracts.maintenance import (
    FailedIntake,
    IMaintenanceIntake,
    IMaintenanceTracking,
    MaintenanceEvent,
    MaintenanceIntake,
    MaintenanceView,
    NeedsClarificationIntake,
    Priority,
    RegisteredIntake,
    StatusResultError,
    StatusResultOk,
)
from contracts.maintenance import (
    MaintenanceStatus as S,
)
from contracts.maintenance import (
    TransitionFailureCode as F,
)
from llm import Assessment, AssessmentFailure
from maintenance.rules import emergency_indicators
from permissions import (
    SQLiteManagerRepository,
    manager_id_from_auth,
    require_manager_property_access,
)
from shared import new_id, utcnow

TRANSITIONS = {
    S.ASSESSED: {S.APPROVED, S.CANCELLED},
    S.APPROVED: {S.IN_PROGRESS, S.CANCELLED},
    S.IN_PROGRESS: {S.AWAITING_CONFIRMATION, S.CANCELLED},
    S.AWAITING_CONFIRMATION: {S.CLOSED, S.IN_PROGRESS, S.CANCELLED},
    S.SUBMITTED: {S.CANCELLED},
    S.UNDER_REVIEW: {S.CANCELLED},
}


class MaintenanceService(IMaintenanceIntake, IMaintenanceTracking):
    """One trusted, pre-authorised property context per service instance."""

    def __init__(self, store, assessor, auth):
        self.store, self.assessor, self.auth = store, assessor, auth

    def require_manager_access(self):
        """Verify the server-resolved manager against the authoritative binding.

        The property carried by an auth/session token is a requested scope, not
        proof of access. Missing managers and missing bindings are default-deny.
        """
        manager_id = manager_id_from_auth(self.auth)
        with self.store.transaction() as db:
            return require_manager_property_access(
                manager_id,
                self.auth.property_id,
                SQLiteManagerRepository(db),
            )

    def _load(self, db, request_id):
        row = db.execute(
            "SELECT * FROM mc_request WHERE id=? AND property_id=?",
            (request_id, self.auth.property_id),
        ).fetchone()
        if not row:
            return None
        v = MaintenanceView.model_validate_json(row["document"])
        if not self.auth.manager_id and v.tenant_id != self.auth.tenant_id:
            return None
        return v

    def _event(self, db, v, previous, actor, reason=None, evidence=None, extra=None):
        event = MaintenanceEvent(
            event_id=new_id(),
            request_id=v.request_id,
            property_id=v.property_id,
            occurred_at=utcnow(),
            actor=actor,
            from_status=previous,
            to_status=v.status,
            priority=v.priority,
            reason=reason,
            evidence_refs=evidence or [],
        )
        v.updated_at = event.occurred_at
        v.events.append(event)
        db.execute(
            "INSERT INTO mc_history VALUES (?,?,?)",
            (event.event_id, v.request_id, event.model_dump_json()),
        )
        for target in ["communication", "evidence"] + (extra or []):
            payload = event.model_dump(mode="json")
            payload.update(
                recipient_id="tenant:" + v.tenant_id,
                review_required=v.emergency_flag or v.status == S.CANCELLED,
                message_type="maintenance_status",
            )
            if target == "manager_alert":
                payload["recipient_id"] = "property_manager:" + v.property_id
                payload["message_type"] = "emergency_alert"
            db.execute(
                "INSERT INTO mc_outbox(event_id,target,request_id,property_id,document) VALUES (?,?,?,?,?)",
                (event.event_id, target, v.request_id, v.property_id, json.dumps(payload)),
            )
        db.execute(
            "UPDATE mc_request SET document=?,version=? WHERE id=?",
            (v.model_dump_json(), v.version, v.request_id),
        )
        return event

    def submit_issue(self, intake: MaintenanceIntake, *, strict_replay=False):
        if intake.property_id != self.auth.property_id or intake.tenant_id != self.auth.tenant_id:
            return FailedIntake(code="forbidden", retryable=False)
        missing = [k for k in ("title", "description") if not getattr(intake, k).strip()]
        if missing:
            return NeedsClarificationIntake(missing_fields=missing)
        # Commit intake and emergency notification before any model call.
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM mc_request WHERE property_id=? AND (message_id=? OR intake_event=?)",
                (intake.property_id, intake.message_id, intake.event_id),
            ).fetchone()
            if row:
                if row["tenant_id"] != intake.tenant_id:
                    return FailedIntake(code="forbidden", retryable=False)
                if strict_replay:
                    original = MaintenanceView.model_validate_json(row["document"])
                    if (
                        (original.title, original.description, original.location)
                        != (intake.title, intake.description, intake.location)
                        or original.events[0].evidence_refs != intake.evidence_refs
                    ):
                        return FailedIntake(code="idempotency_conflict", retryable=False)
                return RegisteredIntake.model_validate_json(row["original_result"])
            emergency = intake.emergency_flag or bool(
                emergency_indicators(intake.title + ". " + intake.description)
            )
            v = MaintenanceView(
                request_id=new_id(),
                tenant_id=intake.tenant_id,
                property_id=intake.property_id,
                message_id=intake.message_id,
                title=intake.title,
                description=intake.description,
                location=intake.location,
                status=S.SUBMITTED,
                version=1,
                emergency_flag=emergency,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            initial = RegisteredIntake(request_id=v.request_id, persisted_status=v.status)
            db.execute(
                "INSERT INTO mc_request VALUES (?,?,?,?,?,?,?,?)",
                (
                    v.request_id,
                    v.property_id,
                    v.tenant_id,
                    v.message_id,
                    intake.event_id,
                    1,
                    v.model_dump_json(),
                    initial.model_dump_json(),
                ),
            )
            self._event(
                db,
                v,
                None,
                "tenant:" + v.tenant_id,
                evidence=intake.evidence_refs,
                extra=["manager_alert"] if emergency else [],
            )
        try:
            a = self.assessor.assess(
                title=intake.title,
                description=intake.description,
                location=intake.location,
                intake_emergency_flag=emergency,
            )
            if isinstance(a, Assessment):
                if (
                    not isinstance(a.priority, Priority)
                    or not a.rationale.strip()
                    or not 0 <= a.confidence <= 1
                ):
                    a = AssessmentFailure("llm_error", "Invalid assessment")
            else:
                a = (
                    a
                    if isinstance(a, AssessmentFailure)
                    else AssessmentFailure("llm_error", "Invalid assessment")
                )
        except Exception:  # noqa: BLE001 - plugin boundary must fail closed without leaking secrets
            a = AssessmentFailure("llm_error", "Assessor failed; manual review required")
        with self.store.transaction() as db:
            v = self._load(db, v.request_id)
            # Late model replies cannot reopen a cancelled or modified request.
            if v.status == S.SUBMITTED and v.version == 1:
                old = v.status
                v.version += 1
                if isinstance(a, Assessment) and a.confidence >= 0.6:
                    v.status, v.priority = S.ASSESSED, a.priority
                    new_emergency = a.emergency_indicator or a.priority == Priority.EMERGENCY
                    alert = new_emergency and not v.emergency_flag
                    v.emergency_flag = v.emergency_flag or new_emergency
                    if v.emergency_flag:
                        v.priority = Priority.EMERGENCY
                    v.provider_suggestion = a.provider_suggestion
                    reason = json.dumps(
                        {
                            "rationale": a.rationale,
                            "confidence": a.confidence,
                            "source": a.source,
                            "source_message_id": str(v.message_id),
                            "evidence_refs": intake.evidence_refs,
                        }
                    )
                else:
                    v.status, alert = S.UNDER_REVIEW, False
                    reason = (
                        a.detail if isinstance(a, AssessmentFailure) else "Uncertain assessment"
                    )
                self._event(
                    db,
                    v,
                    old,
                    "system:priority_assessor",
                    reason,
                    intake.evidence_refs,
                    ["manager_alert"] if alert else [],
                )
            result = RegisteredIntake(
                request_id=v.request_id, persisted_status=v.status, priority=v.priority
            )
            db.execute(
                "UPDATE mc_request SET original_result=? WHERE id=?",
                (result.model_dump_json(), v.request_id),
            )
            return result

    def get(self, request_id, property_id):
        if property_id != self.auth.property_id:
            return None
        if self.auth.manager_id:
            self.require_manager_access()
        with self.store.transaction() as db:
            return self._load(db, request_id)

    def list_requests(self):
        if self.auth.manager_id:
            self.require_manager_access()
        with self.store.transaction() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM mc_request WHERE property_id=?", (self.auth.property_id,)
                )
            ]
            return [v for i in ids if (v := self._load(db, i)) is not None]

    def change_status(self, command):
        if not self.auth.manager_id or command.manager_id != self.auth.manager_id:
            return StatusResultError(code=F.FORBIDDEN)
        try:
            self.require_manager_access()
        except PermissionError:
            return StatusResultError(code=F.FORBIDDEN)
        with self.store.transaction() as db:
            v = self._load(db, command.request_id)
            if not v:
                return StatusResultError(code=F.NOT_FOUND)
            if v.version != command.expected_version:
                return StatusResultError(
                    code=F.VERSION_MISMATCH, current_status=v.status, current_version=v.version
                )
            if command.target_status not in TRANSITIONS.get(v.status, set()):
                return StatusResultError(code=F.ILLEGAL_TRANSITION)
            if not command.reason or not command.reason.strip():
                return StatusResultError(
                    code=F.ILLEGAL_TRANSITION,
                    detail="A recorded manager reason/decision is required.",
                )
            if command.requires_evidence() and not any(e.strip() for e in command.evidence_refs):
                return StatusResultError(code=F.EVIDENCE_REQUIRED)
            old = v.status
            v.status, v.version = command.target_status, v.version + 1
            event = self._event(
                db, v, old, "manager:" + self.auth.manager_id, command.reason, command.evidence_refs
            )
            return StatusResultOk(
                request_id=v.request_id, persisted_status=v.status, version=v.version, event=event
            )

    def manual_assessment(self, request_id, expected_version, priority, rationale):
        if not self.auth.manager_id:
            return StatusResultError(code=F.FORBIDDEN)
        try:
            self.require_manager_access()
        except PermissionError:
            return StatusResultError(code=F.FORBIDDEN)
        with self.store.transaction() as db:
            v = self._load(db, request_id)
            if not v:
                return StatusResultError(code=F.NOT_FOUND)
            if v.version != expected_version:
                return StatusResultError(
                    code=F.VERSION_MISMATCH, current_status=v.status, current_version=v.version
                )
            if v.status not in (S.SUBMITTED, S.UNDER_REVIEW, S.ASSESSED) or not rationale.strip():
                return StatusResultError(code=F.ILLEGAL_TRANSITION)
            old = v.status
            alert = priority == Priority.EMERGENCY and not v.emergency_flag
            v.priority, v.status, v.version = priority, S.ASSESSED, v.version + 1
            v.emergency_flag = v.emergency_flag or priority == Priority.EMERGENCY
            e = self._event(
                db,
                v,
                old,
                "manager:" + self.auth.manager_id,
                rationale,
                extra=["manager_alert"] if alert else [],
            )
            return StatusResultOk(
                request_id=v.request_id, persisted_status=v.status, version=v.version, event=e
            )

    def summaries(self):
        self.require_manager_access()
        return [
            {
                "request_id": v.request_id,
                "property_id": v.property_id,
                "priority": v.priority,
                "status": v.status,
                "updated_at": v.updated_at,
                "outstanding_reason": None
                if v.status in (S.CLOSED, S.CANCELLED)
                else v.status.value,
            }
            for v in self.list_requests()
        ]
