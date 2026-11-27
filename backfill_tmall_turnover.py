#!/usr/bin/env python3
"""天猫昨日交易额一次性回填工具（合并自 backfill_tmall_turnover.py + backfill_tmall_daily_turnover.py）。

口径（与 tmall_ecommerce_to_mysql.py 一致）：
  「日期」= 数据日 + 1 —— 数据日 2026-09-16 的交易额 8545.25
                          写入 日期=2026-09-17 的「昨日交易额」列。
  只写「昨日交易额」一列，其余列不动/保持 NULL。
  定位键：客户=保民、平台=天猫、日期。

子命令：
  builtin   使用脚本内置的 30 天序列（2026-08-18 ~ 2026-09-16）  <- backfill_tmall_turnover.py
  snapshot  从 daily 快照 JSON 的 data.portal.metrics.按日交易额 提取序列 <- backfill_tmall_daily_turnover.py

示例：
  python tools/backfill_tmall_turnover.py builtin --dry-run
  python tools/backfill_tmall_turnover.py snapshot --snapshot received/xxx.json --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import pymysql

CONFIG_PATH = "/opt/subsidy/config.json"
SCHEMA = "adjust_price"
TABLE = "esale_system"
CLIENT = "保民"
PLATFORM = "天猫"

TURNOVER_KEY = "昨日交易额"
SERIES_KEY = "按日交易额"
DATE_KEY = "日期"
AMOUNT_KEY = "交易额"

# 内置序列：从 Win 服务器 daily 快照提取的 30 天序列（数据日 -> 交易额）
BUILTIN_SERIES: list[tuple[str, str]] = [
    ("2026-08-18", "6605.19"),
    ("2026-08-19", "3356.08"),
    ("2026-08-20", "2906.02"),
    ("2026-08-21", "1950.49"),
    ("2026-08-22", "4475.16"),
    ("2026-08-23", "5095.57"),
    ("2026-08-24", "7457.08"),
    ("2026-08-25", "7311.71"),
    ("2026-08-26", "9678.07"),
    ("2026-08-27", "0.0"),
    ("2026-08-28", "1463.06"),
    ("2026-08-29", "492.74"),
    ("2026-08-30", "295.5"),
    ("2026-08-31", "124.25"),
    ("2026-09-01", "0.0"),
    ("2026-09-02", "49.47"),
    ("2026-09-03", "195.84"),
    ("2026-09-04", "502.86"),
    ("2026-09-05", "1917.37"),
    ("2026-09-06", "37102.45"),
    ("2026-09-07", "153711.01"),
    ("2026-09-08", "30713.28"),
    ("2026-09-09", "132832.49"),
    ("2026-09-10", "2854.69"),
    ("2026-09-11", "18650.81"),
    ("2026-09-12", "23637.0"),
    ("2026-09-13", "31419.06"),
    ("2026-09-14", "4626.26"),
    ("2026-09-15", "9883.71"),
    ("2026-09-16", "8545.25"),
]


def load_mysql_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)["mysql"]


def build_plan(rows: list[tuple[dt.date, str]]) -> list[tuple[dt.date, str]]:
    """返回 [(目标日期, 昨日交易额)]，目标日期 = 数据日 + 1。"""
    return [(data_day + dt.timedelta(days=1), amount) for data_day, amount in rows]


def series_from_builtin() -> list[tuple[dt.date, str]]:
    return [
        (dt.date.fromisoformat(data_day), amount)
        for data_day, amount in BUILTIN_SERIES
    ]


def series_from_snapshot(snapshot: dict) -> list[tuple[dt.date, str]]:
    """从快照里抽出 (数据日, 交易额原始字符串) 列表。"""
    try:
        metrics = snapshot["data"]["portal"]["metrics"]
        series = metrics[SERIES_KEY]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"snapshot is missing data.portal.metrics.{SERIES_KEY}") from exc
    if not isinstance(series, list) or not series:
        raise ValueError("按日交易额 series must be a non-empty list")

    rows: list[tuple[dt.date, str]] = []
    for index, item in enumerate(series):
        if not isinstance(item, dict):
            raise ValueError(f"series[{index}] must be an object")
        raw_date = item.get(DATE_KEY)
        amount = item.get(AMOUNT_KEY)
        if amount is None:
            raise ValueError(f"series[{index}] missing {AMOUNT_KEY}")
        try:
            data_day = dt.date.fromisoformat(str(raw_date))
        except ValueError as exc:
            raise ValueError(f"series[{index}] invalid date: {raw_date!r}") from exc
        rows.append((data_day, str(amount)))

    # 序列必须严格按日递增且无重复，否则说明上游数据异常
    days = [r[0] for r in rows]
    if days != sorted(days) or len(set(days)) != len(days):
        raise ValueError("按日交易额 series must be strictly ascending and unique")
    return rows


def write_plan(conn, plan: list[tuple[dt.date, str]]) -> int:
    sql = (
        f"INSERT INTO `{SCHEMA}`.`{TABLE}` (`日期`,`客户`,`平台`,`{TURNOVER_KEY}`) "
        "VALUES (%s, %s, %s, %s) "
        f"ON DUPLICATE KEY UPDATE `{TURNOVER_KEY}`=VALUES(`{TURNOVER_KEY}`)"
    )
    params = [(d.isoformat(), CLIENT, PLATFORM, a) for d, a in plan]
    with conn.cursor() as cur:
        affected = cur.executemany(sql, params)
    conn.commit()
    return affected


def main() -> int:
    parser = argparse.ArgumentParser(
        description="天猫昨日交易额回填（builtin / snapshot）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("builtin", help="使用内置 30 天序列回填")
    p.add_argument("--config", default=CONFIG_PATH)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(source="builtin")

    p = sub.add_parser("snapshot", help="从 daily 快照 JSON 提取按日交易额序列回填")
    p.add_argument("--snapshot", required=True, help="daily 快照 JSON 路径")
    p.add_argument("--config", default=CONFIG_PATH)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(source="snapshot")

    args = parser.parse_args()

    if args.source == "builtin":
        rows = series_from_builtin()
    else:
        with open(args.snapshot, "r", encoding="utf-8") as fh:
            rows = series_from_snapshot(json.load(fh))

    plan = build_plan(rows)
    print(f"序列 {len(rows)} 天：{rows[0][0]} ~ {rows[-1][0]}")
    print(f"计划写入 {len(plan)} 行：{plan[0][0]} ~ {plan[-1][0]}")

    if args.dry_run:
        for target_date, amount in plan:
            print(f"  {target_date}  {TURNOVER_KEY}={amount}")
        print(f"共 {len(plan)} 行 (dry_run=True)")
        return 0

    conn = pymysql.connect(**load_mysql_config(args.config), autocommit=False)
    try:
        affected = write_plan(conn, plan)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    for target_date, amount in plan:
        print(f"  {target_date}  {TURNOVER_KEY}={amount}")
    print(f"executemany rowcount={affected}，共 {len(plan)} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
