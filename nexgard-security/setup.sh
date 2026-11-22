#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: Python 3 is required."
  exit 1
fi

if ! python3 -c 'import venv' >/dev/null 2>&1; then
  echo "ERROR: Python venv support is missing. On Ubuntu, install it with:"
  echo "  sudo apt update && sudo apt install -y python3-venv python3-pip"
  exit 1
fi

python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt

if [[ ! -f .env ]]; then
  .venv/bin/python - <<'PY'
from pathlib import Path
import secrets
from cryptography.fernet import Fernet

admin_password = secrets.token_urlsafe(24)
session_secret = secrets.token_urlsafe(48)
encryption_key = Fernet.generate_key().decode("ascii")
env = (
    f"NOVATECH_ADMIN_PASSWORD={admin_password}\n"
    f"NOVATECH_SESSION_SECRET={session_secret}\n"
    f"NOVATECH_ENCRYPTION_KEY={encryption_key}\n"
    "NOVATECH_HTTPS_ONLY=false\n"
)
Path('.env').write_text(env, encoding='utf-8')
Path('.env').chmod(0o600)
Path('data').mkdir(exist_ok=True)
print("\nFIRST-RUN ADMIN PASSWORD (save this now):")
print(admin_password)
print("\nThe password is stored in the local .env file. Do not share that file.\n")
PY
else
  echo "Existing .env found; keeping existing credentials."
fi

mkdir -p data models
chmod 700 data

if command -v ffprobe >/dev/null 2>&1; then
  echo "FFprobe found: $(command -v ffprobe)"
else
  echo "NOTE: ffprobe was not found. RTSP connection tests require FFmpeg."
  echo "On Ubuntu: sudo apt update && sudo apt install -y ffmpeg"
fi

echo ""
echo "Setup complete. Start NexGard Security with:"
echo "  ./run.sh"
echo ""
echo "Then open http://127.0.0.1:8000 in a browser on the server."
