# RentMate Stage 2 — Chaney progress

Branch: `chaney/uc5-draft-source-and-ownership` (clean tree, 10 unpushed local commits; no push to main, no auto-merge)

Test command: `cd backend && .venv/bin/python -m pytest tests -q`
Baseline: 85 passed (verified 2026-10-23)
Phase 1 done: 96 passed (85 baseline + 11 permissions tests)

## Completed
- Verified repo state: HEAD `ab91222` (merge lease-rent) → `8d20bcb` (merge sharon/reporting) → `9afb991` (draft review_reason + source_facts)
- Jira: NOT connected (no code-mode tool, no CLI). Continuing without Jira; not claiming Jira verification.

## Phase 1 — manager permissions (DONE, pending commit)
- `db/schema.sql`: `manager(manager_id PK, name, created_at)` + `manager_property(manager_id FK→manager, property_id FK→property, created_at, PK(manager_id, property_id))` + `idx_manager_property_property`.
- `backend/shared/sqlite.py`: `SCHEMA_VERSION = 2`; idempotent v2 migration step (`_MANAGER_TABLE_DDL`, mirrors schema.sql) applied by `init_connection` via `PRAGMA user_version` chain; raises if DB newer than supported. Pre-existing v1 DBs get the tables on next connect.
- `backend/permissions/` (new package, owner: Chaney):
  - `repository.py`: `SQLiteManagerRepository` — `manager_exists`, `get_manager_name`, `list_property_ids` → `Sequence[ManagerPropertyRecord]`, `is_manager_of_property`; read-only (SELECTs only).
  - `service.py`:
    - `manager_id_from_auth(auth: AuthContext) -> ManagerId` — entry-point identity extraction; raises `MissingManagerIdentityError` if `auth.manager_id` is None/empty. Identity is NEVER taken from request body/header.
    - `list_manager_properties(manager_id, repository) -> Sequence[ManagerPropertyRecord]` — empty = no bindings (default deny).
    - `require_manager_property_access(manager_id, property_id, repository) -> ManagerId` — raises `ManagerAccessDeniedError(PermissionError)` on any denial (no binding / other manager / unknown manager).
- `backend/contracts`: `ManagerId = NewType("ManagerId", str)` added to `common.py`, exported from `contracts/__init__.py` — **contracts package is DRAFT; this is a proposal, not team-approved** (change process: PR touching only backend/contracts/ reviewed by affected owners).
- Tests (`backend/tests/test_permissions.py`, 11): identity from AuthContext only (raises on None/""); legit pass; no-binding deny; cross-manager deny; multi-property manager; unknown-manager deny; empty list for unbound manager; existing tenant flow unaffected; migration: v1 DB + `init_connection` ⇒ manager tables present, `user_version = 2`, idempotent re-connect.
- Demo data: `db/demo_data.sql` (new; no prior seed convention) — idempotent, 2 tenants, 2 properties, 2 managers each bound to a different property, 1 resident binding. NOT applied by `init_connection` (demo/seed only).
- Boundary: no session-auth layer exists; `AuthContext.manager_id` is populated by the entry point and is the trust boundary. This phase does NOT claim auth is complete.

## Phase 2 — conditional review policy (PENDING)
- Fix "all replies pending_review": fact-based reply without triggers ⇒ auto-send eligible; clarification allowed; emergency ⇒ manager notify + registration continues + substantive replies need approval; unclear/conflict/financial/contract/legal ⇒ manager; rent dunning + lease-expiry tenant reminders ⇒ manager approval; never fabricate registration success; send eligibility = explicit testable rules, LLM cannot grant send; no notification dispatcher yet ⇒ express "auto-send allowed but not yet delivered".

## Phase 3 — wiring (PENDING)
- Communication thin HTTP router in main.py; fix broken `run_due_alerts` import (real: `lease_rent.scheduler.run_overdue_rent_check` / `run_lease_deadline_check`); review + notification/outbox/retry; demo data; startup docs; clean-directory deploy verification.

## Boundaries / notes
- `backend/{notification,review,maintenance,evidence}` are docstring-only stubs — dispatcher/workflow NOT implemented.
- LLM = test doubles only (KeywordClassifier / KeywordPriorityAssessor); single adapter slot in backend/llm/.
- Do not rewrite Kerwin (maintenance), Ziheng (lease_rent), Sharon (reporting), Jenny (evidence) modules.
