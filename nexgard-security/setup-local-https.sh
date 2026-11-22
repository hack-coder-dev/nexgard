#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo ./setup-local-https.sh" >&2
  exit 1
fi

APP_USER="${SUDO_USER:-}"
if [[ -z "$APP_USER" || "$APP_USER" == "root" ]]; then
  APP_USER="$(stat -c '%U' "$ROOT_DIR")"
fi
if ! id "$APP_USER" >/dev/null 2>&1; then
  echo "Could not determine the project owner account." >&2
  exit 1
fi
APP_GROUP="$(id -gn "$APP_USER")"
SERVER_IP="${NEXGARD_SERVER_IP:-$(hostname -I | awk '{print $1}') }"
SERVER_IP="$(printf '%s' "$SERVER_IP" | xargs)"
SERVER_HOST="$(hostname -s | tr -cd 'A-Za-z0-9.-')"
SERVER_HOST="${SERVER_HOST:-nexgard}"
if [[ -z "$SERVER_IP" ]]; then
  echo "Could not detect a LAN IP. Run with NEXGARD_SERVER_IP=192.168.x.x sudo ./setup-local-https.sh" >&2
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "OpenSSL is required. Install it with: sudo apt install openssl" >&2
  exit 1
fi
if ! command -v nginx >/dev/null 2>&1; then
  echo "Nginx is required. Install and configure Nginx before enabling local HTTPS." >&2
  exit 1
fi
if [[ ! -f "$ROOT_DIR/.env" ]]; then
  echo "Missing $ROOT_DIR/.env. Run ./setup.sh first." >&2
  exit 1
fi

TLS_DIR="/etc/nexgard-security/tls"
install -d -o root -g root -m 0700 "$TLS_DIR"
if [[ ! -s "$TLS_DIR/ca.key" || ! -s "$TLS_DIR/ca.crt" ]]; then
  openssl req -x509 -newkey rsa:4096 -sha256 -nodes -days 3650 \
    -keyout "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt" \
    -subj "/CN=NexGard Local Root CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash"
fi

openssl genrsa -out "$TLS_DIR/server.key" 2048
openssl req -new -key "$TLS_DIR/server.key" -out "$TLS_DIR/server.csr" \
  -subj "/CN=${SERVER_IP}"
cat > "$TLS_DIR/server.ext" <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:${SERVER_IP},DNS:${SERVER_HOST},DNS:${SERVER_HOST}.local
EXT
openssl x509 -req -in "$TLS_DIR/server.csr" \
  -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" -CAcreateserial \
  -out "$TLS_DIR/server.crt" -days 825 -sha256 -extfile "$TLS_DIR/server.ext"
chmod 0600 "$TLS_DIR/ca.key" "$TLS_DIR/server.key"
chmod 0644 "$TLS_DIR/ca.crt" "$TLS_DIR/server.crt"
rm -f "$TLS_DIR/server.csr" "$TLS_DIR/server.ext" "$TLS_DIR/ca.srl"

CA_COPY="$ROOT_DIR/nexgard-local-ca.crt"
install -o "$APP_USER" -g "$APP_GROUP" -m 0644 "$TLS_DIR/ca.crt" "$CA_COPY"

ACTIVE_SITE="/etc/nginx/sites-enabled/nexgard-security"
AVAILABLE_SITE="/etc/nginx/sites-available/nexgard-security-https"
DEFAULT_SITE="/etc/nginx/sites-enabled/default"
if [[ -e "$DEFAULT_SITE" || -L "$DEFAULT_SITE" ]]; then
  echo "An enabled Nginx default site exists at $DEFAULT_SITE." >&2
  echo "Review/disable that site first so this HTTPS site can own ports 80 and 443." >&2
  exit 1
fi
OLD_TARGET=""
OLD_REGULAR_BACKUP=""
if [[ -L "$ACTIVE_SITE" ]]; then OLD_TARGET="$(readlink "$ACTIVE_SITE")"; fi
if [[ -e "$ACTIVE_SITE" && ! -L "$ACTIVE_SITE" ]]; then
  OLD_REGULAR_BACKUP="${ACTIVE_SITE}.backup.$(date +%Y%m%d%H%M%S)"
  cp -a "$ACTIVE_SITE" "$OLD_REGULAR_BACKUP"
fi

cat > "$AVAILABLE_SITE" <<NGINX
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    return 308 https://\$host\$request_uri;
}

server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name _;

    ssl_certificate ${TLS_DIR}/server.crt;
    ssl_certificate_key ${TLS_DIR}/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:NexGardTLS:10m;
    ssl_session_timeout 10m;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
NGINX

ln -sfn "$AVAILABLE_SITE" "$ACTIVE_SITE"
if ! nginx -t; then
  if [[ -n "$OLD_TARGET" ]]; then
    ln -sfn "$OLD_TARGET" "$ACTIVE_SITE"
  elif [[ -n "$OLD_REGULAR_BACKUP" ]]; then
    cp -a "$OLD_REGULAR_BACKUP" "$ACTIVE_SITE"
  else
    rm -f "$ACTIVE_SITE"
  fi
  echo "Nginx configuration test failed; the previous active site was restored." >&2
  exit 1
fi

ENV_FILE="$ROOT_DIR/.env"
cp -a "$ENV_FILE" "${ENV_FILE}.before-https"
if grep -q '^NOVATECH_HTTPS_ONLY=' "$ENV_FILE"; then
  sed -i 's/^NOVATECH_HTTPS_ONLY=.*/NOVATECH_HTTPS_ONLY=true/' "$ENV_FILE"
else
  printf '\nNOVATECH_HTTPS_ONLY=true\n' >> "$ENV_FILE"
fi
chown "$APP_USER:$APP_GROUP" "$ENV_FILE"
chmod 0600 "$ENV_FILE"

systemctl reload nginx
systemctl restart nexgard-security.service

echo
echo "NexGard local HTTPS is enabled: https://${SERVER_IP}/"
echo "Root CA certificate copied to: ${CA_COPY}"
echo "Copy it to your Windows PC, then import it into Current User > Trusted Root Certification Authorities."
echo "Example PowerShell copy command (run on Windows):"
echo "  scp ${APP_USER}@${SERVER_IP}:${ROOT_DIR}/nexgard-local-ca.crt \"\$HOME\\Downloads\\nexgard-local-ca.crt\""
echo "Then import (PowerShell):"
echo "  Import-Certificate -FilePath \"\$HOME\\Downloads\\nexgard-local-ca.crt\" -CertStoreLocation Cert:\\CurrentUser\\Root"
echo "Restart Chrome and open https://${SERVER_IP}/. Microphone permission will now be available on this trusted HTTPS origin."
