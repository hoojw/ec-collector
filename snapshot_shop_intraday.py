#!/usr/bin/env python3
"""当日交易额时点快照（12 点 / 17 点）。

为什么单独一张表：`esale_system` 只有「昨日交易额」，当日实时值散落在各平台
自己的采集表里且**一天只保留最后一条**（按日期 upsert）。实时值过点即失、无法
回补，所以必须在固定时点单独留一份。

数据源（只读，不触发任何采集、不改任何现有表）：
- 京东：各 `jd_<vender_id>.jd_business_report_daily` 的 `成交金额`
- 美团：各库 `ops_business_report` 中 `时间='实时'` 的 `成交金额`
- 天猫：暂无当日交易额落库来源，记 missing（不猜、不填 0）

质量标记：
- ok      源采集时间落在目标时点容差内；京东页面显示「-」（当天暂无成交）也算 ok，金额按 0 落
- stale   数据存在但采集时间过旧（说明那一轮没跑成）
- missing 取不到值（京东整行没解析出来，或源表无当天记录）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from decimal import Decimal

import pymysql

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
TABLE = "esale_shop_intraday"
DEFAULT_TOLERANCE_MIN = 30

# (客户, 平台, 库名, 类型)。京东分库、美团分库均在此登记；
# 新增门店时只需在这里加一行，视图与看板不用改。
JD_SOURCES = [
    ("保民", "jd_136186144"),
    ("百恩堂", "jd_67337414"),
    ("津南南无门诊", "jd_19924616"),
    ("福康综合门诊", "jd_135003681"),
    ("璟和堂", "jd_203228988"),
]
MT_SOURCES = [
    ("adjust_price", "保民"),
    ("baientang", "百恩堂"),
    ("mt_32187777", "津南南无门诊"),
    ("mt_33770492", "福康综合门诊"),
    ("mt_34461640", "璟和堂"),
]

DDL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE}` (
  `记录日期` DATE NOT NULL,
  `时点` TINYINT NOT NULL COMMENT '12 或 17',
  `客户` VARCHAR(64) NOT NULL,
  `平台` VARCHAR(16) NOT NULL,
  `当日交易额` DECIMAL(18,2) NULL,
  `源采集时间` DATETIME NULL COMMENT '源表采集时间；美团源表无此列则为 NULL',
  `数据质量` VARCHAR(16) NOT NULL DEFAULT 'ok' COMMENT 'ok/stale/missing',
  `说明` VARCHAR(255) NULL,
  `写入时间` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`记录日期`,`时点`,`客户`,`平台`),
  KEY `idx_date_platform` (`记录日期`,`平台`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='当日交易额时点快照：过点即失，必须按 12/17 点独立留存'
"""


def connect():
    mysql = json.loads(CONFIG.read_text(encoding="utf-8"))["mysql"]
    return pymysql.connect(
        host=mysql["host"],
        port=int(mysql["port"]),
        user=mysql["user"],
        password=mysql["password"],
        database=mysql.get("dw_database") or mysql["database"],
        charset=mysql.get("charset", "utf8mb4"),
        autocommit=False,
        init_command="SET time_zone='+08:00'",
    )


def ensure_table(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(DDL)
    conn.commit()


def read_jd(conn, schema: str, day: dt.date):
    """京东：当天最新一条业务报告的成交金额、采集时间，以及页面是否解析到了数据。

    「店铺星级 / 可报名活动」只是用来判断这一行是不是正常解析出来的：
    京东后台当天没有成交时，成交金额/单量/客户数/访客数一律显示「-」，
    这些列会被写成 NULL，但同一行的星级、活动数是有值的。
    """
    sql = (
        f"SELECT `成交金额`,`采集时间`,`店铺星级`,`可报名活动` FROM `{schema}`.`jd_business_report_daily` "
        "WHERE `日期`=%s ORDER BY `采集时间` DESC LIMIT 1"
    )
    with conn.cursor() as cursor:
        cursor.execute(sql, (day,))
        return cursor.fetchone()


def read_meituan(conn, schema: str):
    """美团：ops_business_report 的「实时」行。该表无采集时间列。"""
    sql = f"SELECT `成交金额` FROM `{schema}`.`ops_business_report` WHERE `时间`='实时'"
    with conn.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchone()


def table_update_time(conn, schema: str, table: str):
    sql = (
        "SELECT update_time FROM information_schema.tables "
        "WHERE table_schema=%s AND table_name=%s"
    )
    with conn.cursor() as cursor:
        cursor.execute(sql, (schema, table))
        row = cursor.fetchone()
    return row[0] if row else None


def judge(amount, source_time, target: dt.datetime, tolerance_min: int, page_ok: bool = False):
    """返回 (质量, 说明)。"""
    if amount is None:
        # 页面正常解析、只是成交金额显示「-」：当天还没成交，是真实 0，不是采集失败。
        # 成交额只增不减，所以即使采集时间距目标时点较远，判 0 也不会错。
        if page_ok:
            return "ok", "源页成交金额显示「-」（当日暂无成交），按 0 落"
        return "missing", "源表无成交金额（采集异常或字段未解析）"
    if source_time is None:
        # 美团侧无法取到采集时间，只能记 ok 并注明
        return "ok", "美团源表无采集时间列，无法校验新鲜度"
    delta = abs((target - source_time).total_seconds()) / 60.0
    if delta > tolerance_min:
        return "stale", f"源采集时间 {source_time:%H:%M} 偏离目标时点 {delta:.0f} 分钟"
    return "ok", None


def inactive_pairs(conn) -> set[tuple[str, str]]:
    """bi_store_config 里 `是否启用=0` 的 (客户,平台)，快照时跳过。"""
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT `客户`,`平台` FROM `bi_store_config` WHERE `是否启用`=0")
            return {(row[0], row[1]) for row in cursor.fetchall()}
    except pymysql.err.ProgrammingError:
        return set()


def collect(conn, day: dt.date, slot: int, tolerance_min: int,
            skip: set[tuple[str, str]]) -> list[tuple]:
    target = dt.datetime.combine(day, dt.time(hour=slot))
    rows: list[tuple] = []

    for client, schema in JD_SOURCES:
        if (client, "京东") in skip:
            continue
        try:
            found = read_jd(conn, schema, day)
        except pymysql.err.ProgrammingError:
            found = None
        if not found:
            rows.append((day, slot, client, "京东", None, None, "missing",
                         f"库 {schema} 无当天记录", None))
            continue
        amount, captured, star, acts = found
        page_ok = star is not None or acts is not None
        quality, note = judge(amount, captured, target, tolerance_min, page_ok)
        if quality == "ok" and amount is None:
            amount = Decimal("0.00")
        rows.append((day, slot, client, "京东", amount, captured, quality, note, None))

    for schema, client in MT_SOURCES:
        if (client, "美团") in skip:
            continue
        try:
            found = read_meituan(conn, schema)
        except pymysql.err.ProgrammingError:
            found = None
        if not found:
            rows.append((day, slot, client, "美团", None, None, "missing",
                         f"库 {schema}.ops_business_report 无「实时」行", None))
            continue
        amount = found[0]
        quality, note = judge(amount, None, target, tolerance_min)
        if amount is not None and amount == 0:
            quality = "ok" if quality == "ok" else quality
            note = note or "实时成交金额为 0（可能无成交或采集未刷新）"
        rows.append((day, slot, client, "美团", amount, None, quality, note, None))

    # 天猫：当日交易额尚无落库来源
    for client in ("保民", "璟和堂"):
        if (client, "天猫") in skip:
            continue
        rows.append((day, slot, client, "天猫", None, None, "missing",
                     "天猫当日交易额无落库来源（待定显示方式）", None))
    return rows


def write(conn, rows: list[tuple]) -> int:
    sql = (
        f"INSERT INTO `{TABLE}` "
        "(`记录日期`,`时点`,`客户`,`平台`,`当日交易额`,`源采集时间`,`数据质量`,`说明`) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE `当日交易额`=VALUES(`当日交易额`),"
        "`源采集时间`=VALUES(`源采集时间`),`数据质量`=VALUES(`数据质量`),"
        "`说明`=VALUES(`说明`)"
    )
    with conn.cursor() as cursor:
        cursor.executemany(sql, [row[:8] for row in rows])
        affected = cursor.rowcount
    conn.commit()
    return affected


def main() -> int:
    parser = argparse.ArgumentParser(description="当日交易额时点快照")
    parser.add_argument("--slot", type=int, choices=(12, 17), required=True, help="目标时点")
    parser.add_argument("--date", help="记录日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--tolerance", type=int, default=DEFAULT_TOLERANCE_MIN,
                        help=f"新鲜度容差分钟数，默认 {DEFAULT_TOLERANCE_MIN}")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写库")
    args = parser.parse_args()

    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    conn = connect()
    try:
        ensure_table(conn)
        skip = inactive_pairs(conn)
        if skip:
            print(f"跳过停用门店 {len(skip)} 个")
        rows = collect(conn, day, args.slot, args.tolerance, skip)
        print(f"目标 {day} {args.slot}:00，容差 ±{args.tolerance} 分钟")
        for row in rows:
            _d, _s, client, platform, amount, captured, quality, note, _x = row
            shown = "-" if amount is None else f"{Decimal(str(amount)):.2f}"
            print(f"  {platform:<4} {client:<14} {shown:>12}  {quality:<8} {note or ''}")
        bad = [row for row in rows if row[6] != "ok"]
        if args.dry_run:
            print(f"[dry-run] 未写库，异常 {len(bad)} 条")
            return 0
        # 停用门店的旧快照要清掉；放在 dry-run 之后，保证 --dry-run 严格只读
        if skip:
            with conn.cursor() as cursor:
                cursor.executemany(
                    f"DELETE FROM `{TABLE}` WHERE `客户`=%s AND `平台`=%s", sorted(skip))
            conn.commit()
        write(conn, rows)
        # 注意：rowcount 反映「实际发生变更」的行数（MySQL affected-rows 语义），
        # 值未变化的行计 0，因此这里用提交条数而不是 rowcount。
        print(f"提交 {len(rows)} 条，异常 {len(bad)} 条")
        return 0
    finally:
        conn.close()


# EC_DW_RESET_PAUSED_20261126
if __name__ == '__main__':
    raise SystemExit('ec_dw has been cleared; writer paused pending new schema')

if __name__ == "__main__":
    sys.exit(main())
