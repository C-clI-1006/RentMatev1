# Monthly reporting integration

The reporting module now provides:

- maintenance and lease/rent SQLite adapters owned by their source modules;
- deterministic open-task and risk derivation;
- persisted, versioned report snapshots with source references;
- event-id idempotency and conflict detection;
- `complete`, `partial_success`, and `failed` outcomes;
- a live narrative adapter that preserves structured data and marks prose
  `missing` when the model is unavailable or invalid;
- a trusted monthly scheduler endpoint (ops token);
- manager HTTP endpoints: manual generation plus read-only single-report
  and version-list fetches (manager session tokens).

## Demo data (seed script)

Seed one demo property with:

```bash
python backend/scripts/seed_reporting_demo.py --db <DATABASE_PATH value>
```

- Demo property: `reporting-demo-property`, report month `2026-09`.
- The `--db` path must be the **same database the running service opens**:
  the service reads the `DATABASE_PATH` environment variable (default
  `backend/rentmate.db`). Seed the same path the service was started with.
- Seeding never calls the model and never generates a report; it only
  inserts fixture rows. All writes are `INSERT OR IGNORE` keyed on stable
  IDs, so re-running it is safe and adds nothing (each existing row is
  reported as "already present, left untouched").

## Tokens: scheduler vs. manager session

There are two distinct credentials, never interchangeable:

- **Scheduler token** — one shared ops secret, set server-side as
  `RENTMATE_SCHEDULER_TOKEN`. It authenticates the scheduled monthly run
  (`POST /api/ops/monthly-reports/run`). It carries no manager identity.
- **Manager session tokens** — the `RENTMATE_AUTH_TOKENS` JSON map
  (strict Bearer), one entry per manager session, e.g.
  `{"<manager session token>": {"tenant_id": "...", "property_id": "...", "manager_id": "..."}}`.
  Manager endpoints derive identity only from this server-side binding; a
  body `manager_id` is rejected (422). The manager must also hold a
  `manager_property` row for the requested property.

All examples below use **placeholders only** — never put a real token in
docs or logs.

## Scheduler entrypoint

```text
RENTMATE_SCHEDULER_TOKEN=<random secret>
```

```http
POST /api/ops/monthly-reports/run
Authorization: Bearer <scheduler token placeholder>
Content-Type: application/json

{"property_id":"reporting-demo-property","year":2026,"month":9}
```

The server derives a stable event ID of
`scheduled:<YYYY-MM>:<property_id>`. Repeating the same monthly slot (same
event ID) returns the stored report with `reused=true`; it does not create
another generation. Ordinary user/manager session tokens cannot
authenticate this endpoint.

Source records are read at generation time. `data_as_of` records when those
sequential reads completed; it is not a historical month-end snapshot. If one
or two primary sections are unavailable, available data is retained and the
result is `partial_success`. If all three are unavailable, generation stops:
no report row is saved (the event ID is not consumed, so the same monthly
slot can be retried once data recovers) and only an audit-only failure
record is written; the endpoint returns an error. Narrative failure never
deletes calculated data.

The endpoint generates and returns a report; dashboard presentation and
external delivery remain separate work.

## Automatic monthly execution on Windows

The repository includes a Windows Task Scheduler runner. It runs on the first
day of each month and generates the previous completed calendar month for every
row in `property`. It calls the same `ReportService` and scheduled trigger used
by the HTTP endpoint; the stable event ID makes retries idempotent.

Run `Start-RentMate.cmd` once to create the local runtime, then register the
task from PowerShell:

```powershell
.\Register-Monthly-Reports.ps1
```

The default schedule is day 1 at 02:00. To choose another time:

```powershell
.\Register-Monthly-Reports.ps1 -StartTime '03:30'
```

The school/demo launcher uses `.runtime\rentmate-demo.db`. A deployed service
must register the task with the same persistent database used by the backend:

```powershell
.\Register-Monthly-Reports.ps1 `
  -DatabasePath 'D:\RentMate\rentmate.db' `
  -LogPath 'D:\RentMate\logs\monthly-report-runs.jsonl'
```

For an immediate acceptance run (an explicit period is optional):

```powershell
.\Run-Monthly-Reports.ps1 -Year 2026 -Month 9
```

Each run appends one credential-free JSON record to
`.runtime\monthly-report-runs.jsonl`. The record contains timestamps, period,
per-property outcome and whether an existing idempotent result was reused. A
failure for one property does not stop the remaining properties, but makes the
process exit non-zero so Task Scheduler records a failed run. The machine must
be powered on and the configured user must be logged on under the default
school/demo registration; a production deployment should register the same
runner under its service account and use its persistent database path.

## Manager entrypoints

All three use the manager session token (`Bearer <manager session token
placeholder>`).

**Generate a report** (manager-initiated):

```http
POST /api/manager/monthly-reports/generate
Authorization: Bearer <manager session token placeholder>
Content-Type: application/json

{"property_id":"reporting-demo-property","year":2026,"month":9,
 "idempotency_key":"<client-minted key>","reason":"optional"}
```

- Retrying with the same `idempotency_key` reuses the stored report
  (`reused=true`); a deliberate regeneration mints a new key and produces
  the next generation. A key already used by another property/period
  returns 409. Errors: 401/403 (identity/access), 409 (key conflict),
  503 (all sources unavailable).
- The scheduler token does NOT work here (401).

**Fetch one report** (use `property_id` when selecting another property
bound to the same manager; omission uses the token's current property):

```http
GET /api/manager/monthly-reports/<report_id placeholder>?property_id=reporting-demo-property
Authorization: Bearer <manager session token placeholder>
```

**List all generations for a period** (ascending `generation`; empty list
when nothing is stored):

```http
GET /api/manager/monthly-reports?property_id=reporting-demo-property&year=2026&month=9
Authorization: Bearer <manager session token placeholder>
```

Both GETs are strictly read-only: they collect no data, call no LLM, and
write nothing. A manager without a binding for the requested property is
denied with the same response whether or not the report exists, so the
denial never leaks report existence (unbound property → 404; a revoked own
binding → 403).

## Running the tests

Run from `backend/` with the three LLM variables explicitly emptied so the
live adapter is guaranteed unconfigured (fail-closed) and no model is
called; `backend/.env` is left untouched (dotenv does not override
pre-set variables):

```bash
cd backend
LLM_BASE_URL= LLM_MODEL= LLM_API_KEY= .venv/bin/python -m pytest tests -q
```
