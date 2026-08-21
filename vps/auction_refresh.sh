#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${RADAR_CAR_ROOT:-/home/krt/car_deal_finder}"
STATE="${RADAR_STATE_DIR:-/var/lib/sonardeals-radar}"
SITE="${RADAR_SITE:-/srv/sonardeals-radar/site}"
LOCK="${RADAR_REFRESH_LOCK_FILE:-/run/lock/sonardeals-radar-refresh.lock}"
IMPORTER="${RADAR_UNIVERSE_IMPORTER:-$ROOT/import_live_offers_to_universe.py}"
AUCTION_DATABASE="${RADAR_AUCTION_DATABASE:-$STATE/auction_offers.sqlite}"
AUDIT="$STATE/latest_selection_audit.json"
LIVE_AUDIT="$STATE/latest_live_selection_audit.json"
LIVE_DATA_URL="${RADAR_LIVE_DATA_URL:-https://krt123456.github.io/dz-auto-radar/data.enc}"

mkdir -p "$STATE/logs" "$STATE/runtime" /home/krt/eu_harvest
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "AUCTION_REFRESH_SKIPPED_BUSY at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  exit 0
fi
exec > >(tee -a "$STATE/logs/auction-refresh-$(date -u +%Y%m%d).log") 2>&1
echo "AUCTION_REFRESH_START at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

python3 /opt/sonardeals-radar/zoll_auktion_fetcher.py \
  --out /home/krt/eu_harvest/zoll_auktion_live.csv
python3 "$IMPORTER" --input-csv /home/krt/eu_harvest/zoll_auktion_live.csv \
  --db "$AUCTION_DATABASE" --batch-size 5000

python3 /opt/sonardeals-radar/justiz_auktion_fetcher.py \
  --out /home/krt/eu_harvest/justiz_auktion_live.csv \
  --report "$STATE/justiz_auction_fetch_report.json"
python3 "$IMPORTER" --input-csv /home/krt/eu_harvest/justiz_auktion_live.csv \
  --db "$AUCTION_DATABASE" --batch-size 5000

xvfb-run -a python3 /opt/sonardeals-radar/multi_official_auction_fetcher.py \
  --out /home/krt/eu_harvest/official_auction_live.csv \
  --report "$STATE/official_auction_fetch_report.json" \
  --max-candidates "${RADAR_AUCTION_MAX_CANDIDATES_PER_SOURCE:-40}"
python3 "$IMPORTER" --input-csv /home/krt/eu_harvest/official_auction_live.csv \
  --db "$AUCTION_DATABASE" --batch-size 5000

python3 /opt/sonardeals-radar/capture_alces_fx.py \
  --config "$STATE/fx/display_currency.json" \
  --intermediate /opt/sonardeals-radar/certs/sectigo-public-server-authentication-ca-dv-r36.pem \
  || echo "AUCTION_FX_CAPTURE_SKIPPED"

generated_at="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["data_generated_at_utc"])' "$ROOT/mobile_site_local/board.json")"
python3 /opt/sonardeals-radar/build_auction_board.py \
  --database "$AUCTION_DATABASE" \
  --board "$ROOT/mobile_site_local/board.json" \
  --generated-at "$generated_at" \
  --output "$ROOT/mobile_site_local/auction_lane.json"
python3 /opt/sonardeals-radar/enrich_auction_ouedkniss.py \
  --lane "$ROOT/mobile_site_local/auction_lane.json" \
  --cache "$STATE/runtime/ouedkniss_auction_reference_cache.json" \
  --ttl-hours "${RADAR_OUEDKNISS_REFERENCE_TTL_HOURS:-6}" \
  --max-queries "${RADAR_OUEDKNISS_MAX_QUERIES:-80}" \
  || echo "AUCTION_OUEDKNISS_REFERENCE_SKIPPED"

python3 /opt/sonardeals-radar/publish_radar_dashboard.py \
  --root "$ROOT" --site "$SITE" --prepare-only --top-n "${VERIFIED_TARGET:-10000}"
python3 /opt/sonardeals-radar/audit_best_selection.py \
  --root "$ROOT" --site "$SITE" --output "$AUDIT" --top-n "${VERIFIED_TARGET:-10000}"
python3 /opt/sonardeals-radar/publish_radar_dashboard.py \
  --root "$ROOT" --site "$SITE" --push-only

expected_generation="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["generation_id"])' "$AUDIT")"
python3 /opt/sonardeals-radar/audit_live_convergence.py \
  --root "$ROOT" --data-url "$LIVE_DATA_URL" --expected-generation "$expected_generation" \
  --selection-manifest "$STATE/latest_selection_manifest.json" \
  --selection-audit "$AUDIT" --output "$LIVE_AUDIT" \
  --deadline-sec "${RADAR_AUCTION_LIVE_AUDIT_DEADLINE_SEC:-900}" \
  --request-timeout-sec 30 --initial-backoff-sec 5 --max-backoff-sec 60 --max-network-errors 8

echo "AUCTION_REFRESH_PASS at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
