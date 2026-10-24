# Kerwin's maintenance demo / 演示操作稿

## Before demonstration

1. Start USYD VPN and verify the course proxy is reachable.
2. Double-click root Start-Maintenance.cmd. Keep the terminal open.
3. Open http://127.0.0.1:8020/api/maintenance/demo.
4. Copy the tenant, Manager 1 and Manager 2 tokens from that terminal. They are
   random local demo sessions, not the model API key. The launcher also prints
   a deliberately unbound token for the expected-403 demonstration. Do not
   display the real model key.
5. Use synthetic issue descriptions and uploaded synthetic documents only.
   References must now be actual Evidence document IDs in the same property.

## A. Happy path, approximately 3 minutes (suggested, not course time limit)

Tenant token -> issue "Loose cupboard handle"; description
"The kitchen cupboard handle is loose but the cupboard works. No injury or immediate hazard."
Submit. Expect registered -> assessed, normally routine. Inspect history for
source=llm, rationale, source message and timestamp. Model choices can vary;
if it requests review, explain and follow the manual-review path.

Switch to the Manager 1 token -> Load requests. Select the issue. Each public
HTTP submission receives fresh server-generated message/event IDs and creates a
new request; the internal maintenance contract is idempotent only when its
trusted caller retries with the same source IDs.

Keep a meaningful decision reason. Choose approved -> Record status change;
then in_progress. Attempt awaiting_confirmation with empty evidence: expect
rejection. Upload a synthetic completion document as the tenant through
`POST /api/evidence/documents` in Swagger and copy its returned document_id.
Use that document ID as the evidence reference ->
awaiting_confirmation -> closed. Each successful command adds one version/history
entry. Closure is a manager confirmation, not a model decision.

View maintenance summaries and the handoff queue. These are maintenance-owned
HTTP views, not the UC7 Reporting router. Explain pending events honestly:
until consumers are integrated, no claim is made that a tenant was notified.

English speaking notes:

"My module owns the maintenance request lifecycle. The model recommends a
priority and provides a rationale, while deterministic rules and manager decisions
control state changes. Trusted internal retries reuse source IDs and return the
same request; a new public HTTP POST is a new submission. Manager
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
A: Evidence validation/receipt/timeline now have local integration tests, but
Kerwin's joint review and trusted adapter deployment wiring remain pending.
Communication delivery, shared login, Reporting consumption and the group frontend
remain separate group work. Distinguish local tests from deployed integration.

## D. Seeded manager-review and scope demonstration

The launcher seeds two independent properties and review drafts:

| Account | Property | Seeded draft |
|---|---|---|
| `demo-manager` | `demo-property` | `demo-review-draft` |
| `demo-manager-2` | `demo-property-2` | `demo-review-draft-2` |

Use the random tokens printed by the launcher (PowerShell examples):

```powershell
$headers = @{ Authorization = "Bearer $manager1Token" }
Invoke-RestMethod http://127.0.0.1:8020/api/reviews -Headers $headers

$body = @{
  expected_version = 1
  decision = 'approved'
  final_content = 'The manager approved this exact response.'
} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8020/api/reviews/demo-review-draft/decision `
  -Method Post -ContentType 'application/json' -Headers $headers -Body $body
```

Manager 1 lists only `demo-review-draft`; Manager 2 lists only
`demo-review-draft-2`. A direct lookup of the other property's draft returns
404 so its existence is not disclosed. To demonstrate default-deny binding,
call `GET /api/reviews` with the printed forbidden-scope test token; it maps
`demo-manager` to `demo-property-2`, where no binding exists, and returns 403.

`accepted`, `pending` or `handed_off` records describe queue/handoff state only;
they do not prove delivery. Communication, Notification, Lease/Rent and UC7
Reporting currently have backend/test seams but no mounted HTTP routers. The
only Lease/Rent HTTP surface is the explicit 503 ops stub.

## Deployment rehearsal

Extract the code archive to a new directory. Install the pinned tested dependency
snapshot in a fresh environment. Run all tests without any personal .env file.
Supply runtime credentials/VPN separately, then run the live acceptance script.
This is a rehearsal, not evidence of a Canvas upload/download or tutor assessment.
