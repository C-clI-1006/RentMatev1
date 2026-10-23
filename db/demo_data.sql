-- RentMate demo data (idempotent — safe to re-apply on any existing DB).
--
-- Usage (fresh demo database):
--   sqlite3 rentmate.db < db/schema.sql
--   sqlite3 rentmate.db < db/demo_data.sql
--
-- Shape: 2 tenants, 2 properties, 2 managers on DIFFERENT properties
-- (manager-1 → property-1 only; manager-2 → property-2 only), plus one
-- resident tenant binding so the existing tenant flow has data to work
-- with. All timestamps are UTC ISO-8601.
--
-- This is demo/seed data only — it is NOT applied by init_connection and
-- must never be treated as production data.

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO tenant (tenant_id, name, contact, created_at) VALUES
    ('tenant-1', 'Alex', 'alex@example.com', '2026-01-01T00:00:00Z'),
    ('tenant-2', 'Bailey', 'bailey@example.com', '2026-01-01T00:00:00Z');

INSERT OR IGNORE INTO property (property_id, name, address, created_at) VALUES
    ('property-1', 'Home A', '1 A St', '2026-01-01T00:00:00Z'),
    ('property-2', 'Home B', '2 B St', '2026-01-01T00:00:00Z');

INSERT OR IGNORE INTO tenant_property (tenant_id, property_id, role, created_at) VALUES
    ('tenant-1', 'property-1', 'resident', '2026-01-01T00:00:00Z'),
    ('tenant-2', 'property-2', 'resident', '2026-01-01T00:00:00Z');

-- 2 managers, each bound to a different property only (default deny applies
-- to everything else).
INSERT OR IGNORE INTO manager (manager_id, name, created_at) VALUES
    ('manager-1', 'Alice', '2026-01-01T00:00:00Z'),
    ('manager-2', 'Bob', '2026-01-01T00:00:00Z');

INSERT OR IGNORE INTO manager_property (manager_id, property_id, created_at) VALUES
    ('manager-1', 'property-1', '2026-01-01T00:00:00Z'),
    ('manager-2', 'property-2', '2026-01-01T00:00:00Z');
