"""Maintenance-owned tables; never writes another module's private tables."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS mc_request (
 id TEXT PRIMARY KEY, property_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
 message_id TEXT NOT NULL, intake_event TEXT NOT NULL, version INTEGER NOT NULL,
 document TEXT NOT NULL, original_result TEXT NOT NULL,
 UNIQUE(property_id,message_id), UNIQUE(property_id,intake_event));
CREATE TABLE IF NOT EXISTS mc_history (
 event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES mc_request(id),
 document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mc_outbox (
 event_id TEXT NOT NULL, target TEXT NOT NULL, request_id TEXT NOT NULL,
 property_id TEXT NOT NULL, document TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT, PRIMARY KEY(event_id,target));
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
