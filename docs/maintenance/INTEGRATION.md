# Maintenance integration handover / 维修模块联调

Owner: Member 2 Kerwin. Scope: UC3, UC1 intake, maintenance events and UC7 summaries.
Local module implementation and synthetic live-model acceptance are complete.
No cross-owner signoff or downstream delivery is claimed by this document.

## 1. Agreed code entry points

```python
from maintenance.store import Store
from maintenance.service import MaintenanceService
from llm.live import LlmPriorityAssessor
from shared import AuthContext

# Resolve this context from the group's trusted login/property membership,
# NOT directly from tenant request bodies or property headers.
service = MaintenanceService(Store(database_path), LlmPriorityAssessor(), auth_context)
result = service.submit_issue(maintenance_intake)
```

Use a shared Store instance/factory in application wiring. No direct reads or
writes to another module's private tables. Maintenance currently owns mc_* tables.
The old shared maintenance_request schema is incompatible; decide migration
jointly before switching any consumer to it. No destructive migration is supplied.

## 2. Chang / Communication -> Maintenance

Input is contracts.maintenance.MaintenanceIntake:

```json
{
  "message_id":"msg-001", "event_id":"intake-001",
  "tenant_id":"tenant-001", "property_id":"property-001",
  "title":"Loose handle", "description":"The cupboard handle is loose but works.",
  "location":"Kitchen", "submitted_at":"2026-10-24T00:00:00Z",
  "emergency_flag":false, "evidence_refs":[]
}
```

tenant_id/property_id are populated from verified context. Retrying must reuse
the source message ID and event ID. Both keys are deduplicated per property.

Three mutually exclusive results:

```json
{"status":"registered","request_id":"...","persisted_status":"assessed","priority":"routine"}
{"status":"needs_clarification","missing_fields":["description"]}
{"status":"failed","code":"forbidden","retryable":false,"detail":null}
```

An uncertain but registered issue returns registered, persisted_status=under_review,
priority=null. Do not describe it as approved, booked or completed. Duplicate intake
returns its stored original result; query get() for current status after later changes.
Do not regenerate source IDs on a transport retry. A newly clarified message may
use a new event, but preserve original conversation/source linkage in Communication.

Additive shared-contract changes needing review by Chang/Kerwin:
emergency_flag/evidence_refs on intake; nullable priority on registered result,
maintenance event and view. The three outcomes and service names are preserved.

## 3. Maintenance -> Chang / Communication and Notification

Pending mc_outbox records target communication. Wire a trusted callback:

```python
from maintenance.outbox import dispatch_pending

def accept_status_event(payload):
    # Adapt to your public communication contract and durably deduplicate.
    # Apply review_required, recipient scope and exact approved draft version.
    # Return True ONLY after durable acceptance; not because tenant delivery succeeded.
    return communication_accept_event(payload)

dispatch_pending(store, {"communication": accept_status_event})
```

Payload is MaintenanceEvent plus recipient_id, message_type, review_required.
Key fields: event_id, request_id, property_id, occurred_at, actor, from_status,
to_status, priority (nullable), reason, evidence_refs. recipient_id is tenant:<id>.
Use stable event_id + recipient_id + message_type for logical notification identity.
Emergency/high-impact messages must respect Communication's manager-review policy.
The assessment rationale is audit data, not automatically tenant-facing wording.

manager_alert target is separate: recipient_id=property_manager:<propertyId>,
message_type=emergency_alert. It is committed before inference when configured
indicators/intake flags match. A real notification adapter must promptly consume
it; queuing alone does not prove a manager received the alert.

Handoff states: pending -> processing -> handed_off, retry, or uncertain.
handed_off does NOT mean delivered. The downstream notification service owns
actual tenant-delivery state and transport retry; this queue owns only retry of
its producer-to-consumer handoff. No second sender exists in Maintenance.
Only a definite non-acceptance may raise RetryableHandoffError. Timeouts are
uncertain; reconcile the receipt before retry. Consumers must deduplicate replay.

## 4. Maintenance -> Jenny / Evidence

Wire an evidence callback receiving the same event envelope. Persist event_id
once; link request_id/property_id/evidence_refs to authorised existing records.
Return True only after durable consumer acceptance. Replayed events cannot create
another timeline entry. Unknown/cross-property evidence must be rejected or sent
to review by the evidence owner. Current closure guards validate nonempty refs;
actual evidence validity/ownership checking must be connected here and before
accepting completion in the shared manager workflow.

## 5. Maintenance -> Sharon / Reporting

`service.summaries()` returns read-only property-scoped records:

```json
{"request_id":"...","property_id":"...","priority":"routine","status":"closed",
 "updated_at":"...","outstanding_reason":null}
```

Use a trusted manager/source-agent-authorised context. Keep request_id as source
provenance. Query does not modify any maintenance record. It currently returns
current records; Reporting must agree period filtering/snapshot rules rather than
claiming a historical monthly snapshot from this endpoint alone.

## 6. Frontend / shared auth owner

API base /api/maintenance; OpenAPI /docs. See module README for all endpoints.
Body-supplied tenant/property/manager identity is rejected. Replace api.get_auth
with the group's verified session dependency; the prototype maps random Bearer
tokens to server-side identity/property contexts. Manager commands require reason,
expected_version, and authorised evidence references for completion/closure.
409 means refresh the record and seek a fresh decision, not blindly retrying a
stale command. 403/404 must not leak another tenant/property's record.

## Joint acceptance checklist (NOT signed off yet)

- [ ] Chang + Kerwin: intake success/clarification/failure + source dedup.
- [ ] Chang + Kerwin: no reply claiming a request exists when registration failed.
- [ ] Chang + Kerwin: emergency alert actually reaches manager; sensitive reply held.
- [ ] Chang + Kerwin: status consumer acceptance and actual delivery kept distinct.
- [ ] Jenny + Kerwin: evidence ownership/existence, event replay and failure result.
- [ ] Sharon + Kerwin: source refs, period semantics, missing-data behaviour.
- [ ] Shared owners: authentication, shared SQLite migration, frontend wiring.

These checks require the owners' actual code and execution. Do not mark them
complete using the local test adapters or this handover document alone.
