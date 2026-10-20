"""Cross-cutting helpers: auth context, ID generation, UTC time.

- AuthContext: carries tenantId/propertyId from login/authorisation.
  Business code must take identity from here, never from user input.
- new_id(): UUID4 string for stable IDs (message, request, event, ...).
- utcnow(): timezone-aware UTC timestamp helper.
"""

from .context import AuthContext, new_id, utcnow

__all__ = ["AuthContext", "new_id", "utcnow"]
