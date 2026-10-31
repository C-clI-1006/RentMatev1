# RentMate

RentMate is an AI-assisted property management system developed for ELEC5620
Stage 2. The application integrates tenant communication, maintenance management,
lease and rent monitoring, document evidence and monthly reporting within a
shared, property-scoped workflow.

The system combines validated language-model outputs with deterministic business
rules, persistent records and manager approval controls. Its architecture uses
FastAPI, SQLite and a browser-based frontend.

## System requirements

- Python 3.11 or later.
- Windows or macOS for the provided launchers.
- Network access for initial dependency installation and external model requests.
- Node.js for frontend automated tests.

## Installation

Clone the repository:

```sh
git clone https://github.com/C-clI-1006/RentMatev1.git
cd RentMatev1
```

Update an existing checkout with the application stopped:

```sh
git pull --ff-only origin main
```

Source archives are also available through GitHub. Local configuration and runtime
data should be retained separately when replacing an extracted source directory.

## Deployment on macOS

Open Terminal in the repository root:

```sh
bash Start-RentMate.sh
```

Alternatively, double-click `Start-RentMate.command`. The launcher creates a
local environment, installs dependencies and seeds demo fixtures before starting.
Initial dependency installation needs network access.

```sh
bash Start-RentMate.sh --no-seed       # preserve prepared demo fixtures
bash Start-RentMate.sh --port 8024     # choose a different port
```

## Deployment on Windows

Double-click `Start-RentMate.cmd`, or run:

```powershell
.\Start-RentMate.ps1
```

The launcher prepares dependencies and demo fixtures. Default startup runs the
seed script; use a separate runtime/source folder for repeatable demonstrations.

## Application access

| Page | Default address | Purpose |
| --- | --- | --- |
| Business portal | http://127.0.0.1:8020/app | Tenant and manager workflows |
| Verification console | http://127.0.0.1:8020/demo | Full System Check |
| API documentation | http://127.0.0.1:8020/docs | Available HTTP operations |

Use the port printed by your launcher if different. Open the HTTP address rather
than an HTML file. Ctrl+C stops the server. Restart after backend updates and
refresh the browser. Runtime data is stored under `.runtime/` by default.

Enter credentials in **Session settings**:

| Session | Demo token | Property |
| --- | --- | --- |
| Tenant | `tenant-demo` | `demo-property` |
| Manager | `manager-demo` | `demo-property` |
| Other-property manager | `manager-other-demo` | `demo-property-2` |
| Operations | `scheduler-demo` | Scans and dispatch |

The credentials above belong to the bundled local demonstration environment.
Integrated launchers bind the server to localhost.

## Language model configuration

Open **AI settings** and enter the API base URL, model name and private API key.
Use the course-provided proxy details and required network/VPN connection.
API keys must remain outside version control. Connection verification and
workflow acceptance are performed separately.

Browser configuration lives in the server process and needs reconfiguration after
restart. Separate monthly batch processes use environment configuration. Copy
`.env.example` to `backend/.env` and fill in these values if needed:

```dotenv
LLM_BASE_URL=<OpenAI-compatible base URL ending in /v1>
LLM_MODEL=<model name>
LLM_API_KEY=<private API key>
LLM_TIMEOUT_SECONDS=30
```

Model-dependent operations expose their processing source and failure status.
Unavailable or invalid responses enter the applicable review, handoff or partial
result workflow.

## Functional capabilities

1. **Communication:** as tenant, send `hello`. When classification and the routine
   reply policy succeed, the saved reply appears directly in the conversation.
   Messages persist across refresh and remain tenant/property scoped. Notification
   delivery has its own state.
2. **Maintenance:** send a repair message and inspect its linked request. Try
   `There is a strong smell of gas in my kitchen.` for emergency handling.
   Pending-review reply text remains hidden from tenants.
3. **Evidence:** upload a fictional [sample document](demo/samples/README.md),
   inspect classification and link it to a relevant record where authorized.
4. **Manager Review:** switch to manager, inspect a draft and approve or reject
   its exact version. Resolve/dismiss human handoff cases separately; this does
   not approve or send a reply.
5. **Notification:** dispatch using the operations credential, then inspect the
   tenant inbox. Delivery is to the local inbox, not external email or SMS.
6. **Lease & Rent:** run the authorized reminder scan. Assess current risks to
   inspect rule findings and validated model recommendations when available.
   Recommendations remain subject to manager review.
7. **Reporting:** generate a property/month report, inspect source and narrative
   availability, then verify report reuse or a requested new version.
8. **Full System Check:** use `/demo` to run the 13 integrated checks. These
   operations write data; use isolated prepared fixtures for repeatable runs.

More detail: [English demo guide](frontend/DEMO_GUIDE.md),
[live model acceptance](docs/LIVE_MODEL_ACCEPTANCE.md).

## Verification

macOS:

```sh
bash Start-RentMate.sh --verify --no-seed
bash Start-RentMate.sh --verify --live --no-seed
```

Windows:

```powershell
.\Start-RentMate.ps1 -Verify
.\Start-RentMate.ps1 -Verify -Live
```

With dependencies installed in your active environment, run from the repo root:

```sh
python -m pytest backend/tests -q
node --test frontend/tests/*.test.mjs
git diff --check
```

Automated suites cover backend behavior, authorization, workflow integration and
frontend session isolation. Controlled fixtures are used for repeatability;
live model acceptance is enabled explicitly with the live verification option.

## Monthly report operations

Trigger the previous Australia/Sydney calendar month's batch immediately:

```sh
bash Start-RentMate.sh --monthly-run
```

Execution records go to `.runtime/monthly-report-runs.jsonl`. This command does
not install a recurring macOS task. Windows task registration and verification
are separate: see [Windows acceptance](docs/WINDOWS_MONTHLY_TASK_VERIFICATION.md).
Scheduled execution is configured and verified in the deployment environment.

## Project structure and module ownership

| Location | Responsibility | Owner |
| --- | --- | --- |
| `backend/communication/` | Intake, classification routing, drafts and updates | Chaney |
| `backend/maintenance/` | Repair assessment and lifecycle | Kerwin |
| `backend/lease_rent/` | Authorized facts, reminders and risk advice | Ziheng |
| `backend/evidence/` | Uploads, classification, links and timeline | Jenny |
| `backend/reporting/` | Monthly collection and reports | Sharon |
| `backend/review/` | Manager decisions and human handoffs | Chaney |
| `backend/notification/` | Queue, dispatcher, retries and local inbox | Chaney |
| `backend/contracts/` | Shared interfaces and models | Shared |
| `backend/llm/` | Model adapters and response validation | Shared |
| `backend/shared/`, `db/` | Authorization, SQLite schema and migrations | Shared |
| `frontend/app.html`, `frontend/app.js` | Business portal | Integrated frontend |
| `frontend/index.html` | Technical verification console | Integrated frontend |

Stage 1 reports and UML are separate design deliverables. Shared contract
changes follow the [contracts review process](backend/contracts/README.md).

## Authorization and workflow integrity

- Identity comes from server-configured tokens; persisted tenant/manager property
  bindings enforce access. Caller-supplied property names do not grant permission.
- Review triggers override automatic eligibility. Approval belongs to an exact
  draft version. Pending or rejected replies cannot be dispatched.
- Queued does not mean delivered. Dispatch checks current authorization and
  relevant reminder facts before delivery.
- Unknown/failed classification creates a durable human handoff.
- Stable event IDs support retry deduplication. Separate HTTP submissions create
  separate messages; there is no general HTTP replay guarantee.
- Reports expose missing sources and narrative explicitly; partial success is
  distinct from complete success.
