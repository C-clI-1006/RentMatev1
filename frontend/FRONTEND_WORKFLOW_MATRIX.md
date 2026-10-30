# Frontend workflow matrix

Base: latest pushed `main@8896284`; implementation: `codex/business-frontend`.
Verification uses real FastAPI HTTP endpoints and isolated seeded databases.

| Agent / workflow | Frontend entry | API | Role | Expected result | Actual verification | Limitations |
|---|---|---|---|---|---|---|
| Communication intake | Tenant Messages | POST communication/messages | Tenant | Persist message, truthful reply/delivery states | Browser PASS: gas → pending_review/not_dispatched; hello → auto_eligible/not_dispatched | No classification or historical message read API; transient receipts labelled |
| Communication routing | Message receipt → Maintenance | GET maintenance/requests; exact message_id join | Tenant | Verified related request only | Browser PASS: gas message creates exact linked Maintenance request | No lease/rent routing metadata in response; unavailable rather than inferred |
| Maintenance intake | Tenant Maintenance | POST maintenance/requests | Tenant | Registered request + real ID | Browser PASS: routine repair with uploaded document ID | LLM disabled; under_review is truthful |
| Maintenance assessment/lifecycle | Manager Maintenance → Open | GET request; POST assessment/status | Manager | Versioned transition/history | Browser PASS: manual routine assessment then approved/in_progress/awaiting_confirmation/closed | Command controls use contract values; backend rejects invalid transitions |
| Evidence upload | Documents | POST evidence/documents multipart | Tenant/manager | Original saved; distinct classification state | Browser PASS: invoice PDF accepted; machine failed without Qwen | Machine suggestion/source absent from read API |
| Evidence classification review | Manager Documents → Open | POST evidence/reviews | Manager | Confirm suggested or select real category | Browser PASS: select_category invoice; persisted reviewed_by and association | Confirm-suggested success requires a machine suggestion; not claimed in this no-LLM run |
| Evidence association/history | Maintenance detail / Activity Timeline | GET evidence/maintenance-timeline | Scoped tenant/manager | Exact request events and links | Browser PASS: recorded references; associated document | Background Evidence dispatcher must be configured; no universal timeline |
| Lease & Rent scan | Manager Lease & Rent → Local operations | POST ops/rent-alerts/run | Separate scheduler | Persist rent/lease reminder drafts, dedup source | Browser PASS: created reminders, reviewable source_kind/source facts | No general ledger/list read API; no frontend finance calculations |
| Shared approval | Review Centre | POST reviews/{id}/decision | Manager | Exact-version approval; queue only | Browser PASS: manager edited rent reminder approved | Settled decisions shown only as session receipts; no settled-history read API |
| Shared rejection | Review Centre | Same, decision=rejected | Manager | Reason recorded; no delivery | Browser PASS: lease reminder rejected with reason | Pending queue drops settled rows |
| Maintenance-to-Communication | Local operations | POST ops/communication/maintenance-events/run | Scheduler | Real durable handoff | Browser PASS: actual handoff result | Does not itself confirm tenant delivery |
| Notification delivery | Local operations / Tenant Notifications | POST ops/notifications/dispatch; GET notifications/inbox | Scheduler / tenant | Real delivered records, no fabricated success | Browser PASS: edited rent reply visible with delivered_at | Local inbox only, not email/SMS |
| Reporting | Manager Reports | POST manager/monthly-reports/generate | Manager | Persist scoped month with idempotency key | Browser PASS: generated partial_success with narrative missing | Live Qwen not called; structured data retained |
| Reporting history | Reports → Load history / Open | GET manager/monthly-reports and /{id} | Manager | Reopen stored generation | Browser PASS: generated record reopened | History is per selected month, not global |
| Role/property authorization | All views + direct HTTP negative probes | reviews, evidence, ops notifications | Server Bearer scope | 403/404/401 as appropriate | Browser HTTP PASS: tenant reviews 403; manager dispatch 401; cross-property evidence 403/review 404 | Role switch is presentation, never authorization |
| Guided journeys | Guided Workflows A–D | Same real APIs via individual pages | Both + explicit ops | Presenter chooses each step | Browser navigation verified; selected records drive completed steps | No fake global workflow status; unproven links stay pending |
| Existing verification | /demo | Existing 13 checks, unchanged | Prototype role tokens | 13 PASS / 0 FAIL | Fresh database browser PASS: 13 pass rows, 0 fail rows | Model disabled; deterministic/manual fallback paths exercised |

No new business API, database schema or business policy was introduced. A
request-scoped SQLite worker-handoff compatibility fix was necessary for real
concurrent browser reads in Communication and Notification; it has two focused
regressions. The existing route test now checks `/app`, its assets and preserved
`/demo`, and expects the intentional root redirect to `/app`.
