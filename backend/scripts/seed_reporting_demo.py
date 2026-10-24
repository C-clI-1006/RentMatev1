"""TEST-ONLY: explicit seed for the reporting demo.

Seeds ONE fictional tenant (reporting-demo-tenant), ONE fictional property
(reporting-demo-property) and ONE fictional manager (reporting-demo-manager)
with the correct bindings, ONE lease covering 2026-09, ONE unpaid
overdue rent record for 2026-09, and ONE fictional OPEN maintenance request
(reporting-demo-maintenance) created in 2026-09 whose priority has never
been assessed (priority=None is preserved as-is). These rows are fake
identities for the prototype only: they are NOT real authentication and
must never be treated as real users.

Run explicitly from a shell (the service never calls this on startup):

    python backend/scripts/seed_reporting_demo.py --db path/to/other.db

Rules:
  * Explicit --db path is required; the script never touches .env or any
    default database.
  * shared.sqlite.init_connection applies the schema (idempotent,
    CREATE ... IF NOT EXISTS) and foreign keys are ON.
  * All seed data is written in ONE transaction; any error rolls back the
    whole round. INSERT OR IGNORE only — rows with the same IDs that
    already exist are NEVER overwritten, so re-running is a no-op.
  * The maintenance row is built as a validated MaintenanceView /
    MaintenanceEvent (priority=None, still open / SUBMITTED) and written
    through maintenance.Store, which owns its own mc_* tables. The real
    LLM assessor is NEVER called; no status change is inferred.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make `shared` importable when run as a plain script from any CWD
# (the same convention pytest applies via pythonpath in pyproject.toml).
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from contracts.maintenance import (  # noqa: E402
    MaintenanceEvent,
    MaintenanceStatus,
    MaintenanceView,
    RegisteredIntake,
)
from maintenance.store import Store  # noqa: E402
from shared.sqlite import init_connection  # noqa: E402

TENANT_ID = "reporting-demo-tenant"
PROPERTY_ID = "reporting-demo-property"
MANAGER_ID = "reporting-demo-manager"
LEASE_ID = "reporting-demo-lease"
RENT_RECORD_ID = "reporting-demo-rent-2026-09"
MAINTENANCE_REQUEST_ID = "reporting-demo-maintenance"
MAINTENANCE_EVENT_ID = "reporting-demo-maintenance-created"
MAINTENANCE_MESSAGE_ID = "reporting-demo-message"
PERIOD = "2026-09"
# The demo maintenance request was created inside 2026-09 and stayed open
# through the whole month (no status change until after the period end).
MAINTENANCE_CREATED_AT = "2026-09-10T09:30:00+00:00"

_TENANT_NAME = "TEST-ONLY Reporting Demo Tenant (fictional; not a real identity)"
_PROPERTY_NAME = "TEST-ONLY Reporting Demo Property (fictional; not a real identity)"
_MANAGER_NAME = "TEST-ONLY Reporting Demo Manager (fictional; not a real identity)"


def _seed(conn: sqlite3.Connection, now: str) -> dict[str, bool]:
    """Run the entire seed in ONE transaction.

    Returns a per-item flag map (created vs already present). All writes
    are INSERT OR IGNORE keyed on stable primary keys, so existing rows
    are never modified and a re-run adds nothing. Any exception rolls
    back the whole round via the `with conn:` context.
    """
    result = {
        "tenant": False,
        "property": False,
        "manager": False,
        "tenant_property": False,
        "manager_property": False,
        "lease": False,
        "rent_record": False,
    }

    with conn:  # commit on success, rollback on any exception
        def present(table: str, column: str, value: str) -> bool:
            return (
                conn.execute(
                    f"SELECT 1 FROM {table} WHERE {column} = ?", (value,)
                ).fetchone()
                is not None
            )

        # Identities (auth-owned stubs) + verified bindings.
        if not present("tenant", "tenant_id", TENANT_ID):
            conn.execute(
                "INSERT OR IGNORE INTO tenant (tenant_id, name, contact, created_at) "
                "VALUES (?, ?, NULL, ?)",
                (TENANT_ID, _TENANT_NAME, now),
            )
            result["tenant"] = True

        if not present("property", "property_id", PROPERTY_ID):
            conn.execute(
                "INSERT OR IGNORE INTO property (property_id, name, address, created_at) "
                "VALUES (?, ?, NULL, ?)",
                (PROPERTY_ID, _PROPERTY_NAME, now),
            )
            result["property"] = True

        if not present("manager", "manager_id", MANAGER_ID):
            conn.execute(
                "INSERT OR IGNORE INTO manager (manager_id, name, created_at) "
                "VALUES (?, ?, ?)",
                (MANAGER_ID, _MANAGER_NAME, now),
            )
            result["manager"] = True

        if not present("tenant_property", "tenant_id", TENANT_ID):
            conn.execute(
                "INSERT OR IGNORE INTO tenant_property "
                "(tenant_id, property_id, role, created_at) VALUES (?, ?, 'resident', ?)",
                (TENANT_ID, PROPERTY_ID, now),
            )
            result["tenant_property"] = True

        if not present("manager_property", "manager_id", MANAGER_ID):
            conn.execute(
                "INSERT OR IGNORE INTO manager_property "
                "(manager_id, property_id, created_at) VALUES (?, ?, ?)",
                (MANAGER_ID, PROPERTY_ID, now),
            )
            result["manager_property"] = True

        # One lease covering the 2026-09 report month.
        if not present("lease", "lease_id", LEASE_ID):
            conn.execute(
                "INSERT OR IGNORE INTO lease "
                "(lease_id, property_id, tenant_id, start_date, end_date, "
                " document_ref, version, created_at) "
                "VALUES (?, ?, ?, '2026-09-01', '2026-12-31', NULL, 'v1', ?)",
                (LEASE_ID, PROPERTY_ID, TENANT_ID, now),
            )
            result["lease"] = True

        # One unpaid rent record for 2026-09 whose due date has already
        # passed: it demonstrates an open task and the overdue risk rule.
        # The stored status column is derived at collection time by
        # lease_rent.rules; the seed only persists the deterministic facts.
        if not present("rent_record", "rent_record_id", RENT_RECORD_ID):
            conn.execute(
                "INSERT OR IGNORE INTO rent_record "
                "(rent_record_id, lease_id, property_id, period, due_date, "
                " amount_due, amount_paid, paid_at, status, created_at) "
                "VALUES (?, ?, ?, ?, '2026-09-05', 2500.0, 0.0, NULL, "
                "'unpaid-but-not-due', ?)",
                (RENT_RECORD_ID, LEASE_ID, PROPERTY_ID, PERIOD, now),
            )
            result["rent_record"] = True

    return result


def _seed_maintenance(db: sqlite3.Connection) -> bool:
    """Seed ONE open maintenance request inside a Store transaction.

    Returns True when the row was created in this run, False when it
    already exists (never overwritten). The stored document is a
    pydantic-validated MaintenanceView carrying exactly one matching
    MaintenanceEvent, mirroring how MaintenanceService persists intake:
    mc_request row (document + original result) plus one mc_history row
    per event and one mc_outbox row per dispatch target. No assessor or
    LLM is called; priority stays None as-is.
    """
    existing = db.execute(
        "SELECT id FROM mc_request WHERE id=? "
        "OR (property_id=? AND (message_id=? OR intake_event=?))",
        (
            MAINTENANCE_REQUEST_ID,
            PROPERTY_ID,
            MAINTENANCE_MESSAGE_ID,
            MAINTENANCE_EVENT_ID,
        ),
    ).fetchone()
    if existing is not None:
        return False

    # Deterministic in-month timestamp (fixed, not "now") so the 2026-09
    # report month and every re-run stay reproducible.
    occurred_at = datetime.fromisoformat(MAINTENANCE_CREATED_AT)
    event = MaintenanceEvent(
        event_id=MAINTENANCE_EVENT_ID,
        request_id=MAINTENANCE_REQUEST_ID,
        property_id=PROPERTY_ID,
        occurred_at=occurred_at,
        actor=f"tenant:{TENANT_ID}",
        from_status=None,
        to_status=MaintenanceStatus.SUBMITTED,
        priority=None,
        reason="Initial registration",
    )
    view = MaintenanceView(
        request_id=MAINTENANCE_REQUEST_ID,
        tenant_id=TENANT_ID,
        property_id=PROPERTY_ID,
        message_id=MAINTENANCE_MESSAGE_ID,
        title="Leaking kitchen tap (fictional demo request)",
        description=(
            "TEST-ONLY: kitchen tap has been dripping since morning. "
            "No flood and no electrical issue."
        ),
        location="Kitchen",
        priority=None,
        status=MaintenanceStatus.SUBMITTED,
        version=1,
        emergency_flag=False,
        created_at=occurred_at,
        updated_at=occurred_at,
        events=[event],
    )
    # Self-consistency: view, version and event history must agree.
    assert view.version == 1 and view.events == [event]
    assert view.status is event.to_status and view.priority is None
    registered = RegisteredIntake(
        request_id=view.request_id,
        persisted_status=view.status,
    )

    db.execute(
        "INSERT OR IGNORE INTO mc_request VALUES (?,?,?,?,?,?,?,?)",
        (
            view.request_id,
            view.property_id,
            view.tenant_id,
            view.message_id,
            event.event_id,
            view.version,
            view.model_dump_json(),
            registered.model_dump_json(),
        ),
    )
    db.execute(
        "INSERT OR IGNORE INTO mc_history VALUES (?,?,?)",
        (event.event_id, view.request_id, event.model_dump_json()),
    )
    # Same outbox rows the real service writes on submission
    # (communication + evidence); this seed never dispatches them.
    payload = event.model_dump(mode="json")
    payload.update(
        recipient_id=f"tenant:{view.tenant_id}",
        review_required=view.emergency_flag
        or view.status == MaintenanceStatus.CANCELLED,
        message_type="maintenance_status",
    )
    for target in ("communication", "evidence"):
        db.execute(
            "INSERT OR IGNORE INTO mc_outbox"
            "(event_id,target,request_id,property_id,document) VALUES (?,?,?,?,?)",
            (event.event_id, target, view.request_id, view.property_id, json.dumps(payload)),
        )
    return True


def _print_state(label: str, created: bool) -> None:
    if created:
        print(f"  {label}: created (TEST-ONLY, fictional)")
    else:
        print(f"  {label}: already present, left untouched")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "TEST-ONLY: seed the fictional reporting demo tenant/property/"
            "manager, a lease covering 2026-09, one unpaid overdue rent "
            "record and one open maintenance request created in 2026-09. "
            "Not real authentication."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="SQLite database path (explicit path required)",
    )
    args = parser.parse_args(argv)

    print(
        "TEST-ONLY seed: fictional identities for the reporting prototype. "
        "NOT real authentication; never treat these rows as real users."
    )

    now = datetime.now(timezone.utc).isoformat()

    conn = init_connection(args.db)
    store = Store(args.db)  # owns the mc_* maintenance tables (idempotent)
    try:
        try:
            result = _seed(conn, now)
            with store.transaction() as db:
                result["maintenance_request"] = _seed_maintenance(db)
        except sqlite3.Error as exc:
            print(f"seed failed, round rolled back: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    for label, created in result.items():
        _print_state(label, created)
    if not any(result.values()):
        print("nothing to do: all rows already present.")
    print(f"seed complete: db={args.db}, at={now} (UTC)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
