"""Audit trail contract.

Every cross-module state change that affects a tenant-facing outcome
(intake registered/failed, review decision, delivery status change,
emergency alert) must emit an AuditEvent. Audit records are append-only
and owned by the `audit` table via repositories.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .common import AuditId, EventId, RequestId


class AuditEvent(BaseModel):
    audit_id: AuditId
    actor: str  # "tenant:<id>" | "manager:<id>" | "system:<component>"
    action: str  # e.g. "intake.registered", "review.rejected", "delivery.failed"
    subject_type: str  # e.g. "maintenance_request" | "draft" | "notification"
    subject_id: str
    request_id: RequestId | None = None
    event_id: EventId | None = None
    details: dict = Field(default_factory=dict)
    at: datetime  # UTC
