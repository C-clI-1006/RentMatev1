# Notification delivery

The notification component owns the durable `notification_event` outbox and
is the single retry owner for recipient-facing delivery.

- Manager approval and queue insertion commit atomically. The event payload
  contains only the exact approved draft version and its property scope.
- `accepted` means queued, never sent.
- Tenant approved-draft notifications recheck the immutable approval and the
  current tenant/property binding immediately before delivery.
- Manager alerts use their own minimal payload validation and recheck the
  current manager/property binding; they never require tenant draft fields.
- Temporary adapter failures become `pending_retry`; permanent failures become
  `failed`; successful delivery becomes `delivered`.
- `(event_id, recipient_id)` is the stable deduplication key.
- The executable prototype uses `SQLiteNotificationInboxAdapter` to route
  tenant notifications to `tenant_inbox` and manager alerts to
  `manager_inbox`. Both role-specific sinks deduplicate on the stable delivery
  key in the same transaction as the delivered status.

Operations:

- `POST /api/ops/notifications/dispatch` requires the trusted scheduler Bearer
  token and drains accepted/retryable events.
- `GET /api/notifications/inbox` requires a tenant Bearer token and returns
  only that tenant's current property-scoped messages.

The local inbox is a demonstrable transport boundary, not a claim that an
email/SMS provider is configured. A real provider implements the same
idempotent `DeliveryAdapter` contract.
