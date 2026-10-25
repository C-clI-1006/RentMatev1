# Evidence / Maintenance Integration Contract

The local implementation and temporary-database regression tests are complete. Joint review and deployment wiring with the Maintenance owner remain pending. The implementation reuses MaintenanceEvent and does not introduce an HTTP event ingestion endpoint.

## Identity and evidence IDs

Use the document_id returned by a successful upload in evidence_refs. New IDs are UUID4 strings; existing stored string IDs remain supported. Never pass local file paths. References must identify saved evidence records in the same property.

- Managers require a verified manager_id and manager_property binding and may reference saved evidence in their assigned property.
- Tenants require a tenant_property binding and may reference only their own uploaded evidence.
- The callback is a trusted in-process integration boundary. AuthContext must come from server-side session resolution or trusted configuration, never payload.actor, recipient_id, or client identity fields.
- actor is Maintenance audit metadata. The receipt separately stores the verified received_by identity.

## Callable interfaces

```python
from functools import partial
from maintenance.outbox import dispatch_pending, reconcile

# Both services use the same application database.
# Resolve manager_auth server-side and wire one authorized property at a time.
callback = partial(evidence_service.receive_maintenance_event, auth=manager_auth)
results = dispatch_pending(
    maintenance_store, {"evidence": callback}, property_id=manager_auth.property_id
)

validation = evidence_service.validate_references(document_ids, property_id, manager_auth)
receipt = evidence_service.maintenance_receipt(event_id, property_id, manager_auth)
timeline = evidence_service.maintenance_timeline(request_id, property_id, manager_auth)
```

The receiver accepts the existing contracts.maintenance.MaintenanceEvent or the actual dispatch_pending dictionary: snake_case event fields plus recipient_id, message_type, and review_required. Unknown fields, invalid types, timestamps without time zones, and empty event IDs are rejected. Fields are normalized through the existing model and timestamps converted to UTC before comparing the full event and transport fields. JSON key order does not affect identity.

The callback returns Python True only after the receipt, timeline entry, and all evidence links commit together. A persistent event_id primary key prevents duplicates. An identical replay returns True without new records; different content under the same ID raises event_id_conflict. Every replay still checks the caller's current permissions.

## HTTP validation and queries

These endpoints reuse the existing Bearer authentication dependency.

| Endpoint | Authorization and result |
|---|---|
| POST /api/evidence/references/validate with property_id and evidence_refs | Tenant or manager; returns status=valid, property_id, and deduplicated document_ids. Extra identity fields return 422. |
| GET /api/evidence/maintenance-events/{event_id}?property_id=... | Manager; returns accepted with received_at, received_by, and payload, or not_received. Receipts from another property appear not_received. |
| GET /api/evidence/maintenance-timeline?property_id=...&request_id=... | Manager; returns events, receipt times, and receiving managers, ordered by occurrence time and event ID. |

Unknown tokens return 401; unauthorized or unbound identities return 403. Missing and cross-property references both return 404 evidence_not_found, without disclosing another property's evidence. Query storage failures return 503 evidence_unavailable. General validation permits an empty list because no references need validation; completion and closure independently require at least one real reference.

## Validation before closure

MaintenanceService.change_status calls Evidence validate_references using its existing BEGIN IMMEDIATE transaction connection. The check revalidates manager access and saved evidence before changing state, preventing deletion or rebinding between validation and closure.

Invalid references return evidence_invalid; unavailable evidence storage returns evidence_unavailable. Neither outcome changes state, version, history, or outbox. Existing evidence_required, version, transition, and manager guards remain. All status commands carrying references validate them; awaiting_confirmation and closed require references.

## Failure and timeout semantics

| Callback outcome | dispatch_pending status | Recovery |
|---|---|---|
| Commit succeeds, or identical event was already accepted: True | handed_off | Confirms durable consumer acceptance, not notification delivery. |
| PermanentHandoffError: invalid_event, forbidden, evidence_required, evidence_not_found, event_id_conflict | rejected | No automatic retry; last_error stores the stable code. |
| SQLite busy/locked with confirmed transaction rollback: RetryableHandoffError | retry | Retry the original event_id and content. |
| TimeoutError, unknown storage/commit error, or a non-True return | uncertain | Reconcile the receipt before retrying. |

After a timeout, query the receipt in the same authorized property. If accepted, compare its payload with the original event before calling reconcile(store, event_id, 'evidence', accepted=True). Conflicting content must not be acknowledged as success.

not_received is not a permanent rejection record and does not prove an in-flight callback cannot commit later. Confirm that the original attempt has ended, then verify receipt absence in the authoritative database before reconciling accepted=False to retry. Query failure leaves the handoff uncertain.

A successful event has one receipt, one timeline entry, and unique document links. Linked document queries return association=associated. This covers Maintenance event links and the Maintenance timeline, not lease association, a general frontend timeline, or production login.

No background dispatcher or second sender was added. Other outbox consumers retain their existing behavior. Deployment still requires trusted property-scoped adapter wiring and agreement on operational handling of rejected events.

## Implementation and validation history

Date: 2026-10-24. The remote branch was verified at 2a5d894939ea90260f4b93abb8a58be712480070 before implementation. Changes initially remained uncommitted; delivery subsequently completed in 275c718a10ccdb45ba0fd3fb0e5749907fd2af5f.

The new tests/test_evidence_maintenance.py uses real temporary SQLite databases, real uploaded records, MaintenanceService, and dispatch_pending. Only document classification and priority inference use labeled test doubles. Coverage includes durable acceptance, restart and concurrent replay, conflicting content, invalid/cross-property references, forged actor denial, tenant ownership, permission revocation, partial-write rollback, confirmed SQLite-busy rollback, timeout reconciliation before/after commit, queue outcomes, unchanged state/version/history/outbox on closure rejection, HTTP isolation, v3-to-v4 upgrades, and independent property dispatch.

An early storage-failure fixture attempted to drop a referenced temporary table and was correctly blocked by foreign keys. It was replaced with a table rename to simulate unavailability without weakening assertions. An intermediate full run passed 292 tests; the final property-filter regression increased the count to 293.

The environment was independent Python 3.12.14 with backend/.venv/integration-test-deps. The original virtual environment referenced a missing Python 3.11 installation. Test collection disables dotenv, isolates database/storage paths, replaces model credentials, and blocks external socket connections. The online maintenance.verify_live tool was not run.

Final implementation validation:

- Related suite: 67 passed, 1 warning, 20.04s.
- Full backend suite: 293 passed, 1 warning, 47.88s; no failures or skips. This adds 33 event integration tests to the 260-test baseline.
- Command: independent Python -m pytest -q -p no:cacheprovider --tb=short, from backend with PYTHONDONTWRITEBYTECODE=1 and PYTHONPATH pointing to the independent dependencies.
- The warning concerns deprecated Starlette/httpx TestClient integration. Dependency constraints were not changed for it.
- Working-tree and staged git diff --check passed; new files also passed syntax and trailing-whitespace checks.

## Delivery verification

Before delivery, the local branch and remote branch both pointed to 2a5d894939ea90260f4b93abb8a58be712480070, with no merge in progress. Eighteen source, test, and documentation files were reviewed. No real databases, uploads, credentials, dependency directories, or test-result logs were included. Existing local logs and the task instruction file remained untracked.

No features were added during delivery review. The reviewer message was kept outside the repository; technical contracts and validation records were retained. The full backend suite was rerun with Python 3.12.14: 293 passed, 1 warning, 48.37s. Whitespace checks passed. No live Qwen calls, sensitive configuration reads, or real database operations were performed.

Delivery commit 275c718a10ccdb45ba0fd3fb0e5749907fd2af5f was pushed normally to jenny/scrum-8-document-upload. No force push or PR merge was performed. Joint integration review and deployment validation remain pending.
