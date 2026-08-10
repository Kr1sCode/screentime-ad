#!/usr/bin/env bash
# screentime-ad agent installer — uruchom jako root NA maszynie Linux (np. CachyOS AD-joined).
#
#   sudo SCREENTIME_SERVER=http://192.168.1.50 SCREENTIME_TOKEN=<token_z_panelu> \
#     bash -c "$(curl -fsSL https://raw.githubusercontent.com/Kr1sCode/screentime-ad/main/agent_linux/install.sh)"
#
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "uruchom jako root" >&2; exit 1; }

REPO="${SCREENTIME_REPO:-Kr1sCode/screentime-ad}"
BRANCH="${SCREENTIME_BRANCH:-main}"
SERVER="${SCREENTIME_SERVER:-}"
TOKEN="${SCREENTIME_TOKEN:-}"

[ -n "$SERVER" ] || read -rp "Adres serwera (np. http://192.168.1.50): " SERVER
[ -n "$TOKEN" ]  || read -rp "Token agenta (z panelu, sekcja Konfiguracja): " TOKEN

command -v zenity >/dev/null 2>&1 || { pacman -Sy --noconfirm zenity 2>/dev/null || apt-get -qq update && apt-get -qq install -y zenity; }

mkdir -p /opt/screentime-ad-agent /etc/screentime-ad
tmp=$(mktemp -d)
curl -fsSL "https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz" | tar -xz -C "$tmp"
src=$(echo "$tmp"/*/)
cp "$src"/agent_linux/agent.py /opt/screentime-ad-agent/
cp "$src"/agent_linux/screentime-ad-agent.service /etc/systemd/system/
rm -rf "$tmp"

cat > /etc/screentime-ad/agent.conf <<JSON
{"server_url": "${SERVER}", "token": "${TOKEN}"}
JSON
chmod 600 /etc/screentime-ad/agent.conf

systemctl daemon-reload
systemctl enable --now screentime-ad-agent.service
echo "==> agent zainstalowany i uruchomiony (systemctl status screentime-ad-agent)"
