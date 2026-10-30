"""Communication service: message intake -> classification -> routing ->
fact-grounded draft (guide Ch.13).

Flow (one inbound tenant message):
 1. Auth context binding verified against the `tenant_property` table —
    identity comes from the auth context, never from user input.
 2. Message persisted (message_id generated here, the owning entry point).
 3. Classification via a `BaseClassifier` (default: the labelled
    KeywordClassifier test double). Binding rule: invalid classifier
    output (None) and unambiguous 'unknown' BOTH route to human handoff —
    neither may produce an automatic tenant reply.
 4. Routing per contracts:
      maintenance -> IMaintenanceIntake.submit_issue (UC1)
      lease_rent  -> ILeaseRentQuery.get_facts (UC2)
      smalltalk   -> simple acknowledgement draft (no module call)
 5. Drafting grounded ONLY in returned facts (no fabrication). A deterministic
    policy selects auto-eligible or manager-review drafts. Eligibility is
    not delivery: this service does not dispatch tenant replies.
 6. Delivery-result recording is truthful: ACCEPTED is queued, NOT sent.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel

from contracts import (
    Draft,
    DraftStatus,
    EventId,
    NotificationEvent,
    NotificationKind,
    Priority,
    IMaintenanceIntake,
    ILeaseRentQuery,
    INotificationService,
    IntakeResult,
    MaintenanceIntake,
    MessageId,
    NotificationStatus,
    PropertyId,
    QuestionType,
    RecipientId,
    RequestId,
    ReviewRequest,
    TenantId,
)
from contracts.lease_rent import (
    Fact,
    FactAvailability,
    LeaseRentFactResult,
    LeaseRentQuery,
)
from . import policy, templates
from llm.classifier import BaseClassifier, Category, KeywordClassifier
from shared import AuthContext, new_id, utcnow

# event_id conventions (stable dedup keys, guide 18.2):
#   one intake per message, one reply notification per message.
INTAKE_EVENT_PREFIX = "intake:"
REPLY_EVENT_PREFIX = "reply:"


def intake_event_id(message_id: str) -> EventId:
    return EventId(f"{INTAKE_EVENT_PREFIX}{message_id}")


def reply_event_id(message_id: str) -> EventId:
    return EventId(f"{REPLY_EVENT_PREFIX}{message_id}")


# Truthful delivery labels: ACCEPTED must never be shown as "sent".
_DELIVERY_LABELS: dict[NotificationStatus, str] = {
    NotificationStatus.ACCEPTED: "accepted (queued — not sent yet)",
    NotificationStatus.PENDING_REVIEW: "pending review",
    NotificationStatus.DELIVERED: "delivered",
    NotificationStatus.FAILED: "failed",
    NotificationStatus.PENDING_RETRY: "pending retry",
}


def delivery_label(status: NotificationStatus, detail: str | None = None) -> str:
    label = _DELIVERY_LABELS[status]
    if detail and status in (NotificationStatus.FAILED, NotificationStatus.PENDING_RETRY):
        label = f"{label}: {detail}"
    return label


class CommunicationOutcome(BaseModel):
    """Result of handling one inbound tenant message."""

    message_id: MessageId
    category: str | None  # None when the classifier output was invalid
    routed_to: Literal["maintenance", "lease_rent", "smalltalk", "human"]
    handoff_reason: str | None = None  # set when routed_to == "human"
    intake: IntakeResult | None = None  # UC1 result, when routed
    fact_result: LeaseRentFactResult | None = None  # UC2 result, when routed
    draft: Draft | None = None  # pending tenant reply (None: no auto reply)
    review: ReviewRequest | None = None  # only when review is required
    eligibility: policy.EligibilityDecision | None = None
    emergency_alert: policy.EmergencyAlert | None = None
    dispatch_state: Literal["not_dispatched"] = "not_dispatched"

    def tenant_response(self) -> dict:
        """Allowlisted acknowledgement; internal drafts/facts stay private.

        The future HTTP layer must authenticate and authorise the caller.
        This projection does not itself authenticate or deliver a reply.
        """
        return {"message_id": self.message_id,
                "reply_status": self.draft.status.value if self.draft else "manual_handling",
                "delivery_status": self.dispatch_state}


class CommunicationService:
    """Entry point for tenant-facing communication.

    All cross-module calls go through contracts (IMaintenanceIntake,
    ILeaseRentQuery, INotificationService). The classifier is injected so
    the labelled test double can be swapped for the real model-backed
    one without touching this module.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        maintenance: IMaintenanceIntake,
        lease_rent: ILeaseRentQuery,
        notification: INotificationService | None = None,
        classifier: BaseClassifier | None = None,
        emergency_terms: tuple[str, ...] | None = None,
    ) -> None:
        self._conn = conn
        self._maintenance = maintenance
        self._lease_rent = lease_rent
        self._notification = notification
        self._classifier = classifier or KeywordClassifier()
        self._emergency_terms = emergency_terms or policy.DEFAULT_EMERGENCY_TERMS

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def handle_message(self, auth: AuthContext, content: str) -> CommunicationOutcome:
        if not self._binding_verified(auth.tenant_id, auth.property_id):
            raise PermissionError(
                "tenant/property binding not verified for auth context "
                f"({auth.tenant_id}, {auth.property_id})"
            )

        message_id = MessageId(new_id())
        now = utcnow()
        try:
            raw = self._classifier.classify(content)
        except Exception:
            raw = None  # classifier failure never authorises an automatic reply
        emergency = policy.has_emergency_indicator(content, self._emergency_terms)
        # Persist before any external call, including manager notification.
        self._store_message(message_id, auth, content,
                            raw.category.value if raw else "unknown", now,
                            handoff_reason=("classifier output invalid or unavailable" if raw is None
                                            else "classification unclear (unknown)" if raw.category is Category.UNKNOWN
                                            else None))
        alert = self._attempt_emergency_alert(auth, message_id) if emergency else None

        if raw is None:
            # Binding rule: invalid model output is NOT "unknown"; it is
            # invalid -> human handoff. We still store the message.
            self._audit(
                "message.classified_invalid", message_id,
                {"tenant_id": auth.tenant_id, "property_id": auth.property_id},
            )
            return CommunicationOutcome(
                message_id=message_id, category=None,
                routed_to="human",
                handoff_reason="classifier output invalid", emergency_alert=alert,
            )

        category = raw.category
        triggers = []
        if emergency:
            triggers.append(policy.ReviewReason.EMERGENCY)
        if policy.is_decision_request(content):
            triggers.append(policy.ReviewReason.DECISION_REQUEST)
        if category is Category.MAINTENANCE:
            outcome = self._route_maintenance(auth, message_id, content, now, triggers, alert)
        elif category is Category.LEASE_RENT:
            outcome = self._route_lease_rent(auth, message_id, content, now, triggers)
        elif category is Category.SMALLTALK:
            outcome = self._route_smalltalk(message_id, content, triggers)
        else:
            outcome = self._handoff(message_id, "classification unclear (unknown)",
                                    category="unknown")
        if outcome.emergency_alert is None:
            outcome.emergency_alert = alert
        return outcome

    # ------------------------------------------------------------------
    # UC1: maintenance routing
    # ------------------------------------------------------------------
    def _route_maintenance(self, auth, message_id, content, now, triggers, alert):
        intake = MaintenanceIntake(
            message_id=message_id, tenant_id=TenantId(auth.tenant_id),
            property_id=PropertyId(auth.property_id), title=self._title(content),
            description=content, location=None, event_id=intake_event_id(message_id),
            submitted_at=now,
        )
        try:
            result = self._maintenance.submit_issue(intake)
        except Exception:
            return self._handoff(message_id, "maintenance service unavailable",
                                 category="maintenance")
        self._audit(f"intake.{result.status}", message_id,
                    {"request_id": getattr(result, "request_id", None)})
        if result.status == "failed":
            return self._handoff(message_id, f"maintenance intake failed (code={result.code})",
                                 category="maintenance", intake=result)
        triggers = list(triggers)
        refs, request_id = [], None
        if result.status == "registered":
            request_id = result.request_id
            if result.priority is Priority.EMERGENCY:
                triggers.append(policy.ReviewReason.EMERGENCY)
                if alert is None:
                    alert = self._attempt_emergency_alert(auth, message_id)
            reply = templates.maintenance_confirmation(request_id, result.persisted_status.value)
            refs = [_system_fact_ref(request_id, "maintenance request registered")]
        else:
            reply = templates.clarification(result.missing_fields)
        # Only a narrow, explicit repair report qualifies without review.
        decision = policy.decide(policy.is_routine_maintenance(content), *triggers)
        draft, review = self._draft_reply(
            message_id=message_id, request_id=request_id, content=reply,
            decision=decision, fact_refs=refs)
        return CommunicationOutcome(message_id=message_id, category="maintenance",
                                    routed_to="maintenance", intake=result, draft=draft,
                                    review=review, eligibility=decision, emergency_alert=alert)

    def _route_lease_rent(self, auth, message_id, content, now, triggers):
        query = LeaseRentQuery(message_id=message_id, tenant_id=TenantId(auth.tenant_id),
                               property_id=PropertyId(auth.property_id),
                               question_type=self._question_type(content),
                               question=content, as_of=now)
        try:
            result = self._lease_rent.get_facts(query)
        except Exception:
            return self._handoff(message_id, "lease/rent service unavailable", category="lease_rent")
        self._audit("lease_rent.queried", message_id, {"availability": result.availability.value})
        if result.availability in (FactAvailability.AUTHORIZATION_DENIED, FactAvailability.CONFLICT):
            # Do not return potentially unauthorised facts even as internal outcome data.
            return self._handoff(message_id, f"lease/rent facts unavailable: {result.availability.value}",
                                 category="lease_rent")
        triggers = list(triggers)
        facts = result.facts if result.availability is FactAvailability.AVAILABLE else []
        if not facts:
            triggers.append(policy.ReviewReason.UNAVAILABLE_FACT)
            reply = templates.unavailable_fact("your lease or rent question")
        else:
            reply = templates.rent_lease_facts(facts)
            if any(f.stale or not f.references for f in facts):
                triggers.append(policy.ReviewReason.UNCERTAIN)
        decision = policy.decide(bool(facts) and policy.is_routine_fact_question(content), *triggers)
        draft, review = self._draft_reply(message_id=message_id, request_id=None,
                                         content=reply, decision=decision,
                                         fact_refs=self._fact_refs(facts))
        return CommunicationOutcome(message_id=message_id, category="lease_rent",
                                    routed_to="lease_rent", fact_result=result, draft=draft,
                                    review=review, eligibility=decision)

    def _route_smalltalk(self, message_id, content, triggers):
        decision = policy.decide(policy.is_routine_greeting(content), *triggers)
        draft, review = self._draft_reply(message_id=message_id, request_id=None,
                                         content=templates.smalltalk(), decision=decision)
        return CommunicationOutcome(message_id=message_id, category="smalltalk",
                                    routed_to="smalltalk", draft=draft, review=review,
                                    eligibility=decision)

    def _attempt_emergency_alert(self, auth, message_id):
        """Queue minimal manager alerts; never claim acceptance is delivery."""
        managers = tuple(row[0] for row in self._conn.execute(
            "SELECT manager_id FROM manager_property WHERE property_id = ? ORDER BY manager_id",
            (auth.property_id,)).fetchall())
        accepted, failures = [], []
        if not managers:
            failures.append("no bound manager")
        elif self._notification is None:
            failures.append("notification service unavailable")
        else:
            for manager in managers:
                # Recheck the authoritative scope immediately before publishing.
                if not self._conn.execute(
                    "SELECT 1 FROM manager_property WHERE manager_id = ? AND property_id = ?",
                    (manager, auth.property_id)).fetchone():
                    failures.append("manager binding removed")
                    continue
                try:
                    delivery = self._notification.publish(NotificationEvent(
                        event_id=EventId(f"alert:{message_id}"), recipient_id=RecipientId(manager),
                        kind=NotificationKind.MANAGER_ALERT, message_id=message_id,
                        payload={"property_id": auth.property_id, "message_id": message_id,
                                 "emergency": True}, created_at=utcnow()))
                    if delivery.status in (NotificationStatus.ACCEPTED, NotificationStatus.DELIVERED):
                        accepted.append(manager)
                    else:
                        failures.append(delivery.status.value)
                except Exception:
                    failures.append("notification publisher error")  # no raw exception leakage
        self._audit("communication.emergency_alert_attempted", message_id,
                    {"recipient_count": len(managers), "accepted_count": len(accepted),
                     "failures": failures})
        return policy.EmergencyAlert(dispatched=bool(accepted), manager_ids=managers,
                                     failure="; ".join(failures) or None)

    def _handoff(
        self,
        message_id: MessageId,
        reason: str,
        *,
        category: str | None = None,
        intake: IntakeResult | None = None,
        fact_result: LeaseRentFactResult | None = None,
    ) -> CommunicationOutcome:
        self._audit("communication.handoff", message_id, {"reason": reason})
        from review.handoffs import record_handoff
        record_handoff(self._conn, message_id, reason)
        return CommunicationOutcome(
            message_id=message_id,
            category=category,
            routed_to="human",
            handoff_reason=reason,
            intake=intake,
            fact_result=fact_result,
        )

    # ------------------------------------------------------------------
    # Delivery-result recording (truthful; ACCEPTED != sent)
    # ------------------------------------------------------------------
    def delivery_label_for(self, message_id: str) -> str | None:
        """Honest delivery status for the reply to `message_id`.

        Returns None when no notification service is configured. Never
        presents ACCEPTED as sent.
        """
        if self._notification is None:
            return None
        row = self._conn.execute(
            "SELECT tenant_id FROM message WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        result = self._notification.status(
            reply_event_id(message_id), RecipientId(row["tenant_id"])
        )
        return delivery_label(result.status, result.detail)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _binding_verified(self, tenant_id: str, property_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM tenant_property WHERE tenant_id = ? AND property_id = ?",
            (tenant_id, property_id),
        ).fetchone()
        return row is not None

    def _store_message(
        self, message_id: MessageId, auth: AuthContext, content: str,
        message_type: str, now,
        handoff_reason: str | None = None,
    ) -> None:
        try:
            self._conn.execute(
                "INSERT INTO message (message_id, tenant_id, property_id, content, "
                "message_type, sentiment, received_at) VALUES (?, ?, ?, ?, ?, 'none', ?)",
                (
                    message_id, auth.tenant_id, auth.property_id, content,
                    message_type, now.isoformat().replace("+00:00", "Z"),
                ),
            )
            if handoff_reason:
                # Message, case and creation audit commit together: a restart
                # cannot leave a classification failure without a human item.
                from review.handoffs import record_handoff
                record_handoff(self._conn, message_id, handoff_reason)
            else:
                self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _draft_reply(
        self,
        *,
        message_id: MessageId,
        request_id: RequestId | None,
        content: str,
        decision: policy.EligibilityDecision,
        fact_refs: list | None = None,
    ) -> tuple[Draft, ReviewRequest | None]:
        now = utcnow()
        reason = decision.reason.value if decision.reason else None
        draft = Draft(
            draft_id=new_id(),
            message_id=message_id,
            request_id=request_id,
            content=content,
            tone_hint=None,
            source_facts=fact_refs or [],
            status=decision.status,
            created_at=now,
        )
        self._conn.execute(
            "INSERT INTO draft (draft_id, message_id, request_id, content, "
            "tone_hint, review_reason, source_facts, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                draft.draft_id, message_id, request_id, content,
                draft.tone_hint, reason,
                json.dumps([ref.model_dump(mode="json") for ref in (fact_refs or [])],
                           ensure_ascii=False), draft.status.value,
                now.isoformat().replace("+00:00", "Z"),
            ),
        )
        self._conn.commit()
        self._audit("draft.created", message_id, {"draft_id": draft.draft_id, "reason": reason})
        if decision.status is DraftStatus.AUTO_ELIGIBLE:
            self._queue_auto_reply(draft, auth_tenant_id=self._message_tenant(message_id))
        review = None
        if decision.status is DraftStatus.PENDING_REVIEW:
            review = ReviewRequest(
                review_id=new_id(),
                draft=draft,
                original_message=self._message_content(message_id),
                reason=reason,
                source_facts=fact_refs or [],
            )
        return draft, review

    def _message_tenant(self, message_id: MessageId) -> str | None:
        row = self._conn.execute(
            "SELECT tenant_id FROM message WHERE message_id=?", (message_id,)
        ).fetchone()
        return row["tenant_id"] if row else None

    def _queue_auto_reply(self, draft: Draft, *, auth_tenant_id: str | None) -> None:
        """Queue only the exact persisted routine version for dispatch.

        The dispatcher independently rechecks the draft status, source,
        emergency audit, policy triggers and property binding before delivery.
        This producer only creates the durable accepted event; it never claims
        that the tenant has received the reply.
        """
        if self._notification is None or auth_tenant_id is None:
            return
        row = self._conn.execute(
            "SELECT property_id,message_type FROM message WHERE message_id=?",
            (draft.message_id,),
        ).fetchone()
        if row is None:
            return
        kind = (
            NotificationKind.LEASE_RENT_REPLY
            if row["message_type"] == Category.LEASE_RENT.value
            else NotificationKind.OTHER
        )
        try:
            self._notification.publish(
                NotificationEvent(
                    event_id=reply_event_id(draft.message_id),
                    recipient_id=RecipientId(auth_tenant_id),
                    kind=kind,
                    request_id=draft.request_id,
                    message_id=draft.message_id,
                    payload={
                        "property_id": row["property_id"],
                        "draft_id": draft.draft_id,
                        "draft_version": draft.version,
                        "content": draft.content,
                    },
                    created_at=draft.created_at,
                )
            )
        except Exception:
            # Queue failure is not a successful delivery and must not leak
            # provider/credential details into a tenant-facing response.
            self._audit(
                "communication.auto_reply_queue_failed",
                draft.message_id,
                {"draft_id": draft.draft_id},
            )

    def _message_content(self, message_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT content FROM message WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row["content"] if row else None

    def _audit(
        self, action: str, subject_id: str, details: dict | None = None,
    ) -> None:
        now = utcnow()
        self._conn.execute(
            "INSERT INTO audit (audit_id, actor, action, subject_type, "
            "subject_id, details, at) VALUES (?, 'system:communication', "
            "?, 'message', ?, ?, ?)",
            (
                new_id(), action, subject_id,
                json.dumps(details or {}, ensure_ascii=False),
                now.isoformat().replace("+00:00", "Z"),
            ),
        )
        self._conn.commit()

    @staticmethod
    def _title(content: str) -> str:
        return content.strip().replace("\n", " ")[:80]

    @staticmethod
    def _question_type(content: str) -> QuestionType:
        """Cheap deterministic mapping to QuestionType. The provider
        (lease_rent module) does the actual verification — this only
        labels the question so the provider can pick its sources."""
        lowered = content.lower()
        if any(k in lowered for k in ("how much", "amount", "price")):
            return QuestionType.RENT_AMOUNT
        if any(k in lowered for k in ("paid", "payment", "pay ", "due", "status")):
            return QuestionType.RENT_STATUS
        if any(k in lowered for k in ("start", "end date", "when", "until", "renew", "how long")):
            return QuestionType.LEASE_DATES
        if any(k in lowered for k in ("term", "clause", "rule", "policy", "lease", "deposit")):
            return QuestionType.LEASE_TERMS
        return QuestionType.OTHER

    @staticmethod
    def _reply_from_facts(facts: list[Fact]) -> str:
        lines = [f"  - {f.name}: {f.value}" for f in facts]
        body = "\n".join(lines)
        return f"Here's what I found for you:\n{body}\nLet me know if you need anything else."

    @staticmethod
    def _fact_refs(facts: list[Fact]) -> list:
        refs = []
        for fact in facts:
            refs.extend(fact.references)
        return refs


def _system_fact_ref(source_id: str, snippet: str):
    from contracts import FactReference

    return FactReference(source_type="system", source_id=source_id, snippet=snippet)
