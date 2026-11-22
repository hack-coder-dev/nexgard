#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "Environment missing. Run ./setup.sh first."
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "Missing .env. Run ./setup.sh first."
  exit 1
fi
# The application loads .env with python-dotenv. Do not source it in a shell:
# passwords may contain shell metacharacters, and dotenv quoting is not shell quoting.
exec .venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
