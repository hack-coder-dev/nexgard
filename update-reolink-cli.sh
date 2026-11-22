#!/usr/bin/env bash
set -euo pipefail
CLI="$HOME/.local/bin/reolink-cli"
if [[ ! -x "$CLI" ]]; then
  echo "Reolink CLI was not found at $CLI. Install the official Reolink CLI for Linux first." >&2
  exit 1
fi
sudo systemctl stop nexgard-security
"$CLI" self-update --yes
"$CLI" --version
sudo systemctl start nexgard-security
sudo systemctl status nexgard-security --no-pager
