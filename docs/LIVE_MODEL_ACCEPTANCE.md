# Live Campus Qwen business-workflow acceptance

Verified on 2026-10-29, 12:54–12:57 UTC, against the running local application
at `http://127.0.0.1:8024` with the operator-configured `campus-qwen` connection.
All model requests used the actual configured API. No response mocks, API key
readback, credential changes or notification dispatch were performed.

This is a later successful verification. The earlier HTTP 502 probe in
`output/verification/real-api-probe.json` is retained as historical evidence;
it no longer describes the current tested connection.

| Workflow | Real endpoint / persisted evidence | Result |
|---|---|---|
| Communication lease question | POST `/api/communication/messages`; message type `lease_rent`; draft pending review; delivery not_dispatched | PASS |
| Routine Maintenance | POST `/api/maintenance/requests`; GET exact request; assessed, routine, emergency=false; history source=llm, confidence 0.95 | PASS |
| Invoice classification | Real browser multipart upload of fictional `demo/samples/test-invoice.pdf`; original accepted; persisted invoice classification source=llm, model=campus-qwen, confidence 0.99 | PASS |
| Monthly report | POST `/api/manager/monthly-reports/generate`; complete; narrative.source=live with non-empty English text; same-key retry reused exact report ID; GET readback 200 | PASS |
| Emergency review safeguard | Gas-smell Communication intake → actual linked request assessed/emergency with source=llm, confidence 0.98; draft pending_review; manager alert accepted (queued); no tenant delivery | PASS |

## Exact persisted records

- Lease-question message: `b077f00b-addc-413c-acb2-a7caf09402e9`.
- Its pending draft: `d52a61c4-9362-4296-ab16-cc1f4e7c4e35`.
- Routine request: `3dc3653a-18c5-4e7a-8b24-fd7f3c09caf1` (version 2).
- Invoice document: `170fdd47-5076-4728-82dc-936a87398268`.
- October report: `425bedf4-41a9-4c08-a3ce-e7de656b0101`.
- Gas-smell message: `2df3c9ef-9e0d-497e-b8b4-d89cf94decd4`.
- Its request: `dc336fce-b374-4b29-ae45-ff29311c0b32`.
- Its pending draft: `421a39c9-bd12-4f2e-a8de-c2acc35482f8`.

The report's first generation returned `reused=false`; repeating the same
idempotency key returned `reused=true` and the same report ID. The report was
also independently read back through its manager GET endpoint. Its data was
read before the subsequent gas-smell case, so that later request is correctly
absent from this already-stored generation.

The Communication response does not expose its raw classifier result or a
per-message provider label. Category evidence comes from the exact persisted
message row, with the configured Qwen adapter active. Maintenance and Evidence
persist explicit `llm` source markers; Reporting persists `live`. These markers
are reported using their actual contract values.

The two tested message IDs were checked against notification events and tenant
inbox records: the gas alert is only `accepted`, and there are **zero** tenant
inbox rows for those messages. Pending review has not been represented as an
approval or as delivery. The test did not invoke the dispatcher.

## Viewing the results in the portal

- Tenant Maintenance: open the request titled **LIVE ACCEPTANCE: Loose cupboard
  handle**. Its assessment history contains source=llm.
- Documents: open the latest `test-invoice.pdf`, matching the exact document ID
  above. The model classified it as invoice; human final-category review remains
  a separate operation.
- Manager Reports: choose **2026-10**, load history and open the report ID above.
  It shows complete structured data and a separately labelled live narrative.
- Manager Review Centre: the lease-query and emergency drafts remain pending.

## Limits and retained state

These are representative live acceptance cases, not proof of every model
output, failure branch or user workflow. Notification delivery was not exercised
live in this run; the earlier backend and browser dispatch tests remain separate
evidence. Windows Task Scheduler is still an unverified deployment dependency.
The applied API configuration remains in the current server process; separate
scheduled processes need environment/`.env` configuration.

Test records have been retained for inspection. No application code was changed,
and no commit, push or merge was performed for this live verification.
Machine-readable evidence: `output/verification/live-domain-acceptance.json`.
