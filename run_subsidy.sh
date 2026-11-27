#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/subsidy"
INBOX_DIR="$APP_DIR/inbox"
LOG_DIR="$APP_DIR/logs"
CHROME_PORT="9333"
STATE_DIR="/var/lib/subsidy-sync"

TODAY="$(date +%Y%m%d)"
RUN_LOG="$LOG_DIR/daily_${TODAY}.log"
STATE_FILE="$STATE_DIR/meituan-status.json"

mkdir -p "$INBOX_DIR" "$LOG_DIR" "$STATE_DIR"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$RUN_LOG"
}

ensure_chrome() {
  if curl -fs "http://127.0.0.1:${CHROME_PORT}/json/version" >/dev/null 2>&1; then
    log "Chrome DevTools already listening on 127.0.0.1:${CHROME_PORT}"
    return
  fi

  log "Starting persistent Meituan Chrome service"
  systemctl start meituan-store-chrome@baomin-E.service
  for _ in $(seq 1 15); do
    if curl -fs "http://127.0.0.1:${CHROME_PORT}/json/version" >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  log "Chrome DevTools did not become ready on 127.0.0.1:${CHROME_PORT}"
  exit 1
}

run_sync() {
  log "Syncing Meituan subsidy activities directly from API"
  python "$APP_DIR/meituan_subsidy_sync.py" \
    --config "$APP_DIR/config.json" \
    --port "$CHROME_PORT" \
    --state-file "$STATE_FILE" \
    --log-file "$LOG_DIR/run.log" | tee -a "$RUN_LOG"
  log "Meituan API sync finished"
}

main() {
  log "Daily subsidy job started"
  ensure_chrome
  run_sync
  log "Daily subsidy job completed"
}

main "$@"
