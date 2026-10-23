"""Permissions tests: manager property-access checks (default deny).

Identity model under test:
* ``AuthContext.manager_id`` is the only manager-identity source
  (server-side resolved at the entry point). ``manager_id_from_auth``
  raises when it is absent/empty — there is no session-auth layer yet,
  so the entry-point boundary is the trust boundary, not a user header.
* Property scope is verified against ``manager_property`` bindings;
  any missing binding (including an unknown manager, which by the
  foreign key can have no binding row) is a denial.

Run: pytest backend/tests/ -k test_permissions
"""

import sqlite3
from pathlib import Path

import pytest

from contracts.common import ManagerId, PropertyId
from permissions import (
    ManagerAccessDeniedError,
    MissingManagerIdentityError,
    SQLiteManagerRepository,
    list_manager_properties,
    manager_id_from_auth,
    require_manager_property_access,
)
from shared import AuthContext
from shared.sqlite import SCHEMA_VERSION, init_connection


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
TS = "2026-01-01T00:00:00Z"


@pytest.fixture()
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.executescript(
        f"""
        INSERT INTO tenant (tenant_id, name, created_at)
        VALUES ('tenant-1', 'Tenant', '{TS}');
        INSERT INTO property (property_id, name, created_at)
        VALUES ('property-1', 'Home A', '{TS}'),
               ('property-2', 'Home B', '{TS}'),
               ('property-3', 'Home C', '{TS}');
        INSERT INTO tenant_property (tenant_id, property_id, created_at)
        VALUES ('tenant-1', 'property-1', '{TS}');
        INSERT INTO manager (manager_id, name, created_at)
        VALUES ('manager-1', 'Alice', '{TS}'),
               ('manager-2', 'Bob', '{TS}');
        INSERT INTO manager_property (manager_id, property_id, created_at)
        VALUES ('manager-1', 'property-1', '{TS}'),
               ('manager-1', 'property-2', '{TS}'),
               ('manager-2', 'property-3', '{TS}');
        """
    )
    yield connection
    connection.close()


def _auth(manager_id: str | None) -> AuthContext:
    """Entry-point stand-in: identity resolved server-side, trusted here."""
    return AuthContext(
        tenant_id="tenant-1", property_id="property-1", manager_id=manager_id
    )


# --- identity resolution (entry-point boundary) ----------------------------


def test_manager_id_resolved_only_from_auth_context(db):
    assert manager_id_from_auth(_auth("manager-1")) == ManagerId("manager-1")

    with pytest.raises(MissingManagerIdentityError):
        manager_id_from_auth(_auth(None))
    with pytest.raises(MissingManagerIdentityError):
        manager_id_from_auth(_auth(""))


# --- list_manager_properties -----------------------------------------------


def test_list_properties_for_manager_with_bindings(db):
    records = list_manager_properties(
        ManagerId("manager-1"), SQLiteManagerRepository(db)
    )
    assert [r.property_id for r in records] == ["property-1", "property-2"]
    assert all(r.manager_id == "manager-1" for r in records)


def test_list_properties_default_deny_without_binding(db):
    # A real manager with zero bindings: empty, not an error.
    db.execute("INSERT INTO manager VALUES ('manager-3', 'Cara', ?)", (TS,))
    records = list_manager_properties(
        ManagerId("manager-3"), SQLiteManagerRepository(db)
    )
    assert records == ()


def test_list_properties_unknown_manager_is_empty(db):
    records = list_manager_properties(
        ManagerId("no-such-manager"), SQLiteManagerRepository(db)
    )
    assert records == ()


# --- require_manager_property_access ----------------------------------------


def test_require_access_legitimate_binding_passes(db):
    manager = require_manager_property_access(
        ManagerId("manager-1"),
        PropertyId("property-2"),
        SQLiteManagerRepository(db),
    )
    assert manager == ManagerId("manager-1")


def test_require_access_denied_when_no_binding(db):
    with pytest.raises(ManagerAccessDeniedError) as excinfo:
        require_manager_property_access(
            ManagerId("manager-1"),
            PropertyId("property-3"),
            SQLiteManagerRepository(db),
        )
    assert excinfo.value.manager_id == "manager-1"
    assert excinfo.value.property_id == "property-3"


def test_require_access_denied_across_managers(db):
    # property-3 belongs to manager-2 only; manager-1 must be denied.
    with pytest.raises(ManagerAccessDeniedError):
        require_manager_property_access(
            ManagerId("manager-1"),
            PropertyId("property-3"),
            SQLiteManagerRepository(db),
        )
    # ... while manager-2 is allowed.
    assert (
        require_manager_property_access(
            ManagerId("manager-2"),
            PropertyId("property-3"),
            SQLiteManagerRepository(db),
        )
        == ManagerId("manager-2")
    )


def test_require_access_multi_property_manager(db):
    repo = SQLiteManagerRepository(db)
    for property_id in ("property-1", "property-2"):
        assert (
            require_manager_property_access(
                ManagerId("manager-1"), PropertyId(property_id), repo
            )
            == ManagerId("manager-1")
        )
    with pytest.raises(ManagerAccessDeniedError):
        require_manager_property_access(
            ManagerId("manager-1"), PropertyId("property-3"), repo
        )


def test_require_access_unknown_manager_denied(db):
    # Unknown manager cannot have a binding row (foreign key), so denial.
    with pytest.raises(ManagerAccessDeniedError):
        require_manager_property_access(
            ManagerId("no-such-manager"),
            PropertyId("property-1"),
            SQLiteManagerRepository(db),
        )


# --- existing tenant flow is unaffected -------------------------------------


def test_existing_tenant_flow_uses_binding_check_not_manager(db):
    # The tenant path keeps working off tenant_property, independent of the
    # new manager tables; adding managers must not disturb it.
    from lease_rent.repository import SQLiteLeaseRentRepository

    assert SQLiteLeaseRentRepository(db).is_authorized(
        tenant_id="tenant-1", property_id="property-1"
    )
    assert not SQLiteLeaseRentRepository(db).is_authorized(
        tenant_id="tenant-1", property_id="property-2"
    )


# --- migration for pre-existing databases ------------------------------------


def test_init_connection_upgrades_old_schema_to_manager_tables(tmp_path):
    # Simulate a v1 database: original tables, no manager tables,
    # user_version = 1. init_connection must add the manager tables.
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.row_factory = sqlite3.Row
    legacy.executescript(
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
        PRAGMA user_version = 1;
        """
    )
    legacy.commit()
    legacy.close()

    conn = init_connection(db_path)
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"manager", "manager_property"} <= tables
    assert (
        conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    )

    # Idempotent: a second connect re-applies everything without error.
    conn.close()
    conn = init_connection(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()
