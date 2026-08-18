#!/usr/bin/env bash
set -Eeuo pipefail

MODE="${1:-smart}"
JOB_ID="${2:-scheduled}"
ROOT="${RADAR_CAR_ROOT:-/home/krt/car_deal_finder}"
STATE="${RADAR_STATE_DIR:-/var/lib/sonardeals-radar}"
CLIENT="${RADAR_CONTROL_CLIENT:-/opt/sonardeals-radar/radar_control_client.py}"
PUBLISHER="${RADAR_PUBLISHER:-/opt/sonardeals-radar/publish_radar_dashboard.py}"
AUDITOR="${RADAR_AUDITOR:-/opt/sonardeals-radar/audit_best_selection.py}"
LIVE_CONVERGENCE="${RADAR_LIVE_CONVERGENCE:-/opt/sonardeals-radar/audit_live_convergence.py}"
RANKER="${RADAR_RANKER:-/opt/sonardeals-radar/build_observed_value_board.py}"
VALIDATION_SEALER="${RADAR_VALIDATION_SEALER:-/opt/sonardeals-radar/seal_validation_report.py}"
LANE_BUILDER="${RADAR_LANE_BUILDER:-/opt/sonardeals-radar/build_auction_board.py}"
VALIDATION_CHECKPOINT="$STATE/runtime/top400_validation.checkpoint.json"
REFRESH_LOCK_FILE="${RADAR_REFRESH_LOCK_FILE:-/run/lock/sonardeals-radar-refresh.lock}"
RANKED_POOL_LIMIT="${RADAR_RANKED_POOL_LIMIT:-60000}"
VERIFIED_TARGET="${RADAR_VERIFIED_TARGET:-10000}"
SITE="${RADAR_SITE:-/srv/sonardeals-radar/site}"
AUDIT="$STATE/latest_selection_audit.json"
LIVE_AUDIT="$STATE/latest_live_selection_audit.json"
LIVE_DATA_URL="${RADAR_LIVE_DATA_URL:-https://krt123456.github.io/dz-auto-radar/data.enc}"
LOG_DIR="$STATE/logs"
NOTIFY=1
PHASE="starting"

if [[ "$MODE" != "smart" && "$MODE" != "full" ]]; then
  echo "unsupported refresh mode: $MODE" >&2
  exit 2
fi
if [[ ! "$RANKED_POOL_LIMIT" =~ ^[0-9]+$ ]] || (( RANKED_POOL_LIMIT < 1 || RANKED_POOL_LIMIT > 100000 )); then
  echo "invalid ranked-pool limit: $RANKED_POOL_LIMIT" >&2
  exit 64
fi
if [[ ! "$VERIFIED_TARGET" =~ ^[0-9]+$ ]] || (( VERIFIED_TARGET < 1 || VERIFIED_TARGET > RANKED_POOL_LIMIT )); then
  echo "invalid verified target: $VERIFIED_TARGET" >&2
  exit 64
fi
if [[ "$JOB_ID" == scheduled* ]]; then
  NOTIFY=0
fi

mkdir -p "$STATE" "$LOG_DIR"
exec 9>"$REFRESH_LOCK_FILE"
if [[ "$JOB_ID" == scheduled* ]]; then
  if ! flock -n 9; then
    echo "RADAR_REFRESH_SKIPPED_BUSY mode=$MODE job=$JOB_ID" >&2
    exit 0
  fi
elif ! flock -w 300 9; then
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

PHASE="ranking_preflight"
notify running "$PHASE" "يتحقق الخادم من توفر محرك ترتيب آمن قبل الحصاد"
python3 "$RANKER" --capability-check
python3 "$VALIDATION_SEALER" --capability-check
echo "RADAR_RANKING_PREFLIGHT_PASS"

PHASE="disk_preflight"
notify running "$PHASE" "يتحقق الخادم من مساحة العمل الآمنة قبل التحديث"
if [[ "$MODE" == "full" ]]; then
  REQUIRED_FREE_BYTES="${RADAR_FULL_MIN_FREE_BYTES:-68719476736}"
else
  REQUIRED_FREE_BYTES="${RADAR_SMART_MIN_FREE_BYTES:-34359738368}"
fi
if [[ ! "$REQUIRED_FREE_BYTES" =~ ^[0-9]+$ ]]; then
  echo "invalid minimum-free-bytes policy: $REQUIRED_FREE_BYTES" >&2
  (exit 64)
fi
AVAILABLE_FREE_BYTES="$(df -PB1 -- "$ROOT" | awk 'NR==2 {print $4}')"
if [[ ! "$AVAILABLE_FREE_BYTES" =~ ^[0-9]+$ ]] || (( AVAILABLE_FREE_BYTES < REQUIRED_FREE_BYTES )); then
  echo "insufficient disk headroom: available=$AVAILABLE_FREE_BYTES required=$REQUIRED_FREE_BYTES" >&2
  (exit 73)
fi
echo "RADAR_DISK_PREFLIGHT_PASS available=$AVAILABLE_FREE_BYTES required=$REQUIRED_FREE_BYTES"

RESUME_VALIDATION=0
if [[ -e "$VALIDATION_CHECKPOINT" ]]; then
  if [[ ! -s "$ROOT/mobile_site_local/board.json" || ! -s "$ROOT/top_offers.json" ]]; then
    echo "validation checkpoint exists but its immutable board inputs are missing" >&2
    (exit 66)
  fi
  RESUME_VALIDATION=1
  echo "RADAR_VALIDATION_RESUME checkpoint=$VALIDATION_CHECKPOINT; preserving board inputs"
fi

if (( RESUME_VALIDATION == 0 )); then
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
if [[ "${RADAR_SKIP_UNIVERSE_IMPORT:-0}" == "1" ]]; then
  echo "RADAR_SKIP_UNIVERSE_IMPORT=1; using the validated current universe store"
else
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
fi

PHASE="fx"
notify running "$PHASE" "يجلب سعر الصرف الجمركي لليورو (اختياري، لا يوقف النشر)"
python3 "/opt/sonardeals-radar/capture_alces_fx.py" \
  --config "$STATE/fx/display_currency.json" \
  --intermediate "/opt/sonardeals-radar/certs/sectigo-public-server-authentication-ca-dv-r36.pem" \
  || echo "RADAR_FX_CAPTURE_SKIPPED keeping the last sealed rate"

PHASE="ranking"
notify running "$PHASE" "يبني مقارنة سعرية مرصودة من لقطة ثابتة للكون المؤهل"
python3 "$RANKER" \
  --database "$ROOT/universe_offers.sqlite" \
  --ranked-output "$ROOT/top_offers.json" \
  --board-output "$ROOT/mobile_site_local/board.json" \
  --validation-report "$ROOT/top400_validation.json" \
  --top-n "$RANKED_POOL_LIMIT"
else
  PHASE="validation_resume"
  notify running "$PHASE" "يستأنف فحص الروابط من نقطة التحقق المحفوظة"
fi

PHASE="validation"
notify running "$PHASE" "يفحص الروابط الأعلى ويستبعد المؤكد ميتًا"
if [[ "$MODE" == "full" ]]; then
  VERIFY_LIMIT="${RADAR_FULL_VERIFY_LIMIT:-0}"
else
  VERIFY_LIMIT="${RADAR_SMART_VERIFY_LIMIT:-0}"
fi
xvfb-run -a python3 "$ROOT/validate_top400.py" \
  --input "$ROOT/mobile_site_local/board.json" \
  --id-index "$ROOT/top_offers.json" \
  --output-json "$ROOT/top400_validation.json" \
  --checkpoint "$VALIDATION_CHECKPOINT" \
  --verified-target "$VERIFIED_TARGET" \
  --checkpoint-batch-size "${RADAR_VALIDATION_CHECKPOINT_BATCH_SIZE:-1000}" \
  --checkpoint-interval-sec "${RADAR_VALIDATION_CHECKPOINT_INTERVAL_SEC:-120}" \
  --checkpoint-max-age-sec "${RADAR_VALIDATION_CHECKPOINT_MAX_AGE_SEC:-21600}" \
  --checkpoint-resume-grace-sec "${RADAR_VALIDATION_CHECKPOINT_RESUME_GRACE_SEC:-0}" \
  --checkpoint-compatible-identity-sha256 "${RADAR_VALIDATION_COMPATIBLE_IDENTITY_SHA256:-}" \
  --checkpoint-compatible-sha256 "${RADAR_VALIDATION_COMPATIBLE_CHECKPOINT_SHA256:-}" \
  --limit "$VERIFY_LIMIT" --workers "${RADAR_VERIFY_WORKERS:-24}" \
  --timeout-sec "${RADAR_VERIFY_TIMEOUT:-8}" \
  --browser-fallback \
  --browser-limit "${RADAR_BROWSER_VERIFY_LIMIT:-$RANKED_POOL_LIMIT}" \
  --browser-workers "${RADAR_BROWSER_VERIFY_WORKERS:-8}" \
  --browser-session-size "${RADAR_BROWSER_SESSION_SIZE:-1000}" \
  --browser-timeout-sec "${RADAR_BROWSER_VERIFY_TIMEOUT:-30}"
python3 "$VALIDATION_SEALER" \
  --board "$ROOT/mobile_site_local/board.json" \
  --validation "$ROOT/top400_validation.json"
python3 "$RANKER" \
  --database "$ROOT/universe_offers.sqlite" \
  --ranked-output "$ROOT/top_offers.json" \
  --board-output "$ROOT/mobile_site_local/board.json" \
  --validation-report "$ROOT/top400_validation.json" \
  --top-n "$RANKED_POOL_LIMIT"

PHASE="auction_lane"
notify running "$PHASE" "يبني مسار المزادات الموثق من الكون المقبول (fail-closed)"
python3 "$LANE_BUILDER" \
  --database "$ROOT/universe_offers.sqlite" \
  --board "$ROOT/mobile_site_local/board.json" \
  --generated-at "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["data_generated_at_utc"])' "$ROOT/mobile_site_local/board.json")" \
  --output "$ROOT/mobile_site_local/auction_lane.json"

PHASE="publication_audit"
notify running "$PHASE" "يدقق مستقلًا أن المنشور هو الأفضل من كامل الكون المؤهل"
python3 "$PUBLISHER" --root "$ROOT" --site "$SITE" --prepare-only --top-n "$VERIFIED_TARGET"
python3 "$AUDITOR" --root "$ROOT" --site "$SITE" --output "$AUDIT" --top-n "$VERIFIED_TARGET"

PHASE="publish"
notify running "$PHASE" "ينشر النسخة المشفرة بعد اجتياز تدقيق الاختيار"
python3 "$PUBLISHER" --root "$ROOT" --site "$SITE" --push-only

PHASE="live_publication_audit"
notify running "$PHASE" "يتحقق أن النسخة العامة تطابق المصدر والترتيب الصارم حقلًا بحقل"
expected_generation="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["generation_id"])' "$AUDIT")"
python3 "$LIVE_CONVERGENCE" \
  --root "$ROOT" \
  --data-url "$LIVE_DATA_URL" \
  --expected-generation "$expected_generation" \
  --selection-manifest "$STATE/latest_selection_manifest.json" \
  --selection-audit "$AUDIT" \
  --output "$LIVE_AUDIT" \
  --deadline-sec "${RADAR_LIVE_AUDIT_DEADLINE_SEC:-1800}" \
  --request-timeout-sec "${RADAR_LIVE_AUDIT_REQUEST_TIMEOUT_SEC:-30}" \
  --initial-backoff-sec "${RADAR_LIVE_AUDIT_INITIAL_BACKOFF_SEC:-5}" \
  --max-backoff-sec "${RADAR_LIVE_AUDIT_MAX_BACKOFF_SEC:-60}" \
  --max-network-errors "${RADAR_LIVE_AUDIT_MAX_NETWORK_ERRORS:-8}"

PHASE="complete"
notify ok "$PHASE" "اكتمل التحديث والتدقيق والنشر بنجاح" --metrics-file "$AUDIT"
trap - ERR INT TERM
echo "RADAR_REFRESH_PASS mode=$MODE job=$JOB_ID at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
