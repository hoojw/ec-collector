#!/usr/bin/env python3
"""Export Meituan subsidy data: ONE sheet per store, three status blocks merged.

Each store has its own database (per-store config). For each store we read the
three source tables, keep ALL original columns plus a 状态 column, union the
columns, and stack rows in fixed status order: 补贴中 -> 未补贴 -> 未建品.
Columns that do not apply to a status are left empty (as in the source).
"""
from __future__ import annotations
import argparse, json, pathlib

import pymysql
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# 店铺 -> config（南无已停止合作，不包含）
STORE_CONFIGS = {
    "保民": "/opt/subsidy/config.json",
    "福康综合门诊": "/opt/subsidy/stores/mt-33770492/config.json",
    "璟和堂": "/opt/subsidy/stores/mt-34461640/config.json",
}

# 状态顺序 + 源表
STATUS_TABLES = [
    ("补贴中", "mt_subsidy_subsidized_daily"),
    ("未补贴", "mt_subsidy_unsubsidized_daily"),
    ("未建品", "mt_subsidy_unlisted_daily"),
]

# 所有列的顺序（三张表字段的并集），第一列为状态
COLUMNS = [
    ("数据日期", 12), ("店铺", 12), ("poi_id", 12), ("poi_name", 34),
    ("upc", 16), ("sku_id", 14), ("sku_name", 42), ("类别", 12),
    ("活动类型", 12), ("活动名称", 32), ("补贴状态", 12),
    ("原价", 12), ("当前售价", 12), ("补贴门槛", 12), ("补贴后价", 12),
    ("单件补贴", 12), ("商家承担", 12), ("门槛盒数", 10), ("补贴范围", 40),
    ("活动开始", 20), ("活动结束", 20), ("活动时间段", 14), ("活动周期", 14),
    ("未补贴原因", 20), ("我的价格", 12), ("至少再降", 12),
    ("预估补贴", 12), ("建议定价", 12), ("reject_time", 20),
]
DATE_COLS = {"数据日期", "活动开始", "活动结束", "reject_time"}
HEADER_FILL = PatternFill("solid", fgColor="305496")
STATUS_FILLS = {
    "补贴中": PatternFill("solid", fgColor="C6EFCE"),
    "未补贴": PatternFill("solid", fgColor="FFEB9C"),
    "未建品": PatternFill("solid", fgColor="DDEBF7"),
}
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def q(name):
    return "`" + name.replace("`", "``") + "`"


def connect(cfg_path):
    c = json.loads(pathlib.Path(cfg_path).read_text(encoding="utf-8"))["mysql"]
    return pymysql.connect(host=c["host"], port=int(c["port"]), user=c["user"],
        password=c["password"], database=c["database"],
        charset=c.get("charset", "utf8mb4"))


def table_exists(cur, table):
    cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_name=%s", (table,))
    return cur.fetchone()[0] > 0


def existing_columns(cur, table):
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=DATABASE() AND table_name=%s", (table,))
    return {r[0] for r in cur.fetchall()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out_cols = [c for c, _ in COLUMNS]
    wb = Workbook()
    wb.remove(wb.active)
    summary = {}

    for store, cfg_path in STORE_CONFIGS.items():
        try:
            conn = connect(cfg_path)
        except Exception as e:
            summary[store] = {"error": f"db connect: {e}"}
            continue
        store_rows = []
        counts = {}
        try:
            with conn.cursor() as cur:
                for status, table in STATUS_TABLES:
                    if not table_exists(cur, table):
                        counts[status] = 0
                        continue
                    have = existing_columns(cur, table)
                    cur.execute(f"SELECT MAX(`数据日期`) FROM {q(table)} WHERE `店铺`=%s", (store,))
                    latest = cur.fetchone()[0]
                    if latest is None:
                        counts[status] = 0
                        continue
                    # 只取该表真实存在、且我们列里定义的字段
                    select_cols = [c for c in out_cols if c in have]
                    col_sql = ", ".join(q(c) for c in select_cols)
                    cur.execute(f"SELECT {col_sql} FROM {q(table)} "
                                f"WHERE `数据日期`=%s AND `店铺`=%s ORDER BY `upc`",
                                (latest, store))
                    rows = cur.fetchall()
                    counts[status] = len(rows)
                    for row in rows:
                        rec = dict(zip(select_cols, row))
                        current = rec.get('补贴状态')
                        if current is None or str(current).strip() == '':
                            rec['补贴状态'] = status
                        store_rows.append(rec)
        finally:
            conn.close()

        # 该店三张表全空则跳过
        if not store_rows:
            summary[store] = counts
            continue
        summary[store] = counts

        ws = wb.create_sheet(store)
        ws.append(out_cols)
        for cell in ws[1]:
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for rec in store_rows:
            ws.append([rec.get(c) for c in out_cols])
        for idx, (col, width) in enumerate(COLUMNS, start=1):
            letter = get_column_letter(idx)
            ws.column_dimensions[letter].width = width
            if col in DATE_COLS:
                fmt = "yyyy-mm-dd" if col == "数据日期" else "yyyy-mm-dd hh:mm:ss"
                for cell in ws[letter][1:]:
                    cell.number_format = fmt
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(out_cols))}{len(store_rows)+1}"
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.border = BORDER

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(json.dumps({"out": str(out), "sheets": wb.sheetnames,
        "columns": len(out_cols), "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
