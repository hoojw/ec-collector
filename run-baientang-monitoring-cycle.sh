#!/usr/bin/env bash
set -u

APP_DIR=/opt/subsidy
STORE_DIR="$APP_DIR/baientang"
CHROME_PORT=9360
AFTER_SALES_STATE=/var/lib/subsidy-sync/baientang-after-sales-status.json
SUBSIDY_STATE=/var/lib/subsidy-sync/baientang-subsidy-status.json
BUSINESS_REPORT_STATE=/var/lib/subsidy-sync/baientang-business-report-status.json

after_sales_status=0
subsidy_status=0
business_report_status=0
activity_metrics_status=0
product_analysis_status=0
top3_status=0
product_sales_status=0
listed_products_status=0
subsidy_campaign_status=0

browser_ready=0
for _ in {1..30}; do
  if /usr/bin/curl --fail --silent --show-error \
    "http://127.0.0.1:${CHROME_PORT}/json/version" >/dev/null; then
    browser_ready=1
    break
  fi
  /usr/bin/sleep 1
done

if (( browser_ready == 0 )); then
  echo "Baientang Chrome did not become ready on port ${CHROME_PORT}" >&2
  exit 1
fi

/usr/bin/flock -n /run/lock/baientang-after-sales.lock \
  /opt/subsidy/venv/bin/python "$APP_DIR/meituan_after_sales_pending.py" \
    --config "$STORE_DIR/config.json" \
    --port "$CHROME_PORT" \
    --state-file "$AFTER_SALES_STATE" \
    --detail-delay 0.5 \
  || after_sales_status=$?

/usr/bin/flock -n /run/lock/baientang-subsidy-sync.lock \
  /opt/subsidy/venv/bin/python "$APP_DIR/meituan_subsidy_sync.py" \
    --config "$STORE_DIR/config.json" \
    --port "$CHROME_PORT" \
    --state-file "$SUBSIDY_STATE" \
    --log-file "$STORE_DIR/run.log" \
  || subsidy_status=$?

/usr/bin/flock -n /run/lock/baientang-business-report.lock \
  /opt/subsidy/venv/bin/python "$APP_DIR/meituan_business_report.py" \
    --config "$STORE_DIR/config.json" \
    --port "$CHROME_PORT" \
    --state-file "$BUSINESS_REPORT_STATE" \
  || business_report_status=$?

# Baientang joins the shared workbench (adjust_price.esale_system): its
# activity-product and completed-period transaction/turnover columns are
# written by the same serialized cycle.
/usr/bin/flock -n /run/lock/baientang-activity-metrics.lock \
  /opt/subsidy/venv/bin/python "$APP_DIR/esale_activity_metrics.py" \
    --config "$STORE_DIR/config.json" --platform 美团 \
    --allow-empty-current-snapshot \
  || activity_metrics_status=$?

/usr/bin/flock -n /run/lock/baientang-product-analysis.lock \
  /opt/subsidy/venv/bin/python "$APP_DIR/meituan_product_analysis.py" \
    --config "$STORE_DIR/config.json" --port "$CHROME_PORT" \
    --state-file /var/lib/subsidy-sync/baientang-product-analysis-status.json \
  || product_analysis_status=$?

# Platform-shared TOP3 lowest discount price keyed by UPC. Every store returns
# its own hot-selling UPC set with the same platform price, so the upsert is
# idempotent and the table converges on the union of all stores' UPCs.
/usr/bin/flock -n /run/lock/baientang-top3-lowest-discount.lock \
  /opt/subsidy/venv/bin/python "$APP_DIR/meituan_top3_lowest_discount_price.py" \
    --config "$STORE_DIR/config.json" --port "$CHROME_PORT" \
    --state-file /var/lib/subsidy-sync/baientang-top3-lowest-discount-status.json \
  || top3_status=$?

# 商品销量（近 7 天，按 SKU）落 adjust_price.esale_product。周期本身是分钟级，
# 日频由 --state-file 保证：一天只真正采集一次。
/usr/bin/flock -n /run/lock/baientang-product-sales.lock \
  /opt/subsidy/venv/bin/python $APP_DIR/meituan_product_sales_collect.py \
    --config "$STORE_DIR/config.json" --port "$CHROME_PORT" --persist \
    --state-file /var/lib/subsidy-sync/baientang-product-sales-status.json \
  || product_sales_status=$?


# 上架商品全表（商品管理 → 商品列表）落同表的 30天销量/库存 两列。与上面那条互补：
# skuRank 只返回窗口内有成交的 SKU，这里返回全部上架商品（含零销量）。一天一次，
# 状态文件独立，互不阻塞。
/usr/bin/flock -n /run/lock/baientang-listed-products.lock \
  /opt/subsidy/venv/bin/python ${APP_DIR}/meituan_listed_products_collect.py \
    --config "$STORE_DIR/config.json" --port "$CHROME_PORT" --persist \
    --state-file /var/lib/subsidy-sync/baientang-listed-products-status.json \
  || listed_products_status=$?

# 美团补贴三张表（已补贴/未补贴/未建品）+ 本店历史最低售价，按店铺落库。
/usr/bin/flock -n /run/lock/baientang-subsidy-campaign.lock \
  /opt/subsidy/venv/bin/python "$APP_DIR/meituan_subsidy_collect.py" \
    --config "$STORE_DIR/config.json" --port "$CHROME_PORT" \
  || subsidy_campaign_status=$?

# Retry only the after-sales stage after the initial browser/resource peak.
if (( after_sales_status != 0 )); then
  echo "Baientang retrying after-sales collection after the store cycle" >&2
  /usr/bin/sleep 5
  after_sales_status=0
  /usr/bin/flock -n /run/lock/baientang-after-sales.lock \
    /opt/subsidy/venv/bin/python "$APP_DIR/meituan_after_sales_pending.py" \
      --config "$STORE_DIR/config.json" \
      --port "$CHROME_PORT" \
      --state-file "$AFTER_SALES_STATE" \
      --detail-delay 0.5 \
    || after_sales_status=$?
fi

if (( after_sales_status != 0 || subsidy_status != 0 || business_report_status != 0 || activity_metrics_status != 0 || product_analysis_status != 0 || top3_status != 0 || product_sales_status != 0 || listed_products_status != 0 || subsidy_campaign_status != 0 )); then
  echo "Baientang monitoring cycle failed: after_sales=${after_sales_status} subsidy=${subsidy_status} business_report=${business_report_status} activity_metrics=${activity_metrics_status} product_analysis=${product_analysis_status} top3=${top3_status} product_sales=${product_sales_status} listed_products=${listed_products_status} subsidy_campaign=${subsidy_campaign_status}" >&2
  exit 1
fi
