# Business frontend API inventory

Inspected against `origin/main@8896284` on 2026-10-29 before implementation.
All business routes validate Bearer tokens server-side. The UI's role label
is presentation only. Property context must match the token's verified binding.
No schema, authentication or domain rule changes are needed for `/app`.

| Method / path | Role | Request | Response | Workflow / existing tests |
|---|---|---|---|---|
| POST `/api/communication/messages` | bound tenant | `{content}` | `{message_id,reply_status,delivery_status:"not_dispatched"}` | Intake; communication API tests |
| POST `/api/maintenance/requests` | bound tenant | `{title,description,location,evidence_refs}`; optional `Idempotency-Key` header | registered / clarification / failure union | Intake; maintenance/integrated tests |
| GET `/api/maintenance/requests` | tenant (own), manager (property) | none | `MaintenanceView[]` | List; maintenance tests |
| GET `/api/maintenance/requests/{id}` | same scope | none | view with `request_id,message_id,status,priority,version,events,evidence` | Details/history; maintenance tests |
| POST `/api/maintenance/requests/{id}/assessment` | bound manager | `{expected_version,priority,rationale}` | status result union | Manual assessment; maintenance tests |
| POST `/api/maintenance/requests/{id}/status` | bound manager | `{expected_version,target_status,reason,evidence_refs}` | status result union | Versioned lifecycle; maintenance tests |
| GET `/api/maintenance/summaries` | manager | none | property summaries | Overview; integrated tests |
| GET `/api/maintenance/outbox` | manager | none | `event_id,target,request_id,status,attempts,last_error` rows | Handoff evidence; maintenance tests |
| POST `/api/evidence/documents` | bound tenant or manager | multipart `file,property_id`; pdf/jpg/jpeg/png/doc/docx/xls/xlsx/txt | accepted + document OR rejected/code/reason | Upload; evidence upload/manager-upload tests |
| GET `/api/evidence/documents?property_id=` | tenant / manager | scoped property | refs with filename, classification state, final category, review metadata, timestamp | Documents; evidence tests |
| GET `/api/evidence/documents/{id}?property_id=` | scoped tenant / manager | property | stored document | Details; evidence tests |
| GET `/api/evidence/documents/{id}/download?property_id=` | same | property | original bytes | Download; evidence tests |
| GET `/api/evidence/documents/{id}/text?property_id=` | same | property | extracted text OR manual_review_required | Extraction; evidence tests |
| POST `/api/evidence/reviews` | manager | `{status:"confirm_suggested",document_id,property_id}` OR `{status:"select_category",document_id,property_id,category}` | final_category, reviewed_by, reviewed_at | Document classification review; evidence review tests |
| POST `/api/evidence/references/validate` | scoped tenant / manager | `{property_id,evidence_refs}` | validation outcome | References; evidence maintenance tests |
| GET `/api/evidence/maintenance-timeline?request_id=&property_id=` | scoped tenant / manager | exact request/property | persisted maintenance timeline | Contextual activity; evidence maintenance tests |
| GET `/api/evidence/maintenance-events/{id}?property_id=` | scoped tenant / manager | event/property | receipt status / payload | Event receipt; evidence tests |
| GET `/api/reviews` | manager | none | pending `ReviewQueueItem[]` (draft, property_id, original_message, reason, source_facts) | Queue; review tests |
| GET `/api/reviews/{id}` | manager | none | pending item; 404 if missing/settled/outside scope | Detail; review tests |
| POST `/api/reviews/{id}/decision` | manager | `{expected_version,decision:"approved"|"rejected",final_content?,reason?}` | ReviewDecisionRecord | Approve/reject; review tests |
| GET `/api/notifications/inbox` | bound tenant | none | `{event_id,content,delivered_at}[]` | Delivered local inbox; notification/integrated tests |
| POST `/api/ops/notifications/dispatch` | scheduler | optional limit query | processed/delivered/pending_retry/failed counts | Dispatch; communication dispatch tests |
| POST `/api/ops/communication/maintenance-events/run` | scheduler | optional limit | processed/handoff/retry/rejected/uncertain counts | Maintenance status replies; communication maintenance tests |
| POST `/api/ops/rent-alerts/run` | scheduler | optional UTC `now` query | scan/draft/reuse counts | UC4/UC5; lease-rent/integrated tests |
| POST `/api/manager/monthly-reports/generate` | manager | `{property_id,year,month,idempotency_key,reason?}` | generated result with ReportRecord + reused | Reporting; manager reporting tests |
| GET `/api/manager/monthly-reports?property_id=&year=&month=` | manager | scoped period | ReportRecord[] | History; manager reporting tests |
| GET `/api/manager/monthly-reports/{id}?property_id=` | manager | property | ReportRecord | Previous generation; manager reporting tests |
| POST `/api/ops/monthly-reports/run` | scheduler | ScheduledRunBody | generated result | Automatic report entry; reporting tests (not needed by manager UI) |
| GET `/health` | public | none | `{status:"ok"}` | Health; route tests |

## Local model configuration (added after frontend self-audit)

All four routes below require a loopback client and the separate scheduler
Bearer credential; cross-origin requests are denied. Responses never include
the key. Runtime configuration is process-only and never saved to browser storage.

| Method / path | Request | Response | Verification |
|---|---|---|---|
| GET `/api/ops/llm/settings` | none | base_url, model, configured flags, source, persistence | settings API tests + browser |
| PUT `/api/ops/llm/settings` | JSON base_url, model, api_key (blank retains existing key at same address) | same non-secret status | auth, validation and secret-redaction tests + browser |
| DELETE `/api/ops/llm/settings` | none | restored environment configuration status | API tests + browser |
| POST `/api/ops/llm/test` | none; uses saved server settings | validated_model_response, source=llm, category, confidence OR sanitized error | mocked successful response; real browser connection-failure path |

## Missing read capabilities (unchanged)

- No Communication message-history/detail/classification-result HTTP API on
  this main. Display the actual submission receipt and state classification,
  emergency and route as unavailable. No frontend keyword inference. The
  message ID can be matched to a maintenance view's persisted `message_id`.
  Session receipts are explicitly transient and never a replacement database.
- No general lease/rent listing, property profile, authorized property-list,
  settled review history or global timeline API. Keep a fixed session property;
  show reminder drafts and reporting data rather than fabricated ledger values.
- Evidence list exposes classification **state**, not the classifier's raw
  category/source. Show final_category only after human review. Do not claim Qwen.
- Use maintenance `events` and Evidence timeline for an exact selected request.
  Shared-review provenance is visible only in pending queue items. Delivered
  inbox records supply exact event IDs but no universal request relationship.
- Existing lifecycle transitions are enforced by backend. UI presents server
  state and command options; stale/invalid commands surface backend errors.
- `/demo` remains the unchanged technical verification console; `/app` is
  the business portal. Operations use a separately entered scheduler credential.
