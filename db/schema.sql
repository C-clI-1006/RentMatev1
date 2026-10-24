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
    amount_due     REAL,
    amount_paid    REAL,
    paid_at        TEXT,
    status         TEXT NOT NULL DEFAULT 'unpaid', -- unpaid | partial | paid | overdue
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rent_property_period ON rent_record (property_id, period);

-- ---------------------------------------------------------------------------
-- Drafts & review (owned: communication + review)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS draft (
    draft_id      TEXT PRIMARY KEY,
    message_id    TEXT NOT NULL REFERENCES message(message_id),
    request_id    TEXT REFERENCES maintenance_request(request_id),
    version       INTEGER NOT NULL DEFAULT 1,
    audience      TEXT NOT NULL DEFAULT 'tenant', -- tenant | manager
    content       TEXT NOT NULL,
    tone_hint     TEXT, -- informational only
    status        TEXT NOT NULL DEFAULT 'pending_review',
        -- pending_review | approved | rejected | superseded | sent
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_draft_message ON draft (message_id);

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
-- Evidence documents (owned: evidence — SCRUM-8)
-- Conventions: upload / classification / association are SEPARATE state
-- machines and must never be merged (upload success != classification
-- success). Statuses in this increment: upload = saved (rejections never
-- create a document row); classification = pending until a classifier
-- lands; association = unassociated until linked by a future increment.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence_document (
    document_id       TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL REFERENCES tenant(tenant_id),
    property_id       TEXT NOT NULL REFERENCES property(property_id),
    original_filename TEXT NOT NULL,
    file_type         TEXT NOT NULL, -- normalised extension, e.g. "pdf"
    size_bytes        INTEGER NOT NULL,
    storage_ref       TEXT NOT NULL, -- local storage reference to the original file
    upload_status     TEXT NOT NULL DEFAULT 'saved', -- saved (rejected uploads store nothing)
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_property
    ON evidence_document (tenant_id, property_id);

-- Reserved for future classification increments. One row per document,
-- inserted as 'pending' at upload time; no classifier exists yet, so this
-- row never leaves 'pending' in this increment.
CREATE TABLE IF NOT EXISTS evidence_classification (
    document_id   TEXT NOT NULL REFERENCES evidence_document(document_id),
    classification_type TEXT,       -- e.g. lease | maintenance | receipt | unknown
    status        TEXT NOT NULL DEFAULT 'pending', -- pending | classified | failed
    source        TEXT NOT NULL DEFAULT 'none',    -- none | stub | llm (labelled doubles)
    confidence    REAL,
    details       TEXT NOT NULL DEFAULT '{}', -- JSON
    created_at    TEXT NOT NULL,
    classified_at TEXT,
    PRIMARY KEY (document_id)
);

-- Reserved for future association increments (document -> maintenance
-- request / lease). No rows are written in this increment.
CREATE TABLE IF NOT EXISTS evidence_association (
    association_id TEXT PRIMARY KEY,
    document_id    TEXT NOT NULL REFERENCES evidence_document(document_id),
    target_type    TEXT NOT NULL,    -- maintenance_request | lease
    target_id      TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'unassociated', -- unassociated | associated | failed
    created_at     TEXT NOT NULL,
    associated_at  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_association_target
    ON evidence_association (target_type, target_id);

-- Manager review of classification outcomes (owned: evidence — manager review
-- increment). Append-only: one row PER REVIEW, so the same document may
-- accumulate several reviews over time and every historical decision is
-- retained. A review NEVER overwrites the machine's own outcome in
-- evidence_classification (classification_type / source / confidence / failed
-- details); it only records the human's settled final category. `action`
-- mirrors the review command discriminator; `final_category` is always one of
-- the five business categories (never `unknown`). `reviewed_by` comes from the
-- auth context, never the command. Scope is enforced through the parent
-- evidence_document row (tenant_id / property_id), same WHERE-clause rule as
-- the other evidence tables.
-- Effective review = the latest row for a document, by the stable sort rule
-- reviewed_at DESC then rowid DESC (SQLite's monotonic append order), so even
-- a tie on the UTC timestamp selects exactly one deterministic row.
CREATE TABLE IF NOT EXISTS evidence_review (
    review_id      TEXT PRIMARY KEY,
    document_id    TEXT NOT NULL REFERENCES evidence_document(document_id),
    action         TEXT NOT NULL,  -- confirm_suggested | select_category
    final_category TEXT NOT NULL,  -- one of the five business categories (see CHECK)
    reviewed_by    TEXT NOT NULL,  -- manager id from the auth context, not the command
    reviewed_at    TEXT NOT NULL,  -- UTC
    CHECK (action IN ('confirm_suggested', 'select_category')),
    CHECK (final_category IN (
        'lease_document', 'invoice', 'inspection_record',
        'maintenance_photo', 'communication_record'
    ))
);

CREATE INDEX IF NOT EXISTS idx_evidence_review_document
    ON evidence_review (document_id, reviewed_at);

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
