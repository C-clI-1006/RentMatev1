# Verification record - 24 October 2026, Australia/Sydney

## Automated and deployment checks

* 35 tests passed in the current checkout.
* 35 tests passed after ZIP extraction to a new directory, a new Python 3.12
  virtual environment, and installation of requirements-tested.txt.
* The Windows Start-Maintenance.ps1 -Verify wrapper also completed successfully.
* Ruff passed for the maintenance implementation, live adapter, main wiring
  and added tests. git diff --check passed.
* One third-party Starlette/httpx deprecation warning remains; tests pass.

## Real model acceptance

See live-api-acceptance.json for generated results from the final verification
wrapper. Real campus-qwen was used for synthetic routine, urgent, emergency
and insufficient-information cases. The first three persisted source=llm;
the last entered under_review with unknown priority. All expected cases passed.
The same run verified duplicate source reuse, manager-controlled full lifecycle,
persisted history, reporting summary, queued manager alert, and pending delivery.
These representative checks are not statistical accuracy or latency guarantees.

## Actual clean HTTP deployment

An independent uvicorn server ran from extracted code and its clean environment
on localhost:8021. GET /health and /api/maintenance/demo returned 200.
A synthetic HTTP request invoked real Qwen and persisted a routine assessment
with source=llm. See clean-http-acceptance.json.

## Browser exercise

The live browser opened the clean deployment demo, entered its temporary local
tenant session and submitted "Browser demo loose cupboard handle". It showed
registered/assessed, routine, v2, with source=llm in the displayed history.
After switching to a manager session, the browser recorded approved v3,
in_progress v4, awaiting_confirmation v5 with a synthetic evidence reference,
and closed v6. A screenshot was saved with the handover deliverables.
No real tenant, address, contract or uploaded evidence was used.

## Boundaries

No actual Communication/Evidence consumer delivery, shared-login integration,
React integration, Jira operation, Canvas submission/deployment or joint-owner
signoff is represented by these checks. The emergency alert was QUEUED, not
verified as delivered. Evidence references in these exercises are synthetic;
actual evidence validity and ownership remain a group integration check.
All developer test servers are stopped after verification.
