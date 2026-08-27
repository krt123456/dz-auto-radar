#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE="${1:-/opt/sonardeals-radar}"
ROOT="/home/krt/car_deal_finder"
CONFIG="/etc/sonardeals-radar"
STATE="/var/lib/sonardeals-radar"
SITE="/srv/sonardeals-radar/site"

install -d -m 0755 /opt/sonardeals-radar "$CONFIG" "$STATE" /srv/sonardeals-radar
install -d -m 0700 "$STATE/jobs" "$STATE/logs" "$STATE/runtime"
install -d -m 0700 "$CONFIG/auction-source-feeds" "$STATE/authorized-feeds"

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
install -m 0755 "$SOURCE/auction_refresh.sh" /opt/sonardeals-radar/auction_refresh.sh
install -m 0644 "$SOURCE/auction_registry.py" /opt/sonardeals-radar/auction_registry.py
install -m 0644 "$SOURCE/auction_source_completion.py" /opt/sonardeals-radar/auction_source_completion.py
install -m 0644 "$SOURCE/listing_condition.py" /opt/sonardeals-radar/listing_condition.py
install -m 0755 "$SOURCE/build_auction_board.py" /opt/sonardeals-radar/build_auction_board.py
install -m 0755 "$SOURCE/run_source_adapter_watch.py" /opt/sonardeals-radar/run_source_adapter_watch.py
install -d -m 0755 /opt/sonardeals-radar/source_launchers_118
shopt -s nullglob
for launcher in "$SOURCE"/source_launchers_118/*.py; do
  install -m 0644 "$launcher" "/opt/sonardeals-radar/source_launchers_118/$(basename "$launcher")"
done
for metadata in "$SOURCE"/source_launchers_118/*.json; do
  install -m 0644 "$metadata" "/opt/sonardeals-radar/source_launchers_118/$(basename "$metadata")"
done
shopt -u nullglob

for source_data in \
  "$SOURCE/../auction_source_inventory.json" "$SITE/auction_source_inventory.json"; do
  if [[ -f "$source_data" ]]; then
    install -m 0644 "$source_data" /opt/sonardeals-radar/auction_source_inventory.json
    break
  fi
done
for source_data in \
  "$SOURCE/../source_completion_ledger.json" "$SITE/source_completion_ledger.json"; do
  if [[ -f "$source_data" ]]; then
    install -m 0644 "$source_data" /opt/sonardeals-radar/source_completion_ledger.json
    break
  fi
done
[[ -s /opt/sonardeals-radar/auction_source_inventory.json ]] || {
  echo "auction source inventory is unavailable" >&2; exit 1;
}
[[ -s /opt/sonardeals-radar/source_completion_ledger.json ]] || {
  echo "source completion ledger is unavailable" >&2; exit 1;
}

install -m 0755 "$SOURCE/zoll_auktion_fetcher.py" /opt/sonardeals-radar/zoll_auktion_fetcher.py
install -m 0755 "$SOURCE/zoll_auktion_fetcher.py" "$ROOT/zoll_auktion_fetcher.py"
install -m 0755 "$SOURCE/justiz_auktion_fetcher.py" /opt/sonardeals-radar/justiz_auktion_fetcher.py
install -m 0755 "$SOURCE/multi_official_auction_fetcher.py" /opt/sonardeals-radar/multi_official_auction_fetcher.py
install -m 0755 "$SOURCE/autobid_official_watch.py" /opt/sonardeals-radar/autobid_official_watch.py
install -m 0644 "$SOURCE/auction_raw_evidence.py" /opt/sonardeals-radar/auction_raw_evidence.py
install -m 0755 "$SOURCE/exleasingcar_official_watch.py" /opt/sonardeals-radar/exleasingcar_official_watch.py
install -m 0755 "$SOURCE/vpauto_official_watch.py" /opt/sonardeals-radar/vpauto_official_watch.py
install -m 0755 "$SOURCE/huutokaupat_official_watch.py" /opt/sonardeals-radar/huutokaupat_official_watch.py
install -m 0755 "$SOURCE/pvp_official_auction_watch.py" /opt/sonardeals-radar/pvp_official_auction_watch.py
install -m 0755 "$SOURCE/boe_kronofogden_watch_fetcher.py" /opt/sonardeals-radar/boe_kronofogden_watch_fetcher.py
install -m 0755 "$SOURCE/fr_cz_de_official_watch.py" /opt/sonardeals-radar/fr_cz_de_official_watch.py
install -m 0755 "$SOURCE/zoll_official_auction_watch.py" /opt/sonardeals-radar/zoll_official_auction_watch.py
install -m 0755 "$SOURCE/be_pl_pt_official_watch.py" /opt/sonardeals-radar/be_pl_pt_official_watch.py
install -m 0755 "$SOURCE/enrich_auction_ouedkniss.py" /opt/sonardeals-radar/enrich_auction_ouedkniss.py
install -m 0755 "$SOURCE/publish_radar_dashboard.py" /opt/sonardeals-radar/publish_radar_dashboard.py
install -m 0755 "$SOURCE/audit_best_selection.py" /opt/sonardeals-radar/audit_best_selection.py
install -m 0755 "$SOURCE/audit_live_convergence.py" /opt/sonardeals-radar/audit_live_convergence.py
install -m 0755 "$SOURCE/build_observed_value_board.py" /opt/sonardeals-radar/build_observed_value_board.py
install -m 0644 "$SOURCE/source_identity.py" /opt/sonardeals-radar/source_identity.py
install -m 0644 "$SOURCE/source_identity.py" "$ROOT/source_identity.py"
install -m 0755 "$SOURCE/import_live_offers_to_universe.py" "$ROOT/import_live_offers_to_universe.py"
install -m 0755 "$SOURCE/seal_validation_report.py" /opt/sonardeals-radar/seal_validation_report.py
install -m 0755 "$SOURCE/radar_freshness_sla.py" /opt/sonardeals-radar/radar_freshness_sla.py
install -m 0755 "$SOURCE/radar_control_client.py" /opt/sonardeals-radar/radar_control_client.py
install -m 0755 "$SOURCE/radar_poller.py" /opt/sonardeals-radar/radar_poller.py
install -m 0755 "$SOURCE/build_schengen_lake.py" /opt/sonardeals-radar/build_schengen_lake.py
install -m 0644 "$SOURCE/listing_availability.py" /opt/sonardeals-radar/listing_availability.py
install -m 0644 "$SOURCE/listing_availability.py" "$ROOT/listing_availability.py"
install -m 0755 "$SOURCE/validate_top400.py" "$ROOT/validate_top400.py"
install -m 0755 "$SOURCE/capture_alces_fx.py" /opt/sonardeals-radar/capture_alces_fx.py
install -d -m 0755 /opt/sonardeals-radar/certs
if [[ -f "$SOURCE/certs/sectigo-public-server-authentication-ca-dv-r36.pem" ]]; then
  install -m 0644 "$SOURCE/certs/sectigo-public-server-authentication-ca-dv-r36.pem" \
    /opt/sonardeals-radar/certs/sectigo-public-server-authentication-ca-dv-r36.pem
fi
install -d -m 0755 /opt/sonardeals-radar/dashboard
if [[ -f "$SOURCE/dashboard_index.html" ]]; then
  DASHBOARD_INDEX="$SOURCE/dashboard_index.html"
elif [[ -f "$SOURCE/dashboard/index.html" ]]; then
  DASHBOARD_INDEX="$SOURCE/dashboard/index.html"
elif [[ -f "$SOURCE/../index.html" ]]; then
  DASHBOARD_INDEX="$SOURCE/../index.html"
else
  echo "dashboard index is unavailable beneath $SOURCE" >&2
  exit 1
fi
install -m 0644 "$DASHBOARD_INDEX" /opt/sonardeals-radar/dashboard/index.html

for unit in "$SOURCE"/systemd/*; do
  install -m 0644 "$unit" "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload
if [[ "${INSTALL_ENABLE:-0}" == "1" ]]; then
  systemctl enable sonardeals-radar-poller.service \
    sonardeals-radar-smart-refresh.timer sonardeals-radar-full-refresh.timer \
    sonardeals-auction-refresh.timer
fi

if [[ "${INSTALL_START:-0}" == "1" ]]; then
  if [[ "${INSTALL_ENABLE:-0}" != "1" ]]; then
    echo "INSTALL_START=1 requires INSTALL_ENABLE=1" >&2
    exit 64
  fi
  systemctl restart sonardeals-radar-poller.service
  systemctl start sonardeals-radar-smart-refresh.timer sonardeals-radar-full-refresh.timer \
    sonardeals-auction-refresh.timer
fi

echo "RADAR_RUNTIME_INSTALL_PASS"
