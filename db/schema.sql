-- RentMate SQLite schema (DRAFT v0.1 — pending team review)
-- Conventions:
--   * All *_at columns are UTC ISO-8601 strings.
--   * tenant_id / property_id come from the auth context; the binding is
--     verified at entry points (never trusted from user input).
--   * event_id is the stable dedup key for intake and notification.
--   * Request status and delivery status are SEPARATE state machines.
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Tenants & properties (auth-owned; prototype stubs here)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tenant (
    tenant_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    contact     TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS property (
    property_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    address     TEXT,
    created_at  TEXT NOT NULL
);

-- Verified tenant<->property binding (auth context source for the prototype)
CREATE TABLE IF NOT EXISTS tenant_property (
    tenant_id   TEXT NOT NULL REFERENCES tenant(tenant_id),
    property_id TEXT NOT NULL REFERENCES property(property_id),
    role        TEXT NOT NULL DEFAULT 'resident', -- resident | manager
    created_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, property_id)
);

-- ---------------------------------------------------------------------------
-- Inbound tenant messages (owned: communication)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS message (
    message_id   TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL REFERENCES tenant(tenant_id),
    property_id  TEXT NOT NULL REFERENCES property(property_id),
    content      TEXT NOT NULL,
    message_type TEXT NOT NULL, -- maintenance | lease_rent | smalltalk | unknown
    -- Sentiment (optional extension): tone hint only. Must never lower an
    -- emergency priority or bypass review.
    sentiment    TEXT, -- positive | neutral | negative | none
    received_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_tenant ON message (tenant_id, property_id);

-- ---------------------------------------------------------------------------
-- Maintenance requests (owned: maintenance)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS maintenance_request (
    request_id   TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL REFERENCES tenant(tenant_id),
    property_id  TEXT NOT NULL REFERENCES property(property_id),
    message_id   TEXT NOT NULL REFERENCES message(message_id),
    event_id     TEXT NOT NULL, -- stable dedup key
    title        TEXT NOT NULL,
    description  TEXT NOT NULL,
    location     TEXT,
    priority     TEXT NOT NULL DEFAULT 'normal', -- emergency | high | normal | low
    status       TEXT NOT NULL DEFAULT 'submitted',
        -- submitted | triage | assigned | in_progress | resolved | closed | rejected
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Idempotency: one request per intake event.
CREATE UNIQUE INDEX IF NOT EXISTS idx_maintenance_event ON maintenance_request (event_id);
CREATE INDEX IF NOT EXISTS idx_maintenance_tenant ON maintenance_request (tenant_id, property_id);

-- ---------------------------------------------------------------------------
-- Lease & rent (owned: lease_rent)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lease (
    lease_id     TEXT PRIMARY KEY,
    property_id  TEXT NOT NULL REFERENCES property(property_id),
    tenant_id    TEXT NOT NULL REFERENCES tenant(tenant_id),
    start_date   TEXT NOT NULL,
    end_date     TEXT,
    document_ref TEXT, -- pointer to the lease document (evidence module)
    version      TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lease_tenant ON lease (tenant_id, property_id);

CREATE TABLE IF NOT EXISTS rent_record (
    rent_record_id TEXT PRIMARY KEY,
    lease_id       TEXT NOT NULL REFERENCES lease(lease_id),
    property_id    TEXT NOT NULL REFERENCES property(property_id),
    period         TEXT NOT NULL, -- e.g. "2026-09"
    due_date       TEXT NOT NULL, -- ISO date; drives UC4/UC5 overdue + reminder alerts
    amount_due     REAL CHECK (amount_due IS NULL OR amount_due >= 0),
    amount_paid    REAL CHECK (amount_paid IS NULL OR amount_paid >= 0),
        -- may be < amount_due (partial payment as data)
    paid_at        TEXT,
    -- Computed from due_date + amounts at "as of" time; only these three:
    -- paid | unpaid-but-not-due | overdue. There is deliberately NO
    -- 'partial' state — partial payment is expressed via
    -- amount_paid < amount_due (agreed decision, see lease_rent.rules).
    status         TEXT NOT NULL DEFAULT 'unpaid-but-not-due'
        CHECK (status IN ('paid', 'unpaid-but-not-due', 'overdue')),
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rent_property_period ON rent_record (property_id, period);
-- UC5 scheduler scans for due/overdue records by due_date.
CREATE INDEX IF NOT EXISTS idx_rent_due ON rent_record (due_date);

-- UC5 detection record (owned: lease_rent). The stable candidate key makes
-- scheduled re-runs idempotent before a cross-module Draft is created.
CREATE TABLE IF NOT EXISTS lease_deadline_alert (
    alert_id       TEXT PRIMARY KEY,
    candidate_key  TEXT NOT NULL UNIQUE,
    lease_id       TEXT NOT NULL REFERENCES lease(lease_id),
    tenant_id      TEXT NOT NULL REFERENCES tenant(tenant_id),
    property_id    TEXT NOT NULL REFERENCES property(property_id),
    end_date       TEXT NOT NULL,
    remaining_days INTEGER NOT NULL CHECK (remaining_days >= 0),
    prepared_on    TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deadline_alert_lease
    ON lease_deadline_alert (lease_id, end_date);

-- ---------------------------------------------------------------------------
-- Drafts & review (owned: communication + review)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS draft (
    draft_id      TEXT PRIMARY KEY,
    -- message_id is the origin for drafts replying to a tenant message.
    -- System-generated drafts (e.g. UC5 rent reminders) have no inbound
    -- message; their origin is (source_kind, source_id) instead.
    -- Invariant: exactly one of message_id / source_id is set.
    message_id    TEXT REFERENCES message(message_id),
    request_id    TEXT REFERENCES maintenance_request(request_id),
    -- Origin provenance for system-generated drafts.
    -- source_kind e.g. 'rent_reminder' | 'lease_deadline'
    -- source_id   stable, deterministic. The adapter should reuse the
    --             lease_rent candidate keys verbatim (see
    --             lease_rent/rules.py rent_reminder_key /
    --             lease_deadline_alert_key), e.g.
    --             'rent-overdue:<rent_record_id>:<due_date>'.
    source_kind   TEXT,
    source_id     TEXT,
    version       INTEGER NOT NULL DEFAULT 1,
    audience      TEXT NOT NULL DEFAULT 'tenant', -- tenant | manager
    content       TEXT NOT NULL,
    tone_hint     TEXT, -- informational only
    -- Why review is required (shown to the manager) + what grounded the
    -- draft (JSON list of FactReference). Persisted by communication so
    -- the review queue can show the full context across processes.
    review_reason TEXT,
    source_facts  TEXT, -- JSON: [{source_type, source_id, version, snippet}]
    status        TEXT NOT NULL DEFAULT 'pending_review',
        -- pending_review | approved | rejected | superseded | sent
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_draft_message ON draft (message_id);
-- Idempotency for system-generated drafts: one draft per source
-- (re-runs of a scheduled job must not create a second review item).
CREATE UNIQUE INDEX IF NOT EXISTS idx_draft_source ON draft (source_id)
    WHERE source_id IS NOT NULL;

-- Approval is bound to a specific draft version. final_content is the exact
-- text that may be delivered; the sending path reads ONLY from here.
CREATE TABLE IF NOT EXISTS review (
    review_id      TEXT PRIMARY KEY,
    draft_id       TEXT NOT NULL REFERENCES draft(draft_id),
    draft_version  INTEGER NOT NULL,
    decision       TEXT NOT NULL, -- approved | rejected
    manager_id     TEXT NOT NULL,
    final_content  TEXT, -- NULL when rejected (no tenant message on reject)
    reason         TEXT,
    decided_at     TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_review_draft ON review (draft_id);

-- ---------------------------------------------------------------------------
-- Outbox & delivery (owned: notification). Single retry owner: the dispatcher.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notification_event (
    event_id      TEXT NOT NULL,
    recipient_id  TEXT NOT NULL,
    kind          TEXT NOT NULL,
    request_id    TEXT,
    message_id    TEXT,
    payload       TEXT NOT NULL, -- JSON; approved content only
    status        TEXT NOT NULL DEFAULT 'accepted',
        -- accepted | pending_review | delivered | failed | pending_retry
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (event_id, recipient_id) -- dedup key
);

-- ---------------------------------------------------------------------------
-- Audit (append-only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit (
    audit_id     TEXT PRIMARY KEY,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    request_id   TEXT,
    event_id     TEXT,
    details      TEXT NOT NULL DEFAULT '{}', -- JSON
    at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_request ON audit (request_id);
