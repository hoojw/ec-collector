#!/usr/bin/env python3
"""把保民京东小官补的售价改回京麦在售 SKU 价。

京东价来自 jd_subsidy_sku（京麦采集），是整单金额；
JoySpace 的实际入围最高/最低价只保留在参考列，不再用于单盒售价。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import pymysql
from openpyxl import load_workbook

sys.path.insert(0, "/opt/subsidy")
from apply_baomin_formulas import load_redline, match  # noqa: E402

DEFAULT_TARGET = pathlib.Path("/tmp/codebuddy-chat-logs/保民三平台活动及成本.xlsx")
DEFAULT_CONFIG = pathlib.Path("/opt/subsidy/config.json")
DEFAULT_RED = pathlib.Path("/tmp/codebuddy-chat-logs/成本&红线表.xlsx")


def load_db(config: pathlib.Path):
    cfg = json.loads(config.read_text(encoding="utf-8"))["mysql"]
    allowed = {"host", "port", "user", "password", "database", "charset"}
    conn = pymysql.connect(**{k: cfg[k] for k in allowed if k in cfg})
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.策略ID, s.京东价, c.实际入围最高价, c.实际入围最低价
                FROM adjust_price.jd_subsidy_sku s
                LEFT JOIN adjust_price.jd_subsidy_catalog c ON s.策略ID = c.策略ID
                WHERE s.记录日期=(SELECT MAX(记录日期) FROM adjust_price.jd_subsidy_sku)
                """
            )
            return {str(r[0]): r for r in cur.fetchall()}
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=pathlib.Path, default=DEFAULT_TARGET)
    ap.add_argument("--config", type=pathlib.Path, default=DEFAULT_CONFIG)
    ap.add_argument("--redline", type=pathlib.Path, default=DEFAULT_RED)
    args = ap.parse_args()

    db_rows = load_db(args.config)
    exact, by_core = load_redline(args.redline)
    wb = load_workbook(args.target)
    ws = wb["保民京东小官补"]
    col = {c.value: i + 1 for i, c in enumerate(ws[1])}

    changed = 0
    for r in range(2, ws.max_row + 1):
        sid = str(ws.cell(r, col["策略ID"]).value or "")
        rec = db_rows.get(sid)
        if not rec:
            continue
        jd_price = rec[1]
        if jd_price is None:
            continue
        try:
            jd_price = float(jd_price)
        except (TypeError, ValueError):
            continue
        boxes = ws.cell(r, col["活动盒数"]).value
        try:
            boxes = int(boxes) if boxes else 1
        except (TypeError, ValueError):
            boxes = 1

        ws.cell(r, col["京东平台售价"]).value = jd_price
        ws.cell(r, col["单盒售价"]).value = round(jd_price / boxes, 2) if boxes else None
        ws.cell(r, col["售价来源"]).value = "京麦平台"

        item = match(
            ws.cell(r, col["通用名称"]).value,
            ws.cell(r, col["单盒规格"]).value,
            exact,
            by_core,
        )
        if item:
            redline = (float(item["cost"]) * boxes + float(item["freight_jd"])) / 0.84
            ws.cell(r, col["是否可尝试报名"]).value = (
                "可参加" if jd_price >= redline else "不参加"
            )
        else:
            ws.cell(r, col["是否可尝试报名"]).value = None
        changed += 1

    wb.save(args.target)
    print(f"updated {changed} JD rows from 京麦 price")


if __name__ == "__main__":
    main()
