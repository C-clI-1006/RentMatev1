# Property evidence timeline and record linking

## Scope

This completes the property-scoped UC6 path alongside the existing Maintenance
event timeline. Uploading evidence does not require an existing Maintenance
request. The manager can see its upload and classification history immediately,
then select an existing related record if the association is not yet known.

The timeline projects persisted upload, classification, review, manual association
and accepted Maintenance event records. It uses the original timestamps and
stable source IDs; reading it does not generate events or invent historical dates.
Classification reflects the persisted machine outcome; every manager review is
retained separately. Missing historical classification/link timestamps are not
replaced with the time of upgrade. Entries are ordered by UTC instant, then stable
entry ID. A sender's Maintenance actor is audit data, not the receiving identity.

This is an evidence history, not a feed of every Communication, Rent or Reporting
action. No new Maintenance event model or HTTP ingestion endpoint is introduced.

## Business UI

1. Connect an authorised Property manager session in `/app`.
2. Open **Documents & Evidence**, upload a sample, then open its detail.
3. An accepted automatic category needs no manager confirmation. Uncertain or
   failed classification must first be settled through classification review.
4. Under **Related records**, click **Manage record links**. Existing associations
   are shown. If unlinked, select an existing lease or Maintenance request from
   this property and click **Link document**.
5. The document becomes `associated`. Repeating the same link returns the existing
   association. Several documents may reference one record, and one document may
   reference several records. This increment adds links; it does not remove or
   silently replace a historical link.
6. Open **Activity Timeline**. **Property evidence timeline** includes documents
   even before linking, with their classification, review and association history.
7. The existing **Follow a maintenance workflow** section still displays the
   selected request's Maintenance and Evidence receipt history.

Linking is explicit: the classifier proposes a document category, not a guessed
business-record ID. A link records a relationship inside Evidence; it does not
change lease terms, `lease.document_ref`, Maintenance state, version or event
history. A Maintenance transition must still supply its own validated evidence
references and satisfy the existing closure rules.

## API

All new operations require a server-resolved manager identity and a current
`manager_property` binding. No actor, reviewer or manager identity is accepted in
the request body. Tenant document permissions and existing Maintenance receipt
endpoints are unchanged.

| Method and path | Input / result |
| --- | --- |
| `GET /api/evidence/timeline` | `property_id`, optional `document_id`; chronologically ordered source entries |
| `GET /api/evidence/link-targets` | `property_id`; authorised `lease` and `maintenance_request` choices |
| `GET /api/evidence/documents/{document_id}/links` | `property_id`; manual and received Maintenance-event links |
| `POST /api/evidence/documents/{document_id}/links` | Body: `property_id`, `target_type`, `target_id`; associated result, `created`, persistent ID, time and verified manager |

Missing and cross-property document/record IDs return the same scoped 404.
Unauthorised identities receive 401/403. Unsupported target types or extra identity
fields return 422. Unsettled classification returns 409 with
`classification_review_required`. Database write errors return a sanitised 503.
Permission checks, target validation and the link write share one transaction.

Only existing `mc_request.id` and `lease.lease_id` values are accepted. Source
tables are read-only. The two record types keep separate IDs and target types.

## Persistence and compatibility

Schema v12 preserves existing association rows and adds nullable `associated_by`
for the verified manager. Historical rows keep an unknown actor rather than a
fabricated identity. The unique index now includes document ID so more than one
document can reference the same business record. Migration DDL uses a savepoint;
failure restores the prior column/index state. Existing Maintenance receipts,
deduplication, automatic dispatcher and document links are retained.

## Verification (2026-10-30)

Base: `0a322db` plus the uncommitted local implementation.

- Complete backend: **541 passed**, one existing Starlette/httpx deprecation
  warning, Python 3.12.14, 198.84 seconds.
- Frontend context and workflow tests: **34 passed**.
- Browser/API specialist scenario: **21 passed**, including actual UI linking,
  repeated links, multiple documents per lease, Maintenance reference without
  state mutation, failed-classification review and property timeline rendering.
- Negative cases cover tenant access, cross-property documents/targets, revoked
  manager bindings, invalid IDs, forged actor fields, rollback and restart.
- Migration coverage includes v11 upgrade, repeated application, preserved data
  and indexes, foreign-key validation, and injected migration failure rollback.

All databases and uploaded files were temporary synthetic test data. Inference
used explicit test doubles. No real Qwen call or private configuration read was
performed. These local results do not replace a partner review or final Canvas
archive deployment acceptance.
