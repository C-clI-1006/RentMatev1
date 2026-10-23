"""SQLite connection helper (shared, owned: repositories).

Every connection gets the schema from db/schema.sql, rowid-style access
(row_factory) and foreign keys ON (schema.sql convention, enforced at
connection time — SQLite does not persist the PRAGMA).

Upgrade path for pre-existing databases
---------------------------------------
``init_connection`` re-applies ``db/schema.sql`` on every connect. Because
the DDL is ``CREATE ... IF NOT EXISTS``, a *brand-new* database is fully
created and a *pre-existing* database is left unchanged for the tables it
already has — new tables are added, but existing columns are never altered.
Re-running the script therefore does NOT, by itself, upgrade a database that
needs a column added or changed.

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
SCHEMA_VERSION = 2

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


# Ordered, 1-based. ``user_version`` is the highest step already applied.
_MIGRATIONS: dict[int, Callable[[Connection], None]] = {
    2: _ensure_manager_tables,
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


def init_connection(db_path: str | Path = ":memory:") -> Connection:
    """Open a connection with schema applied and foreign keys enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _apply_migrations(conn)
    return conn
