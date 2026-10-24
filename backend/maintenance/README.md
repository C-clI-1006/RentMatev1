# Maintenance Coordinator - Kerwin / Member 2

Handover materials: docs/maintenance/ at the repository root, including
INTEGRATION.md, DEMO.md, TRACEABILITY.md and JIRA_AND_CONTRIBUTION.md.
Run `python -m maintenance.verify_live --output path/to/result.json` from backend
for an explicit real-model/API acceptance check (requires configured proxy/VPN).
On this Windows computer, double-click Verify-Maintenance.cmd to run the
automated suite and real-model checks; result JSON stays on D drive.

Implements AR-MC-01, mandatory FR-MC-01 through FR-MC-07 and the local
enforcement of NFR-MC-01 through NFR-MC-04. Provider-type suggestions
(FR-MC-08) are advisory, with no booking/payment functionality.

## Start on this Windows computer

Double-click `Start-Maintenance.cmd` in the repository root. It creates an
isolated Python environment, SQLite database and caches under
`D:\ELEC5620\rentmate-maintenance`. Source code stays in this checkout.
The startup terminal prints two temporary random prototype session tokens.
Open http://127.0.0.1:8020/api/maintenance/demo and paste the tenant token
to submit an issue; switch to the manager token to assess/approve/update it.
Tokens change on restart. Existing requests stay in SQLite.

The local starter reuses the existing Windows-encrypted course key if no
backend/.env exists. Keep USYD VPN connected for live Qwen assessment.
Failure/unconfigured/invalid/uncertain model output enters under_review;
it NEVER silently switches to the keyword test double. Manual assessment
is available even when a previous process died during model assessment.

This is a prototype session map, not production login or a contractor portal.
Do not expose the local demo server publicly.

## Portable setup for teammates / Canvas deployment

Requires Python 3.11+. From `backend`:

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
# Configure backend/.env or environment before launch:
python -m uvicorn main:app --host 127.0.0.1 --port 8020
```

For a repeatable environment matching the local verification, install
`python -m pip install -r maintenance/requirements-tested.txt` instead of
the unpinned root requirements. This snapshot includes runtime and test
dependencies; it does not bundle Python, credentials, or VPN access.

Environment variables:

* DATABASE_PATH: writable SQLite path; defaults to ./rentmate.db.
* LLM_BASE_URL: course proxy URL ending /v1.
* LLM_MODEL: campus-qwen.
* LLM_API_KEY: personal course key, never commit or send to the frontend.
* LLM_TIMEOUT_SECONDS: model timeout, default 30.
* RENTMATE_AUTH_TOKENS: JSON object mapping unpredictable session tokens to
  authorised contexts, for example
  `{"REPLACE_RANDOM_TOKEN":{"tenant_id":"t1","property_id":"p1"}}`.
  A manager context additionally contains `manager_id`. Provision mappings
  on the server; clients cannot supply identity/property IDs in requests.

An absent token map denies API access. Replace `api.get_auth` with the
team's verified login/session resolver during integration.

## Public interfaces and API

`MaintenanceService(store, assessor, trusted_auth_context)` implements
`IMaintenanceIntake` and `IMaintenanceTracking`. Trusted callers must resolve
property membership before constructing the context. A tenant can only read
their own requests; manager access is confined to the context's property.

* POST /api/maintenance/requests: message_id, event_id, title, description,
  optional location/evidence_refs. IDs of tenant/property come from session.
* GET /api/maintenance/requests: scoped list, including event history.
* GET /api/maintenance/requests/{id}: scoped record, current version/history.
* POST /api/maintenance/requests/{id}/assessment: manager manual assessment;
  expected_version, priority, rationale. Does not grant approval.
* POST /api/maintenance/requests/{id}/status: manager transition;
  expected_version, target_status, reason, optional evidence_refs.
* GET /api/maintenance/summaries: manager-scoped reporting read model.
* GET /api/maintenance/outbox: manager-scoped handoff status.
* GET /api/maintenance/demo: standalone demonstration UI, separate from the
  team's future React frontend. API documentation is available at /docs.

State flow: submitted -> assessed -> approved -> in_progress ->
awaiting_confirmation -> closed. Model failure/uncertainty -> under_review;
manager assessment -> assessed. Rework returns to in_progress. Manager can
cancel any active state, with a recorded reason. Every mutation validates
expected_version. Awaiting confirmation and closure require nonempty evidence
references and explicit manager action; the evidence owner must verify those
references against actual authorised evidence during integration.

Intake deduplicates on BOTH property/message_id and property/event_id.
Initial intake/emergency events commit before model inference. Model output
can only update the same submitted version, preventing stale callbacks from
reopening cancelled or manually assessed requests. Assessment history labels
source=llm or source=stub and retains rationale/source references. A returned
confidence is an uncertainty signal, not a calibrated guarantee.

## Persistence and integration boundaries

Maintenance owns mc_request, mc_history and mc_outbox. These tables are
created idempotently by Store. The old shared schema.sql remains a team draft;
the module does NOT write to its incompatible maintenance_request table or
directly write another agent's tables. Agree on a migration before retiring
either schema. No existing records are deleted or automatically converted.

Each state/version/history/outbox mutation is one SQLite transaction.
The mc_history table is append-only through the application API; it is not
a tamper-proof database against an operator with raw SQLite access.

Outbox targets are communication, evidence and manager_alert. Pending means
not yet handed to a consumer. `dispatch_pending(store, adapters)` accepts a
mapping of target names to trusted callbacks. Each callback receives the
committed maintenance event plus recipient_id, message_type, review_required.
Return True only after durable, deduplicated consumer acceptance. Replays use
the same event_id/recipient/message type. Adapt this payload to the agreed
shared notification/evidence contract at the boundary.

The dispatcher is the single retry owner for this HANDOFF. It never directly
sends tenant messages or claims delivered. `handed_off` is not `delivered`.
The Communication/Notification owner must apply its approval policy, bind
approval to the exact draft version, send, and expose actual delivery results.
Consumers must deduplicate event IDs. An adapter may raise
RetryableHandoffError only when it can guarantee non-acceptance. Other errors
are uncertain; a crash leaves processing. Both require verified receipt
reconciliation through reconcile() before retrying. No adapters are installed
by default, so the demo truthfully shows pending events.

Remaining team work: real communication/evidence consumers and emergency
notification delivery; shared authentication; evidence-reference verification;
React integration; reporting agent consumption; agreed shared schema migration.
No claim is made that these other modules are completed.

## Traceability / acceptance checks

* FR-MC-01: duplicate source/event and concurrent intake tests.
* FR-MC-02/03: structured live adapter validation, invalid/timeout manual review,
  independent emergency rule before inference; manual assessment recovery.
* FR-MC-04: full lifecycle, approval/closure guards, cancellation and rework.
* FR-MC-05/06: committed status/evidence event queue and scoped summaries.
* FR-MC-07: retryable versus ambiguous handoff, stable keys, receipt reconciliation.
* NFR-MC-01: tenant/property scope, manager-only operations, body spoof rejection.
* NFR-MC-02: persisted actors, timestamps, rationale, evidence refs and history.
* NFR-MC-03/04: concurrent version conflicts, late callbacks, transaction rollback.

Automated tests use temporary databases and labelled test assessors/HTTP
substitutes. These are not evidence of live Qwen calls. Live integration must
be verified separately with the VPN and personal key.

## Contract changes for affected owners to review

contracts/maintenance.py adds emergency_flag and evidence_refs to intake, and
makes priority nullable on RegisteredIntake, MaintenanceEvent and MaintenanceView.
Unknown priority is required to represent submitted/under_review without
inventing a recommendation. Communication must accept priority=null. The
three intake outcomes and existing method names are preserved. Requesting
clarification happens before registration; a valid but uncertain registered
request returns registered with under_review and priority=null.
