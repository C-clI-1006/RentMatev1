# Frontend / integrated demonstration

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
