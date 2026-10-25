# PR #8 Integration Progress

Dates: 2026-10-24 to 2026-10-25 (Asia/Shanghai). User authorization was extended in stages: read-only inspection, local repair, local Git integration, normal push, event integration, and event delivery. Later authorization superseded earlier stop points. No reset, force push, branch switch, or PR merge was performed.

## Evidence integration changes

- Migrated review-router tests to real Bearer resolution using server-side test sessions and manager_property checks. Tenant resident bindings support document reads; independent managers perform reviews. Response and independent database assertions verify reviewed_by as manager_id.
- Preserved classification outcomes, review history, idempotency, rejection codes, and no-write assertions. Added unknown-token, tenant-review denial, forged-header, and cross-property existence-hiding regressions.
- Updated review-service tests for manager bindings while retaining independent persistence and cross-tenant manager cases.
- Added optional keyword-only classifier injection to main.create_app. Production defaults, dotenv loading, Maintenance/Review, health, and rent-alert routes remain intact.
- Explicitly injected test classifiers in upload, extraction, and query tests. The privacy regression uses a configuration-failure double while retaining persistence and internal-detail non-disclosure checks.
- Fixed Evidence seed foreign-key ordering: parent identities are created before bindings within one transaction. Existing identities and conflicting bindings are never overwritten or escalated. No credentials are stored.
- Reused shared SQLite migrations before schema indexes. Legacy due_date values remain unchanged; unknown dates remain NULL, and rent status is not recomputed.
- Added legacy-upgrade, seed-preservation, and cross-thread repository tests. Existing snapshot connections are closed and thread exceptions asserted.
- Isolated main import during test collection with temporary database/storage paths, disabled dotenv, replaced model/session configuration, and blocked external connections.
- Updated demo instructions for separate tenant/manager identities, shared LLM_TIMEOUT_SECONDS, honest acceptance/delivery boundaries, and incomplete feature scope.

## Initial validation environment

The requested backend/.venv/Scripts/python.exe could not start because its referenced Python 3.11 installation was missing. The environment configuration was not rewritten. Validation used independent Codex Python 3.12.14 and requirements installed in the ignored backend/.venv/integration-test-deps directory.

From backend, with PYTHONDONTWRITEBYTECODE=1 and PYTHONPATH pointing to that directory:

```text
<independent-python> -m pytest -q -p no:cacheprovider --tb=short
```

- Initial related suite: 87 passed, 1 warning (upload 19, extract 12, review_flag_queries 10, review_router 16, review_service 12, seed 8, legacy_upgrade 10).
- Initial full run: 253 passed, 1 failed. A success stub changed a configuration-failure privacy test's setup. An explicit failure double, including its source field, corrected the setup without relaxing assertions.
- Final pre-fetch full suite: 254 passed, 1 warning, 30.86s; no failures or skips.
- Working-tree/staged whitespace checks and new-file checks passed. LF/CRLF notices were not whitespace failures.
- The sole test warning concerns deprecated Starlette/httpx TestClient integration. Dependency constraints were not changed for it.

All upgrades were verified using temporary databases. No real rentmate.db, sensitive .env contents, credentials, or live Qwen endpoint was used.

## Git integration and delivery history

Branch: jenny/scrum-8-document-upload.

1. Original branch HEAD: cfe3a18e06bf0ac485b58d55b3a3feb8a82581e7. Pending MERGE_HEAD: 9b4b32e6f61883678c228fe239071479c4d3fd8e.
2. Original conflict files were backend/main.py and backend/tests/test_contracts_smoke.py. App wiring retained existing routes and added Evidence. The smoke test matched the incoming main version; the schema preserved main tables and added Evidence. No conflict entries or markers remained.
3. Created local merge commit 1fa1eace71e91fae0cb6eb856dc9f5e9ffc307e8 with those two original parents.
4. Fetched origin. Latest main was 6c8b0fd4b47c920cf99b5cf099115e8ba1aef9c6 (PR #12 merge), not yet contained locally. Merged it with no conflicts. All 13 incoming files matched main exactly, including Maintenance reporting, HTTP idempotency, demo seed, and regressions. Evidence permissions, wiring, and migrations were preserved.
5. Full backend validation after latest main: 260 passed, 1 warning, 33.73s. Created merge commit 2a5d894939ea90260f4b93abb8a58be712480070 with parents 1fa1eace71e91fae0cb6eb856dc9f5e9ffc307e8 and 6c8b0fd4b47c920cf99b5cf099115e8ba1aef9c6.
6. Pushed 2a5d894 normally to the existing branch. GitHub PR-editing tools were unavailable; the PR description and review-request text were supplied separately. No PR update was claimed.

No tracked database, upload, credential, or binary artifact was included. The .env.example template had empty keys and an empty session map. Existing Maintenance verification documents were unchanged from incoming main and retained as upstream documentation.

Local backend/evidence-test-result.txt, backend/evidence-upload-result.txt, and docs/pr8_integration_task.md were retained untracked and excluded from commits.

## Maintenance / Evidence event integration

The remote branch was verified at 2a5d894939ea90260f4b93abb8a58be712480070 before this work started.

Implemented real reference validation, event reception, atomic receipts/timeline/document links, persistent deduplication, and receipt queries. Maintenance confirmation/closure now validates real saved references within its existing state transaction. MaintenanceEvent remains unchanged, and actor never supplies authorization identity.

The [integration contract](evidence_maintenance_integration.md) records callable/HTTP interfaces, failure and timeout semantics, and validation. Reviewer messages are supplied separately rather than stored in repository documentation. This supersedes the earlier local implementation gap for Maintenance callbacks and timelines; joint review and deployment wiring remain pending.

- Related regressions: 67 passed, 1 warning.
- Full backend suite: 293 passed, 1 warning, 47.88s.
- Delivery rerun: 293 passed, 1 warning, 48.37s with independent Python 3.12.14.
- Working-tree and staged whitespace checks passed.
- Delivery commit: 275c718a10ccdb45ba0fd3fb0e5749907fd2af5f, normally pushed to the existing branch after reviewing 18 delivery files.
- No features were added during delivery review; no PR merge or force push occurred.

The remaining scope includes joint integration validation, deployment adapter wiring, lease association, the general frontend timeline, and production login. Local regression results are not evidence of joint deployment completion.

## English-only documentation correction (2026-10-25)

Chinese explanatory text had been included in the pushed integration documentation, contrary to the assignment's English-only requirement. Tracked documents, headings, and Reporting explanatory docstrings have been translated into English. No application behavior, interfaces, identifiers, or test assertions are changed. Historical Git commits are not rewritten. The user authorized committing this correction together with the Windows launcher dependency fix; pushing this follow-up commit is not included in that instruction.

## Windows launcher dependency correction (2026-10-25)

Start-Maintenance.ps1 installs backend/maintenance/requirements-tested.txt. That pinned snapshot omitted pypdf and python-multipart even though Evidence requires both. Added pypdf==5.9.0 and python-multipart==0.0.32, within the existing backend/requirements.txt constraints, without changing launcher behavior or adding automatic background dispatch.

The complete pinned snapshot was installed into the ignored backend/.venv/launcher-check-deps directory. Validation used Python 3.12.14 with -S and explicitly added only that dependency directory, excluding global site-packages. Imports resolved pypdf 5.9.0, python-multipart 0.0.32, and the launcher's pinned FastAPI 0.161.1. The startup script and its credential-loading/online-verification paths were not executed. Test configuration continued to isolate databases, uploads, dotenv, and external model connections.

Final full-suite result in the pinned launcher environment: **293 passed, 2 warnings, 48.78s**. One warning is the existing Starlette/httpx deprecation; the other is an anyio assertion-rewrite warning because the import smoke check loaded FastAPI before pytest initialization. No tests failed or were skipped. Working-tree and staged whitespace checks passed. The English-only scan found no Chinese characters in tracked text; executable AST comparisons confirmed that Reporting changes affect docstrings only. Existing local test logs and task instructions remain untracked and excluded from the combined commit.
