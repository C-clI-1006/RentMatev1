# Conditional review repair handoff

Local implementation prepared for review; no push or merge performed by Codex.

## Verification

From backend: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider`

Result: **111 passed** (96 existing cases and 15 additional regression cases).
The former communication contract/schema failures are resolved. Tests now
expect the intentional routine auto-eligibility behavior instead of universal
review; unavailable facts still require review. No tests were skipped.

## Changes

- Restored real maintenance, lease/rent, draft, audit and notification contracts.
- Preserved source references and exact draft identity in review requests.
- Conservative full-message routine rules; decisions, uncertainty, unavailable
  facts and emergency outcomes require review. Denied/conflicting facts hand off.
- Preset replies use persisted outcomes; no invented appointments or success.
- Emergency alerts use bound managers and minimal payloads before downstream
  business calls when the inbound emergency indicator is recognised. Returned
  emergency priority also triggers an alert. Failures are audited.
- Tenant acknowledgement projection exposes only message ID and reply/delivery
  status. Internal outcome objects and delivery diagnostics are not HTTP DTOs.
- No tenant message is dispatched and no manager approval is fabricated.

## OpenCode next steps

1. Inspect branch `chaney/uc5-draft-source-and-ownership`, local commits and diff.
   The branch already contained earlier unpushed work; do not force-push or
   silently merge it into main.
2. Re-run the command above and `git diff --check`.
3. Push the reviewed branch with normal `git push`; do not amend other work.
4. Follow backend/contracts/README.md: propose the contracts-only commit as a
   separate PR based on the appropriate upstream base, obtain affected owners'
   review, then land dependent implementation. Separate commits alone do not
   constitute contract approval. Do not merge either PR automatically.

## Remaining boundaries

This is a service-layer prototype. HTTP/session authentication, an authorised
version-bound tenant dispatcher, real LLM integration and an end-to-end UI demo
are not implemented here. Keyword classification and explicit phrase rules
have limited coverage; unknown wording is conservative, but emergency keywords
are not a comprehensive semantic emergency detector. Provider fact values are
trusted by the existing cross-module contract; authorisation must also be
maintained in the fact provider. Real notification delivery was tested with
contract-shaped fakes, not an external gateway. No production migration is
needed for the draft status comment change (status is unconstrained TEXT).
