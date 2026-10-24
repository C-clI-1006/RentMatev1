"""Evidence's trusted Maintenance callback and reference validation.

The caller supplies a server-resolved AuthContext, never event.actor.
The payload is the existing MaintenanceEvent plus the existing outbox envelope.
No HTTP event ingestion endpoint or background sender is introduced.
"""
import json
import sqlite3
from datetime import timezone

from pydantic import ValidationError

from contracts.maintenance import MaintenanceEvent, MaintenanceStatus
from maintenance.outbox import PermanentHandoffError, RetryableHandoffError
from permissions import (
    MissingManagerIdentityError, SQLiteManagerRepository,
    manager_id_from_auth, require_manager_property_access,
)
from shared import utcnow


class EvidenceReferenceError(ValueError):
    code = "evidence_not_found"


def require_manager(db, property_id, auth):
    if auth.property_id != property_id:
        raise PermissionError("Property scope mismatch")
    try:
        manager = manager_id_from_auth(auth)
    except MissingManagerIdentityError:
        raise PermissionError("Manager identity required") from None
    require_manager_property_access(manager, property_id, SQLiteManagerRepository(db))


def validate_references(db, evidence_refs, property_id, auth):
    """Check stored document IDs on the caller's connection; never read files.

    Managers may reference any saved document in their assigned property.
    Tenants may reference only their own saved documents, with a resident binding.
    Missing and cross-property IDs have the same public rejection code.
    """
    if property_id != auth.property_id:
        raise PermissionError("Property scope mismatch")
    if auth.manager_id:
        require_manager(db, property_id, auth)
    elif db.execute(
        "SELECT 1 FROM tenant_property WHERE tenant_id=? AND property_id=?",
        (auth.tenant_id, property_id),
    ).fetchone() is None:
        raise PermissionError("Tenant binding required")
    if not isinstance(evidence_refs, list):
        raise EvidenceReferenceError()
    for document_id in evidence_refs:
        if not isinstance(document_id, str) or not document_id.strip():
            raise EvidenceReferenceError()
        row = db.execute(
            "SELECT tenant_id FROM evidence_document "
            "WHERE document_id=? AND property_id=? AND upload_status='saved'",
            (document_id, property_id),
        ).fetchone()
        if row is None or (not auth.manager_id and row[0] != auth.tenant_id):
            raise EvidenceReferenceError()
    return {"status": "valid", "property_id": property_id,
            "document_ids": list(dict.fromkeys(evidence_refs))}


def canonical_event(payload):
    if isinstance(payload, MaintenanceEvent):
        payload = payload.model_dump(mode="json")
    if not isinstance(payload, dict):
        raise PermanentHandoffError("invalid_event")
    envelope = {"recipient_id": str, "message_type": str, "review_required": bool}
    if set(payload) - set(MaintenanceEvent.model_fields) - set(envelope):
        raise PermanentHandoffError("invalid_event")
    try:
        event = MaintenanceEvent.model_validate(payload)
    except ValidationError:
        raise PermanentHandoffError("invalid_event") from None
    if (any(not value.strip() for value in (event.event_id, event.request_id, event.property_id, event.actor))
            or event.occurred_at.utcoffset() is None):
        raise PermanentHandoffError("invalid_event")
    event = event.model_copy(update={"occurred_at": event.occurred_at.astimezone(timezone.utc)})
    normalized = event.model_dump(mode="json")
    for key, expected in envelope.items():
        if key in payload:
            if type(payload[key]) is not expected:
                raise PermanentHandoffError("invalid_event")
            normalized[key] = payload[key]
    return event, json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class MaintenanceEvidenceReceiver:
    def __init__(self, repository):
        self.repository = repository

    def validate(self, evidence_refs, property_id, auth):
        with self.repository.transaction() as db:
            return validate_references(db, evidence_refs, property_id, auth)

    def receive(self, payload, auth):
        """Return True only after commit; duplicates acknowledge the original.

        PermanentHandoffError -> rejected; confirmed rollback on SQLite busy
        -> RetryableHandoffError. All other errors propagate as uncertain.
        A timeout after commit must be reconciled through receipt(), not retried
        blindly. Receipt and timeline are one atomic write, including all refs.
        """
        event, canonical = canonical_event(payload)
        try:
            with self.repository.transaction() as db:
                require_manager(db, event.property_id, auth)
                existing = db.execute(
                    "SELECT payload FROM evidence_event_receipt WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()
                if existing is not None:
                    if existing[0] != canonical:
                        raise PermanentHandoffError("event_id_conflict")
                    return True
                if event.to_status in (MaintenanceStatus.AWAITING_CONFIRMATION, MaintenanceStatus.CLOSED) and not event.evidence_refs:
                    raise PermanentHandoffError("evidence_required")
                validate_references(db, event.evidence_refs, event.property_id, auth)
                received_at = utcnow().isoformat()
                db.execute(
                    "INSERT INTO evidence_event_receipt "
                    "(event_id,property_id,payload,received_at,received_by) VALUES (?,?,?,?,?)",
                    (event.event_id, event.property_id, canonical, received_at, auth.manager_id),
                )
                db.execute(
                    "INSERT INTO evidence_maintenance_timeline "
                    "(event_id,request_id,property_id,occurred_at) VALUES (?,?,?,?)",
                    (event.event_id, event.request_id, event.property_id, event.occurred_at.isoformat()),
                )
                for document_id in dict.fromkeys(event.evidence_refs):
                    db.execute(
                        "INSERT INTO evidence_event_document (event_id,document_id) VALUES (?,?)",
                        (event.event_id, document_id),
                    )
        except PermissionError:
            raise PermanentHandoffError("forbidden") from None
        except EvidenceReferenceError:
            raise PermanentHandoffError("evidence_not_found") from None
        except sqlite3.OperationalError as exc:
            # transaction() has completed its rollback before this handler.
            # Do not claim definite non-acceptance for unknown IO/commit errors.
            if getattr(exc, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise RetryableHandoffError("evidence_busy") from None
            raise
        return True

    def receipt(self, event_id, property_id, auth):
        with self.repository.transaction() as db:
            require_manager(db, property_id, auth)
            row = db.execute(
                "SELECT received_at,received_by,payload FROM evidence_event_receipt "
                "WHERE event_id=? AND property_id=?", (event_id, property_id),
            ).fetchone()
            if row is None:
                return {"status": "not_received", "event_id": event_id, "property_id": property_id}
            return {"status": "accepted", "event_id": event_id, "property_id": property_id,
                    "received_at": row[0], "received_by": row[1], "payload": json.loads(row[2])}

    def timeline(self, request_id, property_id, auth):
        with self.repository.transaction() as db:
            require_manager(db, property_id, auth)
            rows = db.execute(
                "SELECT r.payload,r.received_at,r.received_by "
                "FROM evidence_maintenance_timeline t JOIN evidence_event_receipt r USING(event_id) "
                "WHERE t.request_id=? AND t.property_id=? ORDER BY t.occurred_at,t.event_id",
                (request_id, property_id),
            ).fetchall()
            return [{"event": json.loads(r[0]), "received_at": r[1], "received_by": r[2]} for r in rows]
