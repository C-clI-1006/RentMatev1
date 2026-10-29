"""Regression tests: upgrading a pre-existing (legacy) database.

The legacy database simulated here was created BEFORE ``due_date`` was
added to ``rent_record`` (it has the manager tables, i.e. ``user_version = 2``)
and BEFORE the evidence tables existed. These tests verify the upgrade is:

* explicit and idempotent (migrations run on every connect, in order);
* a no-op for existing rows (the new column is added as NULL — never a
  guessed date, and no stored rent status is recomputed);
* complete enough for ``init_connection`` to create the indexes the
  current schema depends on (``idx_rent_due`` on ``rent_record(due_date)``);
* usable by ``EvidenceRepository``, which keeps its own cross-thread
  connection (``check_same_thread=False``) and lock while reusing the
  shared migration logic.

All databases are temporary; the real ``rentmate.db`` is never touched.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from evidence.repository import EvidenceRepository
from shared.sqlite import SCHEMA_PATH, SCHEMA_VERSION, init_connection

TENANT = "tenant-1"
PROP = "property-1"
MANAGER = "manager-1"
NOW = "2026-10-23T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Legacy database construction (temporary, explicit)
# ---------------------------------------------------------------------------


def _create_legacy_rent_record(conn: sqlite3.Connection) -> None:
    """Create the rent_record table exactly as an OLD database has it:
    the current schema minus the ``due_date`` column (and therefore the
    ``idx_rent_due`` index)."""
    conn.executescript(
        """
        CREATE TABLE lease (
            lease_id     TEXT PRIMARY KEY,
            property_id  TEXT NOT NULL REFERENCES property(property_id),
            tenant_id    TEXT NOT NULL REFERENCES tenant(tenant_id),
            start_date   TEXT NOT NULL,
            end_date     TEXT,
            document_ref TEXT,
            version      TEXT,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE rent_record (
            rent_record_id TEXT PRIMARY KEY,
            lease_id       TEXT NOT NULL REFERENCES lease(lease_id),
            property_id    TEXT NOT NULL REFERENCES property(property_id),
            period         TEXT NOT NULL,
            amount_due     REAL CHECK (amount_due IS NULL OR amount_due >= 0),
            amount_paid    REAL CHECK (amount_paid IS NULL OR amount_paid >= 0),
            paid_at        TEXT,
            status         TEXT NOT NULL DEFAULT 'unpaid-but-not-due'
                CHECK (status IN ('paid', 'unpaid-but-not-due', 'overdue')),
            created_at     TEXT NOT NULL
        );
        """
    )


def _build_legacy_db(db_path: Path) -> None:
    """Build a temporary v2 (pre-due_date) database with rows that must
    survive the upgrade byte-for-byte."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE tenant (
                tenant_id   TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                contact     TEXT,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE property (
                property_id TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                address     TEXT,
                created_at  TEXT NOT NULL
            );
            """
        )
        # Manager tables: the legacy DB is at user_version = 2, so the v2
        # step has already been applied to it.
        conn.executescript(
            """
            CREATE TABLE manager (
                manager_id  TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE manager_property (
                manager_id  TEXT NOT NULL REFERENCES manager(manager_id),
                property_id TEXT NOT NULL REFERENCES property(property_id),
                created_at  TEXT NOT NULL,
                PRIMARY KEY (manager_id, property_id)
            );
            CREATE INDEX idx_manager_property_property
                ON manager_property (property_id);
            """
        )
        _create_legacy_rent_record(conn)
        conn.executemany(
            "INSERT INTO tenant (tenant_id, name, contact, created_at) "
            "VALUES (?, ?, ?, ?)",
            [(TENANT, "Tenant One", "c1", NOW)],
        )
        conn.executemany(
            "INSERT INTO property (property_id, name, address, created_at) "
            "VALUES (?, ?, ?, ?)",
            [(PROP, "Property One", "addr1", NOW)],
        )
        conn.executemany(
            "INSERT INTO manager (manager_id, name, created_at) VALUES (?, ?, ?)",
            [(MANAGER, "Manager One", NOW)],
        )
        conn.executemany(
            "INSERT INTO manager_property (manager_id, property_id, created_at) "
            "VALUES (?, ?, ?)",
            [(MANAGER, PROP, NOW)],
        )
        conn.execute(
            "INSERT INTO lease (lease_id, property_id, tenant_id, start_date, "
            "end_date, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("lease-1", PROP, TENANT, "2026-09-01", None, NOW),
        )
        # Pre-existing rent rows: one paid, one overdue — their stored
        # `status` values are historical data and MUST NOT be recomputed.
        conn.execute(
            "INSERT INTO rent_record (rent_record_id, lease_id, property_id, "
            "period, amount_due, amount_paid, paid_at, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("rent-1", "lease-1", PROP, "2026-09", 1000.0, 1000.0, NOW, "paid", NOW),
        )
        conn.execute(
            "INSERT INTO rent_record (rent_record_id, lease_id, property_id, "
            "period, amount_due, amount_paid, paid_at, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("rent-2", "lease-1", PROP, "2026-10", 1000.0, 0.0, None, "overdue", NOW),
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()


def _rent_rows(conn: sqlite3.Connection) -> dict[str, dict]:
    """Snapshot every rent_record row as {rent_record_id: {col: value}}."""
    rows = conn.execute(
        "SELECT rent_record_id, lease_id, property_id, period, amount_due, "
        "amount_paid, paid_at, status, created_at FROM rent_record"
    ).fetchall()
    return {
        r[0]: {
            "rent_record_id": r[0],
            "lease_id": r[1],
            "property_id": r[2],
            "period": r[3],
            "amount_due": r[4],
            "amount_paid": r[5],
            "paid_at": r[6],
            "status": r[7],
            "created_at": r[8],
        }
        for r in rows
    }


# ---------------------------------------------------------------------------
# init_connection upgrades a legacy database
# ---------------------------------------------------------------------------


def test_init_connection_upgrades_legacy_db_to_due_date(tmp_path):
    db_path = tmp_path / "legacy.db"
    _build_legacy_db(db_path)

    raw_before = _connect_raw(db_path)
    try:
        before = _rent_rows(raw_before)
    finally:
        raw_before.close()
    assert "rent-1" in before and "rent-2" in before

    # Document the root cause: the OLD code path (schema script with no
    # prior migration) still fails on this legacy db, because
    # ``CREATE INDEX idx_rent_due`` references due_date. Run it on a COPY
    # so the real legacy db is not mutated before the upgrade under test.
    import shutil

    from shared.sqlite import SCHEMA_PATH

    copy_path = tmp_path / "legacy_copy.db"
    shutil.copyfile(db_path, copy_path)
    with pytest.raises(sqlite3.OperationalError, match="due_date"):
        raw = sqlite3.connect(str(copy_path))
        try:
            raw.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        finally:
            raw.close()

    # The fixed path (migrations BEFORE the script) succeeds.
    conn = init_connection(db_path)

    # 1. The index the schema depends on now exists.
    indexes = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_rent_due" in indexes

    # 2. Original rows are unchanged; the new column is NULL (unknown stays
    #    missing — no guessed date), and stored status is never recomputed.
    after = _rent_rows(conn)
    assert after == before
    due = {
        r[0]: r[1]
        for r in conn.execute("SELECT rent_record_id, due_date FROM rent_record")
    }
    assert due == {"rent-1": None, "rent-2": None}
    assert (
        conn.execute(
            "SELECT status FROM rent_record WHERE rent_record_id = 'rent-1'"
        ).fetchone()[0]
        == "paid"
    )

    # 3. The manager and due-date migrations remain ordered, followed by
    #    the Evidence event receipt/timeline tables in v4.
    assert SCHEMA_VERSION == 10
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    # 4. The migration chain that predated this step is untouched.
    assert {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    } >= {"manager", "manager_property", "evidence_document", "evidence_review"}
    assert conn.execute("SELECT count(*) FROM manager_property").fetchone()[0] == 1

    # 5. Connection contract: row_factory and foreign keys.
    assert conn.row_factory is sqlite3.Row
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_upgrade_is_idempotent(tmp_path):
    db_path = tmp_path / "legacy.db"
    _build_legacy_db(db_path)

    conn = init_connection(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()

    # Second connect: migrations are no-ops and the script re-applies
    # without error; the database is left exactly as the first connect left it.
    conn = init_connection(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    indexes = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_rent_due" in indexes
    # A second ALTER TABLE ADD COLUMN would raise "duplicate column name" —
    # reaching here proves the v3 step is idempotent.
    assert (
        conn.execute(
            "SELECT count(*) FROM rent_record WHERE due_date IS NULL"
        ).fetchone()[0]
        == 2
    )
    conn.close()


def test_fresh_db_reaches_same_version(tmp_path):
    # A brand-new database climbs the whole chain in one connect.
    conn = init_connection(tmp_path / "fresh.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    indexes = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_rent_due" in indexes
    conn.close()


def test_v1_db_climbs_through_v2_and_v3(tmp_path):
    # A database predating BOTH the manager tables and due_date (v1) must
    # climb the full chain in order.
    db_path = tmp_path / "v1.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE tenant (
                tenant_id   TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                contact     TEXT,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE property (
                property_id TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                address     TEXT,
                created_at  TEXT NOT NULL
            );
            """
        )
        _create_legacy_rent_record(conn)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()

    conn = init_connection(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"manager", "manager_property"} <= tables
    assert "idx_rent_due" in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    conn.close()


def _connect_raw(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# EvidenceRepository reuses the shared upgrade on its own connection
# ---------------------------------------------------------------------------


def _legacy_db_for(tmp_path: Path) -> Path:
    db_path = tmp_path / "evidence_legacy.db"
    _build_legacy_db(db_path)
    return db_path


def test_evidence_repository_upgrades_legacy_db_and_keeps_threading(tmp_path):
    db_path = _legacy_db_for(tmp_path)

    # Before the fix this raised inside _ensure_schema: the raw script ran
    # before the due_date column existed.
    repo = EvidenceRepository(str(db_path), str(tmp_path / "storage"))
    try:
        # The repository's connection is its own: cross-thread safe.
        assert isinstance(repo._lock, type(threading.RLock()))
        assert repo._conn is not None
        # Check that the connection still allows cross-thread use
        # (check_same_thread=False). A default connection would raise
        # ProgrammingError here.
        errors = []

        def _touch():
            try:
                repo.get_document("missing-doc", TENANT, PROP)
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=_touch)
        t.start()
        t.join()
        assert errors == []
        # The repository's own connection now has the upgraded schema.
        idx = repo._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_rent_due'"
        ).fetchone()
        assert idx is not None

        # The shared upgrade ran on this connection: version chain advanced
        # and the pre-existing migration (manager tables) was applied.
        assert repo._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert repo._conn.execute("SELECT count(*) FROM manager").fetchone()[0] == 1

        # Existing rent rows survived the upgrade untouched.
        rows = repo._conn.execute(
            "SELECT rent_record_id, status, due_date FROM rent_record "
            "ORDER BY rent_record_id"
        ).fetchall()
        assert [(r[0], r[1], r[2]) for r in rows] == [
            ("rent-1", "paid", None),
            ("rent-2", "overdue", None),
        ]

        # Row factory is still set (this module's convention).
        assert repo._conn.row_factory is sqlite3.Row
    finally:
        repo.close()

    # A second EvidenceRepository on the same db: idempotent re-init.
    repo2 = EvidenceRepository(str(db_path), str(tmp_path / "storage"))
    try:
        assert (
            repo2._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        )
    finally:
        repo2.close()


@pytest.mark.parametrize("version", [0, 1, 2])
def test_legacy_seed_uses_shared_migrations(tmp_path, version):
    from scripts.seed_evidence_demo import main as seed_main

    db_path = tmp_path / "legacy_seed.db"
    _build_legacy_db(db_path)
    with sqlite3.connect(db_path) as raw:
        before = _rent_rows(raw)
        raw.execute(f"PRAGMA user_version = {version}")
    assert seed_main(["--db", str(db_path)]) == 0
    assert seed_main(["--db", str(db_path)]) == 0
    conn = init_connection(db_path)
    try:
        assert _rent_rows(conn) == before
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            conn.execute(
                "SELECT count(*) FROM rent_record WHERE due_date IS NULL"
            ).fetchone()[0]
            == 2
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_existing_due_dates_survive_pending_version_upgrade(tmp_path):
    db_path = tmp_path / "existing_due_date.db"
    _build_legacy_db(db_path)
    with sqlite3.connect(db_path) as raw:
        raw.execute("ALTER TABLE rent_record ADD COLUMN due_date TEXT")
        raw.execute(
            "UPDATE rent_record SET due_date = '2026-10-01' WHERE rent_record_id = 'rent-2'"
        )
    conn = init_connection(db_path)
    try:
        assert (
            conn.execute(
                "SELECT due_date FROM rent_record WHERE rent_record_id = 'rent-2'"
            ).fetchone()[0]
            == "2026-10-01"
        )
    finally:
        conn.close()


def test_evidence_repository_on_fresh_db(tmp_path):
    # Fresh (empty) database: the repository applies the full schema.
    db_path = str(tmp_path / "fresh.db")
    repo = EvidenceRepository(db_path, str(tmp_path / "storage"))
    try:
        assert repo._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            repo._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_rent_due'"
            ).fetchone()
            is not None
        )
    finally:
        repo.close()


@pytest.mark.parametrize("branch", ["main_v3", "evidence_v3", "evidence_v4"])
def test_divergent_migration_histories_converge_without_data_loss(tmp_path, branch):
    """Version 3 meant reports on main, but due_date on the Evidence branch."""
    import re
    from shared.sqlite import SCHEMA_PATH

    db_path = tmp_path / "divergent.db"
    _build_legacy_db(db_path)
    if branch == "main_v3":
        conn = sqlite3.connect(db_path)
        # Reproduce main's report table without running the integrated migration.
        report_ddl = re.search(
            r"CREATE TABLE IF NOT EXISTS report \([\s\S]*?;",
            SCHEMA_PATH.read_text(encoding="utf-8"),
        )[0]
        conn.executescript(report_ddl)
        conn.execute(
            "INSERT INTO report VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "report-kept",
                PROP,
                2026,
                10,
                1,
                "scheduled",
                "trigger-kept",
                None,
                "complete",
                "{}",
                "{}",
                NOW,
                NOW,
                "system:scheduler",
            ),
        )
        original_report = conn.execute("SELECT * FROM report").fetchone()
        conn.execute("PRAGMA user_version=3")
    else:
        conn = init_connection(db_path)
        conn.execute("DROP TABLE report")
        if branch == "evidence_v3":
            for table in (
                "evidence_event_document",
                "evidence_maintenance_timeline",
                "evidence_event_receipt",
            ):
                conn.execute(f"DROP TABLE {table}")
            conn.execute("PRAGMA user_version=3")
        else:
            conn.execute(
                "INSERT INTO evidence_document (document_id,tenant_id,property_id,original_filename,file_type,size_bytes,storage_ref,upload_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "doc-kept",
                    TENANT,
                    PROP,
                    "kept.txt",
                    "txt",
                    1,
                    "evidence/kept.txt",
                    "saved",
                    NOW,
                    NOW,
                ),
            )
            conn.execute(
                "INSERT INTO evidence_event_receipt VALUES (?,?,?,?,?)",
                ("event-kept", PROP, "{}", NOW, MANAGER),
            )
            conn.execute(
                "INSERT INTO evidence_maintenance_timeline VALUES (?,?,?,?)",
                ("event-kept", "request-kept", PROP, NOW),
            )
            conn.execute(
                "INSERT INTO evidence_event_document VALUES (?,?)",
                ("event-kept", "doc-kept"),
            )
            conn.execute("PRAGMA user_version=4")
    before = _rent_rows(conn)
    conn.commit()
    conn.close()
    for _ in range(2):
        upgraded = init_connection(db_path)
        try:
            assert (
                upgraded.execute("PRAGMA user_version").fetchone()[0]
                == SCHEMA_VERSION
                == 10
            )
            assert _rent_rows(upgraded) == before
            assert (
                upgraded.execute(
                    "SELECT count(*) FROM rent_record WHERE due_date IS NULL"
                ).fetchone()[0]
                == 2
            )
            assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
            assert (
                upgraded.execute("SELECT count(*) FROM manager_property").fetchone()[0]
                == 1
            )
            if branch == "main_v3":
                assert (
                    tuple(upgraded.execute("SELECT * FROM report").fetchone())
                    == original_report
                )
            else:
                assert (
                    upgraded.execute("SELECT count(*) FROM report").fetchone()[0] == 0
                )
            assert upgraded.execute(
                "SELECT count(*) FROM evidence_event_receipt"
            ).fetchone()[0] == (1 if branch == "evidence_v4" else 0)
            if branch == "evidence_v4":
                assert tuple(
                    upgraded.execute("SELECT * FROM evidence_event_document").fetchone()
                ) == ("event-kept", "doc-kept")
        finally:
            upgraded.close()


def test_application_factory_mounts_both_reporting_and_evidence(tmp_path):
    from main import create_app
    from llm.classify import StubDocumentClassifier

    app = create_app(
        str(tmp_path / "routes.db"),
        str(tmp_path / "uploads"),
        classifier=StubDocumentClassifier(),
    )
    try:
        paths = app.openapi()["paths"]
        assert set(paths) >= {
            "/health",
            "/api/maintenance/requests",
            "/api/reviews",
            "/api/ops/rent-alerts/run",
            "/api/ops/monthly-reports/run",
            "/api/manager/monthly-reports",
            "/api/manager/monthly-reports/generate",
            "/api/evidence/documents",
            "/api/evidence/reviews",
            "/api/evidence/maintenance-events/{event_id}",
        }
    finally:
        app.state.evidence_service._repo.close()


def test_v7_decouples_draft_from_legacy_maintenance_storage(tmp_path):
    """A real mc_request ID must remain usable as draft provenance.

    Communication and Maintenance integrate through the contract. They must
    not also require both modules to write the same private persistence table.
    Existing draft/review rows from the legacy FK schema must survive upgrade.
    """
    db_path = tmp_path / "legacy-draft-fk.db"
    current_schema = SCHEMA_PATH.read_text(encoding="utf-8")
    marker = "    request_id    TEXT,\n    -- Origin provenance"
    assert marker in current_schema
    legacy_schema = current_schema.replace(
        marker,
        "    request_id    TEXT REFERENCES maintenance_request(request_id),\n"
        "    -- Origin provenance",
        1,
    )

    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(legacy_schema)
        raw.executescript(
            """
            CREATE TABLE mc_request (
                id TEXT PRIMARY KEY,
                property_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                intake_event TEXT NOT NULL,
                version INTEGER NOT NULL,
                document TEXT NOT NULL,
                original_result TEXT NOT NULL
            );
            """
        )
        raw.execute(
            "INSERT INTO tenant (tenant_id,name,created_at) VALUES (?,?,?)",
            (TENANT, "Tenant", NOW),
        )
        raw.execute(
            "INSERT INTO property (property_id,name,created_at) VALUES (?,?,?)",
            (PROP, "Property", NOW),
        )
        raw.executemany(
            "INSERT INTO message (message_id,tenant_id,property_id,content,"
            "message_type,sentiment,received_at) VALUES (?,?,?,?,?,?,?)",
            [
                ("message-old", TENANT, PROP, "old", "maintenance", "none", NOW),
                ("message-current", TENANT, PROP, "new", "maintenance", "none", NOW),
            ],
        )
        raw.execute(
            "INSERT INTO maintenance_request (request_id,tenant_id,property_id,"
            "message_id,event_id,title,description,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-request", TENANT, PROP, "message-old", "event-old",
                "Old", "Old request", "submitted", NOW, NOW,
            ),
        )
        raw.execute(
            "INSERT INTO draft (draft_id,message_id,request_id,content,status,"
            "created_at) VALUES (?,?,?,?,?,?)",
            (
                "draft-old", "message-old", "legacy-request", "Old reply",
                "approved", NOW,
            ),
        )
        raw.execute(
            "INSERT INTO review (review_id,draft_id,draft_version,decision,"
            "manager_id,final_content,decided_at) VALUES (?,?,?,?,?,?,?)",
            (
                "review-old", "draft-old", 1, "approved", MANAGER,
                "Old reply", NOW,
            ),
        )
        raw.execute(
            "INSERT INTO mc_request VALUES (?,?,?,?,?,?,?,?)",
            (
                "current-request", PROP, TENANT, "message-current",
                "event-current", 1, "{}", "{}",
            ),
        )
        raw.commit()

        raw.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            raw.execute(
                "INSERT INTO draft (draft_id,message_id,request_id,content,status,"
                "created_at) VALUES (?,?,?,?,?,?)",
                (
                    "draft-blocked", "message-current", "current-request",
                    "Current reply", "pending_review", NOW,
                ),
            )
        raw.rollback()
        raw.execute("PRAGMA user_version = 6")
        raw.commit()
    finally:
        raw.close()

    upgraded = init_connection(db_path)
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 10
        assert not any(
            row[3] == "request_id"
            for row in upgraded.execute("PRAGMA foreign_key_list(draft)")
        )
        assert upgraded.execute(
            "SELECT request_id FROM draft WHERE draft_id='draft-old'"
        ).fetchone()[0] == "legacy-request"
        assert upgraded.execute(
            "SELECT draft_id FROM review WHERE review_id='review-old'"
        ).fetchone()[0] == "draft-old"

        upgraded.execute(
            "INSERT INTO draft (draft_id,message_id,request_id,content,status,"
            "created_at) VALUES (?,?,?,?,?,?)",
            (
                "draft-current", "message-current", "current-request",
                "Current reply", "pending_review", NOW,
            ),
        )
        upgraded.commit()
        assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        upgraded.close()
