"""Manager-scoped UC6 record linking and a projection of persisted evidence history.

Source domains are read-only. A manual link does not transition Maintenance,
alter a lease, or fabricate a received MaintenanceEvent.
"""
from datetime import datetime, timezone
import json

from contracts.evidence import DocumentCategory
from shared import new_id, utcnow
from .maintenance import require_manager, validate_references


class EvidenceLinkError(ValueError):
    def __init__(self, code, status=404):
        super().__init__(code)
        self.code = code
        self.status = status


def _has_requests(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mc_request'").fetchone() is not None


def _document(db, document_id, property_id):
    row = db.execute("SELECT * FROM evidence_document WHERE document_id=? AND property_id=? AND upload_status='saved'",
                     (document_id, property_id)).fetchone()
    if row is None:
        raise EvidenceLinkError("document_not_found")
    return row


def _links(db, document_id, property_id):
    rows = db.execute("""SELECT a.* FROM evidence_association a JOIN evidence_document d USING(document_id)
        WHERE a.document_id=? AND d.property_id=? AND a.status='associated'
        ORDER BY a.associated_at,a.association_id""", (document_id, property_id)).fetchall()
    result = [{"association_id": r["association_id"], "target_type": r["target_type"],
               "target_id": r["target_id"], "associated_at": r["associated_at"],
               "associated_by": r["associated_by"], "source": "manual"} for r in rows]
    seen = {(r["target_type"], r["target_id"]) for r in result}
    for r in db.execute("""SELECT t.request_id,MIN(r.received_at) AS received_at FROM evidence_event_document ed
        JOIN evidence_maintenance_timeline t USING(event_id) JOIN evidence_event_receipt r USING(event_id)
        WHERE ed.document_id=? AND t.property_id=? GROUP BY t.request_id ORDER BY t.request_id""",
                        (document_id, property_id)):
        if ("maintenance_request", r["request_id"]) not in seen:
            result.append({"association_id": None, "target_type": "maintenance_request", "target_id": r["request_id"],
                           "associated_at": r["received_at"], "associated_by": None, "source": "maintenance_event"})
    return result


class PropertyEvidence:
    def __init__(self, repository):
        self.repository = repository

    def targets(self, property_id, auth):
        with self.repository.transaction() as db:
            require_manager(db, property_id, auth)
            result = [{"target_type": "lease", "target_id": r["lease_id"],
                       "label": "Lease " + r["lease_id"] + " (" + r["start_date"] + ")"}
                      for r in db.execute("SELECT lease_id,start_date FROM lease WHERE property_id=? ORDER BY lease_id", (property_id,))]
            if _has_requests(db):
                for r in db.execute("SELECT id,document FROM mc_request WHERE property_id=? ORDER BY id", (property_id,)):
                    record = json.loads(r["document"])
                    result.append({"target_type": "maintenance_request", "target_id": r["id"],
                                   "label": "Maintenance: " + record.get("title", r["id"])})
            return result

    def links(self, document_id, property_id, auth):
        with self.repository.transaction() as db:
            require_manager(db, property_id, auth)
            _document(db, document_id, property_id)
            return _links(db, document_id, property_id)

    def link(self, document_id, property_id, target_type, target_id, auth):
        with self.repository.transaction() as db:
            require_manager(db, property_id, auth)
            doc = _document(db, document_id, property_id)
            validate_references(db, [document_id], property_id, auth)
            if target_type == "lease":
                target = db.execute("SELECT 1 FROM lease WHERE lease_id=? AND property_id=?", (target_id, property_id)).fetchone()
            elif target_type == "maintenance_request" and _has_requests(db):
                target = db.execute("SELECT 1 FROM mc_request WHERE id=? AND property_id=?", (target_id, property_id)).fetchone()
            else:
                target = None
            if target is None:
                raise EvidenceLinkError("related_record_not_found")
            cls = db.execute("SELECT status,classification_type,details FROM evidence_classification WHERE document_id=?", (document_id,)).fetchone()
            review = db.execute("SELECT final_category FROM evidence_review WHERE document_id=? ORDER BY reviewed_at DESC,rowid DESC LIMIT 1", (document_id,)).fetchone()
            try:
                flags = json.loads(cls["details"]) if cls else {}
                accepted = (cls is not None and cls["status"] == "classified" and isinstance(flags, dict)
                            and flags.get("review_required") is False
                            and cls["classification_type"] in {c.value for c in DocumentCategory if c.value != "unknown"})
            except (ValueError, TypeError):
                accepted = False
            if not review and not accepted:
                raise EvidenceLinkError("classification_review_required", 409)
            existing = next((r for r in _links(db, doc["document_id"], property_id)
                             if r["target_type"] == target_type and r["target_id"] == target_id), None)
            if existing:
                return {"status": "associated", "created": False, "document_id": document_id, **existing}
            stamp = utcnow().isoformat()
            association_id = new_id()
            db.execute("""INSERT INTO evidence_association
                (association_id,document_id,target_type,target_id,status,created_at,associated_at,associated_by)
                VALUES (?,?,?,?,'associated',?,?,?)""",
                       (association_id, document_id, target_type, target_id, stamp, stamp, auth.manager_id))
            return {"status": "associated", "created": True, "document_id": document_id,
                    "association_id": association_id, "target_type": target_type, "target_id": target_id,
                    "associated_at": stamp, "associated_by": auth.manager_id, "source": "manual"}

    def timeline(self, property_id, auth, document_id=None):
        """One snapshot; original timestamps and stable IDs, never synthetic backfill."""
        with self.repository.transaction() as db:
            require_manager(db, property_id, auth)
            if document_id is not None:
                _document(db, document_id, property_id)
            docs = db.execute("SELECT * FROM evidence_document WHERE property_id=? AND (? IS NULL OR document_id=?)",
                              (property_id, document_id, document_id)).fetchall()
            entries = []
            for doc in docs:
                did = doc["document_id"]

                def add(key, kind, at, actor=None, **details):
                    if at:
                        entries.append({"entry_id": key, "kind": kind, "occurred_at": at, "actor": actor,
                                        "document_id": did, "filename": doc["original_filename"],
                                        "property_id": property_id, **details})

                actor = ("manager:" + doc["uploaded_by_manager_id"] if doc["uploaded_by_manager_id"] else
                         "tenant:" + doc["tenant_id"] if doc["tenant_id"] else None)
                add("upload:" + did, "uploaded", doc["created_at"], actor)
                cls = db.execute("SELECT * FROM evidence_classification WHERE document_id=?", (did,)).fetchone()
                if cls and cls["status"] in ("classified", "failed"):
                    add("classification:" + did, "classification", cls["classified_at"], "system:classifier",
                        status=cls["status"], category=cls["classification_type"], source=cls["source"])
                for r in db.execute("SELECT * FROM evidence_review WHERE document_id=?", (did,)):
                    add("review:" + r["review_id"], "reviewed", r["reviewed_at"], "manager:" + r["reviewed_by"],
                        category=r["final_category"], action=r["action"])
                for r in db.execute("SELECT * FROM evidence_association WHERE document_id=? AND status='associated'", (did,)):
                    add("link:" + r["association_id"], "linked", r["associated_at"],
                        "manager:" + r["associated_by"] if r["associated_by"] else None,
                        target_type=r["target_type"], target_id=r["target_id"])
                for r in db.execute("""SELECT t.*,r.payload,r.received_at,r.received_by FROM evidence_event_document ed
                    JOIN evidence_maintenance_timeline t USING(event_id) JOIN evidence_event_receipt r USING(event_id)
                    WHERE ed.document_id=? AND t.property_id=?""", (did, property_id)):
                    event = json.loads(r["payload"])
                    add("maintenance:" + r["event_id"] + ":" + did, "maintenance_event", r["occurred_at"], event["actor"],
                        target_type="maintenance_request", target_id=r["request_id"], status=event["to_status"],
                        received_at=r["received_at"], received_by=r["received_by"], event_id=r["event_id"])
            def order(entry):
                stamp = datetime.fromisoformat(entry["occurred_at"].replace("Z", "+00:00"))
                return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
            return sorted(entries, key=lambda e: (order(e), e["entry_id"]))
