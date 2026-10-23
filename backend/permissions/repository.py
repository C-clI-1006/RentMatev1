"""SQLite persistence owned by the permissions module.

Read-only view over the ``manager`` / ``manager_property`` tables
(db/schema.sql). The schema already verifies each binding row
(foreign keys to manager + property), so the repository only ever
SELECTs; it never writes and never trusts a caller-supplied identity
beyond the value handed to it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ManagerPropertyRecord:
    """One verified manager<->property binding."""

    manager_id: str
    property_id: str
    created_at: str


class SQLiteManagerRepository:
    """Reads manager identity rows and verified property bindings."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def manager_exists(self, manager_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM manager WHERE manager_id = ?", (manager_id,)
        ).fetchone()
        return row is not None

    def get_manager_name(self, manager_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT name FROM manager WHERE manager_id = ?", (manager_id,)
        ).fetchone()
        return None if row is None else str(row["name"])

    def list_property_ids(self, manager_id: str) -> Sequence[ManagerPropertyRecord]:
        """All verified bindings for one manager (default deny: empty)."""
        rows = self._connection.execute(
            "SELECT manager_id, property_id, created_at "
            "FROM manager_property WHERE manager_id = ? "
            "ORDER BY property_id",
            (manager_id,),
        ).fetchall()
        return tuple(
            ManagerPropertyRecord(
                manager_id=row["manager_id"],
                property_id=row["property_id"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    def is_manager_of_property(self, manager_id: str, property_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM manager_property "
            "WHERE manager_id = ? AND property_id = ?",
            (manager_id, property_id),
        ).fetchone()
        return row is not None
