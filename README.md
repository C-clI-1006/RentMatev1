# RentMate (Stage 2)

RentMate is a tenant-facing service that helps tenants report maintenance issues,
ask lease/rent questions, and keeps property managers in control of everything
sent to tenants.

This repository holds the Stage 2 implementation. Stage 1 design deliverables
(reports, UML, guide) live in a separate document set — this repo is code only.

## Status

The integration candidate contains working Maintenance, Lease/Rent, Evidence,
Manager Review, Reporting and Notification modules on one FastAPI/SQLite
application. The UC4/UC5 path is executable end to end: scheduled checks make
idempotent review drafts, a manager approves or rejects an exact version, the
single dispatcher records truthful delivery outcomes, and the tenant reads a
property-scoped inbox. Evidence/Maintenance events and monthly reporting are
mounted in the same application.

Run `Start-RentMate.cmd` for the integrated local demonstration. The older
`Start-Maintenance.cmd` remains available for the standalone Maintenance demo.
Live Qwen is configured through the environment; unavailable inference fails
closed or enters manual review rather than silently pretending success.

## Stack

- Backend: Python 3.11+, FastAPI, SQLite (modular monolith, no microservices)
- Frontend: no-build integrated demo console; API boundary supports a later React client
- LLM: school Qwen proxy via a single `backend/llm/` adapter; labelled test
  doubles are used until the proxy connection is confirmed.

## Repository layout

```
backend/
  contracts/     Shared cross-module data + service interfaces (single source of truth)
  communication/ UC1/UC2 intake, classification, routing, drafting (owner: Chaney)
  maintenance/   Maintenance registration, priority, tracking (owner: Kerwin)
  lease_rent/    Authorised lease/rent fact queries + rule checks (owner: Ziheng)
  evidence/      Evidence upload, event consumption (owner: Jenny)
  reporting/     Monthly reports, summaries (owner: Sharon)
  llm/           Single LLM adapter (config, validation, error conversion)
  notification/  Durable delivery outbox, dispatcher, retry and tenant inbox
  review/        Manager review/approval (approval bound to a draft version)
  repositories/  Persistence (SQLite)
  shared/        Cross-cutting helpers (auth context, IDs, time)
  main.py        FastAPI app entrypoint (thin; only wires routers)
db/
  schema.sql     SQLite schema draft
frontend/
  index.html     Integrated API demonstration console
```

## Integration rules (binding)

1. Modules interact **only** via `backend/contracts/` types and public service
   interfaces. Never read or write another module's private data/tables directly.
2. `tenantId` / `propertyId` come from the **auth context**, not user input.
   The tenant↔property binding is verified at the entry point.
3. A manager approval is bound to a **specific draft version**. The sending
   path reads only the approved version — no re-generation after approval.
4. Rejection ⇒ **no tenant message**. Rejection must be observable in the UI.
5. Delivery truth: `Accepted` ≠ `Sent`. Notification failures are recorded as
   `failed` / `pending_retry` and shown as such — never as success.
6. Deduplication: stable `eventId` (plus `recipientId` for notifications).
   One retry owner per delivery flow (the dispatcher), no double retries.
7. Sentiment detection (optional) may influence **tone only**; it can never
   downgrade an emergency or bypass review.

## Getting started

Windows demonstration (creates an ignored local virtual environment/database):

```powershell
.\Start-RentMate.ps1
```

Then open `http://127.0.0.1:8020/demo`. To run the complete automated suite
instead of starting the server, use `.\Start-RentMate.ps1 -Verify`.

Manual setup:

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env   # fill in LLM config when available
uvicorn main:app --reload
```

API startup applies the shared versioned schema/migrations. Maintenance keeps
its aggregate in `mc_*`; other modules use their owned shared-schema tables.

## Ownership

| Area                        | Owner  |
| --------------------------- | ------ |
| Skeleton, contracts, infra  | Chaney |
| communication module        | Chaney |
| maintenance module          | Kerwin |
| lease_rent module           | Ziheng |
| evidence module             | Jenny  |
| reporting module            | Sharon |
| notification module         | Chaney |
| review module               | Chaney |
| repositories (SQLite)       | Chaney |

Note: `db/schema.sql` sections are owned per table (see section comments in
the file): lease/rent tables → Ziheng, drafts/review tables → Chaney,
outbox → Chaney. Contract changes follow `backend/contracts/README.md`.

Contract changes: propose in `backend/contracts/`, review by the two affected
owners, then update the affected models/tests. See
`backend/contracts/README.md`.
