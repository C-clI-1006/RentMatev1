"""Smoke test: shared contracts import cleanly and shapes behave.

Run: pytest backend/tests/
"""

from datetime import datetime, timezone

from contracts.common import EventId, MessageId, PropertyId, TenantId
from contracts.drafting import Draft, DraftStatus, ReviewDecisionRecord, ReviewDecision
from contracts.lease_rent import (
    Fact,
    FactAvailability,
    FactReference,
    LeaseRentFactResult,
    LeaseRentQuery,
    QuestionType,
)
from contracts.maintenance import (
    IMaintenanceIntake,
    IntakeResult,
    MaintenanceIntake,
    NeedsClarificationIntake,
    RegisteredIntake,
)
from contracts.notification import (
    DeliveryResult,
    INotificationService,
    NotificationEvent,
    NotificationKind,
    NotificationStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_intake_registered_shape():
    result = RegisteredIntake(request_id="req-1", persisted_status="submitted")
    assert result.status == "registered"
    assert result.persisted_status == "submitted"
    # The three outcomes are mutually exclusive by the `status` discriminator.
    assert RegisteredIntake.model_fields["status"].default == "registered"
    assert NeedsClarificationIntake.model_fields["status"].default == "needs_clarification"


def test_intake_needs_clarification_is_not_failed():
    result = NeedsClarificationIntake(missing_fields=["location", "description"])
    assert result.status == "needs_clarification"
    assert "location" in result.missing_fields


def test_maintenance_intake_requires_auth_bound_ids():
    intake = MaintenanceIntake(
        message_id=MessageId("m-1"),
        tenant_id=TenantId("t-1"),
        property_id=PropertyId("p-1"),
        title="No hot water",
        description="Kitchen sink has no hot water since Monday",
        event_id=EventId("e-1"),
        submitted_at=_now(),
    )
    assert intake.tenant_id == "t-1"
    assert intake.property_id == "p-1"


def test_lease_rent_result_facts_have_references():
    fact = Fact(
        name="current_period_status",
        value="paid",
        as_of=_now(),
        references=[
            FactReference(source_type="rent_record", source_id="rr-1", version="1")
        ],
    )
    result = LeaseRentFactResult(
        message_id=MessageId("m-2"),
        availability=FactAvailability.AVAILABLE,
        facts=[fact],
    )
    assert result.facts[0].references[0].source_type == "rent_record"


def test_query_requires_auth_bound_ids():
    q = LeaseRentQuery(
        message_id=MessageId("m-3"),
        tenant_id=TenantId("t-1"),
        property_id=PropertyId("p-1"),
        question_type=QuestionType.RENT_STATUS,
        question="Have I paid this month?",
        as_of=_now(),
    )
    assert q.question_type == QuestionType.RENT_STATUS


def test_draft_version_and_approval_binding():
    draft = Draft(
        draft_id="d-1",
        message_id=MessageId("m-4"),
        request_id="req-9",
        version=2,
        content="We've registered your report (REQ-9).",
        status=DraftStatus.PENDING_REVIEW,
        created_at=_now(),
    )
    record = ReviewDecisionRecord(
        review_id="rv-1",
        draft_id="d-1",
        draft_version=draft.version,
        decision=ReviewDecision.APPROVED,
        manager_id="mgr-1",
        final_content="We've registered your report (REQ-9).",
        decided_at=_now(),
    )
    assert record.draft_version == 2
    assert record.final_content is not None


def test_rejection_has_no_deliverable_content():
    record = ReviewDecisionRecord(
        review_id="rv-2",
        draft_id="d-5",
        draft_version=1,
        decision=ReviewDecision.REJECTED,
        manager_id="mgr-1",
        reason="Content inaccurate",
        decided_at=_now(),
    )
    assert record.final_content is None


def test_notification_accepted_is_not_delivered():
    result = DeliveryResult(
        event_id=EventId("e-9"),
        status=NotificationStatus.ACCEPTED,
        attempts=0,
        checked_at=_now(),
    )
    assert result.status != NotificationStatus.DELIVERED
    # Dedup key is (event_id, recipient_id)
    event = NotificationEvent(
        event_id=EventId("e-10"),
        recipient_id="tenant:t-1",
        kind=NotificationKind.MAINTENANCE_REGISTERED,
        request_id="req-9",
        created_at=_now(),
    )
    assert event.recipient_id == "tenant:t-1"


def test_interfaces_are_abstract():
    assert issubclass(IMaintenanceIntake, object)
    for cls in (IMaintenanceIntake, INotificationService):
        import inspect

        assert inspect.isabstract(cls)
