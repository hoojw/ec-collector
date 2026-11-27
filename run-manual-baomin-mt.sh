#!/usr/bin/env bash
# 手动一次性跑保民美团全链路 (baomin-E :9333)
set -u
LOG=/opt/subsidy/logs/manual-run-20260908-baomin-mt.log
exec >>"$LOG" 2>&1
echo "== $(date '+%F %T') start baomin meituan full run =="
systemctl start meituan-store-chrome@baomin-E.service
ready=0
for _ in $(seq 1 90); do
  if curl --fail --silent "http://127.0.0.1:9333/json/version" >/dev/null; then ready=1; break; fi
  sleep 1
done
if (( ready == 0 )); then echo "baomin-E chrome not ready"; exit 1; fi
echo "browser ready"
bash /opt/subsidy/run-meituan-monitoring-cycle.sh
echo "monitoring-cycle rc=$?"
bash /opt/subsidy/run-meituan-product-analysis.sh
echo "product-analysis rc=$?"
echo "== $(date '+%F %T') finished =="
