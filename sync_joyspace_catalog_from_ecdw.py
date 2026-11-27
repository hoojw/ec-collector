#!/usr/bin/env python3
"""把 ec_dw.subsidy 的 JoySpace 目录回写到 adjust_price 的目录表。

当前定时任务只把 JoySpace 写入 ec_dw.subsidy，但 build_jd_subsidy_sku.py
仍读取 adjust_price.jd_subsidy_catalog。这个桥接脚本补上这一步。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pymysql

from sync_joyspace_subsidy import (
    EXPECTED_HEADERS,
    decimal_or_none,
    int_or_none,
    ensure_table,
)

DEFAULT_EC_CONFIG = Path('/opt/subsidy/config.json')
DEFAULT_LOCAL_CONFIG = Path('/opt/subsidy/config.local.json')
TZ = dt.timezone(dt.timedelta(hours=8))
INT_COLS = {'大盘排序', '盒数', '前几个低价补贴'}
DEC_COLS = {'价格下限', '价格上限'}


def convert(header, value):
    if header in INT_COLS:
        return int_or_none(value)
    if header in DEC_COLS:
        return decimal_or_none(value)
    return str(value or '')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ec-config', type=Path, default=DEFAULT_EC_CONFIG)
    ap.add_argument('--local-config', type=Path, default=DEFAULT_LOCAL_CONFIG)
    ap.add_argument('--record-date', type=dt.date.fromisoformat, required=True)
    args = ap.parse_args()

    ec = json.loads(args.ec_config.read_text(encoding='utf-8'))['mysql']
    local = json.loads(args.local_config.read_text(encoding='utf-8'))['mysql']

    ec_conn = pymysql.connect(
        host=ec['host'], port=int(ec['port']), user=ec['user'],
        password=ec['password'], database=ec.get('dw_database', 'ec_dw'),
        charset=ec.get('charset', 'utf8mb4'),
    )
    try:
        with ec_conn.cursor() as cur:
            cur.execute(
                "SELECT 来源原始字段 FROM subsidy WHERE 客户 IS NULL AND 平台='京东' "
                "AND 记录类型='京东补贴目录' AND 修改日期=%s",
                (args.record_date,),
            )
            rows = [json.loads(r[0]) for r in cur.fetchall()]
    finally:
        ec_conn.close()

    if not rows:
        raise SystemExit(f'no JoySpace catalog rows in ec_dw for {args.record_date}')

    values = []
    for raw in rows:
        values.append(tuple(convert(h, raw.get(h)) for h in EXPECTED_HEADERS))

    local_conn = pymysql.connect(
        host=local['host'], port=int(local['port']), user=local['user'],
        password=local['password'], database=local['database'],
        charset=local.get('charset', 'utf8mb4'), autocommit=False,
    )
    try:
        ensure_table(local_conn)
        now = dt.datetime.now(TZ).replace(tzinfo=None)
        cols = ','.join(f'`{h}`' for h in EXPECTED_HEADERS)
        marks = ','.join(['%s'] * len(EXPECTED_HEADERS))
        with local_conn.cursor() as cur:
            cur.execute('DELETE FROM `jd_subsidy_catalog`')
            cur.executemany(
                f"INSERT INTO `jd_subsidy_catalog` (`同步日期`,`更新时间`,{cols}) "
                f"VALUES (%s,%s,{marks})",
                [(args.record_date, now, *row) for row in values],
            )
            cur.execute('DELETE FROM `jd_subsidy_catalog_daily` WHERE `记录日期`=%s', (args.record_date,))
            cur.executemany(
                f"INSERT INTO `jd_subsidy_catalog_daily` (`记录日期`,{cols}) "
                f"VALUES (%s,{marks})",
                [(args.record_date, *row) for row in values],
            )
        local_conn.commit()
    except Exception:
        local_conn.rollback()
        raise
    finally:
        local_conn.close()

    print(json.dumps({
        'record_date': args.record_date.isoformat(),
        'rows': len(values),
        'target': 'adjust_price.jd_subsidy_catalog + jd_subsidy_catalog_daily',
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
