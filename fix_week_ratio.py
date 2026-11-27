#!/usr/bin/env python3
"""为缺失的 2026-09-19 天猫行补齐「昨日周环比」（口径与同步器一致）。"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pymysql

from workbench import qualified, refresh_yesterday_week_ratio, target as workbench_target

CONFIG = pathlib.Path("/opt/subsidy/config.json")
TARGET_DATE = dt.date(2026, 9, 19)
CLIENT = "保民"
PLATFORM = "天猫"

config = json.loads(CONFIG.read_text(encoding="utf-8"))
mysql = config["mysql"]
schema, table = workbench_target(config)
print(f"目标: {qualified(schema, table)}  日期={TARGET_DATE}  {CLIENT}/{PLATFORM}")

conn = pymysql.connect(**mysql, autocommit=False)
try:
    refresh_yesterday_week_ratio(
        conn, schema=schema, table=table,
        record_date=TARGET_DATE, client=CLIENT, platform=PLATFORM,
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT `日期`,`昨日交易额`,`昨日周环比`,`待商家审核数量`,`即将自动退售后单` "
            f"FROM {qualified(schema, table)} WHERE `平台`=%s AND `客户`=%s "
            f"AND `日期` BETWEEN %s AND %s ORDER BY `日期` DESC",
            (PLATFORM, CLIENT, TARGET_DATE - dt.timedelta(days=9), TARGET_DATE + dt.timedelta(days=1)),
        )
        print(f"{'日期':<12}{'昨日交易额':>14}{'昨日周环比':>16}{'待审核':>8}{'即将超时':>10}")
        for row in cur.fetchall():
            print(f"{str(row[0]):<12}{str(row[1]):>14}{str(row[2]):>16}{str(row[3]):>8}{str(row[4]):>10}")
finally:
    conn.close()
