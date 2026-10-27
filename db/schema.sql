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
-- Managers & property assignments (owned: permissions)
--
-- A manager is a trusted staff identity, distinct from a tenant. Scope is
-- explicit and verified: a manager may only touch the properties bound in
-- manager_property. NO binding => default deny (see permissions service).
-- Manager identity comes from the server-side auth context only; it is never
-- accepted from a request body or header.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS manager (
    manager_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Verified manager<->property assignment. A manager may be bound to several
-- properties (one row each); each binding is independently verified.
CREATE TABLE IF NOT EXISTS manager_property (
    manager_id  TEXT NOT NULL REFERENCES manager(manager_id),
    property_id TEXT NOT NULL REFERENCES property(property_id),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (manager_id, property_id)
);

CREATE INDEX IF NOT EXISTS idx_manager_property_property
    ON manager_property (property_id);

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
    -- Cross-module reference. The current Maintenance implementation owns
    -- mc_request, while older databases may still contain maintenance_request.
    -- Keep the identifier for provenance/delivery without coupling this table
    -- to either module's private persistence schema.
    request_id    TEXT,
    -- Origin provenance for system-generated drafts.
    -- source_kind e.g. 'rent_reminder' | 'lease_deadline' | 'maintenance_status'
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
        -- auto_eligible | pending_review | approved | rejected | superseded | sent
        -- (auto_eligible: deterministic policy found no review trigger;
        --  dispatch still goes through the shared notification path —
        --  see contracts/drafting.py DraftStatus)
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_draft_message ON draft (message_id);
-- Idempotency for system-generated drafts: one draft per source
-- (re-runs of a scheduled job must not create a second review item).
CREATE UNIQUE INDEX IF NOT EXISTS idx_draft_source ON draft (source_id)
    WHERE source_id IS NOT NULL;

-- Durable acceptance receipt for Maintenance -> Communication handoffs.
-- The composite producer identity is retained exactly; source_id is the
-- Communication-owned stable draft/notification identity derived from it.
CREATE TABLE IF NOT EXISTS communication_maintenance_receipt (
    event_id       TEXT NOT NULL,
    recipient_id   TEXT NOT NULL,
    message_type   TEXT NOT NULL,
    source_id      TEXT NOT NULL UNIQUE,
    request_id     TEXT NOT NULL,
    tenant_id      TEXT NOT NULL REFERENCES tenant(tenant_id),
    property_id    TEXT NOT NULL REFERENCES property(property_id),
    payload        TEXT NOT NULL,
    draft_id       TEXT NOT NULL REFERENCES draft(draft_id),
    received_at    TEXT NOT NULL,
    PRIMARY KEY (event_id, recipient_id, message_type)
);

CREATE INDEX IF NOT EXISTS idx_communication_maintenance_request
    ON communication_maintenance_receipt (property_id, request_id, received_at);

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

-- Prototype transport used by the executable demo. A row exists only after
-- the dispatcher has durably placed the exact approved text in the tenant's
-- property-scoped inbox. Replays reuse the same composite delivery key.
CREATE TABLE IF NOT EXISTS tenant_inbox (
    event_id      TEXT NOT NULL,
    recipient_id  TEXT NOT NULL REFERENCES tenant(tenant_id),
    property_id   TEXT NOT NULL REFERENCES property(property_id),
    content       TEXT NOT NULL,
    delivered_at  TEXT NOT NULL,
    PRIMARY KEY (event_id, recipient_id),
    FOREIGN KEY (event_id, recipient_id)
        REFERENCES notification_event(event_id, recipient_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_inbox_scope
    ON tenant_inbox (recipient_id, property_id, delivered_at);

-- Manager alerts are a separate delivery boundary: they are authorised by
-- manager_property and never enter the tenant inbox or draft-review path.
CREATE TABLE IF NOT EXISTS manager_inbox (
    event_id      TEXT NOT NULL,
    recipient_id  TEXT NOT NULL REFERENCES manager(manager_id),
    property_id   TEXT NOT NULL REFERENCES property(property_id),
    content       TEXT NOT NULL,
    delivered_at  TEXT NOT NULL,
    PRIMARY KEY (event_id, recipient_id),
    FOREIGN KEY (event_id, recipient_id)
        REFERENCES notification_event(event_id, recipient_id)
);

CREATE INDEX IF NOT EXISTS idx_manager_inbox_scope
    ON manager_inbox (recipient_id, property_id, delivered_at);

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

-- Maintenance event acceptance: receipt + timeline + links are one transaction.
CREATE TABLE IF NOT EXISTS evidence_event_receipt (
    event_id TEXT PRIMARY KEY,
    property_id TEXT NOT NULL REFERENCES property(property_id),
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL,
    received_by TEXT NOT NULL REFERENCES manager(manager_id)
);
CREATE TABLE IF NOT EXISTS evidence_maintenance_timeline (
    event_id TEXT PRIMARY KEY REFERENCES evidence_event_receipt(event_id),
    request_id TEXT NOT NULL,
    property_id TEXT NOT NULL REFERENCES property(property_id),
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_maintenance_request
    ON evidence_maintenance_timeline(property_id,request_id,occurred_at);
CREATE TABLE IF NOT EXISTS evidence_event_document (
    event_id TEXT NOT NULL REFERENCES evidence_maintenance_timeline(event_id),
    document_id TEXT NOT NULL REFERENCES evidence_document(document_id),
    PRIMARY KEY(event_id,document_id)
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

-- ---------------------------------------------------------------------------
-- Monthly property reports (owned: reporting)
-- Source records are read-only; this table stores generated snapshots only.
-- event_id is the trigger idempotency key. A new event_id deliberately creates
-- the next generation for the same property/month.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report (
    report_id      TEXT PRIMARY KEY,
    property_id    TEXT NOT NULL REFERENCES property(property_id),
    period_year    INTEGER NOT NULL,
    period_month   INTEGER NOT NULL CHECK (period_month BETWEEN 1 AND 12),
    generation     INTEGER NOT NULL CHECK (generation >= 1),
    trigger_kind   TEXT NOT NULL CHECK (trigger_kind IN ('scheduled','manager')),
    event_id       TEXT NOT NULL UNIQUE,
    manager_id     TEXT,
    outcome        TEXT NOT NULL CHECK (outcome IN ('complete','partial_success','failed')),
    data_json      TEXT NOT NULL,
    narrative_json TEXT NOT NULL,
    data_as_of     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    UNIQUE (property_id, period_year, period_month, generation)
);

CREATE INDEX IF NOT EXISTS idx_report_property_period
    ON report (property_id, period_year, period_month);
