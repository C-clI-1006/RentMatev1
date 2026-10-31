# Select evidence files in Maintenance

Maintenance intake and manager status updates now offer **Supporting documents**
checkboxes instead of a comma-separated document ID input. Each choice shows the
stored file name, effective category and upload time. Internal document IDs are
still submitted to the existing API; users do not need to copy or exchange them.

## Workflow

1. As a tenant, upload the inspection PDF under **Documents**.
2. Open **Maintenance**, fill in the repair details and select the uploaded
   inspection file under **Supporting documents**. Submit the request.
3. As a manager of the same property, upload the repair invoice under
   **Documents & Evidence**. Open the existing Maintenance request.
4. The latest event's references are selected by default. Select the additional
   invoice, enter a reason and record the allowed status change.
5. After success, the next status form retains that event's selection. Clearing a
   checkbox is an explicit removal from the next event; historical events are
   preserved. Awaiting confirmation and closure still require valid evidence.

Upload new files in Documents and then reopen the Maintenance view to reload its
choices. Intake remains possible with no attachments when the document list is
empty. The server remains authoritative for completion and closure requirements.

## Scope and failure behavior

- Choices come from the existing Bearer-authenticated, property-scoped Evidence
  list endpoint. Tenants see only their documents; managers see the documents
  available to them in their assigned property.
- Multiple checkboxes submit an array of document IDs, not display labels or
  filenames. No actor or manager identity is introduced in the command.
- The latest event's references are retained even if their metadata is unavailable.
  An explicit warning is shown rather than silently dropping a reference. The user
  may deselect it deliberately; selected references are always revalidated by the
  server.
- If the list cannot be loaded, the relevant submit button is disabled and the
  handler refuses to submit an implicit empty list. Reopen the view to retry.
  Request details, history and the manager assessment form remain available.
- Scope/navigation checks also cover delayed document-list responses, so changing
  accounts or navigating away cannot restore old-property file choices.
- File names and metadata are escaped. UUIDs, APIs, database schema, permissions,
  event contracts, optimistic version checks and receipt handling are unchanged.

The change uses no real model invocation itself. Classification still follows the
configured Evidence classifier; this frontend feature does not bypass manual
review or promise successful AI classification.
