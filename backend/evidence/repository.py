"""Evidence persistence (owned: evidence, per db/schema.sql).

SQLite via stdlib `sqlite3` (no ORM) — same rules as the shared
repositories layer: this repository owns ONLY the `evidence_*` tables
and exposes only data the evidence module owns.

Scope enforcement lives here as a WHERE-clause (tenant_id + property_id)
so a document can never be read back from another property, even if an
upper layer forgets to check. The router/service layer additionally
enforces the manager-access rule on every entry point.

`PRAGMA foreign_keys = ON` on every connection (schema convention).
The whole db/schema.sql (single source of truth) is applied once at
first use so the prototype can start from an empty database file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from contracts.evidence import EvidenceDocument, EvidenceDocumentRef
from shared import new_id, utcnow

_SCHEMA_FILE = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


class EvidenceReviewRejectedError(ValueError):
    """A review save was REJECTED (fail-closed): the document is in scope
    but its classification record is missing, or its `details` JSON cannot
    be safely re-written (not a parseable JSON object). Raising inside the
    save transaction rolls everything back — nothing is written."""


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _review_flags_from_details(
    details_json: str | None,
) -> dict[str, bool | str | None]:
    """Map the persisted classification `details` JSON onto the three
    review fields: `review_required` / `review_code` / `review_reason`
    (returned as a dict so the model can be built with `**` unpacking).

    Strictly typed and fail-soft: `details` may be empty, corrupted
    JSON, or a non-object; in every such case — and whenever one of the
    three fields is absent or of the wrong type — the corresponding
    model field is `None`. No exception is raised and `None` is never
    coerced to `False`: an unrecorded review flag must stay explicit
    as "not recorded", not "not required".
    """
    flags: dict[str, bool | str | None] = {
        "review_required": None,
        "review_code": None,
        "review_reason": None,
    }
    try:
        data = json.loads(details_json)
    except (TypeError, json.JSONDecodeError):
        return flags
    if not isinstance(data, dict):
        return flags
    # `bool` is an `int` subclass in Python: check `bool` explicitly so
    # a numeric 0/1 is not accepted as a boolean flag.
    if isinstance(data.get("review_required"), bool):
        flags["review_required"] = data["review_required"]
    if isinstance(data.get("code"), str):
        flags["review_code"] = data["code"]
    if isinstance(data.get("reason"), str):
        flags["review_reason"] = data["reason"]
    return flags


class EvidenceRepository:
    """Owns the evidence_document / evidence_classification /
    evidence_association tables (this increment uses the first two)."""

    def __init__(self, db_path: str, storage_root: str) -> None:
        self._db_path = db_path
        self._storage_root = storage_root
        # FastAPI sync endpoints run in the threadpool, so the single
        # connection is used from several threads. check_same_thread=False
        # plus the lock below makes that safe; the lock also serialises
        # the connection (one in-flight transaction at a time), which is
        # correct for this single-writer prototype. RLock (not Lock) so
        # read_original can call get_document while already holding it.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Apply the shared schema (idempotent: CREATE ... IF NOT EXISTS)."""
        self._conn.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- writes ------------------------------------------------------------

    def save_document(self, doc: EvidenceDocument) -> None:
        """Persist one uploaded document (upload_status='saved') plus its
        classification row in 'pending'. Never called for rejections."""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO evidence_document (
                        document_id, tenant_id, property_id, original_filename,
                        file_type, size_bytes, storage_ref, upload_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'saved', ?, ?)
                    """,
                    (
                        doc.document_id,
                        doc.tenant_id,
                        doc.property_id,
                        doc.original_filename,
                        doc.file_type,
                        doc.size_bytes,
                        doc.storage_ref,
                        doc.uploaded_at.isoformat(),
                        doc.updated_at.isoformat(),
                    ),
                )
                # Classification machine starts PENDING (separate state
                # machine). The classifier runs right after this insert
                # (UC6) and advances it via advance_classification.
                self._conn.execute(
                    """
                    INSERT INTO evidence_classification (
                        document_id, status, source, details, created_at
                    ) VALUES (?, 'pending', 'none', '{}', ?)
                    """,
                    (doc.document_id, doc.uploaded_at.isoformat()),
                )

    def advance_classification(
        self,
        document_id: str,
        tenant_id: str,
        property_id: str,
        status: str,
        *,
        classification_type: str | None = None,
        source: str = "none",
        confidence: float | None = None,
        details: str = "{}",
        classified_at: str | None = None,
    ) -> bool:
        """Persist the outcome of the classification machine for ONE
        document (classified | failed; never a guessed category).

        Scoped: only touches the row whose document belongs to
        (tenant_id, property_id) — `evidence_classification` itself has
        no tenant/property columns, so the scope check runs through the
        parent `evidence_document` row (same WHERE-clause rule as the
        reads). Returns False when the document is not in scope.
        """
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    """
                    UPDATE evidence_classification
                    SET status = ?,
                        classification_type = ?,
                        source = ?,
                        confidence = ?,
                        details = ?,
                        classified_at = ?
                    WHERE document_id = ?
                      AND document_id IN (
                          SELECT document_id FROM evidence_document
                          WHERE tenant_id = ? AND property_id = ?
                      )
                    """,
                    (
                        status,
                        classification_type,
                        source,
                        confidence,
                        details,
                        classified_at,
                        document_id,
                        tenant_id,
                        property_id,
                    ),
                )
                return cur.rowcount == 1

    def save_review(
        self,
        document_id: str,
        tenant_id: str,
        property_id: str,
        action: str,
        final_category: str,
        reviewed_by: str,
    ) -> tuple[bool, dict]:
        """Persist ONE manager review of a document's classification
        outcome. Returns ``(created, record)`` — `created` is False when
        the latest review is a duplicate (same action, final_category AND
        reviewed_by) and its record is returned unchanged; True plus the
        appended record otherwise. `record` keys: `review_id`, `action`,
        `final_category`, `reviewed_by`, `reviewed_at` (ISO UTC).

        Scoped: the scope check runs through the parent
        `evidence_document` row (same WHERE-clause rule as the other
        writes) and a document outside (tenant_id, property_id) fails the
        scope check before anything is read or written.

        Same lock + same transaction as the reads inside it: read the
        latest review, then either append a row or return the existing
        one, AND set the classification `details` review flag
        `review_required=false` (the machine's own outcome —
        classification_type / source / confidence and the original
        review code/reason — is preserved; `details` is only parsed and
        re-serialised, never replaced). Any failure raises inside
        `with self._conn:` and rolls the whole thing back.

        Rejections (`EvidenceReviewRejectedError`, all-or-nothing):
        - no in-scope classification row for the document;
        - its `details` is not a safely parseable JSON object — it is
          never overwritten with a fresh empty object.

        `reviewed_at` is generated by the backend (`shared.utcnow`); the
        caller only supplies the settled category.
        """
        with self._lock:
            with self._conn:
                doc = self._conn.execute(
                    "SELECT 1 FROM evidence_document WHERE document_id = ? "
                    "AND tenant_id = ? AND property_id = ?",
                    (document_id, tenant_id, property_id),
                ).fetchone()
                if doc is None:
                    raise EvidenceReviewRejectedError(
                        f"document {document_id!r} is not in scope "
                        f"({tenant_id!r}, {property_id!r})"
                    )

                cls_row = self._conn.execute(
                    "SELECT details FROM evidence_classification "
                    "WHERE document_id = ?",
                    (document_id,),
                ).fetchone()
                if cls_row is None:
                    raise EvidenceReviewRejectedError(
                        f"document {document_id!r} has no classification record"
                    )
                try:
                    details = json.loads(cls_row["details"])
                    if not isinstance(details, dict):
                        raise TypeError
                except (TypeError, json.JSONDecodeError):
                    raise EvidenceReviewRejectedError(
                        f"document {document_id!r} has unparseable classification "
                        "details; refusing to overwrite them"
                    ) from None

                latest = self._conn.execute(
                    "SELECT review_id, action, final_category, reviewed_by, "
                    "reviewed_at FROM evidence_review WHERE document_id = ? "
                    "ORDER BY reviewed_at DESC, rowid DESC LIMIT 1",
                    (document_id,),
                ).fetchone()
                if (
                    latest is not None
                    and latest["action"] == action
                    and latest["final_category"] == final_category
                    and latest["reviewed_by"] == reviewed_by
                ):
                    return False, {
                        "review_id": latest["review_id"],
                        "action": latest["action"],
                        "final_category": latest["final_category"],
                        "reviewed_by": latest["reviewed_by"],
                        "reviewed_at": latest["reviewed_at"],
                    }

                review_id = new_id()
                reviewed_at = utcnow().isoformat()
                self._conn.execute(
                    "INSERT INTO evidence_review (review_id, document_id, action, "
                    "final_category, reviewed_by, reviewed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (review_id, document_id, action, final_category, reviewed_by, reviewed_at),
                )
                details["review_required"] = False
                self._conn.execute(
                    "UPDATE evidence_classification SET details = ? WHERE document_id = ?",
                    (json.dumps(details), document_id),
                )
                return True, {
                    "review_id": review_id,
                    "action": action,
                    "final_category": final_category,
                    "reviewed_by": reviewed_by,
                    "reviewed_at": reviewed_at,
                }

    # -- property-scoped reads ---------------------------------------------

    def list_documents(self, tenant_id: str, property_id: str) -> list[EvidenceDocumentRef]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT document_id, original_filename, file_type, size_bytes,
                       upload_status, classification_status, association_status,
                       uploaded_at, classification_details
                FROM (
                    SELECT d.document_id, d.original_filename, d.file_type,
                           d.size_bytes, d.upload_status,
                           COALESCE(c.status, 'pending') AS classification_status,
                           'unassociated' AS association_status,
                           d.created_at AS uploaded_at,
                           c.details AS classification_details
                    FROM evidence_document d
                    LEFT JOIN evidence_classification c ON c.document_id = d.document_id
                    WHERE d.tenant_id = ? AND d.property_id = ?
                )
                ORDER BY uploaded_at DESC
                """,
                (tenant_id, property_id),
            ).fetchall()
            return [
                EvidenceDocumentRef(
                    document_id=r["document_id"],
                    original_filename=r["original_filename"],
                    file_type=r["file_type"],
                    size_bytes=r["size_bytes"],
                    upload_status=r["upload_status"],
                    classification=r["classification_status"],
                    association=r["association_status"],
                    uploaded_at=_parse_dt(r["uploaded_at"]),
                    **_review_flags_from_details(r["classification_details"]),
                    **self._latest_review(r["document_id"]),
                )
                for r in rows
            ]

    def get_document(self, document_id: str, tenant_id: str, property_id: str) -> EvidenceDocument | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT d.document_id, d.tenant_id, d.property_id,
                       d.original_filename, d.file_type, d.size_bytes,
                       d.storage_ref, d.upload_status,
                       COALESCE(c.status, 'pending') AS classification_status,
                       'unassociated' AS association_status,
                       d.created_at AS uploaded_at, d.updated_at,
                       c.details AS classification_details
                FROM evidence_document d
                LEFT JOIN evidence_classification c ON c.document_id = d.document_id
                WHERE d.document_id = ? AND d.tenant_id = ? AND d.property_id = ?
                """,
                (document_id, tenant_id, property_id),
            ).fetchone()
            if row is None:
                return None
            return EvidenceDocument(
                document_id=row["document_id"],
                tenant_id=row["tenant_id"],
                property_id=row["property_id"],
                original_filename=row["original_filename"],
                file_type=row["file_type"],
                size_bytes=row["size_bytes"],
                storage_ref=row["storage_ref"],
                upload_status=row["upload_status"],
                classification=row["classification_status"],
                association=row["association_status"],
                uploaded_at=_parse_dt(row["uploaded_at"]),
                updated_at=_parse_dt(row["updated_at"]),
                **_review_flags_from_details(row["classification_details"]),
                **self._latest_review(row["document_id"]),
            )

    def _latest_review(self, document_id: str) -> dict:
        """Latest EFFECTIVE human manager-review record for one document
        as kwargs for the document model: ``{"final_category",
        "reviewed_by", "reviewed_at"}`` — or an EMPTY dict when no
        review row exists (the model fields then stay their default
        None).

        Same stable sort rule as the schema (and `save_review`):
        `reviewed_at DESC` then `rowid DESC` (SQLite's monotonic append
        order), so even a UTC timestamp tie selects exactly one
        deterministic row. Each document contributes at most one review
        record here, no matter how many review rows its history holds.
        Human review is read-only: the machine's own
        `evidence_classification` outcome is never consulted or
        modified.
        """
        row = self._conn.execute(
            "SELECT final_category, reviewed_by, reviewed_at "
            "FROM evidence_review WHERE document_id = ? "
            "ORDER BY reviewed_at DESC, rowid DESC LIMIT 1",
            (document_id,),
        ).fetchone()
        if row is None:
            return {}
        return {
            "final_category": row["final_category"],
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": _parse_dt(row["reviewed_at"]),
        }

    def get_classification(
        self, document_id: str, tenant_id: str, property_id: str
    ) -> tuple[str, str | None] | None:
        """Machine classification outcome for one in-scope document:
        ``(status, classification_type)`` from `evidence_classification`
        only — never the settled (manager-review) final category.

        Same scoped-read rule as `get_document`: the document must belong
        to (tenant_id, property_id). Returns None when the document is
        not in scope or has no classification row (a document without a
        row is NOT the same as one that classified as `unknown`)."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT c.status, c.classification_type
                FROM evidence_classification c
                WHERE c.document_id = ?
                  AND c.document_id IN (
                      SELECT document_id FROM evidence_document
                      WHERE tenant_id = ? AND property_id = ?
                  )
                """,
                (document_id, tenant_id, property_id),
            ).fetchone()
            if row is None:
                return None
            return row["status"], row["classification_type"]

    def read_original(self, document_id: str, tenant_id: str, property_id: str) -> bytes | None:
        """Original bytes for a document in scope (None when absent)."""
        with self._lock:
            doc = self.get_document(document_id, tenant_id, property_id)
            if doc is None:
                return None
            # storage_ref is module-generated (evidence/<id>.<ext>); verify
            # it stays inside the storage root before touching the filesystem.
            root = Path(self._storage_root).resolve()
            path = (root / doc.storage_ref).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                return None
            return path.read_bytes()

    # -- prototype binding check (auth-owned table, read-only) ---------------

    def has_manager_access(self, tenant_id: str, property_id: str) -> bool:
        """Manager access to a property via the PROTOTYPE verified
        tenant<->property binding (role='manager').

        This is a scope check against stub binding data, NOT real
        authorisation: the repo has no auth/session component yet.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tenant_property WHERE tenant_id = ? AND property_id = ? "
                "AND role = 'manager' LIMIT 1",
                (tenant_id, property_id),
            ).fetchone()
            return row is not None
