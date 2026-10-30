# Business frontend verification

This is the original frontend verification record. For the subsequent model
settings, notification revocation and Sydney scheduler fixes, see
`docs/FINAL_SELF_AUDIT_FIXES.md`: latest backend result **456 passed** and the
rerun Full System Check **13/13 PASS**. Subsequent real Campus Qwen acceptance
passed all four LLM-backed domain paths plus emergency-review safety; see
`docs/LIVE_MODEL_ACCEPTANCE.md`. The earlier 502 is historical. Windows Task
Scheduler remains unverified.

Date: 2026-10-29, Australia/Sydney.
Base: `origin/main@8896284`. Branch: `codex/business-frontend`.
No commit, push, merge or deployment was performed.

## Automated results

Executed from repository root using the existing test environment:

```sh
PYTHONDONTWRITEBYTECODE=1 /tmp/rentmate-test-venv/bin/python -m pytest backend/tests -q -p no:cacheprovider -k communication
# 67 passed, 364 deselected, 1 warning
PYTHONDONTWRITEBYTECODE=1 /tmp/rentmate-test-venv/bin/python -m pytest backend/tests -q -p no:cacheprovider
# 431 passed, 1 warning
node --check frontend/app.js
node --check frontend/api.js
git diff --check
# All exit 0
```

The current-main suite was 429 tests before adding two focused regressions.
The older 406-test number was reference evidence, not reused as a current result.
The warning is the pre-existing Starlette/httpx TestClient deprecation.

The existing route-family regression retains every expected API and `/demo`
check. Its root redirect expectation was deliberately updated from `/demo` to
`/app`, with new assertions that `/app` and all business assets load.
No domain assertion was removed or weakened.

## Real browser verification

Executed with Chromium through Playwright CLI, actual frontend forms and a
real uvicorn process. Business tests used a newly seeded isolated database on
port 8024; the original Full System Check used another newly seeded database
on port 8025. Campus Qwen environment variables were explicitly empty.
There were no mocked API responses, fake browser records or live model calls.

| Check | Actual result |
|---|---|
| Tenant session / navigation | PASS; role indicator and matching property displayed |
| Gas-smell message | PASS; real POST, pending_review, not_dispatched; exact message_id match to Maintenance |
| Routine hello / concurrent double submit | PASS; two immediate requestSubmit calls produced exactly one Communication HTTP request; receipt auto_eligible |
| Invoice PDF upload | PASS; original saved; no-LLM classification honestly failed |
| Maintenance intake with document reference | PASS; real request registered with uploaded document_id |
| Manager manual assessment | PASS; routine priority and rationale persisted |
| Manager status transitions | PASS; assessed → approved → in_progress → awaiting_confirmation → closed, with current version and evidence |
| Contextual timeline | PASS; actual request history and Evidence handoff records read |
| Document classification review | PASS; select_category invoice, reviewed_by demo-manager, association associated |
| Rent/lease scheduled scan | PASS; actual scheduler operation created reminder drafts displayed in manager queue |
| Shared draft approval | PASS; edited final content approved for exact rent-reminder version |
| Shared draft rejection | PASS; lease-deadline draft rejected with reason |
| Maintenance Communication handoff | PASS; scheduler-only handoff called through Local operations |
| Notification dispatch | PASS; real scheduler endpoint and actual counts |
| Tenant inbox | PASS; exact manager-edited rent reply visible with delivery timestamp |
| Monthly report generation | PASS; stored report partial_success; narrative source missing |
| Report history | PASS; list loaded and stored report reopened by report_id |
| Tenant calling review API | PASS; HTTP 403 |
| Manager token calling scheduler dispatch | PASS; HTTP 401 |
| Tenant requesting other property's documents | PASS; HTTP 403 |
| Other-property manager requesting seeded draft | PASS; concealed HTTP 404 |
| Concurrent inbox reads after fix | PASS; 12/12 reads succeeded |
| Guided Workflows navigation | PASS; all four business journeys rendered |
| Desktop / mobile | PASS; screenshots inspected at 1440×1000 and 390×844; tables have horizontal overflow containers |
| Existing Full System Check | PASS; browser DOM contained 13 `.check.pass`, 0 `.check.fail` on the separate fresh DB |

Browser automation initially used an incorrect text selector for the technical
demo's aggregate label. The final pass claim uses the actual 13 pass rows and
0 failure rows, not that timed-out selector. Browser console includes expected
401/403/404 negative checks and the existing favicon 404.

## Persisted record evidence

Business database results, independently read back after the UI actions:

- Maintenance request `c70062fc-612a-4d14-85a7-65c53c6dba3d`:
  `closed`, version 7, seven events.
- Evidence document `b533dc56-f763-485e-bf00-d94990d54470`:
  `associated`, human final category `invoice`, reviewed by `demo-manager`.
- Approved rent draft `30b34969-eedd-4898-abe4-faba9b93108b`:
  exact `draft:<draft_id>:v1` has **one** inbox row.
- Rejected lease draft `7d0773cf-118b-4281-9743-cd27b4a447b5`:
  exact `draft:<draft_id>:v1` has **zero** inbox rows.
- Report `b5b2a874-2646-4ae8-b844-01459c3788df`:
  `partial_success`, narrative `missing`; loaded from report history.
- Separate Full System Check report:
  `38319d2e-f0c9-46c6-b61c-a3a6cff8fd00`.

The exact draft-to-inbox counts were checked through a read-only SQL audit of
the isolated test database. The application itself accesses data only through
authorized HTTP APIs and never exposes a browser SQLite adapter.

Screenshots are in `output/playwright/`:

- `business-overview-desktop.png`
- `business-overview-mobile.png`
- `business-guided-desktop.png`
- `full-system-13-pass.png`

## Defect found during real browser testing

Concurrent overview reads exposed an intermittent `sqlite3.ProgrammingError`:
FastAPI executed a synchronous connection dependency and endpoint in different
workers. Communication and Notification used thread-bound request connections.

The shared helper now accepts an explicit `check_same_thread` option, retaining
the original default everywhere else. Those two HTTP dependencies opt out of
thread affinity for their short-lived request connection. Each connection is
still closed by its dependency. No shared global connection, schema change or
authorization relaxation was introduced.

`test_request_connection_can_be_used_by_another_fastapi_worker` runs for both
dependencies and checks a query in a different worker plus foreign-key enforcement.
All 431 backend tests pass after this fix.

## Verification limits

- Live Campus Qwen inference was **not** tested. The frontend does not label
  keyword fallback, failed classification or a missing narrative as Qwen output.
- Successful `confirm_suggested` requires an actual machine suggestion; this
  no-LLM browser run verified the manual `select_category` path. Existing Evidence
  backend tests cover suggestion review and rejection behavior.
- There is no raw Communication classification/history API, general ledger API,
  authorized property-list API or global timeline API. See the inventory.
- No external email/SMS provider or real financial processing was tested.
- Windows startup script was inspected and its printed URL updated; it cannot
  be executed on this macOS verification machine.

## Changed application and documentation files

- `frontend/app.html`, `frontend/app.css`, `frontend/app.js`, `frontend/api.js`
- `frontend/README.md`
- `frontend/FRONTEND_API_INVENTORY.md`
- `frontend/FRONTEND_WORKFLOW_MATRIX.md`
- `frontend/FRONTEND_TEST_RESULTS.md`
- `frontend/DEMO_GUIDE.md`
- `backend/main.py`
- `backend/communication/api.py`
- `backend/notification/api.py`
- `backend/shared/sqlite.py`
- `backend/tests/test_integrated_app_routes.py`
- `backend/tests/test_business_app_connections.py`
- `Start-RentMate.ps1`

Existing untracked audit files under `verification_results/` were preserved.
