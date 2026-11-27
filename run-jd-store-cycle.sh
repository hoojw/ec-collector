#!/usr/bin/env bash
set -euo pipefail

app_dir=/opt/subsidy
store_key=${STORE_KEY:?STORE_KEY is required}
store_config=${STORE_CONFIG:?STORE_CONFIG is required}
chrome_port=${CHROME_PORT:?CHROME_PORT is required}
browser_instance=${BROWSER_INSTANCE:?BROWSER_INSTANCE is required}
state_dir="/var/lib/subsidy-sync/stores/${store_key}"
analysis_marker="${state_dir}/product-analysis-date"
record_date="$(TZ=Asia/Shanghai date +%F)"
current_hm="$(TZ=Asia/Shanghai date +%H%M)"

[[ "$store_key" =~ ^jd-[0-9]+$ ]] || { echo "invalid JD store key: $store_key" >&2; exit 2; }
[[ "$browser_instance" =~ ^[a-z][a-z0-9-]*-E$ ]] || { echo "invalid JD ecommerce browser instance: $browser_instance" >&2; exit 2; }
install -d -m 0750 "$state_dir"

cleanup() {
  systemctl stop "jingdong-store-chrome@${browser_instance}.service" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Acquire the outer global cycle lock before starting Chrome.  Multiple store
# timers may fire together, but only the active store consumes browser memory.
systemctl start "jingdong-store-chrome@${browser_instance}.service"
ready=0
for _ in {1..30}; do
  if curl --fail --silent "http://127.0.0.1:${chrome_port}/json/version" >/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
(( ready == 1 )) || { echo "$store_key Chrome is not ready on port $chrome_port" >&2; exit 1; }

# A retained JD session can first arrive at the passport shell.  The helper
# presses the submit control, but only after Chrome has itself populated the
# password field (length-only check); it never types or reads credentials, and
# never handles QR/captcha.  A still-pending passport page is surfaced as an
# authentication requirement by the ordinary collectors below.
#
# The venv interpreter is required: the helper imports requests + websocket,
# which the system /opt/subsidy/venv/bin/python does not provide.
recovery_python=${RECOVERY_PYTHON:-/opt/subsidy/venv/bin/python}
"$recovery_python" "${app_dir}/jd_login_page_recover.py" --port "$chrome_port" --settle-seconds 12

# Business priority is explicit: operational indicators finish before the
# heavier SKU/activity scan starts on the same authenticated browser.
ops_status=0
"${app_dir}/run-jd-store-ops.sh" || ops_status=$?

# The SKU/activity scan is an enhancement layer.  A transient failure (for
# example the wares list rendering zero on-sale rows after a browser restart)
# must NOT block the daily Product360 business columns, so record it and
# continue; the exit status still surfaces the failure to the timer/journal.
# 处方（问诊开方）也是业务指标，但单轮只抓少量详情，成本比 SKU 扫描低，
# 放在 ops 之后、activity 之前，失败不阻塞其它阶段。
prescription_status=0
"${app_dir}/run-jd-prescription.sh" || prescription_status=$?

# 订单状态计数：轻量（单次加载、只读页签），按小时限流，放在处方之后。
order_status=0
"${app_dir}/run-jd-order-status.sh" || order_status=$?

activity_status=0
"${app_dir}/run-jd-store-activity.sh" || activity_status=$?

# Direct writer gates committed current-schema batches in MySQL.
analysis_rc=0
"${app_dir}/run-jd-store-product-analysis.sh" || analysis_rc=$?
if (( analysis_rc != 0 && analysis_rc != 3 )); then
  echo "$store_key Product360 failed: $analysis_rc" >&2
  ops_status=1
fi

if (( activity_status != 0 )); then
  echo "$store_key SKU/activity collection failed ($activity_status); ops and analysis stages still ran" >&2
fi
if (( prescription_status != 0 )); then
  echo "$store_key prescription collection failed ($prescription_status); ops and activity stages still ran" >&2
fi
if (( order_status != 0 )); then
  echo "$store_key order-status collection failed ($order_status); other stages still ran" >&2
fi
if (( ops_status != 0 || activity_status != 0 || prescription_status != 0 || order_status != 0 )); then
  echo "$store_key monitoring cycle partial failure: ops=${ops_status} activity=${activity_status} prescription=${prescription_status} order_status=${order_status}; all independent stages were attempted" >&2
  exit 1
fi
