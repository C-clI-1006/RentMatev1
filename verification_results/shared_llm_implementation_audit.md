# Shared LLM and Communication Dispatch Audit

Date: 2026-10-29 (Australia/Sydney)

## Executive result

The implementation now preserves the Stage 1 architecture: one shared
OpenAI-compatible Campus Qwen endpoint with task-specific adapters, while the
five agents retain separate contracts, prompts, schemas, permissions and
workflows. No second model provider or deployment was introduced.

The Communication adapter calls Qwen when all three of `LLM_BASE_URL`,
`LLM_API_KEY` and `LLM_MODEL` are configured. It validates a strict JSON
contract and fails closed to human handling for unavailable, malformed,
unsupported or uncertain results. With no valid configuration, the application
uses the explicitly documented deterministic `KeywordClassifier` safe mode.

The confirmed FR-COM-04 defect was that Communication `auto_eligible` drafts
were persisted but never entered the notification outbox. The smallest fix now
queues an exact `reply:<message_id>` event. The dispatcher independently checks
the persisted draft version, message/property/tenant binding, emergency audit,
decision phrases, source references and draft status before delivery.

## Exact changed files

- `backend/llm/classifier.py`: `QwenClassifier` and safe-mode builder.
- `backend/communication/api.py`: API wiring selects the Qwen adapter when configured.
- `backend/communication/service.py`: routine message-origin drafts queue a stable notification event.
- `backend/notification/dispatcher.py`: independent eligibility and source/provenance checks for Communication auto-delivery.
- `backend/tests/test_communication_qwen_classifier.py`: valid, invalid, unsupported, uncertain, timeout and safe-mode tests.
- `backend/tests/test_communication_dispatch.py`: routine delivery, review blocking, duplicate prevention and failure persistence.
- `backend/tests/test_communication_api.py`: real HTTP queue/dispatch integration regression.
- `backend/tests/test_communication_service.py`: updated old eligibility-only assertion to verify queueing without claiming delivery.

No database schema, external dependency, credential, Jira issue or production
configuration was changed.

## Shared adapter evidence

| Agent | Adapter | Input/output validation | Failure behavior | Live call in this run |
|---|---|---|---|---|
| Communication | `QwenClassifier` | strict category enum, confidence range, no extra fields | returns `None`; service persists message and hands off | NOT VERIFIED |
| Maintenance | `LlmPriorityAssessor` | strict priority/rationale/confidence/emergency schema | `AssessmentFailure` and manual review | NOT VERIFIED |
| Evidence | `QwenDocumentClassifier` | existing document classification contract | accepted document plus classification failure state | NOT VERIFIED |
| Reporting | `LlmReportNarrator` | strict summary schema | narrative `missing`, structured report retained | NOT VERIFIED |

Tests used mocked HTTP responses only. No Campus Qwen request was made and no
credential was printed. A configured model name or adapter code is not treated
as evidence of a live invocation.

## FR-COM-04 finding

Before this change, `CommunicationService._draft_reply` inserted an
`auto_eligible` draft but did not call `NotificationService.publish`; only a
manager approval path created a notification event. Therefore routine
Communication replies could never reach the existing dispatcher.

After this change, the producer creates one stable `reply:<message_id>` event.
The HTTP response still says `delivery_status=not_dispatched` because accepted
means queued, not delivered. The dispatcher changes the persisted state to
`delivered`, `pending_retry` or `failed` and writes delivery audit records.

The dispatcher does not trust a client payload's eligibility flag. It verifies:

- exact draft ID, version and content;
- persisted `auto_eligible` status;
- exact tenant/property/message origin;
- no emergency audit and no decision-required phrase;
- non-unknown category;
- maintenance request or lease/rent source references where required;
- current tenant/property authorization.

Rejected, pending-review, stale, emergency, unknown, missing-fact and
conflicting-fact drafts are not automatically dispatched.

## Current limitations

1. Communication classification is a real Qwen adapter when configured, but
   this audit did not make a live model call. The final demonstration needs a
   separately recorded Campus Qwen run if live AI evidence is required.
2. The safe fallback is deterministic and must be described as such; it is not
   LLM inference.
3. The local tenant inbox is a prototype delivery provider, not external email
   or SMS.
4. The current Communication POST has no HTTP idempotency key. Repeated
   intentional submissions create distinct messages; notification retries for
   one message are idempotent.

## Final demonstration

1. Start the application with valid `RENTMATE_AUTH_TOKENS` and, if available,
   Campus Qwen variables.
2. Submit a routine message such as `hello` or a clearly routine maintenance
   message. Show `auto_eligible` and `not_dispatched`, then run Notification
   Dispatch and show one delivered tenant-inbox row.
3. Submit the gas-smell message. Show `pending_review`, no tenant delivery,
   and manager alert queueing. Reject it and show the inbox remains unchanged.
4. Submit `There is no gas smell, but my tap is dripping.` and show emergency
   detection remains false.
5. Submit `Something is wrong.` and show manual handling without an invented
   reply or request.
6. Submit `Has my rent been paid?` with seeded records. Show the grounded
   lease/rent draft and source reference, then demonstrate review blocking for
   a decision-required variant.
7. Run the existing Full System Check and show 13/13 PASS.

