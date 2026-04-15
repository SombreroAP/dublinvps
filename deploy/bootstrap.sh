#!/usr/bin/env bash
# Bootstrap script for QuantVPS Dublin (Ubuntu 22.04/24.04 LTS).
# Run as root: bash bootstrap.sh
set -euo pipefail

# 1. Tight NTP sync — settlement is second-sensitive.
apt-get update
apt-get install -y chrony python3.11 python3.11-venv git
systemctl enable --now chrony
timedatectl set-timezone UTC

# 2. Dedicated unprivileged user.
id -u sniper >/dev/null 2>&1 || useradd -r -m -d /opt/sniper -s /bin/bash sniper

# 3. Code goes under /opt/sniper (clone or rsync your local repo here).
sudo -u sniper mkdir -p /opt/sniper/logs
cd /opt/sniper

# 4. Python venv + deps.
sudo -u sniper python3.11 -m venv .venv
sudo -u sniper .venv/bin/pip install --upgrade pip
sudo -u sniper .venv/bin/pip install -r requirements.txt

# 5. .env (must be created manually — chmod 600).
if [ ! -f .env ]; then
  cp .env.example .env
  chown sniper:sniper .env
  chmod 600 .env
  echo ">> Edit /opt/sniper/.env with real keys, then re-run."
  exit 0
fi
chmod 600 .env

# 6. systemd unit.
cp deploy/sniper.service /etc/systemd/system/sniper.service
systemctl daemon-reload
systemctl enable sniper.service

# 7. Latency check before starting (gut-check Dublin -> Polymarket).
echo ">> Dublin -> Polymarket CLOB latency:"
ping -c 5 clob.polymarket.com || true

echo ">> Bootstrap done. Start with: systemctl start sniper && journalctl -u sniper -f"
