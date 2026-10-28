"""Cross-cutting helpers: auth context, ID generation, UTC time.

- AuthContext: carries tenantId/propertyId from login/authorisation.
  Business code must take identity from here, never from user input.
- new_id(): UUID4 string for stable IDs (message, request, event, ...).
- utcnow(): timezone-aware UTC timestamp helper.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from contracts.common import PropertyId, TenantId


def utcnow() -> datetime:
    """Timezone-aware UTC now. All persisted timestamps use this."""
    return datetime.now(timezone.utc)


def new_id() -> str:
    """A fresh stable UUID4 string for IDs generated at the owning module."""
    return str(uuid.uuid4())


@dataclass(frozen=True)
class AuthContext:
    """Identity + scope resolved at the entry point from the session.

    Business code reads `tenant_id` / `property_id` ONLY from here. The
    tenant<->property binding is verified at the entry point (see
    repositories binding checks); these values are never taken from
    user input.
    """

    tenant_id: TenantId | None  # manager-only sessions do not impersonate a tenant
    property_id: PropertyId
    manager_id: str | None = None  # set for manager-facing entry points
