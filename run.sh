#!/usr/bin/env bash
# macOS/Linux launcher. Run from any directory; .env is read as data, never sourced.
set -euo pipefail

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'HELP'
Usage: ./run.sh [--port 8000] [--host 127.0.0.1]

Sets up .venv, installs project dependencies and Chromium, preserves or creates
.env, then starts Dori. Requires Python 3.11+. Stop with Control+C.
Examples:
  bash run.sh
  bash run.sh --port 8001
Optional: DORI_PYTHON=/path/to/python3 bash run.sh
HELP
  exit 0
fi

if [[ ! -x .venv/bin/python ]]; then
  DORI_LAUNCH_PYTHON=""
  for candidate in "${DORI_PYTHON:-python3}" python3.14 python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      DORI_LAUNCH_PYTHON="$candidate"
      break
    fi
  done
  if [[ -z "$DORI_LAUNCH_PYTHON" ]]; then
    printf '%s\n' 'Python 3.11 or newer is required. Install it from https://www.python.org/downloads/macos/ and run this script again.' >&2
    exit 1
  fi
  printf '%s\n' 'Creating the project virtual environment…'
  "$DORI_LAUNCH_PYTHON" -m venv .venv
fi

DORI_VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
if ! "$DORI_VENV_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  printf '%s\n' 'The existing .venv needs Python 3.11+. Rename it, then run this script again. Your .env and data folder can stay in place.' >&2
  exit 1
fi

printf '%s\n' 'Installing/updating project dependencies…'
"$DORI_VENV_PYTHON" -m pip install -e .

if [[ ! -e .env ]]; then
  cp .env.example .env
  printf '%s\n' 'Created .env. Add OPENAI_API_KEY there to enable AI, then restart the app.'
fi

printf '%s\n' 'Checking the Chromium installation…'
"$DORI_VENV_PYTHON" - <<'PY'
import subprocess
import sys
from dotenv import load_dotenv
load_dotenv('.env')
subprocess.run([sys.executable, '-m', 'playwright', 'install', 'chromium'], check=True)
PY

printf '%s\n' 'Starting Dori admin. Default URL: http://127.0.0.1:8000 — stop with Control+C.'
exec "$DORI_VENV_PYTHON" -m dori.cli serve "$@"
