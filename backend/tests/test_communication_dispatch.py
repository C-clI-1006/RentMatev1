from communication import CommunicationService
import json
from contracts import (
    ILeaseRentQuery,
    IMaintenanceIntake,
    MaintenanceStatus,
    Priority,
    RegisteredIntake,
)
from contracts.lease_rent import FactAvailability, LeaseRentFactResult
from notification import (
    PermanentDeliveryError,
    SQLiteNotificationInboxAdapter,
    SQLiteNotificationService,
    dispatch_one,
)
from shared import AuthContext
from shared.sqlite import init_connection


class NoopMaintenance(IMaintenanceIntake):
    def submit_issue(self, intake):
        raise AssertionError("smalltalk must not route to maintenance")


class RegisteredMaintenance(IMaintenanceIntake):
    def __init__(self, connection):
        self.connection = connection

    def submit_issue(self, intake):
        self.connection.execute(
            "INSERT INTO maintenance_request "
            "(request_id,tenant_id,property_id,message_id,event_id,title,description,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,'submitted',?,?)",
            (
                "request-emergency",
                intake.tenant_id,
                intake.property_id,
                intake.message_id,
                intake.event_id,
                intake.title,
                intake.description,
                "2026-10-29T00:00:00Z",
                "2026-10-29T00:00:00Z",
            ),
        )
        self.connection.commit()
        return RegisteredIntake(
            status="registered",
            request_id="request-emergency",
            persisted_status=MaintenanceStatus.SUBMITTED,
            priority=Priority.ROUTINE,
        )


class NoopLeaseRent(ILeaseRentQuery):
    def get_facts(self, query):
        return LeaseRentFactResult(
            message_id=query.message_id, availability=FactAvailability.NOT_AVAILABLE
        )


def _db():
    db = init_connection(":memory:")
    ts = "2026-10-29T00:00:00Z"
    db.executescript(
        f"""
        INSERT INTO tenant (tenant_id,name,contact,created_at)
          VALUES ('t1','Tenant',NULL,'{ts}');
        INSERT INTO property (property_id,name,address,created_at)
          VALUES ('p1','Property',NULL,'{ts}');
        INSERT INTO tenant_property (tenant_id,property_id,role,created_at)
          VALUES ('t1','p1','resident','{ts}');
        """
    )
    return db


def _service(db):
    return CommunicationService(
        db,
        NoopMaintenance(),
        NoopLeaseRent(),
        SQLiteNotificationService(db),
    )


def test_routine_communication_auto_dispatches_once_with_persisted_eligibility():
    db = _db()
    try:
        outcome = _service(db).handle_message(AuthContext("t1", "p1"), "hello")
        assert outcome.draft.status.value == "auto_eligible"
        event_id = f"reply:{outcome.message_id}"
        first = dispatch_one(
            db, event_id, "t1", SQLiteNotificationInboxAdapter(db)
        )
        second = dispatch_one(
            db, event_id, "t1", SQLiteNotificationInboxAdapter(db)
        )
        assert first.status.value == second.status.value == "delivered"
        assert first.attempts == second.attempts == 1
        assert db.execute("SELECT COUNT(*) FROM tenant_inbox").fetchone()[0] == 1
    finally:
        db.close()


def test_review_required_communication_never_queues_tenant_delivery():
    db = _db()
    try:
        outcome = _service(db).handle_message(
            AuthContext("t1", "p1"), "There is a strong smell of gas in my kitchen."
        )
        assert outcome.draft is None
        assert db.execute("SELECT COUNT(*) FROM notification_event").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM tenant_inbox").fetchone()[0] == 0
    finally:
        db.close()


def test_review_required_maintenance_never_queues_tenant_delivery():
    db = _db()
    try:
        service = CommunicationService(
            db,
            RegisteredMaintenance(db),
            NoopLeaseRent(),
            SQLiteNotificationService(db),
        )
        outcome = service.handle_message(
            AuthContext("t1", "p1"), "There is a strong smell of gas in my kitchen."
        )
        assert outcome.draft is not None
        assert outcome.draft.status.value == "pending_review"
        assert db.execute("SELECT COUNT(*) FROM notification_event").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM tenant_inbox").fetchone()[0] == 0
    finally:
        db.close()


def test_auto_dispatch_delivery_failure_is_persisted():
    db = _db()
    try:
        outcome = _service(db).handle_message(AuthContext("t1", "p1"), "hello")

        class FailingAdapter:
            def deliver(self, **kwargs):
                raise PermanentDeliveryError("demo destination unavailable")

        result = dispatch_one(db, f"reply:{outcome.message_id}", "t1", FailingAdapter())
        assert result.status.value == "failed"
        assert result.attempts == 1
        assert db.execute(
            "SELECT action FROM audit WHERE action='delivery.failed'"
        ).fetchone()
    finally:
        db.close()


def test_routine_communication_revoked_tenant_binding_blocks_dispatch_and_audits():
    """Queue while authorised, revoke, then dispatch against CURRENT bindings."""
    db = _db()
    try:
        outcome = _service(db).handle_message(AuthContext("t1", "p1"), "hello")
        event_id = f"reply:{outcome.message_id}"
        assert outcome.draft.status.value == "auto_eligible"
        assert db.execute(
            "SELECT status FROM notification_event WHERE event_id=?", (event_id,)
        ).fetchone()[0] == "accepted"
        db.execute("DELETE FROM tenant_property WHERE tenant_id='t1' AND property_id='p1'")
        db.commit()

        class RecordingInbox(SQLiteNotificationInboxAdapter):
            calls = 0

            def deliver(self, **kwargs):
                self.calls += 1
                return super().deliver(**kwargs)

        adapter = RecordingInbox(db)
        result = dispatch_one(db, event_id, "t1", adapter)
        assert result.status.value == "failed"
        assert result.detail == "recipient is no longer authorised for property"
        assert result.attempts == 1
        assert adapter.calls == 0
        assert db.execute("SELECT COUNT(*) FROM tenant_inbox").fetchone()[0] == 0
        row = db.execute(
            "SELECT status,last_error FROM notification_event WHERE event_id=?", (event_id,)
        ).fetchone()
        assert tuple(row) == ("failed", result.detail)
        audit = db.execute(
            "SELECT details FROM audit WHERE event_id=? AND action='delivery.failed'", (event_id,)
        ).fetchone()
        assert json.loads(audit[0])["reason"] == "recipient_property_authorization_denied"
        assert db.execute(
            "SELECT COUNT(*) FROM audit WHERE event_id=? AND action='delivery.delivered'", (event_id,)
        ).fetchone()[0] == 0
        # A retry cannot accidentally deliver this permanently denied event.
        assert dispatch_one(db, event_id, "t1", adapter).status.value == "failed"
        assert adapter.calls == 0
    finally:
        db.close()

