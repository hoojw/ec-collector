#!/usr/bin/env bash
# Collect Meituan subsidy (subsidized/unsubsidized/unlisted) for all stores.
# Each store uses its own already-logged-in Chrome instance (CDP). Stores whose
# browser is not currently running are skipped so one offline store never blocks
# the rest. Results are written to mt_subsidy_* tables with a 店铺 column.
set -uo pipefail

cd /opt/subsidy
PY=/opt/subsidy/venv/bin/python
COLLECTOR=/opt/subsidy/meituan_subsidy_collect.py
LOG=${MT_SUBSIDY_LOG:-/opt/subsidy/logs/meituan-subsidy-collect.log}
STATE=${MT_SUBSIDY_STATE:-/opt/subsidy/state/meituan-subsidy}
mkdir -p "$(dirname "$LOG")" "$STATE"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# store key -> display client name
declare -A CLIENT=(
  [baomin]="保民"
  [baientang]="百恩堂"
  [fukan]="福康综合门诊"
  [jinghetang]="璟和堂"
)
KEYS=(baomin baientang fukan jinghetang)

# Discover CDP port for a meituan profile by inspecting running Chrome processes.
port_for_key() {
  local key=$1
  ps -eo args | grep '/opt/google/chrome/chrome ' | grep -v grep \
    | grep "meituan-chrome-profiles/${key}-E" \
    | grep -oE 'remote-debugging-port=[0-9]+' | head -1 | cut -d= -f2
}

log "===== Meituan subsidy cycle start ====="
fail=0
for key in "${KEYS[@]}"; do
  client="${CLIENT[$key]}"
  port=$(port_for_key "$key")
  if [ -z "$port" ]; then
    log "SKIP $client ($key): browser not running"
    continue
  fi
  if ! curl -sf --max-time 3 "http://127.0.0.1:${port}/json/version" >/dev/null; then
    log "SKIP $client ($key): CDP $port not reachable"
    continue
  fi
  log "COLLECT $client ($key) on port $port"
  if timeout 300 "$PY" "$COLLECTOR" --port "$port" --client "$client" \
      >"$STATE/${key}.json" 2>"$STATE/${key}.err"; then
    counts=$("$PY" -c "import json;d=json.load(open('$STATE/${key}.json'));print(d['counts'])" 2>/dev/null)
    log "OK $client: $counts"
  else
    fail=1
    log "FAIL $client: $(tail -3 "$STATE/${key}.err" | tr '\n' ' ')"
  fi
done
log "===== Meituan subsidy cycle done (fail=$fail) ====="
exit $fail
