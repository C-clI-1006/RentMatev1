# Shared LLM package — change note (UC6 document classification)

Scope of this change: `backend/llm/` only. Nothing in `backend/llm/`
behaviour for the maintenance caller is changed; the addition is
isolated to a new submodule (`llm/classify.py`).

## What changed

| File | Change |
|---|---|
| `llm/classify.py` (new) | `IDocumentClassifier` interface, `ClassificationResult` / `ClassifyFailure` types, `StubDocumentClassifier` (labelled `source="stub"` test double), `QwenDocumentClassifier` (live adapter, fail-closed), `build_document_classifier()` default wiring. The timeout env var was aligned to the mainline name `LLM_TIMEOUT_SECONDS` (it was `LLM_TIMEOUT_S` in the first draft); the default (30 s) and all parsing / validation behaviour are unchanged. |
| `llm/__init__.py` | **Docstring correction only** for the `LlmPriorityAssessor` bullet (it was listed as if defined; no such class exists in this repo — it is the future live-priority slot). The new classifier symbols are re-exported from the package root. |
| `llm/assess.py` | **Untouched.** |

## Impact on the maintenance caller (compatibility note)

- The maintenance-priority contract is preserved **by shape**:
  - `Assessment` / `AssessmentFailure` (dataclasses): unchanged.
  - `IPriorityAssessor` (ABC, `assess(title, description, location,
    intake_emergency_flag) -> Assessment | AssessmentFailure`):
    unchanged.
  - `KeywordPriorityAssessor` (in `llm/assess.py`): unchanged.
- When the maintenance module wires its assessor, it will still find
  `IPriorityAssessor` and `KeywordPriorityAssessor` at the exact same
  import paths (`from llm import ...`).
- The document classifier is a **separate interface**
  (`IDocumentClassifier`) — it does not reuse the priority shapes, so
  nothing a maintenance assessor does can be confused with document
  classification output, and vice versa.
- The live Qwen adapter (`QwenDocumentClassifier`) is fail-closed:
  without `LLM_BASE_URL` / `LLM_API_KEY` every call returns a
  `ClassifyFailure` (code `configuration`) and the evidence module
  routes the document to manual review. The maintenance module is
  unaffected by the adapter's configuration.

## Environment (shared by Maintenance and Evidence)

These four variables are the single configuration point for ALL model
calls: the maintenance live assessor (`LlmPriorityAssessor`,
`llm/live.py`) and the evidence document classifier
(`QwenDocumentClassifier`, `llm/classify.py`) read the same names.
Each module keeps its own existing fail-closed behaviour when the
values are missing or the proxy is unavailable.

| Var | Used by | Default |
|---|---|---|
| `LLM_BASE_URL` | `QwenDocumentClassifier`, `LlmPriorityAssessor` | empty → fail-closed |
| `LLM_API_KEY` | `QwenDocumentClassifier`, `LlmPriorityAssessor` | empty → fail-closed |
| `LLM_MODEL` | `QwenDocumentClassifier`, `LlmPriorityAssessor` | classify: placeholder `"qwen"` |
| `LLM_TIMEOUT_SECONDS` | `QwenDocumentClassifier`, `LlmPriorityAssessor` | `30` |

No new runtime dependencies were added (the live classifier adapter
uses stdlib `urllib`). `httpx` remains dev-only.

### How configuration is loaded (verified by reading the code)

- `QwenDocumentClassifier.__init__` (what `build_document_classifier()`
  returns) reads exactly the four `LLM_*` variables above from
  `os.environ` — there is no other config source. `LLM_BASE_URL` and
  `LLM_API_KEY` are required: without them `configured` is `False` and
  every call fails closed to `ClassifyFailure` (code `configuration`).
- `backend/main.py` calls `load_dotenv("backend/.env")` at import time,
  before the business modules are imported, so `backend/.env` provides
  defaults for `DATABASE_PATH`, `EVIDENCE_STORAGE_ROOT`, the four
  `LLM_*` variables, and the auth token map. Values already present in
  the process environment take precedence (the dotenv load does not
  override existing variables) — exporting the same variable in the
  shell (e.g. PowerShell `$env:LLM_BASE_URL="..."`) wins over
  `backend/.env`. Without `LLM_BASE_URL` / `LLM_API_KEY` the classifier
  runs unconfigured and every document routes to manual review.
- The evidence module (`evidence/router.py` → `evidence/service.py`)
  wires `build_document_classifier()` as the default classifier. This
  note covers the classifier config-reading code and `main.py` startup
  wiring; it does **not** claim every `llm` import site in the repo was
  verified.

## Open items (to confirm with the team / Kerwin)

1. ~~Whether the maintenance module prefers its own env prefix or
   shares the `LLM_*` variables~~ — resolved: the `LLM_*` variables are
   the single shared configuration point for both modules (guide
   Ch.19); the timeout name is `LLM_TIMEOUT_SECONDS` everywhere.
2. ~~When the live priority assessor lands, it should be added as a new
   class next to `KeywordPriorityAssessor` (shape-compatible) rather
   than changing `IPriorityAssessor`.~~ — resolved:
   `LlmPriorityAssessor` is in `llm/live.py`, shape-compatible with
   `IPriorityAssessor`, and shares the same `LLM_*` variables.

Live Campus Qwen verification passed for a fictional invoice PDF: persisted category=invoice, source=llm. This verifies one sample, not classification accuracy across all categories.
