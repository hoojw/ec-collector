#!/usr/bin/env bash
# 天猫活动清单导入 + 本店在售商品匹配（每天 11:30 / 16:30）
set -euo pipefail

exec 9>/run/lock/tmall-activity-sync.lock
flock -n 9 || exit 0

record_date="$(TZ=Asia/Shanghai date +%F)"
activity_json="/home/dataease/incoming/tmall-activity-full.json"
config="/opt/subsidy/config.json"
log_tag="tmall-activity-sync"

log() { echo "[$log_tag] $*"; }

# 数据新鲜度检查：活动 JSON 必须是当天或前一天生成的
if [[ ! -f "$activity_json" ]]; then
  log "ERROR: $activity_json not found"
  exit 1
fi
json_mtime="$(TZ=Asia/Shanghai stat -c %y "$activity_json" | cut -d' ' -f1)"
if [[ "$json_mtime" < "$record_date" ]]; then
  log "SKIP: activity JSON is stale (mtime=$json_mtime, today=$record_date); Windows collector may be off"
  exit 0
fi

# 1) 导入活动清单到 adjust_price.tmall_activity_available_daily
/opt/subsidy/venv/bin/python /opt/subsidy/import_tmall_activity_available.py \
  --config "$config" --json "$activity_json" --day "$record_date"

# 2) 匹配本店在售商品 —— 用云端接收的最新 daily 快照里的 on_sale 数据
#    找当天最新的 daily JSON，提取 on_sale 为匹配输入
latest_daily=$(find /opt/subsidy/tmall-ecommerce-data/received/default/daily -type f -name 'daily-*.json' \
  -newermt "$record_date" 2>/dev/null | sort | tail -1)
if [[ -z "$latest_daily" ]]; then
  log "SKIP: no daily snapshot received today for on_sale matching"
  exit 0
fi

items_json="/tmp/tmall_onsale_${record_date}.json"
/opt/subsidy/venv/bin/python /opt/subsidy/extract_tmall_onsale_items.py \
  --daily "$latest_daily" --out "$items_json"

if [[ ! -s "$items_json" ]]; then
  log "SKIP: no on_sale rows in daily snapshot"
  exit 0
fi

/opt/subsidy/venv/bin/python /opt/subsidy/match_tmall_onsale_activities.py \
  --config "$config" --items "$items_json" --day "$record_date" \
  --out "/opt/subsidy/exports/tmall_onsale_activity_match_${record_date}.json"

target_xlsx="/tmp/codebuddy-chat-logs/保民三平台活动及成本.xlsx"
if [[ -f "$target_xlsx" ]]; then
  /opt/subsidy/venv/bin/python /opt/subsidy/refresh_tmall_excel_sheet.py \
    --config "$config" --day "$record_date" --target "$target_xlsx"
  /opt/subsidy/venv/bin/python /opt/subsidy/apply_baomin_formulas.py \
    --target "$target_xlsx"
else
  log "SKIP: target workbook not found: $target_xlsx"
fi

log "done: $(date '+%F %T')"
