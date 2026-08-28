#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${RADAR_CAR_ROOT:-/home/krt/car_deal_finder}"
STATE="${RADAR_STATE_DIR:-/var/lib/sonardeals-radar}"
SITE="${RADAR_SITE:-/srv/sonardeals-radar/site}"
# Keep hourly auction collection independent from the much longer full-radar
# validation run.  Publication itself still validates every shared artifact.
LOCK="${RADAR_AUCTION_REFRESH_LOCK_FILE:-/run/lock/sonardeals-auction-refresh.lock}"
IMPORTER="${RADAR_UNIVERSE_IMPORTER:-$ROOT/import_live_offers_to_universe.py}"
AUCTION_DATABASE="${RADAR_AUCTION_DATABASE:-$STATE/auction_offers.sqlite}"
AUDIT="$STATE/latest_selection_audit.json"
LIVE_AUDIT="$STATE/latest_live_selection_audit.json"
LIVE_DATA_URL="${RADAR_LIVE_DATA_URL:-https://krt123456.github.io/dz-auto-radar/data.enc}"
BOE_KRONO_WATCH="$STATE/runtime/boe_kronofogden_official_auction_watch.json"
FR_CZ_DE_WATCH="$STATE/runtime/fr_cz_de_official_auction_watch.json"
ZOLL_WATCH="$STATE/runtime/zoll_official_auction_watch.json"
BE_PL_PT_WATCH="$STATE/runtime/be_pl_pt_official_auction_watch.json"
ELICYTACJE_KAS_WATCH="$STATE/runtime/elicytacje_kas_official_auction_watch.json"
COPART_SCHENGEN_WATCH="$STATE/runtime/copart_schengen_official_auction_watch.json"
ADDITIONAL_SCHENGEN_WATCH="$STATE/runtime/additional_schengen_official_auction_watch.json"
ADDITIONAL_BATCH_WATCH="$STATE/runtime/additional_batch_official_auction_watch.json"
MEGA_BATCH_WATCH="$STATE/runtime/mega_batch_official_auction_watch.json"
VEBEG_FAST_WATCH="$STATE/runtime/vebeg_fast_watch.json"
ASTE_WATCH="$STATE/runtime/aste_watch.json"
KLARAVIK_WATCH="$STATE/runtime/klaravik_watch.json"
VEACOM_WATCH="$STATE/runtime/veacom_watch.json"
AUTOAUCTION24_WATCH="$STATE/runtime/autoauction24_official_auction_watch.json"
AUCTION24_CZ_WATCH="$STATE/runtime/auction24_cz_official_auction_watch.json"
PVP_WATCH="$STATE/runtime/pvp_official_auction_watch.json"
SCHENGEN_WIDE_WATCH="$STATE/runtime/schengen_wide_official_auction_watch.json"
RETRADE_WATCH="$STATE/runtime/retrade_official_auction_watch.json"
TROOSTWIJK_WATCH="$STATE/runtime/troostwijk_watch.json"
AUKSJONEN_WATCH="$STATE/runtime/auksjonen_watch.json"
AUTOBID_WATCH="$STATE/runtime/autobid_official_auction_watch.json"
EXLEASINGCAR_WATCH="$STATE/runtime/exleasingcar_official_auction_watch.json"
VPAUTO_WATCH="$STATE/runtime/vpauto_official_auction_watch.json"
RBAUCTION_WATCH="$STATE/runtime/rbauction_official_auction_watch.json"
AUTOROLA_WATCH="$STATE/runtime/autorola_official_auction_watch.json"
HUUTOKAUPAT_WATCH="$STATE/runtime/huutokaupat_official_auction_watch.json"
VAVATO_WATCH="$STATE/runtime/vavato_official_auction_watch.json"
PONIP_WATCH="$STATE/runtime/ponip_official_auction_watch.json"
CARAUKCE_WATCH="$STATE/runtime/caraukce_official_auction_watch.json"
AURENA_WATCH="$STATE/runtime/aurena_official_auction_watch.json"
AUCTIONMASTER_WATCH="$STATE/runtime/auctionmaster_official_auction_watch.json"
BILWEB_WATCH="$STATE/runtime/bilweb_official_auction_watch.json"
KVDCARS_WATCH="$STATE/runtime/kvdcars_official_auction_watch.json"
BILAUPPBOD_WATCH="$STATE/runtime/bilauppbod_official_auction_watch.json"
KIERTONET_WATCH="$STATE/runtime/kiertonet_official_auction_watch.json"
AUKTIONSHUSET_DAB_WATCH="$STATE/runtime/auktionshuset_dab_official_auction_watch.json"
AGORASTORE_WATCH="$STATE/runtime/agorastore_official_auction_watch.json"
SOURCE_ADAPTER_WATCH="$STATE/runtime/source_adapter_watch.json"
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
  --source encheres-du-domaine --source boe-subastas --source kronofogden \
  --source finshop --source onlineveilingmeester --source licytacje-komornik \
  --source nabidka-majetku \
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
  --source finshop --source licytacje-komornik --source e-leiloes \
  --out "$BE_PL_PT_WATCH" --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "elicytacje-kas" \
  python3 /opt/sonardeals-radar/elicytacje_kas_official_watch.py \
  --out "$ELICYTACJE_KAS_WATCH" --raw-root "$STATE/raw-evidence" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" \
  --page-size 2000 --max-rows 10000 &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "copart-de,copart-es,copart-fi" \
  python3 /opt/sonardeals-radar/copart_schengen_official_watch.py \
  --out "$COPART_SCHENGEN_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" \
  --workers "${RADAR_COPART_WATCH_WORKERS:-3}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "oksjonikeskus,anabi,eaukcionai,sodnedrazbe,e-arveres-mnv,ropk,nva-latvia" \
  python3 /opt/sonardeals-radar/additional_schengen_official_watch.py \
  --out "$ADDITIONAL_SCHENGEN_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "vebeg,portaldrazeb,campenauktioner,nav-hu" \
  python3 /opt/sonardeals-radar/additional_batch_official_watch.py \
  --out "$ADDITIONAL_BATCH_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" \
  --source vebeg --source portaldrazeb --source campenauktioner --source nav-hu \
  &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "mega-batch"   python3 /opt/sonardeals-radar/mega_batch_fetcher.py   --out "$MEGA_BATCH_WATCH"   --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}"   &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "vebeg-fast"   python3 /opt/sonardeals-radar/vebeg_fast_fetcher.py   --out "$VEBEG_FAST_WATCH"   --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" &
OFFICIAL_WATCH_PIDS+=("$!")


run_official_watch "auksjonen"   python3 /opt/sonardeals-radar/auksjonen_fetcher.py   --out "$AUKSJONEN_WATCH" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "autobid" \
  python3 /opt/sonardeals-radar/autobid_official_watch.py \
  --out "$AUTOBID_WATCH" --raw-root "$STATE/raw-evidence" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" \
  --max-ids "${RADAR_AUTOBID_MAX_IDS:-20000}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "exleasingcar" \
  python3 /opt/sonardeals-radar/exleasingcar_official_watch.py \
  --out "$EXLEASINGCAR_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" \
  --workers "${RADAR_EXLEASINGCAR_WATCH_WORKERS:-16}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "vpauto" \
  python3 /opt/sonardeals-radar/vpauto_official_watch.py \
  --out "$VPAUTO_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" \
  --workers "${RADAR_VPAUTO_WATCH_WORKERS:-8}" \
  --skip-details &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "rbauction-eu" \
  python3 /opt/sonardeals-radar/rbauction_official_watch.py \
  --out "$RBAUCTION_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "autorola-eu" \
  python3 /opt/sonardeals-radar/autorola_official_watch.py \
  --out "$AUTOROLA_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" \
  --max-workers "${RADAR_AUTOROLA_WATCH_WORKERS:-6}" \
  --max-catalogue-rows "${RADAR_AUTOROLA_MAX_CATALOGUE_ROWS:-1000000}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "huutokaupat" \
  python3 /opt/sonardeals-radar/huutokaupat_official_watch.py \
  --out "$HUUTOKAUPAT_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" \
  --workers "${RADAR_HUUTOKAUPAT_WATCH_WORKERS:-4}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "vavato" \
  python3 /opt/sonardeals-radar/vavato_official_watch.py \
  --out "$VAVATO_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" \
  --workers "${RADAR_VAVATO_WATCH_WORKERS:-4}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "fina-ponip" \
  python3 /opt/sonardeals-radar/ponip_official_watch.py \
  --out "$PONIP_WATCH" \
  --timeout "${RADAR_PONIP_WATCH_TIMEOUT_SEC:-360}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "caraukce" \
  python3 /opt/sonardeals-radar/caraukce_official_watch.py \
  --out "$CARAUKCE_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "aurena" \
  python3 /opt/sonardeals-radar/aurena_official_watch.py \
  --out "$AURENA_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "auctionmaster" \
  python3 /opt/sonardeals-radar/auctionmaster_official_watch.py \
  --out "$AUCTIONMASTER_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "bilweb" \
  python3 /opt/sonardeals-radar/bilweb_official_watch.py \
  --out "$BILWEB_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "kvdcars" \
  python3 /opt/sonardeals-radar/kvdcars_official_watch.py \
  --out "$KVDCARS_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "bilauppbod" \
  python3 /opt/sonardeals-radar/bilauppbod_official_watch.py \
  --out "$BILAUPPBOD_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" \
  --workers "${RADAR_BILAUPPBOD_WATCH_WORKERS:-6}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "kiertonet" \
  python3 /opt/sonardeals-radar/kiertonet_official_watch.py \
  --out "$KIERTONET_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "auktionshuset-dab" \
  python3 /opt/sonardeals-radar/auktionshuset_dab_official_watch.py \
  --out "$AUKTIONSHUSET_DAB_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "agorastore" \
  python3 /opt/sonardeals-radar/agorastore_official_watch.py \
  --out "$AGORASTORE_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "troostwijk"   python3 /opt/sonardeals-radar/troostwijk_fetcher.py   --out "$TROOSTWIJK_WATCH" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "aste-giudiziarie"   python3 /opt/sonardeals-radar/aste_fetcher.py   --out "$ASTE_WATCH" &
OFFICIAL_WATCH_PIDS+=("$!")

run_official_watch "klaravik-se,klaravik-dk" \
  python3 /opt/sonardeals-radar/klaravik_official_watch.py \
  --out "$KLARAVIK_WATCH" --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" &
OFFICIAL_WATCH_PIDS+=("$!")

run_official_watch "veacom" \
  python3 /opt/sonardeals-radar/veacom_official_watch.py \
  --out "$VEACOM_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" \
  --workers "${RADAR_VEACOM_WORKERS:-4}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "autoauction24-ch" \
  python3 /opt/sonardeals-radar/autoauction24_official_watch.py \
  --out "$AUTOAUCTION24_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "auction24-cz" \
  python3 /opt/sonardeals-radar/auction24_cz_official_watch.py \
  --out "$AUCTION24_CZ_WATCH" \
  --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-35}" \
  --workers "${RADAR_AUCTION24_CZ_WATCH_WORKERS:-4}" &
OFFICIAL_WATCH_PIDS+=("$!")

run_official_watch "pvp-giustizia"   python3 /opt/sonardeals-radar/pvp_official_auction_watch.py   --out "$PVP_WATCH" --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "schengen-wide"   python3 /opt/sonardeals-radar/schengen_wide_official_watch.py   --out "$SCHENGEN_WIDE_WATCH" --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "retrade"   python3 /opt/sonardeals-radar/retrade_official_auction_watch.py   --out "$RETRADE_WATCH" --timeout "${RADAR_OFFICIAL_WATCH_TIMEOUT_SEC:-30}" &
OFFICIAL_WATCH_PIDS+=("$!")
run_official_watch "source-adapters-118" \
  python3 /opt/sonardeals-radar/run_source_adapter_watch.py \
  --out "$SOURCE_ADAPTER_WATCH" \
  --config-dir "${RADAR_AUCTION_SOURCE_ADAPTER_CONFIG_DIR:-/etc/sonardeals-radar/auction-source-feeds}" \
  --feed-root "${RADAR_AUCTION_SOURCE_ADAPTER_FEED_ROOT:-$STATE/authorized-feeds}" \
  --work-dir "$STATE/runtime/source-adapter-work" \
  --timeout-seconds "${RADAR_SOURCE_ADAPTER_TIMEOUT_SEC:-120}" \
  --workers "${RADAR_SOURCE_ADAPTER_WORKERS:-6}" &
OFFICIAL_WATCH_PIDS+=("$!")

for watch_pid in "${OFFICIAL_WATCH_PIDS[@]}"; do
  wait "$watch_pid"
done

MONITORED_INPUT_ARGS=()
# The generated adapter bridge reports all 118 source identities and merges
# every configured source feed into the same public auction watch.
for watch_file in \
  "$BOE_KRONO_WATCH" "$FR_CZ_DE_WATCH" "$ZOLL_WATCH" "$BE_PL_PT_WATCH" \
  "$ELICYTACJE_KAS_WATCH" "$COPART_SCHENGEN_WATCH" "$ADDITIONAL_SCHENGEN_WATCH" \
  "$ADDITIONAL_BATCH_WATCH" "$MEGA_BATCH_WATCH" "$VEBEG_FAST_WATCH" "$AUKSJONEN_WATCH" \
  "$AUTOBID_WATCH" "$EXLEASINGCAR_WATCH" "$VPAUTO_WATCH" "$RBAUCTION_WATCH" "$AUTOROLA_WATCH" "$HUUTOKAUPAT_WATCH" "$VAVATO_WATCH" "$PONIP_WATCH" "$CARAUKCE_WATCH" "$AURENA_WATCH" "$AUCTIONMASTER_WATCH" "$BILWEB_WATCH" "$KVDCARS_WATCH" "$BILAUPPBOD_WATCH" "$KIERTONET_WATCH" "$AUKTIONSHUSET_DAB_WATCH" "$ASTE_WATCH" "$KLARAVIK_WATCH" "$VEACOM_WATCH" "$AUTOAUCTION24_WATCH" "$AUCTION24_CZ_WATCH" "$PVP_WATCH" \
  "$SCHENGEN_WIDE_WATCH" "$RETRADE_WATCH" "$TROOSTWIJK_WATCH" "$AGORASTORE_WATCH" \
  "$SOURCE_ADAPTER_WATCH"; do
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

FULL_DASHBOARD_PUBLISHED=0
if python3 /opt/sonardeals-radar/publish_radar_dashboard.py \
  --root "$ROOT" --site "$SITE" --prepare-only --top-n "${VERIFIED_TARGET:-10000}"; then
python3 - "$PUBLIC_WATCH" "$STATE/latest_selection_manifest.json" "$SITE" <<'PY'
import hashlib
import json
import sys

sys.path.insert(0, "/opt/sonardeals-radar")
from publish_radar_dashboard import (  # noqa: E402
    canonical_json_sha256,
    load_published_official_auction_watch,
)

with open(sys.argv[1], encoding="utf-8") as handle:
    source = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    manifest = json.load(handle)
with open(f"{sys.argv[3]}/official_auction_watch.json", "rb") as handle:
    published = handle.read()
published_watch, published_root = load_published_official_auction_watch(
    __import__("pathlib").Path(sys.argv[3])
)

source_rows = source.get("rows")
source_count = len(source_rows) if isinstance(source_rows, list) else -1
published_count = manifest.get("official_auction_watch_count")
published_digest = hashlib.sha256(published).hexdigest()
if (
    source.get("row_count") != source_count
    or published_count != source_count
    or manifest.get("official_auction_watch_sha256") != published_digest
    or published_watch.get("row_count") != source_count
    or canonical_json_sha256(published_watch) != canonical_json_sha256(source)
    or manifest.get("official_auction_watch_parts_sha256")
       != canonical_json_sha256(published_root.get("parts"))
):
    raise SystemExit("OFFICIAL_AUCTION_WATCH_PUBLICATION_MISMATCH")
print(json.dumps({
    "result": "OFFICIAL_AUCTION_WATCH_PUBLICATION_PASS",
    "row_count": source_count,
    "manifest_sha256": published_digest,
    "part_count": len(published_root.get("parts", [])),
}, sort_keys=True))
PY
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
FULL_DASHBOARD_PUBLISHED=1
else
  # The regular encrypted board is intentionally left untouched if it is stale.
  # A separate fresh official-auction watch remains safe to publish because the
  # browser validates its registry, price semantics and eight-hour freshness.
  echo "AUCTION_FULL_DASHBOARD_PUBLICATION_DEFERRED at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  python3 /opt/sonardeals-radar/publish_official_auction_watch_only.py \
    --watch "$PUBLIC_WATCH" --site "$SITE"
fi

# Alerts are deliberately opt-in and run only after publication has passed its
# live convergence audit.  Seed the state once with --seed-state before
# enabling this flag so deployment does not notify the whole existing top 50.
if [[ "${RADAR_AUCTION_TOP50_ALERTS_ENABLED:-0}" == "1" && "$FULL_DASHBOARD_PUBLISHED" == "1" ]]; then
  if ! python3 /opt/sonardeals-radar/auction_top50_alerts.py \
    --watch "$PUBLIC_WATCH" \
    --fx "$SITE/display_currency.json" \
    --state "$STATE/runtime/auction_top50_alerts_state.json" \
    --telegram-token-file "${CREDENTIALS_DIRECTORY:?}/auction_alert_bot_token" \
    --telegram-chat-id-file "${CREDENTIALS_DIRECTORY:?}/auction_alert_broadcast_chat"; then
    # Publication is already complete.  Keep its success independent while the
    # alert state preserves unsent entrants for retry on the next hourly run.
    echo "AUCTION_TOP50_ALERT_FAILED at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
  fi
fi

echo "AUCTION_REFRESH_PASS at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
