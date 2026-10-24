# Jira task drafts and contribution evidence

These are LOCAL task drafts, not actual Jira issues, sprint history, owner
signoffs or evidence that Jira was used. Add them to the real group project
and record dates/status honestly. Do not backdate agile activity.

| Suggested task | Local status | Acceptance criteria |
|---|---|---|
| MC-Register and persist requests | Implemented / locally verified | Source/event replay -> one request; SQLite retains it after restart |
| MC-Priority adapter and manual review | Implemented / live verified | Real Qwen structured assessment; missing/invalid output -> review |
| MC-Manager lifecycle and version checks | Implemented / locally verified | Approval required, stale commands rejected, evidence required to close |
| MC-Atomic history and handoff queue | Implemented / locally verified | Event write failure rolls back state; stable replay keys |
| MC-Communication intake/status integration | Pending joint verification | Chang/Kerwin run success/failure/clarification and approved reply paths |
| MC-Evidence consumer and validation | Pending joint verification | Jenny/Kerwin verify property links, actual refs and deduplicated timeline |
| MC-Reporting integration | Pending joint verification | Sharon/Kerwin agree summary and period semantics |
| MC-Demo and deployment rehearsal | Prepared / see verification results | Clean archive installs and tests; live calls separately verified |

## Genuine iteration evidence available now

* Initial baseline: only /health, contract skeleton, 8 tests passed/1 failed.
* Implemented maintenance-owned persistence, lifecycle, authentication boundary,
  failure-safe model adapter, queue and standalone demo.
* Live acceptance found the original emergency keyword rule overrode a negated
  "no gas smell, smoke" statement. Added explicit-denial/contrast handling and
  regression tests; reran live acceptance with the same issue text.
* Distinguished explicit model needs_review from malformed/network failure.
* Clean-environment/package and test evidence should be attached to actual Jira
  tasks. The evidence is local execution, not retrospective sprint participation.

## Contribution statement draft for Kerwin (review before submitting)

"Implemented the Maintenance Coordinator prototype, including property-scoped
request registration and deduplication, live priority assessment with validated
outputs and manual-review fallback, manager-controlled lifecycle transitions,
optimistic concurrency checks, persistent audit history and maintenance handoff
queues. Added module API endpoints, a standalone demonstration interface,
automated tests, live-model acceptance checks, deployment instructions and
cross-agent integration documentation."

Only retain claims matching the final submitted code and your actual contribution.
Group integration, other owners' work and source submission are not implied.
Include any AI-assistance acknowledgement required by course policy.

Contribution percentages are deliberately not invented. The group must enter
member names/Student IDs and agree percentages independently for all four rubric
rows; each row must total 100%. Signoff and final group numbers remain pending.
