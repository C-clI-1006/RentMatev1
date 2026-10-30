# RentMate business demonstration

## Start on Windows

From the repository root, after installing Python 3.11+:

```powershell
.\Start-RentMate.cmd
```

Or:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Start-RentMate.ps1
```

Open `http://127.0.0.1:8020/app`. The existing launcher creates the local
environment, installs dependencies when needed, seeds the demo DB and starts
FastAPI. No frontend npm install or build is required. `/demo` still offers
the technical Full System Check. Use an isolated database for that check if
you have already approved/rejected the seeded reminder drafts.

## Start on macOS / Linux

The one-command launcher now handles local setup and demo identities:

```sh
bash Start-RentMate.sh
# Preserve prepared decisions instead of re-seeding:
bash Start-RentMate.sh --no-seed
# Execute the scheduled monthly batch immediately, without a server:
bash Start-RentMate.sh --monthly-run
```

On macOS you may also double-click `Start-RentMate.command`. The default
business URL is `http://127.0.0.1:8020/app`; `--port 8021` changes the port.
`--monthly-run` does not seed/reset demo records and does not register an OS task.

For manual setup instead of the launcher:

From the repository root (prototype identities only):

```sh
python3 -m venv .runtime/.venv
.runtime/.venv/bin/python -m pip install -r backend/requirements.txt
cd backend
export DATABASE_PATH="$PWD/../.runtime/rentmate-business.db"
export EVIDENCE_STORAGE_ROOT="$PWD/../.runtime/evidence"
export RENTMATE_SCHEDULER_TOKEN='scheduler-demo'
export RENTMATE_AUTH_TOKENS='{"tenant-demo":{"tenant_id":"demo-tenant","property_id":"demo-property"},"manager-demo":{"tenant_id":"demo-tenant","property_id":"demo-property","manager_id":"demo-manager"},"manager-other-demo":{"tenant_id":"demo-tenant-2","property_id":"demo-property-2","manager_id":"demo-manager-2"}}'
export RENTMATE_EVIDENCE_DISPATCH_MANAGERS='{"demo-property":"demo-manager","demo-property-2":"demo-manager-2"}'
../.runtime/.venv/bin/python -m scripts.seed_integrated_demo --database "$DATABASE_PATH"
../.runtime/.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8020
```

The server loads `backend/.env` for existing Campus Qwen settings. Set
`LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` using your authorized local
configuration if demonstrating live inference. Enter a key only in the
protected AI settings form or your local environment, never in a screenshot,
commit, chat message or presentation. With no LLM configured,
Communication uses the labelled keyword fallback; Maintenance asks for manual
assessment, Evidence saves originals with a failed classification state, and
Reporting preserves structure with missing narrative. None is shown as live Qwen.

## Configure a real API from the interface

Open **AI settings** in the top bar. Enter the separate local operations token
(`scheduler-demo` for the supplied local launcher), then **Load current settings**.
Enter your authorized API base address (including `/v1` when required), model
name and API key, then **Apply settings**. **Test model connection** makes an
actual API call with a harmless greeting; a saved configuration alone does not
prove inference works. The raw key is never read back and is cleared from the
input after an action. It is not written to browser storage or a repository file.

All four LLM adapters use one configuration snapshot per call, including the
Evidence service that was created at server startup. Settings are held only in
the running server's memory; restarting returns to environment/`.env` defaults.
**Restore startup settings** discards the override. Separate Windows monthly
task processes use their environment or `backend/.env`, not the browser override.
The settings API requires both a loopback client and scheduler authorization;
an ordinary tenant or manager token cannot change model credentials.

## Connect tenant and manager sessions

Select **Session settings**, choose a portal and enter the matching property
and token. These values are local prototype fixtures, not production accounts:

| Portal | Property | Token |
|---|---|---|
| Tenant | demo-property | tenant-demo |
| Manager | demo-property | manager-demo |
| Other property's manager | demo-property-2 | manager-other-demo |

For explicit Local operations actions, expand the optional scheduler field
and enter `scheduler-demo`. A manager or tenant token cannot impersonate it.
Switching sessions clears prior session receipts and selected records to avoid
showing another session's data. Open the exact saved record after switching.
Keep request/document IDs in presenter notes or use the UI's record lists.

## Final presentation sequence (approximately 10–15 minutes)

1. **Tenant / Dashboard:** connect and explain that counts come from authorized
   API reads. Property profile and ledger data have no general read API.
2. **Messages:** submit `There is a strong smell of gas in my kitchen.`.
   Show real message ID, `pending_review` and `not_dispatched`. The exact
   Maintenance link is found only by matching persisted `message_id`.
   Classification is explicitly unavailable because the current response
   doesn't expose it. A Maintenance emergency flag, when available, is labelled
   as coming from that linked record. Do not promise emergency response.
3. **Documents:** upload `demo/samples/test-invoice.pdf`, open its saved record
   and copy the document ID. Separate upload state from classification state.
4. **Maintenance:** create a routine request, entering title, description,
   location and the existing document ID. Open the record and retain its ID.
5. **Manager / Maintenance:** switch sessions, open that exact request, inspect
   status/version/history. If under review, record a manual routine assessment
   with a rationale. Advance `assessed → approved → in_progress →
   awaiting_confirmation → closed`, entering a reason each time and passing the
   uploaded document ID when closing. Every command uses the currently read
   version. Invalid transitions, stale versions or invalid evidence are surfaced
   as errors. Refresh and reopen the request before retrying a stale version.
6. **Activity Timeline:** select the request. Show Maintenance event IDs and the
   separate Evidence timeline. The configured background worker consumes the
   Evidence outbox; refresh if it hasn't handed off yet. Every visible event
   comes from persisted records. There is no global cross-agent event API.
7. **Documents & Evidence:** open the invoice. If the model failed, select
   `select_category` and `invoice`. If there is a valid persisted suggestion,
   `confirm_suggested` is also available. This review is separate from approving
   a tenant reply. Inspect human final category, manager and timestamp.
8. **Lease & Rent:** explicitly run the authorized rent/lease scan. Inspect its
   real created/reused counts. A rerun should reuse the same reminder sources.
   No browser financial calculations or invented ledger are involved.
9. **Review Centre:** locate a rent reminder via source facts / technical
   details. Edit its final content and approve the exact version. Reject the
   lease reminder with a reason. Rejected drafts do not dispatch. Approved
   drafts leave the pending queue and have only been queued, not delivered.
10. **Local operations:** run Maintenance handoff, then Notification dispatch.
    Inspect actual processed/delivered/retry/failed counts. Inspect any new
    maintenance-status drafts in Review Centre if the backend requires review.
11. **Tenant / Notifications:** switch back and show the actual delivered,
    manager-edited rent reply and timestamp. This is delivery to a local inbox,
    not email/SMS. The rejected lease reminder must not appear.
12. **Manager / Reports:** choose a reporting month and Generate. Inspect the
    structured sections, outcome and data-read time. Narrative is separate and
    its source comes from the backend. Return to history and reopen the saved
    report. Generate reuses this session's period key; New generation explicitly
    mints a new key. Reloading or switching sessions clears transient keys.
13. **Guided Workflows:** show the four journeys and record-specific completed
    steps. Pending steps are not auto-completed from unrelated records.
14. **Technical evidence:** open `/demo` on fresh demo data and run the existing
    Full System Check. It must show 13 passed, 0 failed. Swagger is unnecessary
    throughout the business presentation.

## Useful troubleshooting

- 401/403: correct the token and matching property; selecting a role grants no access.
- 409: refresh and reopen the exact record; do not approve a stale draft.
- 422: read the backend validation message (reason/evidence/state may be invalid).
- Upload accepted + classification failed: original saved; use manager classification review.
- Missing narrative: structured report remains usable; do not label it Qwen-generated.
- A queued draft is absent from inbox: dispatch may be pending, retrying or blocked.
- Repeated deliberate message submissions are separate messages. The UI blocks
  simultaneous duplicate submissions, but Communication has no HTTP idempotency key.
- Four real Campus Qwen business paths subsequently passed; see
  `docs/LIVE_MODEL_ACCEPTANCE.md`. Each team computer still needs its own VPN,
  valid model settings and local rehearsal.
