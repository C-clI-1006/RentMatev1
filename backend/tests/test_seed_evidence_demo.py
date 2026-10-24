"""Temporary-DB seed regressions: separate tenant and manager bindings.

Existing fields and conflicting bindings must never be overwritten.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# The script is deliberately outside the backend package import roots
# (explicit run-only tooling); load it from its file path.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "seed_evidence_demo.py"
_SPEC = importlib.util.spec_from_file_location("seed_evidence_demo", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
seed = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(seed)

# The same schema file the script applies; its idempotent DDL is used here
# only to create the tables for pre-existing rows.
_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"

TENANT_ID = "T-DEMO-01"
PROPERTY_ID = "P-DEMO-01"
MANAGER_ID = "M-DEMO-01"
TABLES = ("tenant", "property", "tenant_property", "manager", "manager_property")
# The seed must never create any of these.
EVIDENCE_TABLES = (
    "evidence_document",
    "evidence_classification",
    "evidence_association",
    "evidence_review",
)

PAST_UTC = "2026-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """A throwaway database file (never the repo's rentmate.db)."""
    return tmp_path / "seed_demo_test.db"


def _apply_schema(db_path: Path) -> None:
    """Apply the shared idempotent schema so pre-existing rows can exist."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    finally:
        conn.close()


def _insert_tenant_property(
    db_path: Path,
    tenant_id: str,
    property_id: str,
    now: str,
    role: str | None = None,
) -> None:
    """Insert pre-existing tenant/property rows (optionally plus a binding)
    with custom field values the seed must never overwrite."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            conn.execute(
                "INSERT INTO tenant (tenant_id, name, contact, created_at) "
                "VALUES (?, ?, ?, ?)",
                (tenant_id, f"pre-existing tenant {tenant_id}", f"contact {tenant_id}", now),
            )
            conn.execute(
                "INSERT INTO property (property_id, name, address, created_at) "
                "VALUES (?, ?, ?, ?)",
                (property_id, f"pre-existing property {property_id}", f"addr {property_id}", now),
            )
            if role is not None:
                conn.execute(
                    "INSERT INTO tenant_property (tenant_id, property_id, role, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (tenant_id, property_id, role, now),
                )
    finally:
        conn.close()


def _rows(db_path: Path, table: str) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()


def _count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _snapshot(db_path: Path) -> dict[str, list[tuple]]:
    return {t: _rows(db_path, t) for t in TABLES}


def _assert_utc(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def _assert_demo_binding(db_path: Path) -> tuple:
    """The demo binding must exist with role='resident'; return its row."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT tenant_id, property_id, role, created_at "
            "FROM tenant_property WHERE tenant_id = ? AND property_id = ?",
            (TENANT_ID, PROPERTY_ID),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "expected the demo binding to exist"
    assert row[2] == "resident"
    managers = _rows(db_path, "manager")
    assert len(managers) == 1 and managers[0][0] == MANAGER_ID
    _assert_utc(managers[0][2])
    bindings = _rows(db_path, "manager_property")
    assert len(bindings) == 1 and bindings[0][:2] == (MANAGER_ID, PROPERTY_ID)
    _assert_utc(bindings[0][2])
    return row


def _assert_no_evidence_rows(db_path: Path) -> None:
    for table in EVIDENCE_TABLES:
        assert _count(db_path, table) == 0, f"seed must not create rows in {table}"


# ---------------------------------------------------------------------------
# first run: correct creation
# ---------------------------------------------------------------------------


def test_first_run_creates_demo_rows(db_path: Path) -> None:
    _apply_schema(db_path)

    assert seed.main(["--db", str(db_path)]) == 0

    assert _count(db_path, "tenant") == 1
    assert _count(db_path, "property") == 1
    assert _count(db_path, "tenant_property") == 1

    (tenant,) = _rows(db_path, "tenant")
    assert tenant[0] == TENANT_ID
    assert tenant[1] and "TEST-ONLY" in tenant[1]
    assert tenant[3] is not None
    _assert_utc(tenant[3])

    (prop,) = _rows(db_path, "property")
    assert prop[0] == PROPERTY_ID
    assert prop[1] and "TEST-ONLY" in prop[1]
    assert prop[3] is not None
    _assert_utc(prop[3])

    binding = _assert_demo_binding(db_path)
    _assert_utc(binding[3])

    _assert_no_evidence_rows(db_path)


def test_first_run_bootstraps_missing_schema(db_path: Path) -> None:
    """A database file with NO schema yet must be bootstrapped by the
    script itself (it applies the idempotent DDL before seeding)."""
    assert not db_path.exists()

    assert seed.main(["--db", str(db_path)]) == 0

    assert _count(db_path, "tenant") == 1
    assert _count(db_path, "property") == 1
    assert _count(db_path, "tenant_property") == 1
    _assert_demo_binding(db_path)
    _assert_no_evidence_rows(db_path)


# ---------------------------------------------------------------------------
# rerun: idempotent, row counts and fields unchanged
# ---------------------------------------------------------------------------


def test_second_run_is_noop_and_rows_unchanged(db_path: Path) -> None:
    _apply_schema(db_path)
    assert seed.main(["--db", str(db_path)]) == 0

    before = _snapshot(db_path)
    _assert_no_evidence_rows(db_path)

    assert seed.main(["--db", str(db_path)]) == 0

    after = _snapshot(db_path)
    assert after == before, "rerun must not change any row or its fields"
    assert _count(db_path, "tenant") == 1
    assert _count(db_path, "property") == 1
    assert _count(db_path, "tenant_property") == 1
    _assert_no_evidence_rows(db_path)


# ---------------------------------------------------------------------------
# pre-existing binding with role='resident': fail, no changes, no escalation
# ---------------------------------------------------------------------------


def test_conflicting_manager_role_fails_and_leaves_data_untouched(db_path: Path) -> None:
    _apply_schema(db_path)
    # The demo tenant/property/binding already exist, but the binding is
    # NOT a manager's: the seed must refuse to touch it.
    _insert_tenant_property(db_path, TENANT_ID, PROPERTY_ID, PAST_UTC, role="manager")
    before = _snapshot(db_path)

    assert seed.main(["--db", str(db_path)]) == 1

    after = _snapshot(db_path)
    assert after == before, "failed run must leave every row unchanged (rolled back)"
    (binding,) = _rows(db_path, "tenant_property")
    assert binding[2] == "manager", "existing role must never be escalated to manager"
    assert binding[3] == PAST_UTC


def test_resident_binding_for_other_pair_does_not_block_seed(db_path: Path) -> None:
    """A resident binding for a DIFFERENT tenant/property pair must not
    block the demo seed, and that pair must be left exactly as it was."""
    _apply_schema(db_path)
    _insert_tenant_property(db_path, "T-RES-1", "P-RES-1", PAST_UTC, role="resident")
    (resident_row,) = _rows(db_path, "tenant_property")

    assert seed.main(["--db", str(db_path)]) == 0

    assert _count(db_path, "tenant") == 2
    assert _count(db_path, "property") == 2
    assert _count(db_path, "tenant_property") == 2
    assert resident_row in _rows(db_path, "tenant_property"), (
        "the pre-existing resident pair must be untouched"
    )
    _assert_demo_binding(db_path)
    _assert_no_evidence_rows(db_path)


# ---------------------------------------------------------------------------
# pre-existing tenant/property rows: custom fields are never overwritten
# ---------------------------------------------------------------------------


def test_existing_demo_rows_keep_custom_fields_on_create_run(db_path: Path) -> None:
    """The tenant and property already exist (custom fields, no binding):
    the seed creates ONLY the binding and must not overwrite the rows."""
    _apply_schema(db_path)
    _insert_tenant_property(db_path, TENANT_ID, PROPERTY_ID, PAST_UTC)
    tenant_before = _rows(db_path, "tenant")
    property_before = _rows(db_path, "property")

    assert seed.main(["--db", str(db_path)]) == 0

    assert _rows(db_path, "tenant") == tenant_before
    assert _rows(db_path, "property") == property_before
    (tenant,) = tenant_before
    assert tenant[1] == f"pre-existing tenant {TENANT_ID}"
    assert tenant[2] == f"contact {TENANT_ID}"
    assert tenant[3] == PAST_UTC
    (prop,) = property_before
    assert prop[1] == f"pre-existing property {PROPERTY_ID}"
    assert prop[2] == f"addr {PROPERTY_ID}"
    assert prop[3] == PAST_UTC

    binding = _assert_demo_binding(db_path)
    _assert_utc(binding[3])
    assert _count(db_path, "tenant_property") == 1
    _assert_no_evidence_rows(db_path)


def test_existing_manager_binding_reruns_keep_custom_fields(db_path: Path) -> None:
    """The full demo triple already exists with role='resident': the seed
    must not duplicate or overwrite anything, across repeated runs."""
    _apply_schema(db_path)
    _insert_tenant_property(db_path, TENANT_ID, PROPERTY_ID, PAST_UTC, role="resident")
    assert seed.main(["--db", str(db_path)]) == 0
    before = _snapshot(db_path)

    assert seed.main(["--db", str(db_path)]) == 0
    assert seed.main(["--db", str(db_path)]) == 0

    after = _snapshot(db_path)
    assert after == before, "custom fields must survive repeated runs"
    assert _count(db_path, "tenant") == 1
    assert _count(db_path, "property") == 1
    assert _count(db_path, "tenant_property") == 1
    assert _assert_demo_binding(db_path)[3] == PAST_UTC
    _assert_no_evidence_rows(db_path)


def test_existing_independent_manager_keeps_custom_fields(db_path: Path) -> None:
    _apply_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO manager VALUES (?, ?, ?)", (MANAGER_ID, "Custom manager", PAST_UTC))
    assert seed.main(["--db", str(db_path)]) == 0
    before = _snapshot(db_path)
    assert seed.main(["--db", str(db_path)]) == 0
    assert _snapshot(db_path) == before
    assert _rows(db_path, "manager") == [(MANAGER_ID, "Custom manager", PAST_UTC)]
    assert _count(db_path, "tenant") == 1
    _assert_demo_binding(db_path)
    _assert_no_evidence_rows(db_path)
