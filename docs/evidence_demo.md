# Evidence Module Demonstration Guide (Windows PowerShell)

This guide demonstrates upload, PDF classification, and manager review through Swagger.

## Scope and limitations

- This walkthrough does not dispatch Maintenance events. Uploaded documents initially have association=unassociated. The Maintenance callback can persist a timeline and link real references, changing linked documents to associated. See the [integration contract](evidence_maintenance_integration.md). Background Evidence dispatch is enabled by Start-RentMate.ps1 for the fictional demo bindings; see the integration contract for other deployments. Final partner/deployment acceptance remains separate. Lease association and a general frontend timeline are not implemented.
- Bearer identities come from the server-side RENTMATE_AUTH_TOKENS mapping. Production login is not implemented. Tenant and manager tokens represent separate identities, and database property authorization checks cannot be bypassed with client identity fields.
- This sample upload/classification/review path does not demonstrate classification accuracy or full UC6 completion.
- accepted confirms upload acceptance and storage. queued, accepted, and handed_off outcomes do not prove external notification delivery.
- All identities and token placeholders below are fictional. Do not put real credentials or private machine paths in documentation or commits.

## 0. Conventions

- Fictional tenant: T-DEMO-01; property: P-DEMO-01; independent manager: M-DEMO-01.
- Use property_id=P-DEMO-01 in this walkthrough.
- Configure separate tenant and manager sessions server-side before testing.
- Always use document.document_id from the actual upload response; never hard-code a local ID.
- Backend: http://127.0.0.1:8000; Swagger: http://127.0.0.1:8000/docs.

## 1. Install backend dependencies

```powershell
cd <repository-path>\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If activation is blocked, use Set-ExecutionPolicy -Scope Process Bypass in the current session before activating.

## 2. Seed fictional identities

From the repository root:

```powershell
python backend\scripts\seed_evidence_demo.py
```

Paths resolve relative to the script, so it can run from another working directory. The script creates one tenant, one independent manager, one shared property, a tenant_property resident binding, and a manager_property binding. It does not create tokens, credentials, documents, classifications, or reviews.

The seed is idempotent and preserves existing fields. A conflicting tenant role aborts and rolls back; the script never overwrites or escalates a binding. Success prints seed complete: db=...; the default database matches main.py.

Database bindings and the server-side session map are separate configuration.

## 3. Configure model and session environment variables

backend/main.py loads backend/.env before business-module imports. Existing process environment variables take precedence because dotenv does not override them.

```powershell
cd <repository-path>
Copy-Item .env.example backend\.env
# Edit the ignored local file, then restart uvicorn.
# Never place real values in tracked files.
```

Alternatively, set variables in the session that starts uvicorn:

```powershell
$env:LLM_BASE_URL = Read-Host "LLM_BASE_URL (proxy base URL, such as https://.../v1)"
$secKey = Read-Host "LLM_API_KEY (hidden input)" -AsSecureString
$env:LLM_API_KEY = [System.Net.NetworkCredential]::new("", $secKey).Password
$env:LLM_MODEL = Read-Host "LLM_MODEL"
$env:LLM_TIMEOUT_SECONDS = Read-Host "LLM_TIMEOUT_SECONDS (default 30)"
$env:RENTMATE_AUTH_TOKENS = '{"REPLACE_WITH_RANDOM_TENANT_TOKEN":{"tenant_id":"T-DEMO-01","property_id":"P-DEMO-01"},"REPLACE_WITH_RANDOM_MANAGER_TOKEN":{"tenant_id":"M-DEMO-SESSION","property_id":"P-DEMO-01","manager_id":"M-DEMO-01"}}'
```

The session map above is a shape example, not usable credentials. Supply real sessions privately without leaving secrets in shell history. Environment variables apply only to the current session. Real keys belong only in ignored local configuration or private process configuration; use hidden input for keys.

### Shared model configuration

Maintenance LlmPriorityAssessor and Evidence QwenDocumentClassifier both read LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, and LLM_TIMEOUT_SECONDS. Keep this shared timeout name. Both retain fail-closed behavior: unavailable or unconfigured inference requires manual review. Evidence returns a configuration failure when required configuration is absent, while the upload remains accepted. The timeout controls inference calls, not upload or review semantics.

### Server-side Bearer session mapping

- Identity fields come exclusively from RENTMATE_AUTH_TOKENS, never request bodies, query parameters, or custom identity headers.
- A tenant session contains tenant_id and property_id and requires a tenant_property binding.
- A manager session contains manager_id and property_id and requires a manager_property binding. tenant_id may be omitted for an Evidence manager-only session. reviewed_by is the verified manager_id, never tenant_id, and there is no tenant fallback.
- The seed creates both required bindings. Manager upload, document reads and review do not require a tenant binding for the manager's session identity.
- Unknown or unconfigured tokens return 401; valid but unauthorized sessions are denied.
- The session map is parsed per request. Updating the process map takes effect without restarting. Changes to a .env file or model configuration require a restart.

## 4. Start the backend

Use a configured session or the ignored backend/.env file:

```powershell
cd <repository-path>\backend
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Keep the server session open when Uvicorn reports its listening URL.

## 5. Open Swagger

Visit http://127.0.0.1:8000/docs. Evidence endpoints appear under the evidence tag and /api/evidence prefix. Use Swagger Authorize with the appropriate Bearer session:

| Operation | Session |
|---|---|
| Upload, list, get, download, text extraction | Tenant session for T-DEMO-01 / P-DEMO-01 |
| POST /api/evidence/reviews | Independent manager session with manager_id=M-DEMO-01 |

Do not send prototype identity headers. This server-provisioned session mapping is not a production login system.

## Flow 1: Text PDF, classification, and confirmation

1. Prepare a nonempty, text-extractable fictional invoice or lease PDF no larger than 10 MiB.
2. Call POST /api/evidence/documents with the tenant Bearer session, the PDF file, and property_id=P-DEMO-01.
3. Expect HTTP 200 with status=accepted and a document object. Save the actual document_id. With usable inference, classification is classified and classification_type is one of lease_document, invoice, inspection_record, maintenance_photo, or communication_record. The actual result depends on the model.
4. Use the tenant session with GET /api/evidence/documents/{document_id}?property_id=P-DEMO-01 to verify persistence.
5. Optionally call GET /api/evidence/documents/{document_id}/text?property_id=P-DEMO-01. Successful extraction returns status=extracted, text, and page_count.
6. Use the manager session to POST /api/evidence/reviews:

```json
{
  "status": "confirm_suggested",
  "document_id": "<actual-document-id-from-upload>",
  "property_id": "P-DEMO-01"
}
```

7. HTTP 200 returns EvidenceReviewResult with final_category, the verified manager_id as reviewed_by, and a UTC reviewed_at timestamp.

If inference is unconfigured or unavailable, classification is failed with a review_code such as configuration. confirm_suggested then returns 409 review_rejected. Use select_category from Flow 2 instead. This is expected fail-closed behavior.

## Flow 2: Image and manual classification

1. Prepare a nonempty fictional JPG or PNG no larger than 10 MiB.
2. Upload through POST /api/evidence/documents with the tenant session and property_id=P-DEMO-01.
3. Save the returned document_id. The upload is accepted, but PDF-only classification records failed with review_code=file_type_not_supported and review_required=true.
4. Optionally call the text endpoint with the tenant session. It returns manual_review_required and file_type_not_supported; the stored original remains downloadable.
5. Use the manager session to choose the appropriate business category:

```json
{
  "status": "select_category",
  "document_id": "<actual-document-id-from-upload>",
  "property_id": "P-DEMO-01",
  "category": "maintenance_photo"
}
```

Allowed choices are lease_document, invoice, inspection_record, maintenance_photo, and communication_record. unknown is not a manual category.

6. HTTP 200 returns the selected final_category and verified manager_id as reviewed_by.

## Independent manager upload (SCRUM-8 acceptance)

Use the existing POST /api/evidence/documents endpoint with the authorized manager
session and its property_id. A manager-only session needs manager_id and property_id
in the server-side mapping; it does not need a tenant_id or tenant_property binding.

1. Authorize as the manager and upload a fictional text PDF.
2. Check status=accepted, tenant_id=null, and uploaded_by_manager_id matching the
   verified manager. Classification still uses the configured classifier and fails
   closed when inference is unavailable.
3. With the same manager session, list, query, download and extract the document.
   The downloaded original must match the uploaded bytes.
4. Confirm a supported machine suggestion, or select a category when manual review
   is required. reviewed_by must be the authenticated manager.
5. A manager without a binding to the requested property receives 403, even if the
   session also carries a valid tenant identity. There is no tenant fallback.
6. A tenant in the same property cannot read or list a manager-owned document.
   Existing tenant uploads retain their original ownership and access rules.

SQLite schema version 10 adds uploaded_by_manager_id and permits tenant_id=null
for manager uploads. The upgrade preserves existing evidence, classifications,
reviews, event links, indexes and triggers. Validate upgrades only against temporary
database copies during acceptance; do not use private operational databases.

Local verification on 2026-10-28 (uncommitted working tree based on d3232ce):
Python 3.12.14, `python -m pytest backend/tests -q` completed with 401 passed
and one existing Starlette/httpx deprecation warning. Seven new regressions cover
independent manager uploads and reads, classification and review, role isolation,
invalid uploads, legacy ownership migration, and migration rollback. Tests use
temporary databases and an explicitly injected classifier stub; dotenv loading
and external network access are disabled. This does not certify live Qwen or
update the previously exported candidate package.

Live verification on 2026-10-28 also passed against this local working tree using
the existing ignored model configuration, temporary database/storage, and one
synthetic invoice PDF. An independent manager uploaded the PDF without a tenant
identity; the real classifier persisted classified / invoice, source=llm,
confidence=0.98. PDF extraction, manager confirmation (reviewed_by=demo-manager),
and byte-for-byte original download passed. The first restricted-environment
attempt returned unavailable and was retained as FAIL; a subsequent network-enabled
execution passed. This is single-sample evidence, not an accuracy evaluation or a
verification of the old desktop candidate package. No credentials were exported.

## Operational boundaries

- Extraction and automatic classification support text PDFs only. Non-PDF files can be stored but classification fails with file_type_not_supported.
- Encrypted, scanned-without-text, or corrupt PDFs require manual review with pdf_encrypted, pdf_no_text, or pdf_corrupt. Extraction never modifies or deletes the original.
- Allowed extensions: pdf, jpg, jpeg, png, doc, docx, xls, xlsx, txt. Maximum size: 10 MiB. Empty or invalid uploads are explicitly rejected and not stored.
- backend/.env is loaded at startup; existing process variables take precedence. Reconfigure new shell sessions explicitly.
- Never commit actual keys or tokens. Demo IDs are fictional, and model test doubles must be explicitly labeled as stubs rather than live inference.
