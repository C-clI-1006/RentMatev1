"""Seed the deterministic identities and review items used by the local demo."""

import argparse
from pathlib import Path

from shared.sqlite import init_connection


def seed(database: str | Path) -> None:
    db = init_connection(database)
    timestamp = "2026-01-01T00:00:00Z"
    try:
        db.execute(
            "INSERT OR IGNORE INTO tenant (tenant_id,name,created_at) VALUES (?,?,?)",
            ("demo-tenant", "Demo Tenant", timestamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO tenant (tenant_id,name,created_at) VALUES (?,?,?)",
            ("demo-tenant-2", "Demo Tenant 2", timestamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO property (property_id,name,created_at) VALUES (?,?,?)",
            ("demo-property", "Demo Property", timestamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO property (property_id,name,created_at) VALUES (?,?,?)",
            ("demo-property-2", "Demo Property 2", timestamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO tenant_property (tenant_id,property_id,created_at) "
            "VALUES (?,?,?)",
            ("demo-tenant", "demo-property", timestamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO tenant_property (tenant_id,property_id,created_at) "
            "VALUES (?,?,?)",
            ("demo-tenant-2", "demo-property-2", timestamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO manager (manager_id,name,created_at) VALUES (?,?,?)",
            ("demo-manager", "Demo Manager", timestamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO manager (manager_id,name,created_at) VALUES (?,?,?)",
            ("demo-manager-2", "Demo Manager 2", timestamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO manager_property (manager_id,property_id,created_at) "
            "VALUES (?,?,?)",
            ("demo-manager", "demo-property", timestamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO manager_property (manager_id,property_id,created_at) "
            "VALUES (?,?,?)",
            ("demo-manager-2", "demo-property-2", timestamp),
        )

        messages = (
            (
                "demo-review-message",
                "demo-tenant",
                "demo-property",
                "Please confirm when the repair can be inspected.",
            ),
            (
                "demo-review-message-2",
                "demo-tenant-2",
                "demo-property-2",
                "Please confirm the next step for the second property.",
            ),
        )
        for message_id, tenant_id, property_id, content in messages:
            db.execute(
                "INSERT OR IGNORE INTO message "
                "(message_id,tenant_id,property_id,content,message_type,sentiment,received_at) "
                "VALUES (?,?,?,?,?,'none',?)",
                (message_id, tenant_id, property_id, content, "maintenance", timestamp),
            )

        drafts = (
            (
                "demo-review-draft",
                "demo-review-message",
                "A manager will review the inspection request and confirm the next step.",
            ),
            (
                "demo-review-draft-2",
                "demo-review-message-2",
                "A manager for the second property will review this request.",
            ),
        )
        for draft_id, message_id, content in drafts:
            # These fixed IDs are synthetic demo fixtures. Reset only their
            # prior decisions so every launcher start offers the same review
            # exercise without touching any real or dynamically created row.
            db.execute("DELETE FROM review WHERE draft_id=?", (draft_id,))
            db.execute(
                "DELETE FROM audit WHERE subject_type='draft' AND subject_id=?",
                (draft_id,),
            )
            db.execute(
                "INSERT INTO draft "
                "(draft_id,message_id,version,audience,content,review_reason,source_facts,"
                "status,created_at) VALUES (?,?,1,'tenant',?,'manager confirmation required',"
                "'[]','pending_review',?) "
                "ON CONFLICT(draft_id) DO UPDATE SET "
                "message_id=excluded.message_id,request_id=NULL,source_kind=NULL,source_id=NULL,"
                "version=1,audience='tenant',content=excluded.content,tone_hint=NULL,"
                "review_reason=excluded.review_reason,source_facts='[]',"
                "status='pending_review',created_at=excluded.created_at",
                (draft_id, message_id, content, timestamp),
            )
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    seed(args.database)
