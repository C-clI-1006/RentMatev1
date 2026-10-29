from communication import CommunicationService
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

