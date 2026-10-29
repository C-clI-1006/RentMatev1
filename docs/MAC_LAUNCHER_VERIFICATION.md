# macOS launcher verification

Client date: 2026-10-30 (Australia/Sydney).

Added executable `Start-RentMate.sh` plus the Finder entry point
`Start-RentMate.command`. Neither writes model keys to disk. The launcher
creates/uses an ignored `.runtime` environment, seeds local fixtures unless
`--no-seed` is selected, and serves `/app` with the existing FastAPI app.
`--monthly-run` never seeds/reset fixtures and runs the existing monthly
batch immediately. It does not install launchd/cron or claim an OS scheduler
execution.

Verified on macOS with an isolated runtime and the existing test interpreter:

```sh
bash -n Start-RentMate.sh
bash -n Start-RentMate.command
bash Start-RentMate.sh --help
RENTMATE_PYTHON=/tmp/rentmate-test-venv/bin/python \
  RENTMATE_RUNTIME_PATH=/tmp/rentmate-mac-launcher-rgMK0C \
  bash Start-RentMate.sh --setup-only
RENTMATE_PYTHON=/tmp/rentmate-test-venv/bin/python \
  RENTMATE_RUNTIME_PATH=/tmp/rentmate-mac-launcher-rgMK0C \
  bash Start-RentMate.sh --port 8026 --no-seed
```

Model environment variables were empty during these isolated checks. The
server started successfully; `/health`, `/app` and authenticated tenant
Maintenance listing all returned HTTP 200.

The launcher verification mode was also executed:

```sh
RENTMATE_PYTHON=/tmp/rentmate-test-venv/bin/python \
  RENTMATE_RUNTIME_PATH=/tmp/rentmate-mac-launcher-rgMK0C \
  bash Start-RentMate.sh --verify --no-seed
```

- Full backend: **456 passed**, one existing deprecation warning.
- Named integrated route/review/notification suite: **13 passed**.
- Optional live Maintenance mode skipped in this launcher run; the separate
  four-domain Campus Qwen evidence is in `docs/LIVE_MODEL_ACCEPTANCE.md`.

Immediate monthly execution succeeded, exit code 0, for two properties.
It selected `2026-09` at `2026-10-29T13:02:50Z` (Oct 30 Sydney), and recorded
`business_timezone=Australia/Sydney`. The reports were
`2717c591-85c7-4a02-9583-c69ae2035381` and
`205cf8d7-538c-4fa3-a748-d6f7c7eeced0`. A later same-slot retry reused them.
Missing narrative was truthful because this isolated process had no model
configuration. Browser-only API settings do not transfer to this process.

Fresh dependency download and Finder double-click were not independently
tested; the actual Bash launcher/startup path was tested with the existing
interpreter. Windows Task Scheduler remains unverified on this macOS host.

The nine-page Chinese guide was generated with ReportLab using an embedded
Chinese-capable font, text-extracted and visually checked after rendering all
pages with Poppler. Its source and generator are checked in alongside the PDF.
