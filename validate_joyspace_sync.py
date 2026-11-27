#!/usr/bin/env python3
"""Validate a JoySpace subsidy snapshot against the local MySQL batch."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pymysql


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--record-date", required=True, type=dt.date.fromisoformat)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    connection = pymysql.connect(**config["mysql"])
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*),
                    COUNT(DISTINCT `策略ID`),
                    COUNT(DISTINCT `药品ID`),
                    SUM(`品牌名` = ''),
                    SUM(`通用名称` = ''),
                    SUM(`规格` = '')
                FROM `jd_subsidy_catalog_daily`
                WHERE `记录日期` = %s
                """,
                (args.record_date,),
            )
            row = cursor.fetchone()
    finally:
        connection.close()

    result = {
        "snapshot_rows": len(snapshot["rows"]),
        "database_rows": int(row[0]),
        "distinct_strategy_ids": int(row[1]),
        "distinct_drug_ids": int(row[2]),
        "blank_brand": int(row[3] or 0),
        "blank_generic_name": int(row[4] or 0),
        "blank_specification": int(row[5] or 0),
    }
    if result["snapshot_rows"] != result["database_rows"]:
        raise RuntimeError(f"snapshot/database row mismatch: {result}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
