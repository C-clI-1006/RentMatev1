"""SQLite integration tests for the lease/rent module."""

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from contracts.common import MessageId, PropertyId, TenantId
from contracts.lease_rent import FactAvailability, LeaseRentQuery, QuestionType
from lease_rent import (
    LeaseRentDataError,
    LeaseRentQueryService,
    SQLiteLeaseRentRepository,
    run_lease_deadline_check,
    run_overdue_rent_check,
)


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
NOW = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.executescript(
        """
        INSERT INTO tenant (tenant_id, name, created_at)
        VALUES ('tenant-1', 'Tenant', '2026-01-01T00:00:00Z');
        INSERT INTO property (property_id, name, created_at)
        VALUES ('property-1', 'Home', '2026-01-01T00:00:00Z');
        INSERT INTO tenant_property (tenant_id, property_id, created_at)
        VALUES ('tenant-1', 'property-1', '2026-01-01T00:00:00Z');
        INSERT INTO lease (
            lease_id, property_id, tenant_id, start_date, end_date, version, created_at
        ) VALUES (
            'lease-1', 'property-1', 'tenant-1', '2026-01-01', '2026-11-19',
            '1', '2026-01-01T00:00:00Z'
        );
        INSERT INTO rent_record (
            rent_record_id, lease_id, property_id, period, due_date,
            amount_due, amount_paid, status, created_at
        ) VALUES (
            'rent-1', 'lease-1', 'property-1', '2026-10', '2026-10-01',
            1000, 250, 'overdue', '2026-10-20T08:00:00Z'
        );
        """
    )
    yield connection
    connection.close()


def _query(question_type: QuestionType) -> LeaseRentQuery:
    return LeaseRentQuery(
        message_id=MessageId("message-1"),
        tenant_id=TenantId("tenant-1"),
        property_id=PropertyId("property-1"),
        question_type=question_type,
        question="test question",
        as_of=NOW,
    )


def test_schema_uses_due_date_and_three_state_model(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(rent_record)")}
    assert "due_date" in columns
    status = db.execute(
        "SELECT status FROM rent_record WHERE rent_record_id = 'rent-1'"
    ).fetchone()[0]
    assert status == "overdue"

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO rent_record (rent_record_id, lease_id, property_id, "
            "period, due_date, amount_due, amount_paid, status, created_at) "
            "VALUES ('rent-partial', 'lease-1', 'property-1', '2026-11', "
            "'2026-11-01', 1000, 250, 'partial', '2026-11-01T00:00:00Z')"
        )


def test_sqlite_repository_verifies_tenant_property_binding(db):
    repository = SQLiteLeaseRentRepository(db)
    assert repository.is_authorized(tenant_id="tenant-1", property_id="property-1")
    assert not repository.is_authorized(
        tenant_id="tenant-1", property_id="unknown-property"
    )


def test_fact_query_computes_status_from_structured_record(db):
    result = LeaseRentQueryService(SQLiteLeaseRentRepository(db)).get_facts(
        _query(QuestionType.RENT_STATUS)
    )
    values = {fact.name: fact.value for fact in result.facts}

    assert result.availability is FactAvailability.AVAILABLE
    assert values == {
        "current_period_status": "overdue",
        "overdue_days": "19",
        "rent_due_date": "2026-10-01",
    }
    assert all(fact.references[0].source_id == "rent-1" for fact in result.facts)


def test_fact_query_returns_rent_amounts_without_partial_state(db):
    result = LeaseRentQueryService(SQLiteLeaseRentRepository(db)).get_facts(
        _query(QuestionType.RENT_AMOUNT)
    )
    values = {fact.name: fact.value for fact in result.facts}

    assert values == {
        "amount_due": "1000",
        "amount_paid": "250",
        "outstanding_amount": "750",
    }


def test_fact_query_returns_active_lease_dates(db):
    result = LeaseRentQueryService(SQLiteLeaseRentRepository(db)).get_facts(
        _query(QuestionType.LEASE_DATES)
    )
    values = {fact.name: fact.value for fact in result.facts}

    assert values == {
        "lease_end_date": "2026-11-19",
        "lease_start_date": "2026-01-01",
    }


def test_uc4_repository_scan_produces_overdue_candidate(db):
    candidates = run_overdue_rent_check(
        SQLiteLeaseRentRepository(db), as_of=NOW.date()
    )

    assert len(candidates) == 1
    assert candidates[0].rent_record_id == "rent-1"
    assert candidates[0].outstanding_amount == 750


def test_uc5_alert_is_persisted_once_across_scheduler_reruns(db):
    repository = SQLiteLeaseRentRepository(db)

    first = run_lease_deadline_check(
        repository,
        as_of=NOW.date(),
        reminder_window_days=30,
        created_at=NOW,
    )
    second = run_lease_deadline_check(
        repository,
        as_of=NOW.date(),
        reminder_window_days=30,
        created_at=NOW,
    )

    assert len(first) == 1
    assert second == ()
    stored = db.execute(
        "SELECT candidate_key, remaining_days FROM lease_deadline_alert"
    ).fetchall()
    assert stored == [("lease-deadline:lease-1:2026-11-19", 30)]


def test_incomplete_rent_record_is_referred_as_data_error(db):
    db.execute(
        "UPDATE rent_record SET amount_paid = NULL WHERE rent_record_id = 'rent-1'"
    )

    with pytest.raises(LeaseRentDataError, match="missing required data"):
        run_overdue_rent_check(SQLiteLeaseRentRepository(db), as_of=date(2026, 10, 20))
