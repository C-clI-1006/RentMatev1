# Business portal and technical demonstration

`/app` is the primary business portal. It is a no-build HTML/CSS/ES-module
client served by the same FastAPI process. `/` redirects to `/app`.
Tenant and manager navigation use real authorized backend APIs; role selection
does not grant permissions. Enter the launcher's token and its matching property
in Session settings. Tokens stay in tab memory and are cleared on reload.
Operations require a separately entered scheduler token.

See [DEMO_GUIDE.md](DEMO_GUIDE.md) for exact startup commands and the complete
interactive presentation, [FRONTEND_API_INVENTORY.md](FRONTEND_API_INVENTORY.md)
for actual API schemas and gaps, [FRONTEND_WORKFLOW_MATRIX.md](FRONTEND_WORKFLOW_MATRIX.md)
for coverage, and [FRONTEND_TEST_RESULTS.md](FRONTEND_TEST_RESULTS.md) for verification.

For file-based attachment selection in Maintenance, see
[MAINTENANCE_EVIDENCE_PICKER.md](MAINTENANCE_EVIDENCE_PICKER.md).

## Technical dashboard (preserved)

`index.html` is a no-build demonstration console served by FastAPI at
`/demo`. It exercises the real UC4/UC5 scheduler, manager review, durable
notification dispatcher and property-scoped tenant inbox. It deliberately
contains no business or authorisation rules: every action calls the backend
API and all tokens are validated server-side.

Run `Start-RentMate.cmd` from the repository root, then open
`http://127.0.0.1:8020/demo`. The fixed values shown in the page are local
prototype credentials created by the launcher; they are not deployment
secrets. A production React client can replace this console without changing
the API boundaries.
