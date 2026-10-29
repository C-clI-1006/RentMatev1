# Final self-audit fixes and verification

Follow-up: the configured live API subsequently passed Communication,
Maintenance, Evidence and Reporting acceptance cases, plus emergency-review
safety. See `docs/LIVE_MODEL_ACCEPTANCE.md` and
`output/verification/live-domain-acceptance.json`. The earlier 502 probe below
is preserved as historical evidence. Windows execution is still unverified.

Date: 2026-10-29 (Australia/Sydney). Branch: `codex/business-frontend`, built
from `main@8896284`. Existing uncommitted business-frontend work and older
untracked audit files were preserved. No commit, push or merge was performed.

## P0 — Current authorization before routine notification delivery

**Reproduced before fixing.** A real Communication `hello` intake persisted an
`auto_eligible` draft and an accepted `reply:<message_id>` event. After deleting
the corresponding persisted `tenant_property` row, the dispatcher called the
inbox adapter and returned `delivered`. The new regression initially failed:

```text
test_routine_communication_revoked_tenant_binding_blocks_dispatch_and_audits
AssertionError: assert 'delivered' == 'failed'
```

The cause was an early return into `_authorised_communication_auto_payload`
before the shared tenant binding check at the bottom of
`_authorised_tenant_payload`. Review-approved and maintenance-status paths
already performed the check; direct Communication auto-replies bypassed it.

The persisted relationship check now runs before any tenant delivery branch
can return. `dispatch_one` holds its existing `BEGIN IMMEDIATE` transaction
through validation, adapter delivery and result commit, preventing a competing
SQLite writer from revoking the binding between that read and delivery.
No queued identity, cached permission or payload flag substitutes for the
current `tenant_property` relationship. Existing draft/version/content/source
checks, emergency blocking and manager-review checks remain in place.

The exact regression now proves:

- queued event starts `accepted` while authorized;
- deleting the binding before dispatch causes permanent `failed`, one attempt;
- the delivery adapter is **never called** and the inbox has **zero** rows;
- `last_error` is `recipient is no longer authorised for property`;
- `delivery.failed` audit details include
  `reason=recipient_property_authorization_denied`;
- there is no `delivery.delivered` audit row;
- a retry remains failed and cannot accidentally send the denied message.

## P1 — Sydney calendar month selection

The previous CLI default used the host computer's local calendar. At Sydney
02:00 on day 1, a UTC-configured computer could still see the previous month
and consequently select a report one month too early.

`scheduled_period()` now starts with an aware instant, converts it using
`ZoneInfo("Australia/Sydney")`, then selects the previous calendar month.
Naive injected times are rejected. Logs record `business_timezone`, while
`started_at`, `finished_at` and report storage timestamps retain UTC semantics.
Explicit `--year/--month` runs and stable scheduled idempotency IDs are preserved.
This corrects scheduler month selection; it does not silently revise the
existing `ReportPeriod` source-data interval contract.

The Windows registration script checks for the daylight-saving-aware Windows
zone `AUS Eastern Standard Time`, because `schtasks /ST` uses the machine's
timezone. It does not change the machine timezone automatically. `tzdata` is
now a Windows-only dependency, and the launcher verifies Sydney zone data is
available before deciding the environment needs no installation.

Covered test cases include:

| UTC instant | Sydney local time | Selected report month |
|---|---|---|
| 2026-09-30 16:00Z | Oct 1, 02:00 AEST | September 2026 |
| 2026-10-31 15:00Z | Nov 1, 02:00 AEDT | October 2026 |
| 2026-03-31 15:00Z | Apr 1, 02:00 AEDT | March 2026 |
| 2026-04-30 16:00Z | May 1, 02:00 AEST | April 2026 |
| 2026-04-04 15:59Z / 16:00Z | Apr 5, 02:59 AEDT / 02:00 AEST | March 2026 |
| 2026-10-03 15:59Z / 16:00Z | Oct 4, 01:59 AEST / 03:00 AEDT | September 2026 |
| 2026-12-31 15:00Z | Jan 1, 02:00 AEDT (2027) | December 2026 |

Additional near-midnight tests catch incorrect fixed +10/+11 offsets. A frozen
UTC-host CLI test specifically verifies that Sep 30 16:00Z selects September,
not August.

## P2 — Windows execution evidence

**UNVERIFIED DEPLOYMENT DEPENDENCY**, not a confirmed Windows bug.
The available host is macOS (`Darwin`), with no `pwsh`, `powershell.exe` or
`schtasks.exe`. Neither task registration nor an actual scheduled Windows
execution was claimed.

`docs/WINDOWS_MONTHLY_TASK_VERIFICATION.md` contains the manual acceptance
procedure. It captures task XML/configuration, account/timezone, actual start
time, terminal state/LastTaskResult, fresh application log and persisted report
ID. The existing runner is PowerShell; there is no monthly `.cmd` wrapper.

The Python batch runner **was** executed on macOS with an isolated seeded DB:

```sh
LLM_BASE_URL='' LLM_API_KEY='' LLM_MODEL='' \
  /tmp/rentmate-test-venv/bin/python -m reporting.monthly_runner \
  --database /tmp/rentmate-final-audit-HA0W2X/monthly.db \
  --log ../output/verification/monthly-run-macos.jsonl
```

Actual exit code: **0**. Execution: `2026-10-29T12:35:45.806551Z` to
`2026-10-29T12:35:45.811481Z`. Period: `2026-09`; timezone: Australia/Sydney;
two properties succeeded, zero failed. Read-only DB inspection confirmed:

- `6232a5ad-dd6d-4c75-b52c-9c6676b5ac17` — demo-property, September 2026;
- `f7745170-9819-4fb9-81c9-ad183eff5ffa` — demo-property-2, September 2026.

Both are `partial_success` because no live narrator was configured for this
isolated run. This proves Python runner execution and persistence, **not**
Windows Task Scheduler operation.

## Real API configuration interface

Open `/app` → **AI settings**. Enter the separate local operations token,
API base address, API key and model. Apply, test and restore controls call
real backend routes under `/api/ops/llm/`.

- Configuration requires both loopback client access and the scheduler Bearer
  credential. Tenant/manager credentials alone do not authorize changes.
- Key input is cleared after actions and on dialog close. There is no browser
  storage, secret readback or credential file write. Status returns only a
  key-configured boolean; validation/transport errors are sanitized.
- Changing the destination requires re-entering the key, preventing accidental
  forwarding of an existing credential to a new endpoint.
- All four adapters use a single immutable configuration snapshot per call.
  Evidence also sees updates without restarting its long-lived service.
- Overrides last for this server process. Restarting restores environment
  defaults. Separate Windows jobs require environment/`backend/.env` settings.
- Saving does not claim inference success. The explicit connection test sends
  a harmless greeting through the actual classifier adapter and requires a
  validated model response.

Browser verification used a synthetic key and a closed local port to test
apply, key clearing, failed connection handling, status readback and restore.
No browser-storage entries were created. An operator later applied the Campus
configuration through the shared local server; that configuration was preserved.
The subsequent actual server-side probe at `2026-10-29T12:45:04.168Z` returned
**HTTP 502**, with a sanitized unavailable/invalid-response error. Therefore
**live Campus Qwen inference is not verified**. The response does not establish
whether the underlying cause is connectivity, credentials or model output.
No key was read back or included in evidence. A successful probe is covered
with mocked transport in automated tests, not presented as live inference.

## Final regression results

```sh
PYTHONDONTWRITEBYTECODE=1 /tmp/rentmate-test-venv/bin/python -m pytest backend/tests -q -p no:cacheprovider -k 'notification or communication'
# 79 passed, 377 deselected, 1 warning
PYTHONDONTWRITEBYTECODE=1 /tmp/rentmate-test-venv/bin/python -m pytest backend/tests -q -p no:cacheprovider -k 'reporting or report or collector or monthly'
# 77 passed, 379 deselected, 1 warning
PYTHONDONTWRITEBYTECODE=1 /tmp/rentmate-test-venv/bin/python -m pytest backend/tests -q -p no:cacheprovider
# 456 passed, 1 warning
node --check frontend/app.js
git diff --check
# Both exit 0
```

The single warning is the existing Starlette/httpx TestClient deprecation.
No existing domain assertion was removed or weakened.

The original `/demo` Full System Check ran in Chromium against a separate
fresh database with real HTTP endpoints and no live model. Actual browser DOM:
**13 `.check.pass`**, **0 `.check.fail`**. Screenshot:
`output/playwright/full-system-self-audit-13-pass.png`.

## Incremental changed files for this request

- P0: `backend/notification/dispatcher.py`,
  `backend/tests/test_communication_dispatch.py`.
- P1: `backend/reporting/monthly_runner.py`,
  `backend/tests/test_monthly_runner.py`, `Register-Monthly-Reports.ps1`,
  `Start-RentMate.ps1`, `backend/requirements.txt`, `backend/reporting/README.md`.
- API configuration: `backend/llm/configuration.py`,
  `backend/llm/settings_api.py`, `backend/llm/classifier.py`,
  `backend/llm/classify.py`, `backend/llm/live.py`, `backend/llm/reporting.py`,
  `backend/main.py`, `backend/tests/test_llm_settings_api.py`,
  `frontend/app.html`, `frontend/app.js`.
- Documentation: `frontend/DEMO_GUIDE.md`, `frontend/FRONTEND_API_INVENTORY.md`,
  `frontend/FRONTEND_TEST_RESULTS.md`, this report and the Windows procedure.
- Evidence: `output/verification/monthly-run-macos.jsonl`,
  `output/verification/real-api-probe.json`,
  `output/playwright/ai-settings-no-secret.png`,
  `output/playwright/full-system-self-audit-13-pass.png`.

Other pre-existing business-frontend changes remain uncommitted and were not
discarded, merged or pushed.
