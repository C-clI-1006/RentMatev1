"""Seed one safe, repeatable end-to-end RentMate demonstration."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from maintenance.seed_demo import seed as seed_maintenance
from shared.sqlite import init_connection


def seed(database: str | Path) -> None:
    seed_maintenance(database)
    now = datetime.now(timezone.utc)
    today = now.date()
    due_date = today - timedelta(days=7)
    end_date = today + timedelta(days=14)
    timestamp = now.isoformat().replace("+00:00", "Z")

    db = init_connection(database)
    try:
        # Remove only prior fixed demo UC4/UC5 workflow rows so each launcher
        # start offers the same review exercise without touching other data.
        draft_rows = db.execute(
            "SELECT draft_id FROM draft WHERE source_id LIKE 'rent-overdue:demo-rent:%' "
            "OR source_id LIKE 'lease-deadline:demo-lease:%'"
        ).fetchall()
        for row in draft_rows:
            event_id = f"draft:{row['draft_id']}:v1"
            db.execute("DELETE FROM tenant_inbox WHERE event_id=?", (event_id,))
            db.execute("DELETE FROM notification_event WHERE event_id=?", (event_id,))
            db.execute("DELETE FROM review WHERE draft_id=?", (row["draft_id"],))
            db.execute("DELETE FROM audit WHERE subject_id=?", (row["draft_id"],))
            db.execute("DELETE FROM draft WHERE draft_id=?", (row["draft_id"],))
        db.execute("DELETE FROM lease_deadline_alert WHERE lease_id='demo-lease'")

        db.execute(
            "INSERT INTO lease "
            "(lease_id,property_id,tenant_id,start_date,end_date,created_at) "
            "VALUES ('demo-lease','demo-property','demo-tenant',?,?,?) "
            "ON CONFLICT(lease_id) DO UPDATE SET property_id=excluded.property_id,"
            "tenant_id=excluded.tenant_id,start_date=excluded.start_date,"
            "end_date=excluded.end_date",
            ((today - timedelta(days=180)).isoformat(), end_date.isoformat(), timestamp),
        )
        db.execute(
            "INSERT INTO rent_record "
            "(rent_record_id,lease_id,property_id,period,due_date,amount_due,"
            "amount_paid,status,created_at) "
            "VALUES ('demo-rent','demo-lease','demo-property',?,?,2000,0,"
            "'unpaid-but-not-due',?) "
            "ON CONFLICT(rent_record_id) DO UPDATE SET lease_id=excluded.lease_id,"
            "property_id=excluded.property_id,period=excluded.period,"
            "due_date=excluded.due_date,amount_due=2000,amount_paid=0,"
            "status='unpaid-but-not-due'",
            (due_date.strftime("%Y-%m"), due_date.isoformat(), timestamp),
        )
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    seed(args.database)
