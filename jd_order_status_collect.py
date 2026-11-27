#!/usr/bin/env python3
"""京东订单列表页的状态计数采集（只读、单次加载）。

为什么只抓计数：
- 电商侧的口径是「订单状态」——暂停订单 = 平台判定开方超时，待出库 = 还没走完。
- 这个页面比处方管理页重得多（一页 245 单 + 一堆组件），所以**每小时最多一次**、
  只读取页签计数，不翻页、不点订单详情，避免把号跑风控。

不采集医生：订单列表没有医生字段。值班医生是每天人工排班，属于配置数据，
不进这里（见 bi_doctor_duty 的设计说明）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time

import pymysql

from jd_prescription_collect import evaluate, navigate, open_tab

ORDER_URL = "https://shop.jd.com/jdm/trade/orders/order-list"
SUSPEND_URL = ORDER_URL + "?tabType=suspend"
TABS = ("待付款", "待出库", "已出库", "暂停订单", "锁定订单")
CONFIG = "/opt/subsidy/config.json"

DDL = ["""
CREATE TABLE IF NOT EXISTS `jd_order_status_daily` (
  `记录日期` DATE NOT NULL,
  `店铺` VARCHAR(64) NOT NULL,
  `待付款` INT NULL,
  `待出库` INT NULL,
  `已出库` INT NULL,
  `暂停订单` INT NULL,
  `锁定订单` INT NULL,
  `合计` INT NULL,
  `采集时间` DATETIME NOT NULL,
  PRIMARY KEY (`记录日期`, `店铺`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='京东订单列表页签计数（暂停订单=平台判定开方超时）'
""", """
CREATE TABLE IF NOT EXISTS `jd_order_status_hourly` (
  `记录日期` DATE NOT NULL,
  `店铺` VARCHAR(64) NOT NULL,
  `小时` DATETIME NOT NULL,
  `待付款` INT NULL,
  `待出库` INT NULL,
  `已出库` INT NULL,
  `暂停订单` INT NULL,
  `锁定订单` INT NULL,
  `合计` INT NULL,
  `采集时间` DATETIME NOT NULL,
  PRIMARY KEY (`记录日期`, `店铺`, `小时`),
  KEY `idx_hour` (`小时`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='京东订单状态小时计数，保留每小时快照'
""", """
CREATE TABLE IF NOT EXISTS `jd_order_pause_history` (
  `记录日期` DATE NOT NULL,
  `店铺` VARCHAR(64) NOT NULL,
  `订单号` VARCHAR(64) NOT NULL,
  `下单时间` DATETIME NULL,
  `付款时间` DATETIME NULL,
  `SKU编码` VARCHAR(64) NULL,
  `商品` VARCHAR(255) NULL,
  `物流单号` VARCHAR(64) NULL,
  `状态说明` VARCHAR(255) NULL,
  `首次发现时间` DATETIME NOT NULL,
  `最后确认时间` DATETIME NOT NULL,
  `确认次数` INT NOT NULL DEFAULT 1,
  `原始行` VARCHAR(1200) NULL,
  PRIMARY KEY (`记录日期`, `店铺`, `订单号`),
  KEY `idx_pause_first` (`店铺`, `首次发现时间`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='京东暂停订单明细历史，首次发现时间用于小时级定位'
"""]


def connect():
    mysql = json.loads(open(CONFIG, encoding="utf-8").read())["mysql"]
    return pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
        password=mysql["password"], database=mysql["database"],
        charset=mysql.get("charset", "utf8mb4"), autocommit=False,
        init_command="SET time_zone='+08:00'",
    )


def read_counts(ws_url: str) -> tuple[dict[str, int], int]:
    """页签上的 (N) 就是全量计数，直接从正文取，不需要点进任何页签。"""
    text = evaluate(ws_url, "(document.body?document.body.innerText:'').replace(/\\s+/g,' ')") or ""
    counts = {}
    for name in TABS:
        m = re.search(re.escape(name) + r"\s*\(\s*(\d+)\+?\s*\)", text)
        counts[name] = int(m.group(1).rstrip("+")) if m else None
    total = None
    m = re.search(r"共\s*(\d+)\s*条", text)
    if m:
        total = int(m.group(1))
    return counts, total


def parse_order_chunks(text: str) -> list[dict]:
    rows = []
    for chunk in text.split("订单编号：")[1:]:
        chunk = chunk.split("订单详情", 1)[0].strip()
        order_no = re.match(r"(\d+)", chunk)
        if not order_no:
            continue
        times = re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", chunk)
        sku = re.search(r"skuId：\s*(\d+)", chunk)
        product = re.search(r"发货\s+(.*?)\s+skuId：", chunk)
        logistics = re.search(r"(jd_[A-Za-z0-9]+)", chunk)
        if "暂停订单需要审核处方之后才可以进行出库" in chunk:
            status = "暂停订单需要审核处方之后才可以进行出库"
        else:
            status = "暂停"
        rows.append({
            "订单号": order_no.group(1),
            "下单时间": times[0] if times else None,
            "付款时间": times[1] if len(times) > 1 and "付款" in chunk else None,
            "SKU编码": sku.group(1) if sku else None,
            "商品": product.group(1).strip()[:255] if product else None,
            "物流单号": logistics.group(1) if logistics else None,
            "状态说明": status,
            "原始行": chunk[:1200],
        })
    return rows


def click_pause_page(ws_url: str, page: int) -> bool:
    expr = (
        "(() => { const items=[...document.querySelectorAll('.jd-pager li,.jd-pager a,.jd-pager button')]; "
        f"const hit=items.find(e => (e.innerText||'').trim() === '{page}'); "
        "if (!hit) return false; hit.click(); return true; })()"
    )
    clicked = evaluate(ws_url, expr)
    if clicked:
        time.sleep(3)
    return bool(clicked)


def read_pause_rows(ws_url: str) -> list[dict]:
    text = evaluate(ws_url, "(document.body?document.body.innerText:'').replace(/\\s+/g,' ')") or ""
    total_match = re.search(r"共\s*(\d+)\s*条", text)
    size_match = re.search(r"(\d+)\s*条/页", text)
    total = int(total_match.group(1)) if total_match else 0
    page_size = int(size_match.group(1)) if size_match else 10
    pages = max(1, (total + page_size - 1) // page_size) if total else 1
    rows = parse_order_chunks(text)
    for page in range(2, pages + 1):
        if not click_pause_page(ws_url, page):
            break
        text = evaluate(ws_url, "(document.body?document.body.innerText:'').replace(/\\s+/g,' ')") or ""
        rows.extend(parse_order_chunks(text))
    unique = {}
    for row in rows:
        unique.setdefault(row["订单号"], row)
    return list(unique.values())


def main() -> int:
    parser = argparse.ArgumentParser(description="京东订单状态计数采集（只读）")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--client", required=True)
    parser.add_argument("--date", help="记录日期 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = dt.datetime.now().replace(microsecond=0)
    day = dt.date.fromisoformat(args.date) if args.date else now.date()

    ws_url = open_tab(args.port, ORDER_URL, ready_len=1500)
    # 页面是异步拼的，等页签数字出来再读
    counts, total = None, None
    for _ in range(10):
        counts, total = read_counts(ws_url)
        if counts.get("待出库") is not None or counts.get("暂停订单") is not None:
            break
        time.sleep(3)

    print(f"[{args.client}] 订单状态 {counts} 合计={total}")

    pause_rows = []
    if navigate(ws_url, SUSPEND_URL, "tabType=suspend", ready_len=1000):
        time.sleep(2)
        pause_rows = read_pause_rows(ws_url)
        print(f"[{args.client}] 暂停订单明细 {len(pause_rows)} 条")
    else:
        print(f"[{args.client}] 暂停订单页没打开，明细跳过")

    if args.dry_run:
        for row in pause_rows[:5]:
            print("  ", {k: row.get(k) for k in ("订单号", "下单时间", "SKU编码", "状态说明")})
        return 0

    conn = connect()
    try:
        with conn.cursor() as cursor:
            for ddl in DDL:
                cursor.execute(ddl)
            cursor.execute(
                "INSERT INTO `jd_order_status_daily` (`记录日期`,`店铺`,`待付款`,`待出库`,"
                "`已出库`,`暂停订单`,`锁定订单`,`合计`,`采集时间`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "`待付款`=VALUES(`待付款`),`待出库`=VALUES(`待出库`),"
                "`已出库`=VALUES(`已出库`),`暂停订单`=VALUES(`暂停订单`),"
                "`锁定订单`=VALUES(`锁定订单`),`合计`=VALUES(`合计`),"
                "`采集时间`=VALUES(`采集时间`)",
                (day, args.client, counts.get("待付款"), counts.get("待出库"),
                 counts.get("已出库"), counts.get("暂停订单"), counts.get("锁定订单"),
                 total, now),
            )
            hour = now.replace(minute=0, second=0, microsecond=0)
            cursor.execute(
                "INSERT INTO `jd_order_status_hourly` (`记录日期`,`店铺`,`小时`,`待付款`,`待出库`,"
                "`已出库`,`暂停订单`,`锁定订单`,`合计`,`采集时间`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "`待付款`=VALUES(`待付款`),`待出库`=VALUES(`待出库`),"
                "`已出库`=VALUES(`已出库`),`暂停订单`=VALUES(`暂停订单`),"
                "`锁定订单`=VALUES(`锁定订单`),`合计`=VALUES(`合计`),"
                "`采集时间`=VALUES(`采集时间`)",
                (day, args.client, hour, counts.get("待付款"), counts.get("待出库"),
                 counts.get("已出库"), counts.get("暂停订单"), counts.get("锁定订单"),
                 total, now),
            )
            pause_payload = [(
                day, args.client, row["订单号"], row.get("下单时间"), row.get("付款时间"),
                row.get("SKU编码"), row.get("商品"), row.get("物流单号"), row.get("状态说明"),
                now, now, row.get("原始行"),
            ) for row in pause_rows if row.get("订单号")]
            if pause_payload:
                cursor.executemany(
                    "INSERT INTO `jd_order_pause_history` (`记录日期`,`店铺`,`订单号`,`下单时间`,"
                    "`付款时间`,`SKU编码`,`商品`,`物流单号`,`状态说明`,`首次发现时间`,"
                    "`最后确认时间`,`原始行`) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE `最后确认时间`=VALUES(`最后确认时间`),"
                    "`确认次数`=`确认次数`+1,"
                    "`下单时间`=COALESCE(VALUES(`下单时间`),`下单时间`),"
                    "`付款时间`=COALESCE(VALUES(`付款时间`),`付款时间`),"
                    "`SKU编码`=COALESCE(VALUES(`SKU编码`),`SKU编码`),"
                    "`商品`=COALESCE(VALUES(`商品`),`商品`),"
                    "`物流单号`=COALESCE(VALUES(`物流单号`),`物流单号`),"
                    "`状态说明`=VALUES(`状态说明`),`原始行`=VALUES(`原始行`)",
                    pause_payload,
                )
        conn.commit()
        print(f"[{args.client}] 已写库，暂停明细 {len(pause_payload)} 条")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
