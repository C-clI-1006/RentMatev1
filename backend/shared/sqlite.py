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
SCHEMA_VERSION = 12

_TENANT_INBOX_DDL = """
CREATE TABLE IF NOT EXISTS tenant_inbox (
    event_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL REFERENCES tenant(tenant_id),
    property_id TEXT NOT NULL REFERENCES property(property_id),
    content TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY(event_id,recipient_id),
    FOREIGN KEY(event_id,recipient_id)
        REFERENCES notification_event(event_id,recipient_id)
);
CREATE INDEX IF NOT EXISTS idx_tenant_inbox_scope
    ON tenant_inbox(recipient_id,property_id,delivered_at);
"""

_MANAGER_INBOX_DDL = """
CREATE TABLE IF NOT EXISTS manager_inbox (
    event_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL REFERENCES manager(manager_id),
    property_id TEXT NOT NULL REFERENCES property(property_id),
    content TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY(event_id,recipient_id),
    FOREIGN KEY(event_id,recipient_id)
        REFERENCES notification_event(event_id,recipient_id)
);
CREATE INDEX IF NOT EXISTS idx_manager_inbox_scope
    ON manager_inbox(recipient_id,property_id,delivered_at);
"""

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

_REPORT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS report (
    report_id      TEXT PRIMARY KEY,
    property_id    TEXT NOT NULL REFERENCES property(property_id),
    period_year    INTEGER NOT NULL,
    period_month   INTEGER NOT NULL CHECK (period_month BETWEEN 1 AND 12),
    generation     INTEGER NOT NULL CHECK (generation >= 1),
    trigger_kind   TEXT NOT NULL CHECK (trigger_kind IN ('scheduled','manager')),
    event_id       TEXT NOT NULL UNIQUE,
    manager_id     TEXT,
    outcome        TEXT NOT NULL CHECK (outcome IN ('complete','partial_success','failed')),
    data_json      TEXT NOT NULL,
    narrative_json TEXT NOT NULL,
    data_as_of     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    UNIQUE (property_id, period_year, period_month, generation)
);

CREATE INDEX IF NOT EXISTS idx_report_property_period
    ON report (property_id, period_year, period_month);
"""


def _ensure_manager_tables(conn: Connection) -> None:
    """Idempotent v2 step: make sure the manager tables exist.

    Future column-level upgrades for pre-existing databases belong in a new
    numbered step below, never in schema.sql alone.
    """
    conn.executescript(_MANAGER_TABLE_DDL)


def _ensure_report_tables(conn: Connection) -> None:
    """Ensure reporting-owned snapshot persistence exists."""
    conn.executescript(_REPORT_TABLE_DDL)


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


def _ensure_v3_lease_and_reports(conn: Connection) -> None:
    """Resolve the two historical v3 candidates idempotently.

    PR #8 used v3 for ``rent_record.due_date`` while PR #11 used v3 for
    reporting persistence. Running both guards makes upgrades safe no matter
    which candidate a developer database previously exercised.
    """
    _ensure_rent_record_due_date(conn)
    _ensure_report_tables(conn)


def _ensure_v4_integrations(conn: Connection) -> None:
    """Ensure all PR #8/#11 integration objects through schema v4."""
    _ensure_rent_record_due_date(conn)
    _ensure_report_tables(conn)
    _ensure_evidence_events(conn)


def _ensure_v5_reconciliation(conn: Connection) -> None:
    """Reconcile databases already stamped by either pre-merge branch."""
    _ensure_v4_integrations(conn)


def _ensure_v6_tenant_inbox(conn: Connection) -> None:
    """Add the durable prototype delivery sink for existing databases."""
    _ensure_v5_reconciliation(conn)
    conn.executescript(_TENANT_INBOX_DDL)


_DRAFT_V7_DDL = """
CREATE TABLE draft_v7 (
    draft_id      TEXT PRIMARY KEY,
    message_id    TEXT REFERENCES message(message_id),
    request_id    TEXT,
    source_kind   TEXT,
    source_id     TEXT,
    version       INTEGER NOT NULL DEFAULT 1,
    audience      TEXT NOT NULL DEFAULT 'tenant',
    content       TEXT NOT NULL,
    tone_hint     TEXT,
    review_reason TEXT,
    source_facts  TEXT,
    status        TEXT NOT NULL DEFAULT 'pending_review',
    created_at    TEXT NOT NULL
)
"""


def _ensure_v7_opaque_draft_request_reference(conn: Connection) -> None:
    """Decouple Communication drafts from the legacy maintenance table.

    ``request_id`` is an identifier crossing a module boundary, not a foreign
    key into another module's private storage. The real Maintenance service
    persists requests in ``mc_request``; retaining the old FK made a successful
    intake fail when Communication tried to persist its grounded reply draft.

    A fresh database has no draft table at migration time (schema.sql runs
    afterwards), so there is nothing to rebuild. Existing databases are
    rebuilt transactionally with foreign-key enforcement temporarily disabled;
    dependent review rows continue to reference the replacement ``draft``
    table after it is renamed.
    """
    table = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='draft'"
    ).fetchone()
    if table is None:
        return

    # Already upgraded (useful when a database was stamped inconsistently).
    request_fk = next(
        (
            row
            for row in conn.execute("PRAGMA foreign_key_list(draft)")
            if row[3] == "request_id"
        ),
        None,
    )
    if request_fk is None:
        return

    conn.commit()
    foreign_keys_were_enabled = bool(
        conn.execute("PRAGMA foreign_keys").fetchone()[0]
    )
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_DRAFT_V7_DDL)
        conn.execute(
            "INSERT INTO draft_v7 (draft_id,message_id,request_id,source_kind,"
            "source_id,version,audience,content,tone_hint,review_reason,"
            "source_facts,status,created_at) "
            "SELECT draft_id,message_id,request_id,source_kind,source_id,"
            "version,audience,content,tone_hint,review_reason,source_facts,"
            "status,created_at FROM draft"
        )
        conn.execute("DROP TABLE draft")
        conn.execute("ALTER TABLE draft_v7 RENAME TO draft")
        conn.execute("CREATE INDEX idx_draft_message ON draft(message_id)")
        conn.execute(
            "CREATE UNIQUE INDEX idx_draft_source ON draft(source_id) "
            "WHERE source_id IS NOT NULL"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if foreign_keys_were_enabled:
            conn.execute("PRAGMA foreign_keys = ON")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            "draft request-reference migration left foreign-key violations: "
            f"{violations}"
        )


_COMMUNICATION_MAINTENANCE_RECEIPT_DDL = """
CREATE TABLE IF NOT EXISTS communication_maintenance_receipt (
    event_id       TEXT NOT NULL,
    recipient_id   TEXT NOT NULL,
    message_type   TEXT NOT NULL,
    source_id      TEXT NOT NULL UNIQUE,
    request_id     TEXT NOT NULL,
    tenant_id      TEXT NOT NULL REFERENCES tenant(tenant_id),
    property_id    TEXT NOT NULL REFERENCES property(property_id),
    payload        TEXT NOT NULL,
    draft_id       TEXT NOT NULL REFERENCES draft(draft_id),
    received_at    TEXT NOT NULL,
    PRIMARY KEY (event_id, recipient_id, message_type)
);
CREATE INDEX IF NOT EXISTS idx_communication_maintenance_request
    ON communication_maintenance_receipt(property_id,request_id,received_at);
"""


def _ensure_v8_communication_maintenance_receipts(conn: Connection) -> None:
    """Add durable, replay-safe Maintenance -> Communication receipts."""
    conn.executescript(_COMMUNICATION_MAINTENANCE_RECEIPT_DDL)


def _ensure_v9_manager_inbox(conn: Connection) -> None:
    """Add the role-specific durable manager-alert sink."""
    conn.executescript(_MANAGER_INBOX_DDL)


def _ensure_v10_manager_evidence(conn: Connection) -> None:
    """Preserve existing evidence and child records while allowing manager ownership."""
    columns = conn.execute("PRAGMA table_info(evidence_document)").fetchall()
    if not columns or any(row[1] == "uploaded_by_manager_id" for row in columns):
        return
    # Rebuild without renaming the old table: child foreign keys retain their target.
    conn.commit()
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='evidence_document'"
        ).fetchone()[0]
        import re
        ddl = re.sub(r"(?i)CREATE TABLE(?: IF NOT EXISTS)?\s+evidence_document",
                     "CREATE TABLE evidence_document_v10", ddl, count=1)
        ddl = re.sub(r"(?i)(tenant_id\s+TEXT)\s+NOT NULL", r"\1", ddl)
        ddl = ddl.rstrip().removesuffix(")") + (
            ", uploaded_by_manager_id TEXT REFERENCES manager(manager_id))"
        )
        indexes = [row[0] for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='evidence_document' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL"
        )]
        conn.execute(ddl)
        names = "rowid, " + ", ".join('"' + row[1] + '"' for row in columns)
        conn.execute(f"INSERT INTO evidence_document_v10 ({names}) "
                     f"SELECT {names} FROM evidence_document")
        conn.execute("DROP TABLE evidence_document")
        conn.execute("ALTER TABLE evidence_document_v10 RENAME TO evidence_document")
        for statement in indexes:
            conn.execute(statement)
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Evidence ownership upgrade failed foreign-key validation")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys={foreign_keys}")


def _ensure_v11_handoffs_and_reminder_basis(conn: Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS reminder_basis ("
                 "draft_id TEXT PRIMARY KEY REFERENCES draft(draft_id) ON DELETE CASCADE,basis TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS communication_handoff (
        case_id TEXT PRIMARY KEY,
        message_id TEXT NOT NULL UNIQUE REFERENCES message(message_id),
        property_id TEXT NOT NULL REFERENCES property(property_id),
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved','dismissed')),
        created_at TEXT NOT NULL,
        resolver_id TEXT REFERENCES manager(manager_id),
        resolution TEXT,
        resolved_at TEXT,
        CHECK ((status='open' AND resolver_id IS NULL AND resolution IS NULL AND resolved_at IS NULL)
            OR (status!='open' AND resolver_id IS NOT NULL AND resolution IS NOT NULL AND resolved_at IS NOT NULL))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_handoff_property "
                 "ON communication_handoff(property_id,status,created_at)")
    # Recover previously audited handoffs without inventing a classification.
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {'message', 'audit'} <= tables:
        conn.execute("""INSERT OR IGNORE INTO communication_handoff
            (case_id,message_id,property_id,reason,created_at)
            SELECT lower(hex(randomblob(16))),m.message_id,m.property_id,'legacy_handoff',MIN(a.at)
            FROM message m JOIN audit a ON a.subject_id=m.message_id
            WHERE a.action IN ('message.classified_invalid','communication.handoff')
            GROUP BY m.message_id,m.property_id""")


def _ensure_v12_evidence_record_links(conn: Connection) -> None:
    """Allow several documents per related record; preserve historical links."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(evidence_association)")}
    if not columns:
        return
    conn.execute("SAVEPOINT evidence_links_v12")
    try:
        if "associated_by" not in columns:
            conn.execute("ALTER TABLE evidence_association ADD COLUMN associated_by TEXT REFERENCES manager(manager_id)")
        conn.execute("DROP INDEX IF EXISTS idx_evidence_association_target")
        conn.execute("CREATE UNIQUE INDEX idx_evidence_association_target "
                     "ON evidence_association(document_id,target_type,target_id)")
        conn.execute("RELEASE evidence_links_v12")
    except BaseException:
        conn.execute("ROLLBACK TO evidence_links_v12")
        conn.execute("RELEASE evidence_links_v12")
        raise



# Ordered, 1-based. ``user_version`` is the highest step already applied.
_MIGRATIONS: dict[int, Callable[[Connection], None]] = {
    2: _ensure_manager_tables,
    3: _ensure_v3_lease_and_reports,
    4: _ensure_v4_integrations,
    5: _ensure_v5_reconciliation,
    6: _ensure_v6_tenant_inbox,
    7: _ensure_v7_opaque_draft_request_reference,
    8: _ensure_v8_communication_maintenance_receipts,
    9: _ensure_v9_manager_inbox,
    10: _ensure_v10_manager_evidence,
    11: _ensure_v11_handoffs_and_reminder_basis,
    12: _ensure_v12_evidence_record_links,
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


def init_connection(
    db_path: str | Path = ":memory:", *, check_same_thread: bool = True
) -> Connection:
    """Open a connection with schema applied and foreign keys enabled."""
    conn = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_schema(conn)
    return conn
