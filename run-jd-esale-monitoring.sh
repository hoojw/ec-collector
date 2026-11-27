#!/usr/bin/env bash
set -euo pipefail
exec 9>/run/lock/jd-esale-monitoring.lock
flock -n 9 || exit 0
exec 8>/run/lock/joyspace-browser-shared.lock
flock -w 600 8 || { echo "timed out waiting for shared JoySpace browser" >&2; exit 1; }
record_date="$(TZ=Asia/Shanghai date +%F)"

# This legacy store shares a browser profile with JoySpace, but catalog
# collection is owned exclusively by joyspace-subsidy-sync.service at 11:05.
# This monitoring run must never refresh the shared sheet itself.
cleanup() {
  # Do not block the terminating one-shot service while Chrome is being
  # stopped.  systemd owns the browser service and completes that shutdown
  # independently.
  systemctl --no-block stop joyspace-login.service >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

systemctl start joyspace-login.service
ready=false
for _ in $(seq 1 30); do
  if curl --fail --silent --max-time 2 http://127.0.0.1:9533/json/version >/dev/null; then
    ready=true
    break
  fi
  sleep 2
done
if [[ "$ready" != true ]]; then
  echo "JD store browser was not ready within 60 seconds" >&2
  exit 1
fi

/opt/subsidy/venv/bin/python /opt/subsidy/jd_store_sku_web_collect.py --config /opt/subsidy/config.json --record-date "$record_date" --click-delay-seconds 3
if /opt/subsidy/venv/bin/python /opt/subsidy/check_jd_catalog_current.py --config /opt/subsidy/config.json --record-date "$record_date"; then
  /opt/subsidy/venv/bin/python /opt/subsidy/build_jd_subsidy_sku.py --config /opt/subsidy/config.local.json --record-date "$record_date"
  /opt/subsidy/venv/bin/python /opt/subsidy/esale_activity_metrics.py --config /opt/subsidy/config.json --platform 京东 --record-date "$record_date" --allow-empty-current-snapshot
else
  echo "shared JoySpace catalog is not current for ${record_date}; activity matching deferred" >&2
fi
/opt/subsidy/venv/bin/python /opt/subsidy/jd_esale_after_sales_metrics.py --config /opt/subsidy/config.json --record-date "$record_date"
