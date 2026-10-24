"""Seed only the synthetic identities used by the local maintenance demo."""

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
            "INSERT OR IGNORE INTO property (property_id,name,created_at) VALUES (?,?,?)",
            ("demo-property", "Demo Property", timestamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO tenant_property (tenant_id,property_id,created_at) "
            "VALUES (?,?,?)",
            ("demo-tenant", "demo-property", timestamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO manager (manager_id,name,created_at) VALUES (?,?,?)",
            ("demo-manager", "Demo Manager", timestamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO manager_property (manager_id,property_id,created_at) "
            "VALUES (?,?,?)",
            ("demo-manager", "demo-property", timestamp),
        )
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    seed(args.database)
