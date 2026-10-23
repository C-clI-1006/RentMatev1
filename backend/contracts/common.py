"""Stable identifiers shared across modules.

Conventions:
- All IDs are opaque strings (typically UUIDs generated once at the
  owning module's entry point).
- `EventId` is the stable dedup key for cross-module events and
  notifications. The same logical submission must always carry the
  same `event_id`; duplicates resolve to the existing outcome instead
  of creating new records (guide 18.2).
- All timestamps in this codebase are UTC (ISO-8601 when serialised).
"""

from typing import NewType

TenantId = NewType("TenantId", str)
PropertyId = NewType("PropertyId", str)
MessageId = NewType("MessageId", str)
RequestId = NewType("RequestId", str)
EventId = NewType("EventId", str)
LeaseId = NewType("LeaseId", str)
ManagerId = NewType("ManagerId", str)
RentRecordId = NewType("RentRecordId", str)
DraftId = NewType("DraftId", str)
ReviewId = NewType("ReviewId", str)
AuditId = NewType("AuditId", str)
RecipientId = NewType("RecipientId", str)
