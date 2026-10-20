"""Schema-level test: draft origin invariants for system-generated drafts.

UC5 (scheduled rent reminders) produces drafts that have NO inbound
tenant message. The schema must therefore allow `message_id IS NULL`
while keeping the origin traceable via `(source_kind, source_id)`, and
dedup re-runs via the unique index on `source_id`.

Run: pytest backend/tests/
"""

import sqlite3
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    yield conn
    conn.close()


def test_system_generated_draft_without_message_id(db):
    # UC5 reminder draft: origin is the rent reminder, not a tenant message.
    # source_id reuses the lease_rent candidate key format verbatim
    # (rent_reminder_key -> 'rent-overdue:<rent_record_id>:<due_date>').
    source_id = "rent-overdue:rr-1:2026-10-01"
    db.execute(
        "INSERT INTO draft (draft_id, message_id, request_id, source_kind, "
        "source_id, content, created_at) VALUES "
        "('d-1', NULL, NULL, 'rent_reminder', ?, "
        "'Reminder: rent due 2026-10-01', '2026-10-19T00:00:00Z')",
        (source_id,),
    )
    row = db.execute(
        "SELECT message_id, source_kind, source_id FROM draft WHERE draft_id = 'd-1'"
    ).fetchone()
    assert row == (None, "rent_reminder", source_id)


def test_duplicate_source_id_rejected(db):
    # A scheduler re-run must not create a second review item.
    def insert(source_id: str, draft_id: str) -> None:
        db.execute(
            "INSERT INTO draft (draft_id, message_id, source_kind, source_id, "
            "content, created_at) VALUES (?, NULL, 'rent_alert', ?, 'x', "
            "'2026-10-19T00:00:00Z')",
            (draft_id, source_id),
        )

    insert("rent-overdue:rr-1:2026-10-01", "d-1")
    with pytest.raises(sqlite3.IntegrityError):
        insert("rent-overdue:rr-1:2026-10-01", "d-2")


def test_tenant_message_draft_still_works(db):
    # Existing reply-to-message flow is unchanged.
    db.execute(
        "INSERT INTO tenant (tenant_id, name, created_at) VALUES "
        "('t-1', 'T', '2026-10-01T00:00:00Z')"
    )
    db.execute(
        "INSERT INTO property (property_id, name, created_at) VALUES "
        "('p-1', 'P', '2026-10-01T00:00:00Z')"
    )
    db.execute(
        "INSERT INTO message (message_id, tenant_id, property_id, content, "
        "message_type, received_at) VALUES "
        "('m-1', 't-1', 'p-1', 'hello', 'lease_rent', '2026-10-19T00:00:00Z')"
    )
    db.execute(
        "INSERT INTO draft (draft_id, message_id, content, created_at) VALUES "
        "('d-3', 'm-1', 'reply', '2026-10-19T00:00:00Z')"
    )
    row = db.execute(
        "SELECT message_id, source_id FROM draft WHERE draft_id = 'd-3'"
    ).fetchone()
    assert row == ("m-1", None)


def test_main_app_imports_cleanly():
    # The UC5 ops endpoint is wired in main.py; importing the app must not
    # require lease_rent to be implemented yet (import is deferred).
    import main  # noqa: F401
