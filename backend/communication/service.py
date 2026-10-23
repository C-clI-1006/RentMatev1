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
 5. Drafting grounded ONLY in returned facts (no fabrication). Every
    tenant reply is a pending_review draft; the send path is owned by
    review + notification and must use the approved version only.
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
    review: ReviewRequest | None = None  # what the manager will see


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
    ) -> None:
        self._conn = conn
        self._maintenance = maintenance
        self._lease_rent = lease_rent
        self._notification = notification
        self._classifier = classifier or KeywordClassifier()

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
        raw = self._classifier.classify(content)

        if raw is None:
            # Binding rule: invalid model output is NOT "unknown"; it is
            # invalid -> human handoff. We still store the message.
            self._store_message(message_id, auth, content, "unknown", now)
            self._audit(
                "message.classified_invalid", message_id,
                {"tenant_id": auth.tenant_id, "property_id": auth.property_id},
            )
            return CommunicationOutcome(
                message_id=message_id, category=None,
                routed_to="human",
                handoff_reason="classifier output invalid",
            )

        category = raw.category
        self._store_message(message_id, auth, content, category.value, now)

        if category is Category.MAINTENANCE:
            return self._route_maintenance(auth, message_id, content, now)
        if category is Category.LEASE_RENT:
            return self._route_lease_rent(auth, message_id, content, now)
        if category is Category.SMALLTALK:
            return self._route_smalltalk(message_id, content)
        # UNKNOWN (valid but no clear signal) -> human handoff, no reply.
        return self._handoff(
            message_id, "classification unclear (unknown)", category="unknown"
        )

    # ------------------------------------------------------------------
    # UC1: maintenance routing
    # ------------------------------------------------------------------
    def _route_maintenance(
        self, auth: AuthContext, message_id: MessageId, content: str, now
    ) -> CommunicationOutcome:
        intake = MaintenanceIntake(
            message_id=message_id,
            tenant_id=TenantId(auth.tenant_id),
            property_id=PropertyId(auth.property_id),
            title=self._title(content),
            description=content,
            location=None,
            event_id=intake_event_id(message_id),
            submitted_at=now,
        )
        result = self._maintenance.submit_issue(intake)
        self._audit(
            f"intake.{result.status}", message_id,
            {
                "event_id": intake.event_id,
                "request_id": getattr(result, "request_id", None),
                "missing_fields": getattr(result, "missing_fields", None),
                "code": getattr(result, "code", None),
            },
        )

        if result.status == "registered":
            draft, review = self._draft_reply(
                message_id=message_id,
                request_id=result.request_id,
                content=(
                    "Thanks — I've logged a maintenance request "
                    f"({result.request_id}) for your property. Our team "
                    "will follow up with you."
                ),
                reason="maintenance intake acknowledgement (first contact)",
                fact_refs=[
                    _system_fact_ref(result.request_id, "maintenance request registered"),
                ],
            )
            return CommunicationOutcome(
                message_id=message_id, category="maintenance",
                routed_to="maintenance", intake=result,
                draft=draft, review=review,
            )
        if result.status == "needs_clarification":
            fields = ", ".join(result.missing_fields)
            draft, review = self._draft_reply(
                message_id=message_id,
                request_id=None,
                content=(
                    "Thanks for the report. To get started I'd need a "
                    f"bit more detail: {fields}."
                ),
                reason="maintenance intake — clarification request",
            )
            return CommunicationOutcome(
                message_id=message_id, category="maintenance",
                routed_to="maintenance", intake=result,
                draft=draft, review=review,
            )
        # Failed intake: no automatic tenant reply; the maintenance module
        # owns the failure record. Human handles it.
        return self._handoff(
            message_id, f"maintenance intake failed (code={result.code})",
            category="maintenance",
            intake=result,
        )

    # ------------------------------------------------------------------
    # UC2: lease & rent routing
    # ------------------------------------------------------------------
    def _route_lease_rent(
        self, auth: AuthContext, message_id: MessageId, content: str, now
    ) -> CommunicationOutcome:
        query = LeaseRentQuery(
            message_id=message_id,
            tenant_id=TenantId(auth.tenant_id),
            property_id=PropertyId(auth.property_id),
            question_type=self._question_type(content),
            question=content,
            as_of=now,
        )
        result = self._lease_rent.get_facts(query)
        self._audit(
            "lease_rent.queried", message_id,
            {"availability": result.availability.value,
             "question_type": query.question_type.value},
        )

        availability = result.availability
        if availability is FactAvailability.AVAILABLE:
            content_out = self._reply_from_facts(result.facts)
            draft, review = self._draft_reply(
                message_id=message_id,
                request_id=None,
                content=content_out,
                reason="lease/rent fact-grounded reply",
                fact_refs=self._fact_refs(result.facts),
            )
            return CommunicationOutcome(
                message_id=message_id, category="lease_rent",
                routed_to="lease_rent", fact_result=result,
                draft=draft, review=review,
            )
        if availability is FactAvailability.NOT_AVAILABLE:
            note = result.note or "no matching record found"
            draft, review = self._draft_reply(
                message_id=message_id,
                request_id=None,
                content=(
                    "I couldn't find verified information about that in "
                    f"our records ({note}). I've passed this to our team "
                    "to follow up with you."
                ),
                reason="lease/rent reply — facts not available",
            )
            return CommunicationOutcome(
                message_id=message_id, category="lease_rent",
                routed_to="lease_rent", fact_result=result,
                draft=draft, review=review,
            )
        # AUTHORIZATION_DENIED / CONFLICT: never auto-reply; a human looks.
        return self._handoff(
            message_id,
            f"lease/rent facts unavailable: {availability.value}",
            category="lease_rent",
            fact_result=result,
        )

    # ------------------------------------------------------------------
    # Smalltalk
    # ------------------------------------------------------------------
    def _route_smalltalk(self, message_id: MessageId, content: str) -> CommunicationOutcome:
        draft, review = self._draft_reply(
            message_id=message_id,
            request_id=None,
            content="Thanks for your message! How can I help you with your "
                    "rental today?",
            reason="smalltalk acknowledgement",
        )
        return CommunicationOutcome(
            message_id=message_id, category="smalltalk",
            routed_to="smalltalk", draft=draft, review=review,
        )

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
    ) -> None:
        self._conn.execute(
            "INSERT INTO message (message_id, tenant_id, property_id, content, "
            "message_type, sentiment, received_at) VALUES (?, ?, ?, ?, ?, 'none', ?)",
            (
                message_id, auth.tenant_id, auth.property_id, content,
                message_type, now.isoformat().replace("+00:00", "Z"),
            ),
        )
        self._conn.commit()

    def _draft_reply(
        self,
        *,
        message_id: MessageId,
        request_id: RequestId | None,
        content: str,
        reason: str,
        fact_refs: list | None = None,
    ) -> tuple[Draft, ReviewRequest]:
        now = utcnow()
        draft = Draft(
            draft_id=new_id(),
            message_id=message_id,
            request_id=request_id,
            content=content,
            tone_hint=None,
            source_facts=fact_refs or [],
            status=DraftStatus.PENDING_REVIEW,
            created_at=now,
        )
        self._conn.execute(
            "INSERT INTO draft (draft_id, message_id, request_id, content, "
            "tone_hint, review_reason, source_facts, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', ?)",
            (
                draft.draft_id, message_id, request_id, content,
                draft.tone_hint, reason,
                json.dumps([ref.model_dump(mode="json") for ref in (fact_refs or [])],
                           ensure_ascii=False),
                now.isoformat().replace("+00:00", "Z"),
            ),
        )
        self._conn.commit()
        self._audit("draft.created", message_id, {"draft_id": draft.draft_id, "reason": reason})
        review = ReviewRequest(
            review_id=new_id(),
            draft=draft,
            original_message=self._message_content(message_id),
            reason=reason,
            source_facts=fact_refs or [],
        )
        return draft, review

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
