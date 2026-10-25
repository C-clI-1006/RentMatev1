"""TEST-ONLY: explicit seed for the evidence prototype demo.

Creates the separated demo identities for the evidence prototype:

* ONE fictional tenant (T-DEMO-01),
* ONE fictional manager (M-DEMO-01),
* ONE fictional property (P-DEMO-01) shared by both,
* a tenant_property binding (T-DEMO-01, P-DEMO-01) with role='resident'
  — the tenant uploads through this binding,
* a manager_property binding (M-DEMO-01, P-DEMO-01)
  — the manager reviews through this binding.

These rows are fake identities for the prototype only: they are NOT
real authentication and must never be treated as real users. The
manager is a proper `manager` row with a `manager_property` binding —
NEVER a fake tenant_property(role='manager') row.

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
  * Existing tenant / property / manager rows are NEVER overwritten:
    only the missing identity rows are created, and only with the
    NOT NULL required columns (name / created_at); optional columns
    (contact / address) stay NULL.
  * Existing bindings are NEVER modified: the intended binding rows
    are skipped, and any OTHER existing binding for the demo pair is
    reported as a conflict and the whole round rolls back. In
    particular an old tenant_property(T-DEMO-01, P-DEMO-01) row with
    role='manager' is NOT silently rewritten to role='resident' — the
    round fails with an explicit error and the operator must resolve
    it by hand. No role is ever upgraded or escalated.
  * Never creates tokens, API keys, or any kind of credential.
  * Never creates evidence / classification / review records.
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

TENANT_ID = "T-DEMO-01"
MANAGER_ID = "M-DEMO-01"
PROPERTY_ID = "P-DEMO-01"
TENANT_ROLE = "resident"

_TENANT_NAME = "TEST-ONLY Demo Tenant (fictional; not a real identity)"
_MANAGER_NAME = "TEST-ONLY Demo Manager (fictional; not a real identity)"
_PROPERTY_NAME = "TEST-ONLY Demo Property (fictional; not a real identity)"


class SeedConflictError(RuntimeError):
    """A demo binding row already exists in a state this seed must
    not change.

    Raised INSIDE the seed transaction so the whole round rolls back;
    the existing row is never modified, no role is ever escalated, and
    an old manager-role tenant binding is never rewritten.
    """


def _seed(conn: sqlite3.Connection, now: str) -> dict[str, bool]:
    """Run the entire seed in ONE transaction.

    Returns a per-item flag map (created vs already present). Any
    exception — including SeedConflictError — rolls back every write
    of this round via the `with conn:` context.
    """
    result = {
        "tenant": False,
        "manager": False,
        "property": False,
        "tenant_binding": False,
        "manager_binding": False,
    }

    with conn:  # commit on success, rollback on any exception
        # Create parent identities before FK-bound rows. Existing identity
        # fields remain untouched; conflicts roll back the whole seed round.
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
                "SELECT 1 FROM manager WHERE manager_id = ?", (MANAGER_ID,)
            ).fetchone()
            is None
        ):
            conn.execute(
                "INSERT INTO manager (manager_id, name, created_at) "
                "VALUES (?, ?, ?)",
                (MANAGER_ID, _MANAGER_NAME, now),
            )
            result["manager"] = True

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

        # -- tenant_property: expect the intended resident binding only
        rows = conn.execute(
            "SELECT role FROM tenant_property "
            "WHERE tenant_id = ? AND property_id = ?",
            (TENANT_ID, PROPERTY_ID),
        ).fetchall()
        for role in (r[0] for r in rows):
            if role != TENANT_ROLE:
                raise SeedConflictError(
                    f"tenant_property({TENANT_ID}, {PROPERTY_ID}) already "
                    f"exists with role={role!r}; refusing to overwrite or "
                    f"rewrite it to '{TENANT_ROLE}'. No data was written; "
                    f"resolve the binding by hand and re-run."
                )
        if not rows:
            conn.execute(
                "INSERT INTO tenant_property (tenant_id, property_id, role, created_at) "
                "VALUES (?, ?, ?, ?)",
                (TENANT_ID, PROPERTY_ID, TENANT_ROLE, now),
            )
            result["tenant_binding"] = True

        # -- manager_property: expect (at most) the intended binding
        existing = conn.execute(
            "SELECT 1 FROM manager_property "
            "WHERE manager_id = ? AND property_id = ?",
            (MANAGER_ID, PROPERTY_ID),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO manager_property (manager_id, property_id, created_at) "
                "VALUES (?, ?, ?)",
                (MANAGER_ID, PROPERTY_ID, now),
            )
            result["manager_binding"] = True

    return result


def _print_state(label: str, created: bool) -> None:
    if created:
        print(f"  {label}: created (TEST-ONLY, fictional)")
    else:
        print(f"  {label}: already present, left untouched")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "TEST-ONLY: seed the separated demo identities (tenant "
            "T-DEMO-01 / manager M-DEMO-01 / property P-DEMO-01) for the "
            "evidence prototype. Not real authentication."
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
        # Upgrade legacy columns before applying schema indexes; not seed data.
        if str(_BACKEND_DIR) not in sys.path:
            sys.path.insert(0, str(_BACKEND_DIR))
        from shared.sqlite import apply_schema

        conn.row_factory = sqlite3.Row
        apply_schema(conn)

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
    _print_state(f"manager {MANAGER_ID}", result["manager"])
    _print_state(f"property {PROPERTY_ID}", result["property"])
    _print_state(
        f"tenant_property ({TENANT_ID}, {PROPERTY_ID}, role={TENANT_ROLE})",
        result["tenant_binding"],
    )
    _print_state(
        f"manager_property ({MANAGER_ID}, {PROPERTY_ID})",
        result["manager_binding"],
    )
    if not any(result.values()):
        print("nothing to do: all rows already present.")
    print(f"seed complete: db={args.db}, at={now} (UTC)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
