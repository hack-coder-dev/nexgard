#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo: sudo ./install-service.sh" >&2
  exit 1
fi

APP_USER="${SUDO_USER:-}"
if [[ -z "${APP_USER}" || "${APP_USER}" == "root" ]]; then
  echo "Could not determine the non-root project owner." >&2
  echo "Run this as: sudo ./install-service.sh" >&2
  exit 1
fi

if ! id "${APP_USER}" >/dev/null 2>&1; then
  echo "User '${APP_USER}' does not exist." >&2
  exit 1
fi

if [[ ! -x "${ROOT_DIR}/.venv/bin/uvicorn" || ! -f "${ROOT_DIR}/.env" ]]; then
  echo "NexGard is not set up yet. Confirm .venv/bin/uvicorn and .env exist." >&2
  echo "Run ./setup.sh as ${APP_USER} first." >&2
  exit 1
fi

APP_GROUP="$(id -gn "${APP_USER}")"
SERVICE_FILE="/etc/systemd/system/nexgard-security.service"

cat >"${SERVICE_FILE}" <<EOF
[Unit]
Description=NexGard Security System
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${ROOT_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${ROOT_DIR}/run.sh
Restart=on-failure
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectControlGroups=true
ProtectKernelModules=true
ReadWritePaths=${ROOT_DIR}/data ${ROOT_DIR}/models

[Install]
WantedBy=multi-user.target
EOF

chmod 0644 "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable --now nexgard-security.service
sleep 2
systemctl --no-pager --full status nexgard-security.service

echo
echo "NexGard is installed and enabled at boot."
echo "Local dashboard: http://127.0.0.1:8000"
echo "Service logs: sudo journalctl -u nexgard-security -f"
