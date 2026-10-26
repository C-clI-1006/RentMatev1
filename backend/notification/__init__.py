"""Durable notification outbox and its single retry-owning dispatcher."""

from .dispatcher import (
    DeliveryAdapter,
    PermanentDeliveryError,
    RetryableDeliveryError,
    SQLiteTenantInboxAdapter,
    dispatch_one,
    dispatch_pending,
)
from .service import (
    NotificationConflictError,
    NotificationNotFoundError,
    SQLiteNotificationService,
)

__all__ = [
    "DeliveryAdapter",
    "PermanentDeliveryError",
    "RetryableDeliveryError",
    "SQLiteTenantInboxAdapter",
    "dispatch_one",
    "dispatch_pending",
    "NotificationConflictError",
    "NotificationNotFoundError",
    "SQLiteNotificationService",
]
