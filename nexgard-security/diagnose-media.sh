#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT_DIR"
echo "===== NEXGARD MEDIA DIAGNOSTICS ====="
printf 'FFmpeg: '; command -v ffmpeg || true
printf 'FFprobe: '; command -v ffprobe || true
printf 'espeak-ng: '; command -v espeak-ng || true
printf 'Reolink CLI: '; command -v reolink-cli || test ! -x "$HOME/.local/bin/reolink-cli" || echo "$HOME/.local/bin/reolink-cli"
if [[ -x "$HOME/.local/bin/reolink-cli" ]]; then
  "$HOME/.local/bin/reolink-cli" --version || true
fi
printf '\nNexGard service:\n'
systemctl is-active nexgard-security || true
printf '\nHTTP health through local backend:\n'
curl -sk https://127.0.0.1/api/health || true
printf '\nRecent service logs:\n'
sudo journalctl -u nexgard-security -n 35 --no-pager
