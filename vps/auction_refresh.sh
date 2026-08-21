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
PVP_WATCH="$STATE/runtime/pvp_official_auction_watch.json"
BOE_KRONO_WATCH="$STATE/runtime/boe_kronofogden_official_auction_watch.json"
FR_CZ_DE_WATCH="$STATE/runtime/fr_cz_de_official_auction_watch.json"
ZOLL_WATCH="$STATE/runtime/zoll_official_auction_watch.json"
BE_PL_PT_WATCH="$STATE/runtime/be_pl_pt_official_auction_watch.json"
PUBLIC_WATCH="$ROOT/mobile_site_local/official_auction_watch.json"

mkdir -p "$STATE/logs" "$STATE/runtime" /home/krt/eu_harvest
exec 9>"$LOCK"
if ! flock -w "${RADAR_AUCTION_LOCK_WAIT_SEC:-3500}" 9; then
  echo "AUCTION_REFRESH_SKIPPED_BUSY at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  exit 0
fi
exec > >(tee -a "$STATE/logs/auction-refresh-$(date -u +%Y%m%d).log") 2>&1
echo "AUCTION_REFRESH_START at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SNAPSHOT_CUTOFF="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
STRICT_FETCH_TIMEOUT="${RADAR_STRICT_AUCTION_FETCH_TIMEOUT_SEC:-600}"
BROAD_FETCH_TIMEOUT="${RADAR_OFFICIAL_WATCH_FETCH_TIMEOUT_SEC:-900}"

if timeout --signal=TERM --kill-after=30s "$STRICT_FETCH_TIMEOUT" \
  python3 /opt/sonardeals-radar/zoll_auktion_fetcher.py \
  --out /home/krt/eu_harvest/zoll_auktion_live.csv; then
  python3 "$IMPORTER" --input-csv /home/krt/eu_harvest/zoll_auktion_live.csv \
    --db "$AUCTION_DATABASE" --batch-size 5000 \
    || echo "STRICT_AUCTION_IMPORT_FAILED source=zoll-auktion"
else
  echo "STRICT_AUCTION_SOURCE_FAILED source=zoll-auktion"
fi

if timeout --signal=TERM --kill-after=30s "$STRICT_FETCH_TIMEOUT" \
  python3 /opt/sonardeals-radar/justiz_auktion_fetcher.py \
  --out /home/krt/eu_harvest/justiz_auktion_live.csv \
  --report "$STATE/justiz_auction_fetch_report.json"; then
  python3 "$IMPORTER" --input-csv /home/krt/eu_harvest/justiz_auktion_live.csv \
    --db "$AUCTION_DATABASE" --batch-size 5000 \
    || echo "STRICT_AUCTION_IMPORT_FAILED source=justiz-auktion"
else
  echo "STRICT_AUCTION_SOURCE_FAILED source=justiz-auktion"
fi

if timeout --signal=TERM --kill-after=30s "$STRICT_FETCH_TIMEOUT" \
  xvfb-run -a python3 /opt/sonardeals-radar/multi_official_auction_fetcher.py \
  --out /home/krt/eu_harvest/official_auction_live.csv \
  --report "$STATE/official_auction_fetch_report.json" \
  --max-candidates "${RADAR_AUCTION_MAX_CANDIDATES_PER_SOURCE:-40}"; then
  python3 "$IMPORTER" --input-csv /home/krt/eu_harvest/official_auction_live.csv \
    --db "$AUCTION_DATABASE" --batch-size 5000 \
    || echo "STRICT_AUCTION_IMPORT_FAILED source=multi-official"
else
  echo "STRICT_AUCTION_SOURCE_FAILED source=multi-official"
fi

# Broad official watch: keep every current/future vehicle lot, then label price
# semantics and bidder/import eligibility honestly in a separate public lane.
# Each connector writes atomically; a transient source failure keeps the prior
# snapshot available for at most the builder's eight-hour freshness window.
run_official_watch() {
  local source_key="$1"
  shift
  local status
  if timeout --signal=TERM --kill-after=30s "$BROAD_FETCH_TIMEOUT" "$@"; then
    return 0
  else
    status=$?
    echo "OFFICIAL_WATCH_SOURCE_FAILED source=$source_key status=$status"
    return 0
  fi
}

OFFICIAL_WATCH_PIDS=()
run_official_watch "pvp-giustizia" \
  python3 /opt/sonardeals-radar/pvp_official_auction_watch.py \
  --out "$PVP_WATCH" --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "boe-subastas,kronofogden" \
  python3 /opt/sonardeals-radar/boe_kronofogden_watch_fetcher.py \
  --out "$BOE_KRONO_WATCH" --report "$STATE/boe_kronofogden_watch_report.json" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "encheres-du-domaine,nabidka-majetku,justiz-auktion,onlineveilingmeester" \
  python3 /opt/sonardeals-radar/fr_cz_de_official_watch.py \
  --out "$FR_CZ_DE_WATCH" --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "zoll-auktion" \
  python3 /opt/sonardeals-radar/zoll_official_auction_watch.py \
  --out "$ZOLL_WATCH" --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" \
  --workers "${RADAR_ZOLL_WATCH_WORKERS:-8}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "finshop,licytacje-komornik,e-leiloes" \
  python3 /opt/sonardeals-radar/be_pl_pt_official_watch.py \
  --out "$BE_PL_PT_WATCH" --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" &
OFFICIAL_WATCH_PIDS+=("$!")

for watch_pid in "${OFFICIAL_WATCH_PIDS[@]}"; do
  wait "$watch_pid"
done

MONITORED_INPUT_ARGS=()
for watch_file in \
  "$PVP_WATCH" "$BOE_KRONO_WATCH" "$FR_CZ_DE_WATCH" "$ZOLL_WATCH" "$BE_PL_PT_WATCH"; do
  if [[ -s "$watch_file" ]]; then
    MONITORED_INPUT_ARGS+=(--monitored-input "$watch_file")
  else
    echo "OFFICIAL_WATCH_SNAPSHOT_MISSING path=$watch_file"
  fi
done

python3 /opt/sonardeals-radar/capture_alces_fx.py \
  --config "$STATE/fx/display_currency.json" \
  --intermediate /opt/sonardeals-radar/certs/sectigo-public-server-authentication-ca-dv-r36.pem \
  || echo "AUCTION_FX_CAPTURE_SKIPPED"

generated_at="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["data_generated_at_utc"])' "$ROOT/mobile_site_local/board.json")"
python3 /opt/sonardeals-radar/build_auction_board.py \
  --database "$AUCTION_DATABASE" \
  --board "$ROOT/mobile_site_local/board.json" \
  --cutoff "$SNAPSHOT_CUTOFF" \
  --generated-at "$generated_at" \
  --output "$ROOT/mobile_site_local/auction_lane.json" \
  --monitored-output "$PUBLIC_WATCH" \
  "${MONITORED_INPUT_ARGS[@]}"
python3 /opt/sonardeals-radar/enrich_auction_ouedkniss.py \
  --lane "$ROOT/mobile_site_local/auction_lane.json" \
  --cache "$STATE/runtime/ouedkniss_auction_reference_cache.json" \
  --ttl-hours "${RADAR_OUEDKNISS_REFERENCE_TTL_HOURS:-6}" \
  --max-queries "${RADAR_OUEDKNISS_MAX_QUERIES:-80}" \
  || echo "AUCTION_OUEDKNISS_REFERENCE_SKIPPED"
python3 /opt/sonardeals-radar/enrich_auction_ouedkniss.py \
  --lane "$PUBLIC_WATCH" \
  --cache "$STATE/runtime/ouedkniss_auction_reference_cache.json" \
  --ttl-hours "${RADAR_OUEDKNISS_REFERENCE_TTL_HOURS:-6}" \
  --max-queries "${RADAR_OUEDKNISS_BROAD_MAX_QUERIES:-80}" \
  || echo "OFFICIAL_WATCH_OUEDKNISS_REFERENCE_SKIPPED"

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
