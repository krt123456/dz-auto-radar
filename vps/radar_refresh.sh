#!/usr/bin/env bash
set -Eeuo pipefail

MODE="${1:-smart}"
JOB_ID="${2:-scheduled}"
ROOT="${RADAR_CAR_ROOT:-/home/krt/car_deal_finder}"
STATE="${RADAR_STATE_DIR:-/var/lib/sonardeals-radar}"
CLIENT="${RADAR_CONTROL_CLIENT:-/opt/sonardeals-radar/radar_control_client.py}"
PUBLISHER="${RADAR_PUBLISHER:-/opt/sonardeals-radar/publish_radar_dashboard.py}"
AUDITOR="${RADAR_AUDITOR:-/opt/sonardeals-radar/audit_best_selection.py}"
SITE="${RADAR_SITE:-/srv/sonardeals-radar/site}"
AUDIT="$STATE/latest_selection_audit.json"
LOG_DIR="$STATE/logs"
NOTIFY=1
PHASE="starting"

if [[ "$MODE" != "smart" && "$MODE" != "full" ]]; then
  echo "unsupported refresh mode: $MODE" >&2
  exit 2
fi
if [[ "$JOB_ID" == scheduled* ]]; then
  NOTIFY=0
fi

mkdir -p "$STATE" "$LOG_DIR"
exec 9>/run/lock/sonardeals-radar-refresh.lock
if ! flock -w 21600 9; then
  echo "refresh lock timeout" >&2
  exit 75
fi

exec > >(tee -a "$LOG_DIR/refresh-$(date -u +%Y%m%d).log") 2>&1

notify() {
  local status="$1" phase="$2" message="$3"
  shift 3
  if [[ "$NOTIFY" == "1" ]]; then
    python3 "$CLIENT" update --job-id "$JOB_ID" --status "$status" \
      --phase "$phase" --message "$message" --mode "$MODE" "$@" >/dev/null || true
  fi
}

fail() {
  local rc=$?
  trap - ERR INT TERM
  notify failed "$PHASE" "فشل التحديث في مرحلة $PHASE" --error-code "refresh_${PHASE}_${rc}"
  echo "RADAR_REFRESH_FAILED mode=$MODE job=$JOB_ID phase=$PHASE rc=$rc" >&2
  exit "$rc"
}
trap fail ERR INT TERM

cd "$ROOT"
echo "RADAR_REFRESH_START mode=$MODE job=$JOB_ID at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

PHASE="harvest"
notify running "$PHASE" "يجلب الخادم أحدث العروض من المصادر"
if [[ "${RADAR_SKIP_HARVEST:-0}" == "1" ]]; then
  echo "RADAR_SKIP_HARVEST=1; using the current VPS observation lake"
elif [[ "$MODE" == "full" ]]; then
  LIVE_SOURCE_PARALLELISM="${LIVE_SOURCE_PARALLELISM:-20}" \
  RUN_RECENT_SIGNAL_HUNT=0 RUN_TOP50_AFTER_RECENT=0 RUN_NON_POLAND_BOOSTER=0 \
    bash "$ROOT/run_full_live_parallel.sh"
  touch "$ROOT/last_full_refresh.marker"
else
  CAR_DEAL_FINDER_RUNTIME_ROOT="$STATE/runtime" \
    bash "$ROOT/run_parallel_smart_harvest.sh"
fi

PHASE="universe"
notify running "$PHASE" "يدمج العروض الجديدة في الكون التراكمي من دون حذف القديم"
if [[ "$MODE" == "full" || ! -s "$STATE/schengen_observation_lake.csv" ]]; then
  python3 /opt/sonardeals-radar/build_schengen_lake.py \
    --output "$STATE/schengen_observation_lake.csv" \
    --report "$STATE/schengen_observation_lake.json"
  python3 "$ROOT/import_live_offers_to_universe.py" \
    --input-csv "$STATE/schengen_observation_lake.csv" \
    --db "$ROOT/universe_offers.sqlite" --batch-size 5000
fi
python3 "$ROOT/import_live_offers_to_universe.py" \
  --input-csv "$ROOT/live_offers.csv" --db "$ROOT/universe_offers.sqlite"

PHASE="ranking"
notify running "$PHASE" "يعيد حساب الربح والمصداقية وترتيب كامل الكون المؤهل"
RUN_BURST=0 RUN_HARVEST=0 VERIFY_LIMIT=0 PURGE_DEAD=0 \
  bash "$ROOT/run_million_planet_cycle.sh"
TOP_N=200000 python3 "$ROOT/precompute_top400.py"

PHASE="validation"
notify running "$PHASE" "يفحص الروابط الأعلى ويستبعد المؤكد ميتًا"
python3 "$ROOT/export_schengen_board.py" --top-n 0
if [[ "$MODE" == "full" ]]; then
  VERIFY_LIMIT="${RADAR_FULL_VERIFY_LIMIT:-5000}"
else
  VERIFY_LIMIT="${RADAR_SMART_VERIFY_LIMIT:-1500}"
fi
python3 "$ROOT/validate_top400.py" \
  --input "$ROOT/mobile_site_local/board.json" \
  --id-index "$ROOT/top_offers.json" \
  --output-json "$ROOT/top400_validation.json" \
  --limit "$VERIFY_LIMIT" --workers "${RADAR_VERIFY_WORKERS:-24}" \
  --timeout-sec "${RADAR_VERIFY_TIMEOUT:-8}"
python3 "$ROOT/export_schengen_board.py" --top-n 0

PHASE="publication_audit"
notify running "$PHASE" "يدقق مستقلًا أن المنشور هو الأفضل من كامل الكون المؤهل"
python3 "$PUBLISHER" --root "$ROOT" --site "$SITE" --prepare-only
python3 "$AUDITOR" --root "$ROOT" --site "$SITE" --output "$AUDIT"

PHASE="publish"
notify running "$PHASE" "ينشر النسخة المشفرة بعد اجتياز تدقيق الاختيار"
python3 "$PUBLISHER" --root "$ROOT" --site "$SITE" --push-only

PHASE="complete"
notify ok "$PHASE" "اكتمل التحديث والتدقيق والنشر بنجاح" --metrics-file "$AUDIT"
trap - ERR INT TERM
echo "RADAR_REFRESH_PASS mode=$MODE job=$JOB_ID at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
