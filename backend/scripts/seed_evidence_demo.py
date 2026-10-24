"""TEST-ONLY: explicit seed for the evidence prototype demo.

Creates ONE fictional tenant (T-DEMO-01), ONE fictional property
(P-DEMO-01) and a tenant_property binding with role='manager'. These
rows are fake identities for the prototype only: they are NOT real
authentication and must never be treated as real users.

Run explicitly from a shell (the service never calls this on startup):

    python backend/scripts/seed_evidence_demo.py
    python backend/scripts/seed_evidence_demo.py --db path/to/other.db

Rules:
  * stdlib sqlite3 only; PRAGMA foreign_keys = ON; parameterised SQL.
  * The shared schema (db/schema.sql, resolved from this file's location,
    so the CWD does not matter) is applied first; it is idempotent
    (CREATE ... IF NOT EXISTS) and is NOT part of the seed data.
  * All seed data is written in ONE transaction; any error rolls back
    the whole round. Plain INSERT only — never INSERT OR REPLACE.
  * Existing tenant/property rows are NEVER overwritten; an existing
    role='manager' binding is left untouched (no duplicate writes).
  * If the binding already exists with a NON-manager role, the script
    reports an error and rolls back: it never upgrades/escalates a role.
  * Never creates evidence / classification / review records.
    No credentials or keys of any kind are written.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent          # .../backend/scripts
_BACKEND_DIR = _SCRIPT_DIR.parent                      # .../backend
# Fixed default, resolved from this file's location (independent of CWD),
# same convention as backend/main.py.
_DEFAULT_DB_PATH = _BACKEND_DIR / "rentmate.db"
# Project schema, resolved relative to this script (backend/scripts -> repo root).
_SCHEMA_FILE = _BACKEND_DIR.parent / "db" / "schema.sql"

TENANT_ID = "T-DEMO-01"
PROPERTY_ID = "P-DEMO-01"
MANAGER_ROLE = "manager"

_TENANT_NAME = "TEST-ONLY Demo Tenant (fictional; not a real identity)"
_PROPERTY_NAME = "TEST-ONLY Demo Property (fictional; not a real identity)"


class SeedConflictError(RuntimeError):
    """The binding already exists with a role other than 'manager'.

    Raised INSIDE the seed transaction so the whole round rolls back;
    the existing row is never modified and the role is never escalated.
    """


def _seed(conn: sqlite3.Connection, now: str) -> dict[str, bool]:
    """Run the entire seed in ONE transaction.

    Returns a per-item flag map (created vs already present). Any
    exception — including SeedConflictError — rolls back every write
    of this round via the `with conn:` context.
    """
    result = {
        "tenant": False,
        "property": False,
        "binding": False,
    }

    with conn:  # commit on success, rollback on any exception
        row = conn.execute(
            "SELECT role FROM tenant_property "
            "WHERE tenant_id = ? AND property_id = ?",
            (TENANT_ID, PROPERTY_ID),
        ).fetchone()

        if row is not None:
            existing_role = row[0]
            if existing_role != MANAGER_ROLE:
                raise SeedConflictError(
                    f"tenant_property({TENANT_ID}, {PROPERTY_ID}) already exists "
                    f"with role={existing_role!r}; refusing to overwrite or "
                    f"upgrade to '{MANAGER_ROLE}'. No data was written."
                )
            # role='manager' binding already present: foreign keys
            # guarantee the parent rows exist, so nothing to write.
            # No duplicates, no overwrite.
            return result

        if (
            conn.execute(
                "SELECT 1 FROM tenant WHERE tenant_id = ?", (TENANT_ID,)
            ).fetchone()
            is None
        ):
            conn.execute(
                "INSERT INTO tenant (tenant_id, name, contact, created_at) "
                "VALUES (?, ?, NULL, ?)",
                (TENANT_ID, _TENANT_NAME, now),
            )
            result["tenant"] = True

        if (
            conn.execute(
                "SELECT 1 FROM property WHERE property_id = ?", (PROPERTY_ID,)
            ).fetchone()
            is None
        ):
            conn.execute(
                "INSERT INTO property (property_id, name, address, created_at) "
                "VALUES (?, ?, NULL, ?)",
                (PROPERTY_ID, _PROPERTY_NAME, now),
            )
            result["property"] = True

        conn.execute(
            "INSERT INTO tenant_property (tenant_id, property_id, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (TENANT_ID, PROPERTY_ID, MANAGER_ROLE, now),
        )
        result["binding"] = True

    return result


def _print_state(label: str, created: bool) -> None:
    if created:
        print(f"  {label}: created (TEST-ONLY, fictional)")
    else:
        print(f"  {label}: already present, left untouched")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "TEST-ONLY: seed the fictional demo tenant/property/manager "
            "binding for the evidence prototype. Not real authentication."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {_DEFAULT_DB_PATH})",
    )
    args = parser.parse_args(argv)

    print(
        "TEST-ONLY seed: fictional identities for the prototype. "
        "NOT real authentication; never treat these rows as real users."
    )

    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(str(args.db))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # Idempotent DDL (CREATE ... IF NOT EXISTS); not seed data.
        conn.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))

        try:
            result = _seed(conn, now)
        except SeedConflictError as exc:
            print(f"seed aborted, round rolled back: {exc}", file=sys.stderr)
            return 1
        except sqlite3.Error as exc:
            print(f"seed failed, round rolled back: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    _print_state(f"tenant {TENANT_ID}", result["tenant"])
    _print_state(f"property {PROPERTY_ID}", result["property"])
    _print_state(
        f"binding ({TENANT_ID}, {PROPERTY_ID}, role={MANAGER_ROLE})",
        result["binding"],
    )
    if not any(result.values()):
        print("nothing to do: all rows already present.")
    print(f"seed complete: db={args.db}, at={now} (UTC)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
