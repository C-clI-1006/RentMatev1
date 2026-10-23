"""Permissions module (owner: Chaney) — manager property-access checks.

Manager identity comes from the server-side ``AuthContext`` only (see
:func:`manager_id_from_auth`); property scope comes from verified
``manager_property`` bindings with DEFAULT DENY semantics.
"""

from .repository import ManagerPropertyRecord, SQLiteManagerRepository
from .service import (
    ManagerAccessDeniedError,
    MissingManagerIdentityError,
    list_manager_properties,
    manager_id_from_auth,
    require_manager_property_access,
)

__all__ = [
    "ManagerAccessDeniedError",
    "ManagerPropertyRecord",
    "MissingManagerIdentityError",
    "SQLiteManagerRepository",
    "list_manager_properties",
    "manager_id_from_auth",
    "require_manager_property_access",
]
