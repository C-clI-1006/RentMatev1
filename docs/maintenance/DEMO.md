# Kerwin's maintenance demo / 演示操作稿

## Before demonstration

1. Start USYD VPN and verify the course proxy is reachable.
2. Double-click root Start-Maintenance.cmd. Keep the terminal open.
3. Open http://127.0.0.1:8020/api/maintenance/demo.
4. Copy tenant and manager tokens from that terminal. They are random local
   demo sessions, not the model API key. Do not display the real model key.
5. Use synthetic issue descriptions and evidence references only. References
   are illustrative unless actual evidence verification is integrated.

## A. Happy path, approximately 3 minutes (suggested, not course time limit)

Tenant token -> issue "Loose cupboard handle"; description
"The kitchen cupboard handle is loose but the cupboard works. No injury or immediate hazard."
Submit. Expect registered -> assessed, normally routine. Inspect history for
source=llm, rationale, source message and timestamp. Model choices can vary;
if it requests review, explain and follow the manual-review path.

Click Replay same submission. The same request ID must appear, with no second
logical request. Switch to manager token -> Load requests. Select the issue.

Keep a meaningful decision reason. Choose approved -> Record status change;
then in_progress. Attempt awaiting_confirmation with empty evidence: expect
rejection. Enter synthetic evidence reference demo-completion-001 ->
awaiting_confirmation -> closed. Each successful command adds one version/history
entry. Closure is a manager confirmation, not a model decision.

View reporting summaries and handoff queue. Explain pending events honestly:
until consumers are integrated, no claim is made that a tenant was notified.

English speaking notes:

"My module owns the maintenance request lifecycle. The model recommends a
priority and provides a rationale, while deterministic rules and manager decisions
control state changes. Duplicate source messages return the same request. Manager
commands check the version, so a stale update cannot overwrite newer work. State,
history and handoff records are committed together. Closing requires a manager
action and completion evidence references. Notification acceptance is separate
from actual delivery."

## B. Emergency and uncertainty, approximately 2 minutes

Emergency example: "Gas leak" / "There is a strong gas smell and hissing from the
kitchen gas pipe." Expect emergency=true; manager_alert queued before inference.
Explain that a real alert consumer must still be integrated for actual delivery.
No contractor booking or spending is authorised by the AI.

Uncertain example: "Something wrong" / "Something is wrong. I cannot explain
what or where." Expect under_review, priority=null. Switch to manager session,
record a manual priority and rationale, then separately approve the action.
Reject/cancel another active case with a reason and show the retained history.

## C. Questions to practise

Q: Why not let the model update request status?
A: Model output can be wrong or late; validation, version checks and manager
authorisation control persistence. A late callback cannot reopen a cancelled case.

Q: Why use an outbox?
A: Persisting state and its handoff entries in one transaction avoids losing a
notification request after a successful business update. Consumers still need
deduplication and truthful receipt/delivery reporting.

Q: Does this guarantee exactly-once external sending?
A: No. Stable keys, consumer deduplication and receipt reconciliation provide a
safe integration design; actual sending depends on the downstream provider.

Q: What happens if the model/VPN fails?
A: The issue is retained for manual assessment. No default priority or approval
is invented. The automated adapter tests cover timeout and malformed output.

Q: Are priority categories statutory response times?
A: No, they are configurable operational categories from our Stage 1 design.

Q: What is still group integration work?
A: Real evidence verification, Communication/Evidence consumers, shared login,
Reporting consumption and the group frontend. Distinguish each from local tests.

## Deployment rehearsal

Extract the code archive to a new directory. Install the pinned tested dependency
snapshot in a fresh environment. Run all tests without any personal .env file.
Supply runtime credentials/VPN separately, then run the live acceptance script.
This is a rehearsal, not evidence of a Canvas upload/download or tutor assessment.
