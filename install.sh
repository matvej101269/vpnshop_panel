#!/usr/bin/env bash
set -Eeuo pipefail

DOMAIN=""
PANEL_PORT=""
REPO_URL=""
INSTALL_DIR="/opt/vpnshop"

usage() {
  cat <<'EOF'
Usage: sudo bash install.sh --domain vpn.example.com --port 443 --repo https://github.com/OWNER/REPO.git

Required:
  --domain   Domain name pointing to this VPS
  --port     Public HTTPS port for the panel (1-65535; 443 is recommended)
  --repo     Public GitHub repository URL
Optional:
  --dir      Install directory (default: /opt/vpnshop)
EOF
}

while (($#)); do
  case "$1" in
    --domain) DOMAIN="${2:?Missing domain}"; shift 2 ;;
    --port) PANEL_PORT="${2:?Missing port}"; shift 2 ;;
    --repo) REPO_URL="${2:?Missing repository URL}"; shift 2 ;;
    --dir) INSTALL_DIR="${2:?Missing install directory}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then echo "Run this installer as root (use sudo)." >&2; exit 1; fi
[[ "$DOMAIN" =~ ^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$ ]] || { echo "Invalid domain name." >&2; exit 2; }
[[ "$PANEL_PORT" =~ ^[0-9]{1,5}$ ]] && ((PANEL_PORT >= 1 && PANEL_PORT <= 65535)) || { echo "Port must be between 1 and 65535." >&2; exit 2; }
[[ "$REPO_URL" =~ ^https://github\.com/[^/]+/[^/]+(\.git)?$ ]] || { echo "Use a public GitHub HTTPS repository URL with --repo." >&2; exit 2; }
[[ ! -e "$INSTALL_DIR" ]] || { echo "Install path already exists: $INSTALL_DIR" >&2; exit 1; }

if [[ "$PANEL_PORT" == 80 ]]; then echo "Port 80 is reserved for certificate validation and HTTP redirect." >&2; exit 2; fi
if [[ "$PANEL_PORT" == 8000 ]]; then echo "Port 8000 is reserved for the app behind the local reverse proxy." >&2; exit 2; fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git docker.io docker-compose-v2 nginx certbot openssl
systemctl enable --now docker
systemctl enable nginx

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q 'Status: active'; then
  ufw allow 80/tcp
  ufw allow "$PANEL_PORT/tcp"
fi

mkdir -p /var/www/certbot/.well-known/acme-challenge
cat >/etc/nginx/sites-available/vpnshop <<'NGINX_HTTP'
server {
    listen 80;
    listen [::]:80;
    server_name __DOMAIN__;
    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 404; }
}
NGINX_HTTP
sed -i "s/__DOMAIN__/$DOMAIN/g" /etc/nginx/sites-available/vpnshop
ln -sfn /etc/nginx/sites-available/vpnshop /etc/nginx/sites-enabled/vpnshop
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx || systemctl start nginx

certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" \
  --non-interactive --agree-tos --register-unsafely-without-email

git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
ADMIN_USER="admin_$(openssl rand -hex 3)"
ADMIN_PASSWORD="$(openssl rand -hex 24)"
CONTROL_TOKEN="$(openssl rand -hex 32)"
BACKUP_ENCRYPTION_KEY="$(openssl rand -base64 32 | tr '+/' '-_')"
PUBLIC_URL="https://$DOMAIN"
if [[ "$PANEL_PORT" != 443 ]]; then PUBLIC_URL="$PUBLIC_URL:$PANEL_PORT"; fi
cat >"$INSTALL_DIR/.env" <<EOF
DATABASE_URL=sqlite:///./data/vpnshop.db
ADMIN_USER=$ADMIN_USER
ADMIN_PASSWORD=$ADMIN_PASSWORD
CONTROL_TOKEN=$CONTROL_TOKEN
BACKUP_ENCRYPTION_KEY=$BACKUP_ENCRYPTION_KEY
PUBLIC_BASE_URL=$PUBLIC_URL
EOF
chmod 600 "$INSTALL_DIR/.env"

if [[ "$PANEL_PORT" == 443 ]]; then
  REDIRECT_URL='https://$host$request_uri'
else
  REDIRECT_URL='https://$host:'"$PANEL_PORT"'$request_uri'
fi
cat >/etc/nginx/sites-available/vpnshop <<NGINX_TLS
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    access_log off;
    error_log /var/log/nginx/vpnshop-error.log warn;
    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 $REDIRECT_URL; }
}

server {
    listen $PANEL_PORT ssl;
    listen [::]:$PANEL_PORT ssl;
    server_name $DOMAIN;
    access_log off;
    error_log /var/log/nginx/vpnshop-error.log warn;
    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    client_max_body_size 2m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
NGINX_TLS
nginx -t
systemctl reload nginx || systemctl start nginx

cat >/etc/letsencrypt/renewal-hooks/deploy/reload-vpnshop-nginx <<'HOOK'
#!/usr/bin/env sh
systemctl reload nginx
HOOK
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-vpnshop-nginx

cd "$INSTALL_DIR"
docker compose up -d --build
systemctl reload nginx

CREDENTIALS_FILE="/root/vpnshop-admin.txt"
cat >"$CREDENTIALS_FILE" <<EOF
VPN Shop admin access
URL: $PUBLIC_URL/admin
Username: $ADMIN_USER
Password: $ADMIN_PASSWORD
EOF
chmod 600 "$CREDENTIALS_FILE"

echo
echo "Installation complete. Save these credentials securely:"
echo "Panel URL: $PUBLIC_URL/admin"
echo "Username:  $ADMIN_USER"
echo "Password:  $ADMIN_PASSWORD"
echo "A copy was saved to $CREDENTIALS_FILE (mode 600)."
echo "Configure Lava.top, 3x-ui, Telegram and Happ in the panel settings."
