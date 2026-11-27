#!/usr/bin/env bash
set -euo pipefail

app_dir=/opt/subsidy
store_key=${STORE_KEY:?STORE_KEY is required}
chrome_port=${CHROME_PORT:?CHROME_PORT is required}
browser_instance=${BROWSER_INSTANCE:?BROWSER_INSTANCE is required}

[[ "$store_key" =~ ^jd-[0-9]+$ ]] || { echo "invalid JD store key: $store_key" >&2; exit 2; }

# 浏览器实例前段就是客户简称（baomin-E -> 保民），与库里其它表的口径对齐
case "${browser_instance%-E}" in
  baomin) client=保民 ;;
  baientang) client=百恩堂 ;;
  fukan) client=福康 ;;
  jinghetang) client=璟和堂 ;;
  *) client="${browser_instance%-E}" ;;
esac

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

# 只读处方管理页的列表：各页签计数（平台判定的 待开方 / 审核超时）+ 活跃单的行信息。
# 不点详情页——处方明细另有系统在采，这里高频点详情徒增风控风险；
# 需要时再临时 JD_PRESCRIPTION_DETAILS=N 补几单。
/usr/bin/flock -n "/run/lock/${store_key}-prescription.lock" \
  /opt/subsidy/venv/bin/python "$app_dir/jd_prescription_collect.py" \
    --port "$chrome_port" --client "$client" \
    --details "${JD_PRESCRIPTION_DETAILS:-0}"
