# UC3 follow-up: intake retries and Reporting source

Implemented on `uc3/reporting-and-intake-followup`, based on main `0e9ef64`.
This is local implementation evidence, not shared-owner approval.

## HTTP intake retries

POST `/api/maintenance/requests` now accepts an optional `Idempotency-Key`
header (1-128 characters). Generate one key per logical issue and preserve it
on retries. The server derives the source identity using the authenticated
tenant/property and key. Never send identity fields in the request body.

Same key and same content returns the original registration outcome without
another assessment or event. Same key with changed title, description, location
or evidence references returns 409 `idempotency_conflict`. Concurrent submissions
share one request. Query the request for its latest state: the replayed intake
response is the original outcome, potentially still `submitted` during inference.
No header preserves compatibility, but provides no HTTP retry deduplication.

The demo retains its key for retry, including after success. Use **Start another
issue** for a deliberate new request. The key lasts for the current page only;
production clients must preserve it across reloads if recovery is required.

## Reporting handover to Sharon

```python
from maintenance.reporting import MaintenanceReportSource
from reporting.collector import ReportDataCollector

source = MaintenanceReportSource(maintenance_service)
collector = ReportDataCollector(source, lease_rent_report_source)
result = collector.collect(property_id, period)
```

`maintenance_service` must carry a trusted manager identity. This provider
rechecks the current manager/property binding; tenant access, cross-property
access and revoked access raise PermissionError. Storage/record validation
failures produce NOT_AVAILABLE, not EMPTY. Source IDs and versions are retained.

Proposed period membership: created within [start,end), a recorded event within
that window, or the last recorded state before end was non-terminal. Terminal
states are `closed` and `cancelled`. The policy is implemented for joint review,
not claimed as an agreed group decision. Returned status/priority are CURRENT
values; this is not a historical snapshot. Reports must distinguish monthly
membership from current status, especially when deriving outstanding tasks.

Shared-contract change requiring Sharon's review: MaintenanceRequestRef.priority
now accepts null, matching an unassessed/under-review request. Never convert null
to routine or drop the request. No other reporting contract fields changed.

## Evidence handover to Jenny: still pending

The current main contains only the Evidence placeholder. Existing Maintenance
events and the evidence outbox adapter boundary remain available; real evidence
existence/property validation and durable receipt lookup need Jenny's public
interface. Agree record ID format, permanent rejection/error codes and receipt
query semantics before wiring. The existing boolean acknowledgement cannot
represent permanent validation failures distinctly; do not classify these as
transient retryable errors. Closure still checks only nonempty references.

## Verification and Jira text

Full backend suite: 153 passed (one existing third-party deprecation warning).
Includes HTTP replay/conflict, concurrent intake, report period boundaries,
unknown priority, invalid stored data, revoked permissions, and the real
Maintenance provider with ReportDataCollector. Lease data in that collector
test is an explicitly empty test double. No live Qwen calls were needed.

Suggested SCRUM-22 update (after entering actual review status):
Implemented HTTP intake idempotency and the Maintenance reporting source.
Full backend suite passes: 153 tests. Reporting period semantics and nullable
priority require Sharon's review. Evidence integration awaits Jenny's interface.
