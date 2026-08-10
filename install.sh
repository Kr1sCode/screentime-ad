#!/usr/bin/env bash
# screentime-ad installer/updater — run INSIDE the target Debian LXC/host.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Kr1sCode/screentime-ad/main/install.sh)"
#
set -euo pipefail

REPO="${SCREENTIME_REPO:-Kr1sCode/screentime-ad}"
BRANCH="${SCREENTIME_BRANCH:-main}"

echo "==> screentime-ad: instaluję z ${REPO}@${BRANCH}"

export DEBIAN_FRONTEND=noninteractive
apt-get -qq update >/dev/null
apt-get -qq install -y python3 python3-venv python3-pip curl >/dev/null

mkdir -p /opt/screentime-ad /var/lib/screentime-ad
tmp=$(mktemp -d)
curl -fsSL "https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz" | tar -xz -C "$tmp"
src=$(echo "$tmp"/*/)

cp -r "$src"/app/. /opt/screentime-ad/
cp "$src"/requirements.txt /opt/screentime-ad/
cp "$src"/systemd/screentime-ad.service /etc/systemd/system/

python3 -m venv /opt/screentime-ad/venv
/opt/screentime-ad/venv/bin/pip install -q --upgrade pip
/opt/screentime-ad/venv/bin/pip install -q -r /opt/screentime-ad/requirements.txt

rm -rf "$tmp"

systemctl daemon-reload
systemctl enable --now screentime-ad.service

echo "==> gotowe. Panel: http://$(hostname -I | awk '{print $1}')/  (login: admin / admin — ZMIEŃ w panelu)"
