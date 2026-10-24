"""Manager property-access checks (owned: permissions).

Security model
--------------
* A manager identity is resolved ONLY from the server-side ``AuthContext``
  (``shared.AuthContext``) at the entry point — never from a request body
  or header. Entry points call :func:`manager_id_from_auth` to extract it
  (raises when absent) and hand that value to the checks below.
* Access is DEFAULT DENY: a manager may only act on properties bound in
  ``manager_property``. No binding — including an unknown manager id that
  can have no binding row (foreign key) — is a denial.
* Read-only: these functions never mutate any state; they only check.

There is no session-auth layer yet; ``AuthContext`` is populated by the
entry point. These functions trust that boundary and nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence

from contracts.common import ManagerId, PropertyId
from shared import AuthContext

from .repository import ManagerPropertyRecord, SQLiteManagerRepository


class MissingManagerIdentityError(RuntimeError):
    """``AuthContext`` carries no manager identity.

    Raised instead of silently falling back to a caller-supplied id: a
    manager-facing call that cannot be tied to a server-side-resolved
    manager is a denial, not an anonymous success.
    """


class ManagerAccessDeniedError(PermissionError):
    """The verified manager has no binding for the requested property."""

    def __init__(self, manager_id: ManagerId, property_id: PropertyId) -> None:
        self.manager_id = manager_id
        self.property_id = property_id
        super().__init__(
            f"manager {manager_id} has no verified binding for property {property_id} "
            "(default deny)"
        )


def manager_id_from_auth(auth: AuthContext) -> ManagerId:
    """The only sanctioned way to obtain a manager identity for checks.

    Entry points resolve identity from the server-side ``AuthContext``
    before calling the permissions service. Raises
    ``MissingManagerIdentityError`` when the context has no ``manager_id``
    (or it is empty), so an unresolvable identity can never slip through
    as an anonymous success.
    """
    manager_id = auth.manager_id
    if manager_id is None or manager_id == "":
        raise MissingManagerIdentityError(
            "AuthContext carries no manager identity; resolve it server-side "
            "before calling the permissions service"
        )
    return ManagerId(manager_id)


def list_manager_properties(
    manager_id: ManagerId, repository: SQLiteManagerRepository
) -> Sequence[ManagerPropertyRecord]:
    """All verified property bindings for ``manager_id``.

    Returns an empty sequence when the manager has no bindings (default
    deny). The caller is responsible for having resolved ``manager_id``
    from the server-side ``AuthContext`` (see :func:`manager_id_from_auth`).
    """
    return repository.list_property_ids(manager_id)


def require_manager_property_access(
    manager_id: ManagerId,
    property_id: PropertyId,
    repository: SQLiteManagerRepository,
) -> ManagerId:
    """Assert ``manager_id`` is bound to ``property_id``.

    Returns the verified ``ManagerId`` on success; raises
    ``ManagerAccessDeniedError`` on any denial (no binding, binding for
    another manager, unknown manager).
    """
    if not repository.is_manager_of_property(manager_id, property_id):
        raise ManagerAccessDeniedError(manager_id, property_id)
    return manager_id
