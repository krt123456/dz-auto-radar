#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE="${1:-/opt/sonardeals-radar}"
ROOT="/home/krt/car_deal_finder"
CONFIG="/etc/sonardeals-radar"
STATE="/var/lib/sonardeals-radar"
SITE="/srv/sonardeals-radar/site"

install -d -m 0755 /opt/sonardeals-radar "$CONFIG" "$STATE" /srv/sonardeals-radar
install -d -m 0700 "$STATE/jobs" "$STATE/logs" "$STATE/runtime"

if [[ ! -s "$CONFIG/pin" ]]; then
  if [[ ! -s "$ROOT/.mobile_site_secret" ]]; then
    echo "existing dashboard secret is unavailable" >&2
    exit 1
  fi
  install -m 0600 "$ROOT/.mobile_site_secret" "$CONFIG/pin"
fi
if [[ ! -s "$CONFIG/internal-token" ]]; then
  umask 077
  openssl rand -base64 48 | tr -d '\n' > "$CONFIG/internal-token"
fi
chmod 0600 "$CONFIG/pin" "$CONFIG/internal-token"

if [[ ! -d "$SITE/.git" ]]; then
  if [[ -d "$SITE" ]] && find "$SITE" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "refusing to replace a non-git publication directory: $SITE" >&2
    exit 1
  fi
  git clone git@github-dzauto:krt123456/dz-auto-radar.git "$SITE"
fi

install -m 0755 "$SOURCE/radar_refresh.sh" /opt/sonardeals-radar/radar_refresh.sh
install -m 0755 "$SOURCE/publish_radar_dashboard.py" /opt/sonardeals-radar/publish_radar_dashboard.py
install -m 0755 "$SOURCE/audit_best_selection.py" /opt/sonardeals-radar/audit_best_selection.py
install -m 0755 "$SOURCE/radar_control_client.py" /opt/sonardeals-radar/radar_control_client.py
install -m 0755 "$SOURCE/radar_poller.py" /opt/sonardeals-radar/radar_poller.py
install -m 0755 "$SOURCE/build_schengen_lake.py" /opt/sonardeals-radar/build_schengen_lake.py
install -d -m 0755 /opt/sonardeals-radar/dashboard
install -m 0644 "$SOURCE/dashboard/index.html" /opt/sonardeals-radar/dashboard/index.html

for unit in "$SOURCE"/systemd/*; do
  install -m 0644 "$unit" "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload
systemctl enable sonardeals-radar-poller.service \
  sonardeals-radar-smart-refresh.timer sonardeals-radar-full-refresh.timer

if [[ "${INSTALL_START:-0}" == "1" ]]; then
  systemctl restart sonardeals-radar-poller.service
  systemctl start sonardeals-radar-smart-refresh.timer sonardeals-radar-full-refresh.timer
fi

echo "RADAR_RUNTIME_INSTALL_PASS"
