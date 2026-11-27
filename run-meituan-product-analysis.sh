#!/usr/bin/env bash
# Completed-period Baomin Meituan indicators, with an on-demand browser.
set -euo pipefail

app_dir=/opt/subsidy
chrome_port=9333

cleanup() {
  systemctl stop meituan-store-chrome@baomin-E.service || true
}
trap cleanup EXIT

ready=0
for _ in {1..30}; do
  if curl --fail --silent "http://127.0.0.1:${chrome_port}/json/version" >/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
(( ready == 1 )) || { echo "Meituan Chrome is not ready on port ${chrome_port}" >&2; exit 1; }

/opt/subsidy/venv/bin/python "$app_dir/meituan_product_analysis.py" \
  --config "$app_dir/config.json" --port "$chrome_port" \
  --state-file /var/lib/subsidy-sync/meituan-product-analysis-status.json
