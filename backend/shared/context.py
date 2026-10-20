"""Auth context, ID generation, UTC time.

These helpers were promised in the shared module docstring and are
implemented here so entry points can take identity from the auth
context (never from user input) and stamp UTC timestamps.

Ownership note: `shared/` is shared infrastructure; this file is the
first concrete implementation. Flag for team review.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class AuthContext:
    """Identity carried from login/authorisation into a business call.

    Business code must take tenant_id / property_id from here, never
    from user input. The tenant<->property binding itself must already
    be verified by the entry point before use (see the communication
    service, which checks the `tenant_property` table).
    """

    tenant_id: str
    property_id: str
    role: str = "resident"  # resident | manager


def new_id() -> str:
    """UUID4 string for stable IDs (message, request, event, ...).

    Generated once at the owning module's entry point (guide: IDs are
    opaque strings created where the record is created).
    """
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Timezone-aware UTC now. All timestamps in this codebase are UTC."""
    return datetime.now(timezone.utc)
