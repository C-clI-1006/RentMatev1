"""Communication service tests: intake -> classify -> route -> draft.

Covers the binding rules:
- Auth-context binding must be verified (tenant_property) before intake.
- Invalid classifier output (None) -> human handoff, NOT "unknown".
- UC1 routing -> IMaintenanceIntake; registered/needs_clarification/failed.
- UC2 routing -> ILeaseRentQuery; available/not_available/denied/conflict.
- Routine drafts are auto_eligible; review-triggered drafts are pending_review with message_id origin (exactly one
  origin), grounded in returned facts (source_facts) — no auto-send.
- Delivery status is shown truthfully: ACCEPTED is never shown as sent.

Fakes for the cross-module ports keep the real contract shapes.

Run: pytest backend/tests/
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
import httpx

from communication import CommunicationOutcome, CommunicationService, policy
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
from llm.classifier import BaseClassifier, Category, Classification, KeywordClassifier
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


@pytest.mark.parametrize("recovers", [True, False])
def test_qwen_retry_persists_one_message_and_preserves_handoff(db, monkeypatch, recovers):
    import json
    from llm.classifier import QwenClassifier

    calls = []

    def post(self, url, **kwargs):
        calls.append(1)
        if len(calls) == 1 or not recovers:
            raise httpx.ReadTimeout("test timeout")
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "choices": [{"message": {"content": json.dumps({"category": "smalltalk", "confidence": 0.95})}}]
        })

    monkeypatch.setattr(httpx.Client, "post", post)
    classifier = QwenClassifier(base_url="http://proxy.invalid/v1", api_key="test-key", model="campus-qwen")
    outcome = make_service(db, classifier=classifier).handle_message(AuthContext(TENANT, PROPERTY), "hello")
    assert len(calls) == 2
    assert db.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM draft").fetchone()[0] == (1 if recovers else 0)
    assert outcome.tenant_response()["delivery_status"] == "not_dispatched"
    if recovers:
        assert outcome.category == "smalltalk"
        assert outcome.draft.status is DraftStatus.AUTO_ELIGIBLE
    else:
        assert outcome.routed_to == "human"
        assert outcome.draft is None


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


@pytest.mark.parametrize(
    "content",
    [
        "The kitchen cupboard handle is loose but there is no immediate danger.",
        "The bathroom tap is dripping.",
        "The air conditioner stopped working last night.",
    ],
)
def test_keyword_classifier_recognises_natural_maintenance_phrasing(content):
    result = KeywordClassifier().classify(content)
    assert result is not None
    assert result.category is Category.MAINTENANCE
    assert result.note == "keyword:maintenance"


def test_natural_routine_maintenance_routes_to_maintenance_not_human(db):
    outcome = make_service(db).handle_message(
        AuthContext(TENANT, PROPERTY),
        "The kitchen cupboard handle is loose but there is no immediate danger.",
    )
    assert outcome.category == "maintenance"
    assert outcome.routed_to == "maintenance"
    assert outcome.intake is not None
    assert outcome.draft is not None


@pytest.mark.parametrize(
    "content",
    [
        "There is a strong smell of gas in my kitchen.",
        "I can smell gas in the apartment.",
        "There is a gas leak in the kitchen.",
        "The electrical outlet is sparking.",
        "I am not sure whether I can smell gas.",
    ],
)
def test_natural_emergency_phrasing_is_detected(content):
    assert policy.has_emergency_indicator(content)


@pytest.mark.parametrize(
    "content",
    [
        "There is no gas smell.",
        "I cannot smell gas in the apartment.",
        "There has been no hot water since yesterday. Cold water works and there is no gas smell.",
    ],
)
def test_explicitly_negated_gas_smell_is_not_an_emergency(content):
    assert not policy.has_emergency_indicator(content)


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
    assert outcome.draft.status is DraftStatus.AUTO_ELIGIBLE
    assert outcome.draft.message_id == outcome.message_id
    assert outcome.draft.request_id == "req-1"
    assert "req-1" in outcome.draft.content
    assert any(
        r.source_type == "system" and r.source_id == "req-1"
        for r in outcome.draft.source_facts
    )
    assert outcome.review is None


def test_real_maintenance_storage_integrates_without_legacy_projection(tmp_path):
    """UC1 uses the contract, not a hidden duplicate-table side effect."""
    from llm.assess import KeywordPriorityAssessor
    from maintenance.service import MaintenanceService
    from maintenance.store import Store

    db_path = tmp_path / "communication-maintenance.db"
    conn = init_connection(db_path)
    ts = "2026-01-01T00:00:00Z"
    conn.execute(
        "INSERT INTO tenant (tenant_id,name,created_at) VALUES (?, 'T', ?)",
        (TENANT, ts),
    )
    conn.execute(
        "INSERT INTO property (property_id,name,created_at) VALUES (?, 'P', ?)",
        (PROPERTY, ts),
    )
    conn.execute(
        "INSERT INTO tenant_property (tenant_id,property_id,role,created_at) "
        "VALUES (?, ?, 'resident', ?)",
        (TENANT, PROPERTY, ts),
    )
    conn.commit()
    auth = AuthContext(TENANT, PROPERTY)
    maintenance = MaintenanceService(
        Store(db_path), KeywordPriorityAssessor(), auth
    )
    service = CommunicationService(
        conn,
        maintenance,
        FakeLeaseRent(
            LeaseRentFactResult(
                message_id="unused",
                availability=FactAvailability.NOT_AVAILABLE,
            )
        ),
    )
    try:
        outcome = service.handle_message(
            auth, "The kitchen window is broken and needs repair"
        )
        assert outcome.routed_to == "maintenance"
        assert outcome.intake.status == "registered"
        assert outcome.draft.request_id == outcome.intake.request_id
        assert conn.execute("SELECT count(*) FROM mc_request").fetchone()[0] == 1
        assert (
            conn.execute("SELECT count(*) FROM maintenance_request").fetchone()[0]
            == 0
        )
    finally:
        conn.close()


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
    assert "verified information" in outcome.draft.content
    assert "no rent record for this period yet" not in outcome.draft.content
    assert outcome.review is not None
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
    assert outcome.draft.status is DraftStatus.AUTO_ELIGIBLE


# ---------------------------------------------------------------------------
# Draft persistence + origin invariant
# ---------------------------------------------------------------------------
def test_draft_persisted_auto_eligible_with_message_origin(db):
    svc = make_service(db)
    outcome = svc.handle_message(AuthContext(TENANT, PROPERTY), "hello")
    row = db.execute(
        "SELECT message_id, source_id, status, content FROM draft "
        "WHERE draft_id = ?",
        (outcome.draft.draft_id,),
    ).fetchone()
    assert row["message_id"] == outcome.message_id
    assert row["source_id"] is None  # exactly one origin
    assert row["status"] == "auto_eligible"
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


# Conditional-review regression coverage: eligibility is not delivery.
@pytest.mark.parametrize("text", ["please waive my rent", "how much is my rent? Also refund me", "rent advice please"])
def test_nonroutine_facts_require_review(db, text):
    result = LeaseRentFactResult(message_id="m-x", availability=FactAvailability.AVAILABLE,
                                facts=[_fact("rent_amount", "2000")])
    out = make_service(db, lease_rent=FakeLeaseRent(result)).handle_message(AuthContext(TENANT, PROPERTY), text)
    assert out.draft.status is DraftStatus.PENDING_REVIEW
    assert out.review.draft.draft_id == out.draft.draft_id
    assert out.review.original_message == text


@pytest.mark.parametrize("condition", ["stale", "no_refs", "empty"])
def test_unverified_facts_never_autoeligible(db, condition):
    fact = _fact("rent_amount", "2000")
    if condition == "stale":
        fact.stale = True
    if condition == "no_refs":
        fact.references = []
    result = LeaseRentFactResult(message_id="m-x", availability=FactAvailability.AVAILABLE,
                                facts=[] if condition == "empty" else [fact])
    out = make_service(db, lease_rent=FakeLeaseRent(result)).handle_message(AuthContext(TENANT, PROPERTY), "how much is my rent?")
    assert out.draft.status is DraftStatus.PENDING_REVIEW
    assert out.review is not None


def test_autoeligible_queues_exact_version_without_claiming_delivery(db):
    publisher = FakeNotification()
    out = make_service(db, notification=publisher).handle_message(AuthContext(TENANT, PROPERTY), "hello")
    assert out.draft.status is DraftStatus.AUTO_ELIGIBLE
    assert out.review is None
    assert len(publisher.published) == 1
    assert publisher.published[0].event_id == f"reply:{out.message_id}"
    assert publisher.published[0].payload["draft_id"] == out.draft.draft_id
    assert out.tenant_response() == {"message_id": out.message_id, "reply_status": "auto_eligible", "delivery_status": "not_dispatched"}


def bind_manager(db):
    db.execute("INSERT INTO manager VALUES ('manager-secret', 'M', '2026-01-01T00:00:00Z')")
    db.execute("INSERT INTO manager_property VALUES ('manager-secret', ?, '2026-01-01T00:00:00Z')", (PROPERTY,))
    db.commit()


def test_emergency_alert_precedes_failed_intake(db):
    bind_manager(db)
    events = []
    class Publisher(FakeNotification):
        def publish(self, event):
            assert db.execute("SELECT 1 FROM message WHERE message_id=?", (event.message_id,)).fetchone()
            events.append("alert")
            return super().publish(event)
    class Maintenance(FakeMaintenance):
        def submit_issue(self, intake):
            events.append("intake")
            raise RuntimeError("private credential")
    publisher = Publisher()
    out = make_service(db, notification=publisher, maintenance=Maintenance(None, db)).handle_message(
        AuthContext(TENANT, PROPERTY), "my heater is broken and there is smoke")
    assert events == ["alert", "intake"]
    assert out.emergency_alert.dispatched
    assert out.routed_to == "human"
    assert "private credential" not in out.model_dump_json()
    assert set(publisher.published[0].payload) == {"property_id", "message_id", "emergency"}
    assert "manager-secret" not in str(out.tenant_response())


@pytest.mark.parametrize("failure", ["missing_manager", "publisher_error", "invalid_classifier"])
def test_emergency_failures_and_invalid_classification(db, failure):
    class BrokenPublisher(FakeNotification):
        def publish(self, event):
            raise RuntimeError("secret token")
    if failure != "missing_manager":
        bind_manager(db)
    publisher = BrokenPublisher() if failure == "publisher_error" else FakeNotification()
    out = make_service(db, notification=publisher, classifier=InvalidClassifier()).handle_message(
        AuthContext(TENANT, PROPERTY), "gas leak")
    assert out.routed_to == "human"
    assert out.emergency_alert is not None
    assert out.emergency_alert.dispatched == (failure == "invalid_classifier")
    assert "secret token" not in out.model_dump_json()
    assert db.execute("SELECT 1 FROM audit WHERE action='communication.emergency_alert_attempted'").fetchone()


def test_emergency_registered_requires_review(db):
    maint = FakeMaintenance(RegisteredIntake(status="registered", request_id="req-urgent",
                            persisted_status=MaintenanceStatus.SUBMITTED, priority=Priority.EMERGENCY), db)
    out = make_service(db, maintenance=maint).handle_message(AuthContext(TENANT, PROPERTY), "the heater is broken")
    assert out.draft.status is DraftStatus.PENDING_REVIEW
    assert out.review.reason == "emergency"
    assert not out.emergency_alert.dispatched
    assert out.emergency_alert.failure == "no bound manager"


def test_gas_smell_routes_to_maintenance_and_requires_manager_review(db):
    bind_manager(db)
    publisher = FakeNotification(NotificationStatus.ACCEPTED)
    out = make_service(db, notification=publisher).handle_message(
        AuthContext(TENANT, PROPERTY),
        "There is a strong smell of gas in my kitchen.",
    )

    assert out.category == "maintenance"
    assert out.routed_to == "maintenance"
    assert out.draft.status is DraftStatus.PENDING_REVIEW
    assert out.review.reason == "emergency"
    assert out.emergency_alert is not None
    assert out.emergency_alert.dispatched
    assert len(publisher.published) == 1
    assert publisher.published[0].kind.value == "manager_alert"
    # ACCEPTED means queued only. The tenant projection must never claim that
    # this alert or reply has already been delivered.
    assert out.tenant_response()["delivery_status"] == "not_dispatched"


def test_negated_gas_smell_does_not_publish_emergency_alert(db):
    bind_manager(db)
    publisher = FakeNotification(NotificationStatus.ACCEPTED)
    out = make_service(db, notification=publisher).handle_message(
        AuthContext(TENANT, PROPERTY),
        "There has been no hot water since yesterday. Cold water works and there is no gas smell.",
    )

    assert out.category == "maintenance"
    assert out.routed_to == "maintenance"
    assert out.emergency_alert is None
    assert publisher.published == []
    assert out.tenant_response()["delivery_status"] == "not_dispatched"


def test_unbound_scope_never_calls_dependencies(db):
    class MustNotClassify(BaseClassifier):
        def classify(self, content):
            pytest.fail("binding must be checked before classification")
    with pytest.raises(PermissionError):
        make_service(db, classifier=MustNotClassify()).handle_message(AuthContext(TENANT, "other-property"), "fire")
    assert db.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 0


def test_clarification_does_not_echo_internal_field_names(db):
    maint = FakeMaintenance(NeedsClarificationIntake(status="needs_clarification", missing_fields=["manager_private_notes"]), db)
    out = make_service(db, maintenance=maint).handle_message(AuthContext(TENANT, PROPERTY), "something is broken")
    assert "manager_private_notes" not in out.draft.content
    assert "No maintenance request was created" in out.draft.content


def test_denied_facts_are_not_returned(db):
    result = LeaseRentFactResult(message_id="m-x", availability=FactAvailability.AUTHORIZATION_DENIED,
                                facts=[_fact("private_note", "secret")], note="internal reason")
    out = make_service(db, lease_rent=FakeLeaseRent(result)).handle_message(AuthContext(TENANT, PROPERTY), "how much is my rent?")
    assert out.fact_result is None
    assert out.draft is None
    assert "secret" not in str(out.tenant_response())
