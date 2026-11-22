#!/usr/bin/env bash
set -euo pipefail
echo "===== SERVICE STATUS ====="
systemctl --no-pager --full status nexgard-security || true
echo
echo "===== RECENT NEXGARD LOGS (last 8 minutes) ====="
sudo journalctl -u nexgard-security --since "8 minutes ago" --no-pager -n 180 | sed -E 's#rtsp://[^[:space:]]+#rtsp://[credentials hidden]#g'
echo
echo "===== REOLINK GATEWAY STATUS ====="
systemctl --no-pager --full status reolink-gateway || true
echo
echo "===== REOLINK CLI DOCTOR ====="
"$HOME/.local/bin/reolink-cli" doctor || true
