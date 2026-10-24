"""Notification / delivery contract (guide 18.2).

Rules (binding):
- Three+ distinct outcomes: ACCEPTED (queued/acknowledged),
  PENDING_REVIEW, DELIVERED, FAILED, PENDING_RETRY.
  `ACCEPTED` must NEVER be treated by upstream modules as "sent".
- Deduplication key: (`event_id`, `recipient_id`). Duplicate publishes
  resolve to the existing outcome — they must not create a second
  user-facing message.
- Single retry owner: the dispatcher/queue executes retries. Producers
  (communication, maintenance) must not retry on their own, to avoid
  double sends.
- PROPOSAL: tenant sends use either the manager-approved version or an
  exact routine reply version eligible under deterministic policy. Recheck
  permissions and eligibility before dispatch; never fabricate approval.
  Manager alerts do not require tenant-reply approval. Minimise payloads.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from .common import EventId, MessageId, RecipientId, RequestId


class NotificationKind(str, Enum):
    MAINTENANCE_REGISTERED = "maintenance_registered"
    MAINTENANCE_STATUS = "maintenance_status"
    MAINTENANCE_REJECTED = "maintenance_rejected"
    LEASE_RENT_REPLY = "lease_rent_reply"
    MANAGER_ALERT = "manager_alert"  # e.g. emergency, pre-approval
    OTHER = "other"


class NotificationStatus(str, Enum):
    ACCEPTED = "accepted"  # queued only — NOT sent
    PENDING_REVIEW = "pending_review"
    DELIVERED = "delivered"
    FAILED = "failed"
    PENDING_RETRY = "pending_retry"


class NotificationEvent(BaseModel):
    event_id: EventId  # stable dedup key
    recipient_id: RecipientId
    kind: NotificationKind
    request_id: RequestId | None = None
    message_id: MessageId | None = None
    payload: dict = Field(default_factory=dict)  # minimal, authorised payload; see version rules above
    created_at: datetime  # UTC


class DeliveryResult(BaseModel):
    event_id: EventId
    status: NotificationStatus
    detail: str | None = None  # human-readable reason for failed/pending_retry
    attempts: int = 0
    checked_at: datetime  # UTC


class INotificationService(ABC):
    """Public interface for the notification component.

    The dispatcher (queue worker) is the single retry owner.
    """

    @abstractmethod
    def publish(self, event: NotificationEvent) -> DeliveryResult:
        """Queue a notification. Duplicates return the existing outcome."""

    @abstractmethod
    def status(self, event_id: EventId, recipient_id: RecipientId) -> DeliveryResult:
        """Current truthful delivery status for a dedup key."""
