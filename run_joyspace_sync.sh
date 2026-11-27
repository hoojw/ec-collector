#!/usr/bin/env bash
set -euo pipefail

exec 9>/run/lock/joyspace-subsidy-sync.lock
flock -n 9 || {
  echo "JoySpace subsidy sync is already running"
  exit 0
}

record_date="$(TZ=Asia/Shanghai date +%F)"
log_dir=/opt/subsidy/joyspace/logs
mkdir -p "$log_dir"

wait_for_sheet_page() {
  for _ in $(seq 1 60); do
    if curl --fail --silent --max-time 2 http://127.0.0.1:9533/json/list \
      | grep -q 'joyspace.jd.com/sheets/'; then
      return 0
    fi
    sleep 2
  done
  return 1
}

if ! wait_for_sheet_page; then
  echo "JoySpace browser page was not ready after 120 seconds" >&2
  exit 1
fi

sleep 8

if ! /opt/subsidy/venv/bin/python /opt/subsidy/read_joyspace_sheets.py --port 9533 >/dev/null; then
  echo "JoySpace WebOffice was unavailable; restarting its browser" >&2
  systemctl restart joyspace-browser.service
  if ! wait_for_sheet_page; then
    echo "JoySpace browser page was not ready after restart" >&2
    exit 1
  fi
  sleep 8
  /opt/subsidy/venv/bin/python /opt/subsidy/read_joyspace_sheets.py --port 9533 >/dev/null
fi

/opt/subsidy/venv/bin/python /opt/subsidy/sync_joyspace_subsidy.py \
  --port 9533 \
  --config /opt/subsidy/config.json \
  --record-date "$record_date" \
  --output-dir /opt/subsidy/joyspace/inbox

/opt/subsidy/venv/bin/python /opt/subsidy/build_jd_subsidy_sku.py \
  --config /opt/subsidy/config.local.json \
  --record-date "$record_date"

/opt/subsidy/venv/bin/python /opt/subsidy/build_jd_subsidy_daily.py \
  --config /opt/subsidy/config.local.json \
  --record-date "$record_date"

/opt/subsidy/venv/bin/python /opt/subsidy/esale_activity_metrics.py \
  --config /opt/subsidy/config.json \
  --platform 京东 \
  --record-date "$record_date"

echo "JoySpace subsidy sync completed for $record_date"
