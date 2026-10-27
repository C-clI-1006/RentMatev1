# Lease & Rent integration

This module owns the deterministic rules and SQLite access for:

- **UC4:** overdue rent detection and reminder preparation;
- **UC5:** upcoming lease deadline detection and alert preparation.

## Record and property validation

Other modules must not trust a `property_id` supplied beside a lease/rent
record ID. Use `SQLiteLeaseRentRepository.resolve_source_scope(...)` to obtain
the record's authoritative `tenant_id`, `property_id` and parent `lease_id`:

```python
scope = repository.resolve_source_scope(
    source_type="rent_record",  # or "lease"
    source_id="rent-123",
)
```

`None` means no such record. A rent row whose property conflicts with its
parent lease raises `LeaseRentDataError`. For a non-disclosing scope check,
use `source_belongs_to_property(...)`; it returns `False` for both an unknown
record and a record outside the requested property.

The caller still owns actor authorisation. Manager-facing code must first use
the shared `manager_property` permission check, then use this record-scope
lookup. Tenant-facing fact queries continue to use
`LeaseRentQueryService.get_facts`, which verifies `tenant_property` itself.

## Scheduled reminders and alerts

`run_due_alerts(now=...)` performs both checks and writes only eligible
candidates as system-origin `pending_review` drafts:

- the UC4 source key is `rent-overdue:<rent_record_id>:<due_date>`;
- the UC5 source key is `lease-deadline:<lease_id>:<end_date>`.

The source key is reused unchanged in `draft.source_id`, so retries are
idempotent. UC4/UC5 drafts always require manager approval; this step never
dispatches a tenant notification. The HTTP trigger
`POST /api/ops/rent-alerts/run` requires the same
`RENTMATE_SCHEDULER_TOKEN` credential as monthly reporting.
