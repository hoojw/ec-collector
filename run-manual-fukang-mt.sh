#!/usr/bin/env bash
# 手动一次性跑西青福康美团全链路 (fukan-E :9351, mt-33770492)
# 顺序: 起浏览器 -> store-ops(售后/补贴/报表/活动) -> product_analysis(交易动销列) -> 关浏览器
set -u
LOG=/opt/subsidy/logs/manual-run-20260908-fukang-mt.log
exec >>"$LOG" 2>&1
echo "== $(date '+%F %T') start fukang meituan full run =="
systemctl start meituan-store-chrome@fukan-E.service
ready=0
for _ in $(seq 1 90); do
  if curl --fail --silent "http://127.0.0.1:9351/json/version" >/dev/null; then ready=1; break; fi
  sleep 1
done
if (( ready == 0 )); then echo "fukan-E chrome not ready"; exit 1; fi
echo "browser ready"

STORE_KEY=mt-33770492
STORE_CONFIG=/opt/subsidy/stores/mt-33770492/config.json
CHROME_PORT=9351

bash /opt/subsidy/run-meituan-store-ops.sh
echo "store-ops rc=$?"

/opt/subsidy/venv/bin/python /opt/subsidy/meituan_product_analysis.py \
  --config "$STORE_CONFIG" --port "$CHROME_PORT" \
  --state-file /var/lib/subsidy-sync/stores/mt-33770492/product-analysis.json
echo "product-analysis rc=$?"

systemctl stop meituan-store-chrome@fukan-E.service >/dev/null 2>&1 || true
echo "== $(date '+%F %T') finished =="
