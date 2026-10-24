"""SQLite connection helper (shared, owned: repositories).

Every connection gets the schema from db/schema.sql, rowid-style access
(row_factory) and foreign keys ON (schema.sql convention, enforced at
connection time — SQLite does not persist the PRAGMA).

Upgrade path for pre-existing databases
---------------------------------------
Every connection runs the same two-phase upgrade (see ``apply_schema``):
first the idempotent migration chain, then the ``db/schema.sql`` script —
so a column added by a migration exists BEFORE any schema statement that
depends on it (e.g. ``rent_record.due_date`` is added by the v3 step before
``CREATE INDEX idx_rent_due`` is reached). Because the DDL is
``CREATE ... IF NOT EXISTS``, a *brand-new* database is fully created and a
*pre-existing* database is left unchanged for the tables it already has —
new tables are added, but existing columns are never altered. Re-running
the script therefore does NOT, by itself, upgrade a database that needs a
column added or changed.

To keep upgrades explicit and testable, we also track ``PRAGMA user_version``
and run a small, idempotent, versioned migration chain on every connect. New
tables land in both schema.sql (fresh installs) and the matching migration
step (pre-existing installs); any future column-level change MUST be added as
a migration step here, never by relying on the schema script alone.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from sqlite3 import Connection

import sqlite3

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"

# Bump by one each time a migration step is appended below. Fresh databases
# jump straight to this value; pre-existing databases climb up to it.
SCHEMA_VERSION = 4

# v4: receipt, timeline entry and document links are committed together.
_EVIDENCE_EVENT_DDL = """
CREATE TABLE IF NOT EXISTS evidence_event_receipt (
    event_id TEXT PRIMARY KEY,
    property_id TEXT NOT NULL REFERENCES property(property_id),
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL,
    received_by TEXT NOT NULL REFERENCES manager(manager_id)
);
CREATE TABLE IF NOT EXISTS evidence_maintenance_timeline (
    event_id TEXT PRIMARY KEY REFERENCES evidence_event_receipt(event_id),
    request_id TEXT NOT NULL,
    property_id TEXT NOT NULL REFERENCES property(property_id),
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_maintenance_request
    ON evidence_maintenance_timeline(property_id,request_id,occurred_at);
CREATE TABLE IF NOT EXISTS evidence_event_document (
    event_id TEXT NOT NULL REFERENCES evidence_maintenance_timeline(event_id),
    document_id TEXT NOT NULL REFERENCES evidence_document(document_id),
    PRIMARY KEY(event_id,document_id)
);
"""


def _ensure_evidence_events(conn: Connection) -> None:
    conn.executescript(_EVIDENCE_EVENT_DDL)

# Migration step for v2: manager + manager_property tables (owned:
# permissions). The DDL mirrors db/schema.sql so the step is self-contained
# for pre-existing databases; it is idempotent.
_MANAGER_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS manager (
    manager_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manager_property (
    manager_id  TEXT NOT NULL REFERENCES manager(manager_id),
    property_id TEXT NOT NULL REFERENCES property(property_id),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (manager_id, property_id)
);

CREATE INDEX IF NOT EXISTS idx_manager_property_property
    ON manager_property (property_id);
"""


def _ensure_manager_tables(conn: Connection) -> None:
    """Idempotent v2 step: make sure the manager tables exist.

    Future column-level upgrades for pre-existing databases belong in a new
    numbered step below, never in schema.sql alone.
    """
    conn.executescript(_MANAGER_TABLE_DDL)


# Migration step for v3: rent_record.due_date.
#
# Pre-existing databases created before due_date was added have a
# rent_record table WITHOUT that column. The schema script's
# ``CREATE TABLE IF NOT EXISTS`` does not alter the existing table, and
# ``CREATE INDEX idx_rent_due ON rent_record (due_date)`` fails on a table
# without the column. This step adds the column BEFORE the schema script
# runs (see ``apply_schema``), so the index can then be created.
#
# Scope of the upgrade, stated explicitly:
#   * The column is added as ``TEXT`` (the current schema's type). It is
#     intentionally NOT declared NOT NULL at migration time: rows in a
#     pre-existing database may not have a due_date to record, and a
#     NOT NULL addition would force SQLite to rewrite the table (or fail).
#     An unknown due_date therefore stays MISSING (NULL) — the step never
#     guesses a date, never recomputes stored rent status, and never
#     touches any other column or row.
#   * This step fixes EXACTLY the due_date column. It does NOT detect or
#     repair any other structural difference between a legacy rent_record
#     and the current schema.sql (other missing columns, stale CHECKs,
#     etc.). It therefore does NOT claim a "one column fixes all" upgrade:
#     a difference that a later schema statement happens to reference will
#     surface as an error at connect time, but an unrelated difference may
#     remain silently and is the table owner's (lease_rent) responsibility
#     to add as its own migration step.
#
# Idempotent: no-op when rent_record is absent or already has the column.
def _ensure_rent_record_due_date(conn: Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'rent_record'"
    ).fetchone()
    if row is None:
        return  # fresh database: schema.sql creates the full table
    columns = [
        r["name"] for r in conn.execute("PRAGMA table_info(rent_record)")
    ]
    if "due_date" not in columns:
        conn.execute("ALTER TABLE rent_record ADD COLUMN due_date TEXT")


# Ordered, 1-based. ``user_version`` is the highest step already applied.
_MIGRATIONS: dict[int, Callable[[Connection], None]] = {
    2: _ensure_manager_tables,
    3: _ensure_rent_record_due_date,
    4: _ensure_evidence_events,
}


def _apply_migrations(conn: Connection) -> None:
    """Apply any pending, idempotent schema migrations in order."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            "database schema version "
            f"{current} is newer than supported version {SCHEMA_VERSION}"
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        step = _MIGRATIONS.get(version)
        if step is None:
            # Baseline step (v1 = original schema) needs no explicit work.
            continue
        step(conn)
        conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()


def apply_schema(conn: Connection) -> None:
    """Upgrade ``conn`` to the current schema, in the safe order.

    The versioned migration chain runs FIRST, then the schema script.
    Column-level migrations must precede the script because the script's
    ``CREATE ... IF NOT EXISTS`` statements can depend on columns that
    only exist once a migration has run (e.g. ``idx_rent_due`` on
    ``rent_record(due_date)`` — the v3 step adds the column, then the
    script creates the index). Both halves are idempotent, so an already
    up-to-date database is left unchanged.
    """
    _apply_migrations(conn)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def init_connection(db_path: str | Path = ":memory:") -> Connection:
    """Open a connection with schema applied and foreign keys enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_schema(conn)
    return conn
