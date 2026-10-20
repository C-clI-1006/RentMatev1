"""SQLite connection helper (shared, owned: repositories).

Every connection gets the schema from db/schema.sql, rowid-style access
(row_factory) and foreign keys ON (schema.sql convention, enforced at
connection time — SQLite does not persist the PRAGMA).
"""

from __future__ import annotations

from pathlib import Path
from sqlite3 import Connection

import sqlite3

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def init_connection(db_path: str | Path = ":memory:") -> Connection:
    """Open a connection with schema applied and foreign keys enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn
