# Monthly reporting integration

The reporting module now provides:

- maintenance and lease/rent SQLite adapters owned by their source modules;
- deterministic open-task and risk derivation;
- persisted, versioned report snapshots with source references;
- event-id idempotency and conflict detection;
- `complete`, `partial_success`, and `failed` outcomes;
- a live narrative adapter that preserves structured data and marks prose
  `missing` when the model is unavailable or invalid;
- a trusted monthly scheduler endpoint.

## Scheduler entrypoint

Set a random server-side secret:

```text
RENTMATE_SCHEDULER_TOKEN=<random secret>
```

Then call:

```http
POST /api/ops/monthly-reports/run
Authorization: Bearer <scheduler secret>
Content-Type: application/json

{"property_id":"demo-property","year":2026,"month":10}
```

The server derives a stable event ID of
`scheduled:<YYYY-MM>:<property_id>`. Repeating the same monthly slot returns the
stored report with `reused=true`; it does not create another generation.
Ordinary user/manager session tokens cannot impersonate the scheduler.

Source records are read at generation time. `data_as_of` records when those
sequential reads completed; it is not a historical month-end snapshot. If one
or two primary sections are unavailable, available data is retained and the
result is `partial_success`. If all three are unavailable, generation stops,
the failed attempt is persisted/audited for idempotency, and the endpoint
returns an error. Narrative failure never deletes calculated data.

The endpoint generates and returns a report; dashboard presentation and
external delivery remain separate work.
