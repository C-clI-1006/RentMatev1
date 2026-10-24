# Shared Contracts — Status: DRAFT for team review

These types are the single source of truth for all cross-module
interaction. Source: Stage 1 guide §18 (interface contracts), §13
(communication), §12 (state machines).

## Files

| File | Contents | Consumers |
| ---- | -------- | --------- |
| `common.py` | ID types, UTC convention | everyone |
| `maintenance.py` | `MaintenanceIntake`, `IntakeResult` (registered / needs_clarification / failed), `IMaintenanceIntake` | communication ↔ maintenance |
| `lease_rent.py` | `LeaseRentQuery`, `LeaseRentFactResult`, `FactReference`, `ILeaseRentQuery` | communication ↔ lease_rent |
| `drafting.py` | `Draft`, `ReviewRequest`, `ReviewDecisionRecord` | communication ↔ review |
| `notification.py` | `NotificationEvent`, `DeliveryResult`, `INotificationService` | all modules ↔ notification |
| `audit.py` | `AuditEvent` | all modules |
| `evidence.py` | `EvidenceDocument`, `UploadResult`, `IEvidenceUpload`, `IEvidenceClassifier` / `IEvidenceReview` / `IEvidenceTimeline` (reserved) | evidence (Jenny) ↔ maintenance / lease_rent / manager UI |

## Change process (binding)

1. Propose a change as a PR touching only `backend/contracts/`.
2. Review by the **two affected module owners** (see README table).
3. After merge: affected modules update their implementations and tests
   in the same sprint; models/tests must not drift from this package
   (guide 22.3).

## Design invariants (do not break)

- Intake results are a mutually exclusive 3-way split.
- Approval binds to a `draft_version`; delivery reads only the approved
  version; rejection ⇒ no tenant message.
- `ACCEPTED` ≠ `DELIVERED`; failures are `failed` / `pending_retry`,
  never silent success.
- Dedup by stable `event_id` (+ `recipient_id` for notifications);
  single retry owner.
- Identity/scope (`tenant_id`, `property_id`) always from auth context.
