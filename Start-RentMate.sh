#!/bin/bash
# Local macOS/Linux demo launcher. No credentials are written to disk.
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_PATH="${RENTMATE_RUNTIME_PATH:-$PROJECT_ROOT/.runtime}"
PORT=8020
VERIFY=0
LIVE=0
SEED=1
SETUP_ONLY=0
MONTHLY_RUN=0

usage() {
  echo 'Usage: bash Start-RentMate.sh [--port NUMBER] [--no-seed]'
  echo '       bash Start-RentMate.sh --verify [--live]'
  echo '       bash Start-RentMate.sh --monthly-run [--no-seed]'
  echo '       bash Start-RentMate.sh --setup-only'
  echo ''
  echo 'Default: prepare the local runtime, seed demo fixtures and start /app.'
  echo '--no-seed       Preserve existing demo fixtures and decisions.'
  echo '--verify        Run deterministic tests and named integration checks.'
  echo '--live          With --verify, also run live Maintenance acceptance.'
  echo '--monthly-run   Run the scheduled monthly batch now, then exit.'
  echo '--setup-only    Prepare dependencies and demo data without a server.'
  echo 'Optional environment: RENTMATE_PYTHON, RENTMATE_RUNTIME_PATH.'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --port)
      if [ "$#" -lt 2 ]; then echo 'Missing port number.' >&2; exit 2; fi
      PORT="$2"; shift 2 ;;
    --no-seed) SEED=0; shift ;;
    --verify) VERIFY=1; shift ;;
    --live) LIVE=1; shift ;;
    --monthly-run) MONTHLY_RUN=1; SEED=0; shift ;;
    --setup-only) SETUP_ONLY=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
case "$PORT" in ''|*[!0-9]*) echo 'Port must be a number.' >&2; exit 2 ;; esac
if [ "${#PORT}" -gt 5 ] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo 'Port must be between 1 and 65535.' >&2; exit 2
fi
if [ "$LIVE" -eq 1 ] && [ "$VERIFY" -ne 1 ]; then
  echo '--live requires --verify.' >&2; exit 2
fi
if [ "$((VERIFY + SETUP_ONLY + MONTHLY_RUN))" -gt 1 ]; then
  echo 'Choose only one of --verify, --setup-only or --monthly-run.' >&2; exit 2
fi

mkdir -p "$RUNTIME_PATH"
RUNTIME_PATH="$(cd -- "$RUNTIME_PATH" && pwd)"
if [ -n "${RENTMATE_PYTHON:-}" ]; then
  PYTHON_EXECUTABLE="$RENTMATE_PYTHON"
else
  PYTHON_EXECUTABLE="$RUNTIME_PATH/.venv/bin/python"
  if [ ! -x "$PYTHON_EXECUTABLE" ]; then
    if ! command -v python3 >/dev/null 2>&1; then
      echo 'Python 3.11+ is required. Install Python and run again.' >&2; exit 1
    fi
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' || {
      echo 'Python 3.11+ is required.' >&2; exit 1;
    }
    python3 -m venv "$RUNTIME_PATH/.venv"
  fi
fi
"$PYTHON_EXECUTABLE" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' || {
  echo 'The selected Python must be version 3.11+.' >&2; exit 1;
}
if ! "$PYTHON_EXECUTABLE" -c 'import fastapi, uvicorn, pydantic, multipart, pypdf, pytest, httpx, dotenv; from zoneinfo import ZoneInfo; ZoneInfo("Australia/Sydney")'; then
  echo 'Installing RentMate dependencies into the selected local environment...'
  "$PYTHON_EXECUTABLE" -m pip install -r "$PROJECT_ROOT/backend/requirements.txt"
fi

# Always use this launcher's isolated demo runtime, not an inherited production DB.
export DATABASE_PATH="$RUNTIME_PATH/rentmate-demo.db"
export EVIDENCE_STORAGE_ROOT="$RUNTIME_PATH/evidence"
export RENTMATE_SCHEDULER_TOKEN="${RENTMATE_SCHEDULER_TOKEN:-scheduler-demo}"
if [ -z "${RENTMATE_AUTH_TOKENS:-}" ]; then
  export RENTMATE_AUTH_TOKENS='{"tenant-demo":{"tenant_id":"demo-tenant","property_id":"demo-property"},"manager-demo":{"tenant_id":"demo-tenant","property_id":"demo-property","manager_id":"demo-manager"},"manager-other-demo":{"tenant_id":"demo-tenant-2","property_id":"demo-property-2","manager_id":"demo-manager-2"}}'
else
  export RENTMATE_AUTH_TOKENS
fi
if [ -z "${RENTMATE_EVIDENCE_DISPATCH_MANAGERS:-}" ]; then
  export RENTMATE_EVIDENCE_DISPATCH_MANAGERS='{"demo-property":"demo-manager","demo-property-2":"demo-manager-2"}'
else
  export RENTMATE_EVIDENCE_DISPATCH_MANAGERS
fi
cd "$PROJECT_ROOT/backend"

if [ "$SEED" -eq 1 ]; then
  "$PYTHON_EXECUTABLE" -m scripts.seed_integrated_demo --database "$DATABASE_PATH"
fi
if [ "$SETUP_ONLY" -eq 1 ]; then
  echo "Local setup ready: $DATABASE_PATH"
  exit 0
fi
if [ "$MONTHLY_RUN" -eq 1 ]; then
  echo 'Running the scheduled monthly batch now (previous Sydney calendar month).'
  echo 'This invokes the runner directly; it does not register an OS scheduled task.'
  exec "$PYTHON_EXECUTABLE" -m reporting.monthly_runner --database "$DATABASE_PATH" --log "$RUNTIME_PATH/monthly-report-runs.jsonl"
fi
if [ "$VERIFY" -eq 1 ]; then
  export PYTHONDONTWRITEBYTECODE=1
  "$PYTHON_EXECUTABLE" -m pytest -q -p no:cacheprovider
  "$PYTHON_EXECUTABLE" -m pytest -vv -p no:cacheprovider tests/test_integrated_app_routes.py tests/test_integrated_demo.py tests/test_notification.py
  if [ "$LIVE" -eq 1 ]; then
    echo 'Live Maintenance acceptance uses the environment/backend/.env, not browser-only settings.'
    "$PYTHON_EXECUTABLE" -m maintenance.verify_live --output "$RUNTIME_PATH/live-result.json"
  else
    echo 'Live model acceptance skipped; use --verify --live when configured.'
  fi
  echo 'PASS - deterministic and integrated checks completed.'
  exit 0
fi

echo 'RentMate local prototype (Ctrl+C stops the server)'
echo "Business portal: http://127.0.0.1:$PORT/app"
echo "Technical check: http://127.0.0.1:$PORT/demo"
echo 'Demo property: demo-property'
echo 'Default prototype tokens: tenant-demo / manager-demo / scheduler-demo'
echo 'Enter your real API settings in AI settings; no model key is printed here.'
exec "$PYTHON_EXECUTABLE" -m uvicorn main:app --host 127.0.0.1 --port "$PORT"
