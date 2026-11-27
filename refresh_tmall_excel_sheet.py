#!/usr/bin/env python3
"""Refresh the 天猫在售商品可报名匹配 sheet in 保民三平台活动及成本.xlsx.

Data source: adjust_price.tmall_onsale_activity_match_daily_v2 joined to the
activity catalog, plus 天猫红线价 from 成本&红线表.xlsx.  Only this sheet is
rebuilt; all other sheets and their order are preserved.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
from zoneinfo import ZoneInfo

import pymysql
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_TARGET = pathlib.Path("/tmp/codebuddy-chat-logs/保民三平台活动及成本.xlsx")
DEFAULT_RED = pathlib.Path("/tmp/codebuddy-chat-logs/成本&红线表.xlsx")
SHEET = "天猫在售商品可报名匹配"
MONEY_FMT = "#,##0.00"
HEADER_FILL = PatternFill("solid", fgColor="D9E1F2")
HEADER_FONT = Font(bold=True)

HEADERS = [
    "在售商品标题", "在售商品ID", "在售售价(单盒)", "库存", "本店品牌", "在售规格", "活动ID",
    "活动类型", "活动商品名", "活动品牌", "活动规格", "报名状态", "是否补贴", "服务费",
    "包装档位", "档位盒数", "招商价格(档位)", "单盒招商价", "单盒价差(招商-在售)",
    "匹配方式", "skuId", "天猫商品ID", "活动开始", "活动结束", "天猫红线价",
]


def norm(s) -> str:
    s = str(s or "").lower()
    s = re.sub(r"[\[\]（）()/*\s\-—·:：,，。、]", "", s)
    for w in ("盒装", "片装", "粒装", "支装", "瓶装", "袋装", "/盒", "/瓶", "/袋"):
        s = s.replace(w, "")
    return s


def core(s) -> str:
    s = str(s or "")
    s = re.sub(r"^\[[^\]]*\]", "", s)
    m = re.match(r"^([\u4e00-\u9fa5]+)", s)
    return m.group(1) if m else norm(s)


def load_redline(path: pathlib.Path):
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb["成本价对比表"]
    exact, by_core = {}, {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row[1]:
            continue
        item = {"tm": row[5]}
        exact[norm(row[1])] = item
        c = core(row[1])
        if c and c not in by_core:
            by_core[c] = item
    wb.close()
    return exact, by_core


def match_redline(name, exact, by_core):
    n = norm(name)
    if n in exact:
        return exact[n]
    c = core(name)
    if c in by_core:
        return by_core[c]
    if len(c) >= 3:
        for k, v in by_core.items():
            if (c in k or k in c) and min(len(c), len(k)) >= 4:
                return v
    return None


def num(value):
    if value is None:
        return None
    return float(value)


def fetch_rows(conn, day: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.`在售商品标题`,m.`在售商品ID`,m.`在售售价`,m.`库存`,m.`本店品牌`,m.`在售规格`,m.`活动ID`,
                   m.`报名状态`,m.`是否补贴`,m.`服务费`,m.`规格`,m.`包装档位`,m.`招商价格`,m.`match_type`,
                   a.`活动类型`,a.`活动商品名`,a.`品牌`,a.`skuId`,a.`天猫商品ID`,a.`活动开始`,a.`活动结束`
            FROM tmall_onsale_activity_match_daily_v2 m
            LEFT JOIN tmall_activity_available_daily a
              ON a.`记录日期`=m.`记录日期` AND a.`活动ID`=m.`活动ID` AND a.`skuId`=m.`skuId`
            WHERE m.`记录日期`=%s
            ORDER BY m.`在售商品ID`,m.`活动ID`,m.`包装档位`
            """,
            (day,),
        )
        return cur.fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/opt/subsidy/config.json")
    ap.add_argument("--day", default=dt.datetime.now(TZ).date().isoformat())
    ap.add_argument("--target", type=pathlib.Path, default=DEFAULT_TARGET)
    ap.add_argument("--redline", type=pathlib.Path, default=DEFAULT_RED)
    args = ap.parse_args()

    cfg = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8"))["mysql"]
    allowed = {"host", "port", "user", "password", "database", "charset"}
    conn = pymysql.connect(**{k: cfg[k] for k in allowed if k in cfg})
    try:
        rows = fetch_rows(conn, args.day)
    finally:
        conn.close()

    exact, by_core = load_redline(args.redline)

    wb = load_workbook(args.target)
    if SHEET in wb.sheetnames:
        index = wb.sheetnames.index(SHEET)
        wb.remove(wb[SHEET])
    else:
        index = min(3, len(wb.sheetnames))
    ws = wb.create_sheet(SHEET, index)
    ws.append(HEADERS)
    for c in ws[1]:
        c.fill, c.font = HEADER_FILL, HEADER_FONT
        c.alignment = Alignment(horizontal="center", vertical="center")

    red_matched = 0
    for row in rows:
        (
            title, item_id, sale_price, stock, brand, onsale_spec, act_id, applied, subsidy,
            fee, spec, pack, thr, match_type, act_type, act_name, act_brand, sku_id,
            tmall_item_id, act_start, act_end,
        ) = row
        m = re.search(r"(\d+)\s*盒装", str(pack or ""))
        boxes = int(m.group(1)) if m else 1
        red = match_redline(title, exact, by_core)
        redline = num(red["tm"]) if red else None
        if redline is not None:
            red_matched += 1
        ws.append([
            title, item_id, num(sale_price), num(stock), brand, onsale_spec, act_id,
            act_type, act_name, act_brand, spec, applied, subsidy, fee, pack, boxes,
            num(thr), None, None, match_type, sku_id, tmall_item_id, act_start, act_end,
            redline,
        ])

    for name in ("在售售价(单盒)", "招商价格(档位)", "单盒招商价", "单盒价差(招商-在售)"):
        col = HEADERS.index(name) + 1
        for n in range(2, ws.max_row + 1):
            ws.cell(n, col).number_format = MONEY_FMT
    box_col = HEADERS.index("档位盒数") + 1
    thr_col = HEADERS.index("招商价格(档位)") + 1
    unit_col = HEADERS.index("单盒招商价") + 1
    price_col = HEADERS.index("在售售价(单盒)") + 1
    diff_col = HEADERS.index("单盒价差(招商-在售)") + 1
    for n in range(2, ws.max_row + 1):
        ws.cell(n, unit_col, f'=IF(OR({get_column_letter(thr_col)}{n}="",'
                             f'{get_column_letter(box_col)}{n}=""),"",'
                             f'{get_column_letter(thr_col)}{n}/{get_column_letter(box_col)}{n})')
        ws.cell(n, diff_col, f'=IF(OR({get_column_letter(unit_col)}{n}="",'
                             f'{get_column_letter(price_col)}{n}=""),"",'
                             f'{get_column_letter(unit_col)}{n}-{get_column_letter(price_col)}{n})')

    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"
    wb.save(args.target)
    print(
        json.dumps(
            {
                "day": args.day,
                "sheet": SHEET,
                "rows": len(rows),
                "redline_matched": red_matched,
                "target": str(args.target),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
