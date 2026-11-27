"""Check whether the shared JD activity catalog is usable.

The catalog is a single overwritten snapshot (``jd_subsidy_catalog``) rather
than one partition per day, so "current" no longer means "a partition for
today exists".  The only hard requirement is that the snapshot is non-empty;
freshness is reported for monitoring but never blocks a calculation, which is
the whole point of the overwritten table.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
from zoneinfo import ZoneInfo

import pymysql


TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_TABLE = "jd_subsidy_catalog"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--record-date", type=dt.date.fromisoformat)
    args = parser.parse_args()
    record_date = args.record_date or dt.datetime.now(TZ).date()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    mysql = config["mysql"]
    table = (config.get("tables") or {}).get("jd_subsidy_catalog", DEFAULT_TABLE)
    if not table.replace("_", "").isalnum():
        raise ValueError(f"unsafe JD catalog table: {table!r}")
    conn = pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]), user=mysql["user"], password=mysql["password"],
        database=mysql["database"], charset=mysql.get("charset", "utf8mb4"),
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT MAX(`更新时间`), MAX(`同步日期`), COUNT(*) FROM `{table}`")
            updated_at, synced_date, count = cursor.fetchone()
    finally:
        conn.close()
    print(
        json.dumps(
            {
                "record_date": record_date.isoformat(),
                "table": table,
                "updated_at": updated_at,
                "synced_date": synced_date,
                "rows": int(count),
            },
            ensure_ascii=False,
            default=str,
        )
    )
    return 0 if int(count) > 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
