# Final integration PR notes (draft — update after remote merges)

Do not post this unchanged until the remote merge order is known.

## Candidate composition

- Base candidate: Reporting PR11 head `58da3d7`.
- Evidence/Maintenance candidate: PR8 head `275c718`.
- Isolated UC4/UC5 scheduled-draft change transplanted from `fee454b`.
- Integration/test commits: `2904f83`, `127bb96`, `1d55a50`, `aa96905`.
- Approved-delivery implementation: `e076845`.
- Executable integrated demo: `0c8fa3b`.
- Timestamp-only delivery replay correction: `c6f38eb`.

## Conflicts resolved

- Preserved every router in `backend/main.py` instead of allowing one branch to
  replace Maintenance, Review, Reporting or Evidence routes.
- Reconciled the two historical schema-v3 assignments and added convergent
  migrations through schema v6.
- Resolved scheduled rent-reminder recipient scope through
  `rent_record → lease → tenant`.

## Verification

- Full local candidate before the final replay-only correction: 363 passed,
  1 existing Starlette/httpx deprecation warning, 0 failures.
- Replay correction targeted suite: 8 passed, same warning.
- A final full run is mandatory after rebasing on the actual merged `main`.

## Reviewer focus

- Exact approved-version delivery and rejection-no-send invariant.
- Tenant/property and manager/property scope rechecks.
- Stable delivery key/idempotent retry behaviour.
- Schema v3/v4/v5 legacy convergence into v6.
- Evidence/Maintenance event receipt and router preservation.
- Demo claims versus production boundaries.
