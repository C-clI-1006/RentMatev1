"""Communication service tests: intake -> classify -> route -> draft.

Covers the binding rules:
- Auth-context binding must be verified (tenant_property) before intake.
- Invalid classifier output (None) -> human handoff, NOT "unknown".
- UC1 routing -> IMaintenanceIntake; registered/needs_clarification/failed.
- UC2 routing -> ILeaseRentQuery; available/not_available/denied/conflict.
- Drafts are ALWAYS pending_review with message_id origin (exactly one
  origin), grounded in returned facts (source_facts) — no auto-send.
- Delivery status is shown truthfully: ACCEPTED is never shown as sent.

Fakes for the cross-module ports keep the real contract shapes.

Run: pytest backend/tests/
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from communication import CommunicationOutcome, CommunicationService
from contracts import (
    DraftStatus,
    FactAvailability,
    FailedIntake,
    IMaintenanceIntake,
    ILeaseRentQuery,
    INotificationService,
    MaintenanceStatus,
    NeedsClarificationIntake,
    Priority,
    NotificationStatus,
    RegisteredIntake,
    DeliveryResult,
    NotificationEvent,
)
from contracts.lease_rent import Fact, FactReference, LeaseRentFactResult
from llm.classifier import BaseClassifier, Category, Classification
from shared import AuthContext
from shared.sqlite import init_connection

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"

TENANT = "t-1"
PROPERTY = "p-1"
NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes (labelled test doubles — they only implement the contracts)
# ---------------------------------------------------------------------------
class FakeMaintenance(IMaintenanceIntake):
    """Simulates the real module's side effect: a registered intake means
    the maintenance module already persisted a `maintenance_request`
    row (the draft FK depends on it)."""

    def __init__(self, result, conn: sqlite3.Connection):
        self.result = result
        self.conn = conn
        self.submitted = []

    def submit_issue(self, intake):
        self.submitted.append(intake)
        if self.result.status == "registered":
            now = "2026-10-20T12:00:00Z"
            self.conn.execute(
                "INSERT INTO maintenance_request (request_id, tenant_id, "
                "property_id, message_id, event_id, title, description, "
                "status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?)",
                (
                    self.result.request_id, intake.tenant_id,
                    intake.property_id, intake.message_id,
                    intake.event_id, "test request", intake.description,
                    now, now,
                ),
            )
            self.conn.commit()
        return self.result


class FakeLeaseRent(ILeaseRentQuery):
    def __init__(self, result):
        self.result = result
        self.queries = []

    def get_facts(self, query):
        self.queries.append(query)
        return self.result


class InvalidClassifier(BaseClassifier):
    """Returns None — simulates invalid / non-matching model output."""

    def classify(self, content):
        return None


class SmalltalkOnlyClassifier(BaseClassifier):
    def classify(self, content):
        return Classification(Category.SMALLTALK, 0.9, "test")


class FakeNotification(INotificationService):
    # NOTE: the stored status must not be named `self.status` — that would
    # shadow the INotificationService.status() method.
    def __init__(self, status=NotificationStatus.ACCEPTED, detail=None):
        self._default_status = status
        self.detail = detail
        self.published = []

    def publish(self, event: NotificationEvent) -> DeliveryResult:
        self.published.append(event)
        return self._result(event)

    def status(self, event_id, recipient_id) -> DeliveryResult:
        return self._result(None)

    def _result(self, event) -> DeliveryResult:
        return DeliveryResult(
            event_id=event.event_id if event else "e",
            status=self._default_status,
            detail=self.detail,
            attempts=1,
            checked_at=NOW,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = init_connection(":memory:")
    ts = "2026-01-01T00:00:00Z"
    conn.execute(
        "INSERT INTO tenant (tenant_id, name, created_at) VALUES (?, 'T', ?)",
        (TENANT, ts),
    )
    conn.execute(
        "INSERT INTO property (property_id, name, created_at) VALUES (?, 'P', ?)",
        (PROPERTY, ts),
    )
    conn.execute(
        "INSERT INTO tenant_property (tenant_id, property_id, role, created_at) "
        "VALUES (?, ?, 'resident', ?)",
        (TENANT, PROPERTY, ts),
    )
    yield conn
    conn.close()


def make_service(
    db,
    maintenance=None,
    lease_rent=None,
    notification=None,
    classifier=None,
) -> CommunicationService:
    return CommunicationService(
        db,
        maintenance or FakeMaintenance(
            RegisteredIntake(
                status="registered",
                request_id="req-1",
                persisted_status=MaintenanceStatus.SUBMITTED,
                priority=Priority.ROUTINE,
            ),
            db,
        ),
        lease_rent or FakeLeaseRent(LeaseRentFactResult(
            message_id="m-x", availability=FactAvailability.NOT_AVAILABLE
        )),
        notification,
        classifier,
    )


# ---------------------------------------------------------------------------
# Entry point: binding + intake persistence
# ---------------------------------------------------------------------------
def test_unverified_binding_rejected(db):
    svc = make_service(db)
    with pytest.raises(PermissionError):
        svc.handle_message(
            AuthContext("t-unbound", PROPERTY), "my kitchen sink is leaking"
        )
    assert db.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 0


def test_message_persisted_with_category(db):
    svc = make_service(db)
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "the heater is broken")
    row = db.execute(
        "SELECT tenant_id, property_id, message_type, sentiment, content "
        "FROM message WHERE message_id = ?",
        (outcome.message_id,),
    ).fetchone()
    assert row["tenant_id"] == TENANT
    assert row["property_id"] == PROPERTY
    assert row["message_type"] == "maintenance"
    assert row["sentiment"] == "none"
    assert row["content"] == "the heater is broken"


def test_invalid_classifier_output_hands_off_not_unknown(db):
    # Binding rule: invalid output is NOT "unknown" — it is invalid ->
    # human handoff. The message is still stored (as unknown type) but
    # the outcome category must be None.
    svc = make_service(db, classifier=InvalidClassifier())
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "the heater is broken")
    assert outcome.routed_to == "human"
    assert outcome.category is None
    assert outcome.handoff_reason == "classifier output invalid"
    assert outcome.draft is None
    stored = db.execute(
        "SELECT message_type FROM message WHERE message_id = ?",
        (outcome.message_id,),
    ).fetchone()
    assert stored["message_type"] == "unknown"


def test_unknown_category_hands_off(db):
    # A VALID "unknown" classification (no signal) is also a handoff,
    # but is reported as "unknown", not as invalid.
    from llm.classifier import KeywordClassifier

    svc = make_service(db)  # default KeywordClassifier: no keywords here
    outcome = svc.handle_message(
        AuthContext(TENANT, PROPERTY), "xyzzy frobnicate the gizmo"
    )
    assert outcome.routed_to == "human"
    assert outcome.category == "unknown"
    assert outcome.draft is None


# ---------------------------------------------------------------------------
# UC1: maintenance routing
# ---------------------------------------------------------------------------
def test_maintenance_registered_drafts_ack(db):
    maint = FakeMaintenance(
        RegisteredIntake(
            status="registered",
            request_id="req-1",
            persisted_status=MaintenanceStatus.SUBMITTED,
            priority=Priority.ROUTINE,
        ),
        db,
    )
    svc = make_service(db, maintenance=maint)
    outcome = svc.handle_message(
        AuthContext(TENANT, PROPERTY), "my kitchen sink is leaking badly"
    )
    assert outcome.routed_to == "maintenance"
    assert isinstance(outcome.intake, RegisteredIntake)
    # The intake carried the auth-identity + a stable event_id dedup key.
    intake = maint.submitted[0]
    assert intake.tenant_id == TENANT
    assert intake.property_id == PROPERTY
    assert intake.event_id == f"intake:{outcome.message_id}"
    assert intake.description == "my kitchen sink is leaking badly"
    # Draft is grounded: request id appears in content and a system ref.
    assert outcome.draft is not None
    assert outcome.draft.status is DraftStatus.PENDING_REVIEW
    assert outcome.draft.message_id == outcome.message_id
    assert outcome.draft.request_id == "req-1"
    assert "req-1" in outcome.draft.content
    assert any(
        r.source_type == "system" and r.source_id == "req-1"
        for r in outcome.draft.source_facts
    )
    assert outcome.review.original_message == "my kitchen sink is leaking badly"


def test_maintenance_needs_clarification_asks_tenant(db):
    svc = make_service(
        db,
        maintenance=FakeMaintenance(
            NeedsClarificationIntake(
                status="needs_clarification", missing_fields=["location", "date started"]
            ),
            db,
        ),
    )
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "something is broken")
    assert outcome.routed_to == "maintenance"
    assert outcome.draft is not None  # clarification is NOT a failure
    assert "location" in outcome.draft.content
    assert outcome.draft.request_id is None  # no request was created


def test_maintenance_failed_hands_off(db):
    svc = make_service(
        db,
        maintenance=FakeMaintenance(
            FailedIntake(status="failed", code="intake.storage_error", retryable=True),
            db,
        ),
    )
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "plumbing is leaking")
    assert outcome.routed_to == "human"
    assert "intake.storage_error" in outcome.handoff_reason
    assert outcome.draft is None
    assert isinstance(outcome.intake, FailedIntake)


# ---------------------------------------------------------------------------
# UC2: lease & rent routing
# ---------------------------------------------------------------------------
def _fact(name, value, ref=None):
    return Fact(
        name=name,
        value=value,
        as_of=NOW,
        references=ref or [
            FactReference(
                source_type="rent_record",
                source_id="rr-1",
                version="1",
                snippet="period 2026-10, amount_paid=0",
            )
        ],
    )


def test_lease_rent_available_fact_grounded_reply(db):
    result = LeaseRentFactResult(
        message_id="m-x",
        availability=FactAvailability.AVAILABLE,
        facts=[
            _fact("current_period", "2026-10"),
            _fact("payment_status", "paid", ref=[
                FactReference(source_type="rent_record", source_id="rr-1",
                              snippet="amount_paid=2000, paid_at 2026-09-28")
            ]),
        ],
        lease_id="l-1",
    )
    svc = make_service(db, lease_rent=FakeLeaseRent(result))
    outcome = svc.handle_message(
        AuthContext(TENANT, PROPERTY), "have you received my rent payment yet?"
    )
    assert outcome.routed_to == "lease_rent"
    assert outcome.fact_result is result
    # Query carried auth identity + as_of + a labelled question type.
    query = svc._lease_rent.queries[0]
    assert query.tenant_id == TENANT
    assert query.property_id == PROPERTY
    assert query.question_type.value == "rent_status"
    # Reply contains the facts, and the draft is traceable to their sources.
    assert "2026-10" in outcome.draft.content
    assert "paid" in outcome.draft.content
    assert all(r.source_type == "rent_record" for r in outcome.draft.source_facts)
    assert {r.source_id for r in outcome.draft.source_facts} == {"rr-1"}


def test_lease_rent_not_available_never_claims_rule_missing(db):
    result = LeaseRentFactResult(
        message_id="m-x",
        availability=FactAvailability.NOT_AVAILABLE,
        note="no rent record for this period yet",
    )
    svc = make_service(db, lease_rent=FakeLeaseRent(result))
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "how much is my rent?")
    assert outcome.routed_to == "lease_rent"
    assert outcome.draft is not None
    assert "no rent record for this period yet" in outcome.draft.content
    assert outcome.draft.source_facts == []  # no facts were fabricated


@pytest.mark.parametrize(
    "availability",
    [FactAvailability.AUTHORIZATION_DENIED, FactAvailability.CONFLICT],
)
def test_lease_rent_denied_or_conflict_hands_off(db, availability):
    result = LeaseRentFactResult(
        message_id="m-x", availability=availability, note="needs a human"
    )
    svc = make_service(db, lease_rent=FakeLeaseRent(result))
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "what is my lease end date?")
    assert outcome.routed_to == "human"
    assert availability.value in outcome.handoff_reason
    assert outcome.draft is None  # never an automatic tenant reply


# ---------------------------------------------------------------------------
# Smalltalk
# ---------------------------------------------------------------------------
def test_smalltalk_ack_draft(db):
    svc = make_service(db)
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "hi, good morning!")
    assert outcome.routed_to == "smalltalk"
    assert outcome.draft is not None
    assert outcome.draft.status is DraftStatus.PENDING_REVIEW


# ---------------------------------------------------------------------------
# Draft persistence + origin invariant
# ---------------------------------------------------------------------------
def test_draft_persisted_pending_review_with_message_origin(db):
    svc = make_service(db)
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "hello")
    row = db.execute(
        "SELECT message_id, source_id, status, content FROM draft "
        "WHERE draft_id = ?",
        (outcome.draft.draft_id,),
    ).fetchone()
    assert row["message_id"] == outcome.message_id
    assert row["source_id"] is None  # exactly one origin
    assert row["status"] == "pending_review"
    assert row["content"] == outcome.draft.content


def test_audit_rows_written(db):
    svc = make_service(db)
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "my stove is broken")
    rows = db.execute(
        "SELECT action, subject_id FROM audit WHERE subject_id = ?",
        (outcome.message_id,),
    ).fetchall()
    actions = {r["action"] for r in rows}
    assert "intake.registered" in actions
    assert "draft.created" in actions


# ---------------------------------------------------------------------------
# Truthful delivery status (ACCEPTED is never shown as sent)
# ---------------------------------------------------------------------------
def test_delivery_label_accepted_is_not_sent(db):
    notif = FakeNotification(status=NotificationStatus.ACCEPTED)
    svc = make_service(db, notification=notif)
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "hello")
    label = svc.delivery_label_for(outcome.message_id)
    assert "accepted" in label
    assert "not sent" in label


def test_delivery_label_failed_shown_truthfully(db):
    notif = FakeNotification(
        status=NotificationStatus.FAILED, detail="gateway timeout"
    )
    svc = make_service(db, notification=notif)
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "hello")
    label = svc.delivery_label_for(outcome.message_id)
    assert "failed" in label
    assert "gateway timeout" in label


def test_delivery_label_pending_retry(db):
    notif = FakeNotification(status=NotificationStatus.PENDING_RETRY)
    svc = make_service(db, notification=notif)
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "hello")
    assert "pending retry" in svc.delivery_label_for(outcome.message_id)


def test_delivery_label_without_notification_service(db):
    svc = make_service(db)
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "hello")
    assert svc.delivery_label_for(outcome.message_id) is None


def test_delivery_label_unknown_message(db):
    svc = make_service(db, notification=FakeNotification())
    assert svc.delivery_label_for("no-such-message") is None
