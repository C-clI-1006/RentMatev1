# Maintenance requirements -> implementation -> verification

All paths below are relative to backend. Local automated checks use isolated
temporary SQLite files; live acceptance separately contacts campus-qwen.

| Requirement | Implementation | Evidence / boundary |
|---|---|---|
| FR-MC-01 Register once | service.submit_issue, Store uniqueness per property/source and event | Duplicate, alternate-event/same-source and concurrent intake tests |
| FR-MC-02 Structured priority | llm/live.py, assessment rationale/source in history | Schema validation/uncertainty tests; real Routine/Urgent/Emergency calls |
| FR-MC-03 Emergency and uncertainty | rules.py, pre-inference alert queue, under_review, manual_assessment | Emergency queue + failures + negated hazard tests; actual manager notification awaits consumer |
| FR-MC-04 Authorised lifecycle | TRANSITIONS, change_status, expected_version, manager reason, evidence guards | Full lifecycle, forbidden transitions, stale/concurrent updates, rework and late callbacks |
| FR-MC-05 History and factual updates | mc_history, communication outbox envelope | Atomic history/outbox tests; actual tenant reply review/sending awaits Communication |
| FR-MC-06 Evidence and reporting | evidence outbox, summaries | Scoped summary verified; actual evidence validity/consumer integration pending |
| FR-MC-07 Safe retry | outbox.dispatch_pending, reconcile | definite non-acceptance vs ambiguous timeout tests; no exactly-once transport claim |
| FR-MC-08 Optional trade suggestion | provider_suggestion from validated model output | Live suggestions observed; no booking/payment operation; not required for core lifecycle |
| NFR-MC-01 Scoped access | trusted AuthContext, Bearer prototype map, scoped load/list | Tenant/property denial and API body spoof tests; shared production login not supplied |
| NFR-MC-02 Audit history | immutable-by-API mc_history, actor/time/source/reason/evidence | Persistence after Store restart and complete lifecycle history; raw DB operator can edit SQLite |
| NFR-MC-03 Reliability | SQLite transactions, unique keys, version checks, callback guard | simultaneous intake/manager commands, rollback injection, late-result cancellation tests |
| NFR-MC-04 Human authority | no model approval; explicit manager commands | skipped approval/evidence rejection, malformed/timeout output -> review |

## Technology rationale (for Stage 2 explanation)

* FastAPI provides typed endpoints and OpenAPI integration contracts for the
  group frontend. Its presence alone does not establish a rubric score.
* Pydantic validates model suggestions before business code consumes them.
* SQLite transactions plus an outbox make the local state/history/event boundary
  reproducible without requiring five network microservices.
* A real OpenAI-compatible Qwen adapter is separated from business logic and
  labelled test substitutes; runtime evidence identifies which was used.
* Version checks and guarded state transitions preserve manager authority under
  concurrent commands and slow model replies.

The English emergency indicator rules support a narrow set of explicit denials
and contrast clauses. They are not a general negation/NLP guarantee; emergency
classification and manual review policy still need evaluation on realistic data.
Confidence is an uncertainty signal rather than a calibrated probability.

## Reproduce

Automated: `python -m pytest -q` from backend.
Live: `python -m maintenance.verify_live --output path/to/live-result.json` with
the course LLM environment variables configured and VPN connected. The live
script uses synthetic API data and a temporary DB, and never sends real messages.
