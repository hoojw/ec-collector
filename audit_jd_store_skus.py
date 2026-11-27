#!/usr/bin/env python3
"""Audit imported JD store SKU raw and normalized tables."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pymysql


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--record-date", required=True, type=dt.date.fromisoformat)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    connection = pymysql.connect(**config["mysql"])
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM `jd_store_sku_daily`
                     WHERE `记录日期` = %s),
                    (SELECT COUNT(*) FROM `jd_store_sku_normalized_daily`
                     WHERE `记录日期` = %s),
                    (SELECT COUNT(DISTINCT `商品编码`)
                     FROM `jd_store_sku_normalized_daily`
                     WHERE `记录日期` = %s),
                    (SELECT COUNT(DISTINCT `SKUID`)
                     FROM `jd_store_sku_normalized_daily`
                     WHERE `记录日期` = %s)
                """,
                (args.record_date,) * 4,
            )
            totals = cursor.fetchone()
            cursor.execute(
                """
                SELECT `解析状态`, COUNT(*)
                FROM `jd_store_sku_normalized_daily`
                WHERE `记录日期` = %s
                GROUP BY `解析状态`
                ORDER BY `解析状态`
                """,
                (args.record_date,),
            )
            statuses = {row[0]: int(row[1]) for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT
                    `SKUID`, `商品编码`, `商品名称`, `商家SKU原值`,
                    `组合数量`, `标注组合数量`, `解析状态`, `解析说明`
                FROM `jd_store_sku_normalized_daily`
                WHERE `记录日期` = %s
                  AND `解析状态` <> 'ok'
                ORDER BY `SKUID`
                """,
                (args.record_date,),
            )
            exceptions = [
                {
                    "sku_id": row[0],
                    "product_code": row[1],
                    "product_name": row[2],
                    "merchant_sku": row[3],
                    "bundle_quantity": row[4],
                    "listed_quantity": row[5],
                    "status": row[6],
                    "message": row[7],
                }
                for row in cursor.fetchall()
            ]
    finally:
        connection.close()

    print(
        json.dumps(
            {
                "raw_rows": int(totals[0]),
                "normalized_rows": int(totals[1]),
                "distinct_product_codes": int(totals[2]),
                "distinct_sku_ids": int(totals[3]),
                "parse_statuses": statuses,
                "exceptions": exceptions,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
