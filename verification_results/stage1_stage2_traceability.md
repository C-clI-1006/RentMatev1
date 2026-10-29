# Stage 1 → Stage 2 traceability

## Architecture preserved

Stage 1's five responsibility boundaries remain intact:

| Responsibility | Stage 2 owner | Shared Qwen use |
|---|---|---|
| Tenant Communication | `backend/communication/` | classification adapter only |
| Maintenance Coordinator | `backend/maintenance/` | priority adapter only |
| Lease and Rent Administration | `backend/lease_rent/` | verified fact queries and deterministic UC4/UC5 rules |
| Document and Evidence Management | `backend/evidence/` and `backend/llm/classify.py` | document classification adapter |
| Property Reporting | `backend/reporting/` and `backend/llm/reporting.py` | narrative adapter only |

There is one model endpoint configuration (`LLM_BASE_URL`, `LLM_MODEL`,
`LLM_API_KEY`) and separate task prompts/contracts. No agent decides tenant
identity, property ownership, approval, or delivery. Those decisions remain in
server-side bindings, manager review and NotificationService/dispatcher.

## Traceability by requirement

| Stage 1 intent | Stage 2 implementation | Evidence |
|---|---|---|
| Tenant message intake | `POST /api/communication/messages`; server-side token scope | Communication API and HTTP tests |
| Safe intent classification | `QwenClassifier` when configured; KeywordClassifier safe mode otherwise | Qwen tests; 67 Communication tests |
| Emergency independence | `policy.has_emergency_indicator` runs separately and handles negation | existing emergency/negation tests |
| Maintenance ownership | `MaintenanceService.submit_issue` and `mc_request` persistence | Communication integration tests |
| Lease/rent ownership | `LeaseRentQueryService` + `SQLiteLeaseRentRepository` | grounded fact tests and fresh HTTP probe |
| Review ownership | `ReviewService.decide`, exact draft version | review tests |
| Routine auto-reply | stable `reply:<message_id>` outbox event plus dispatcher recheck | dispatch tests and HTTP dispatch test |
| Delivery truth | accepted/queued separate from delivered/failed/retry | notification tests and dispatch evidence |
| Idempotency | notification composite key and terminal state checks | duplicate dispatch test |

## Acceptance status

- UC1: PASS for the implemented Communication path.
- UC2: PASS for the implemented Communication-to-Lease/Rent path.
- FR-COM-04: PASS in the prototype delivery path; routine drafts now reach the
  existing dispatcher without bypassing safeguards.
- Campus Qwen live invocation: NOT VERIFIED in this audit.
- Full System Check: fresh database browser run = 13 PASS / 0 FAIL.

## No-commit rule

This audit branch contains uncommitted implementation and evidence changes by
design. Nothing was committed, pushed, merged or deployed automatically.
