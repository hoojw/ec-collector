#!/usr/bin/env bash
set -euo pipefail

exec 9>/run/lock/joyspace-catalog-sync.lock
flock -n 9 || exit 0
exec 8>/run/lock/joyspace-browser-shared.lock
flock -w 600 8 || { echo "timed out waiting for shared JoySpace browser" >&2; exit 1; }
record_date="$(TZ=Asia/Shanghai date +%F)"

cleanup() {
  systemctl --no-block stop joyspace-login.service >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

systemctl start joyspace-login.service
ready=0
for _ in {1..60}; do
  if curl --fail --silent --max-time 2 http://127.0.0.1:9533/json/version >/dev/null \
    && /opt/subsidy/venv/bin/python /opt/subsidy/read_joyspace_sheets.py --port 9533 >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
(( ready == 1 )) || { echo "JoySpace shared catalog was not ready within 120 seconds" >&2; exit 1; }

/opt/subsidy/venv/bin/python /opt/subsidy/sync_joyspace_subsidy.py \
  --port 9533 \
  --config /opt/subsidy/config.json \
  --record-date "$record_date" \
  --output-dir /opt/subsidy/joyspace/inbox

# Catalog now commits directly to ec_dw.subsidy.  Mirror it back to the local
# catalog tables so the JD small-subscription match jobs keep working.
/opt/subsidy/venv/bin/python /opt/subsidy/sync_joyspace_catalog_from_ecdw.py \
  --ec-config /opt/subsidy/config.json \
  --local-config /opt/subsidy/config.local.json \
  --record-date "$record_date"

/opt/subsidy/venv/bin/python /opt/subsidy/build_jd_subsidy_sku.py \
  --config /opt/subsidy/config.local.json \
  --record-date "$record_date"

# build_jd_subsidy_daily.py still depends on retired dataease cost tables;
# keep it out of the timer until those inputs are restored.
