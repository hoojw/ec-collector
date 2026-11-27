#!/usr/bin/env python3
"""Validate the final JD subsidy SKU output table."""

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
                    COUNT(*),
                    COUNT(DISTINCT `策略ID`),
                    SUM(`商品upc` LIKE '%%*%%'),
                    SUM(
                        (`活动盒数` = 1 AND `商家SKU` <> `商品upc`)
                        OR
                        (`活动盒数` > 1 AND
                         `商家SKU` <> CONCAT(`商品upc`, '*', `活动盒数`))
                    ),
                    SUM(`京东价` IS NULL)
                FROM `jd_subsidy_sku`
                WHERE `记录日期` = %s
                """,
                (args.record_date,),
            )
            totals = cursor.fetchone()
            cursor.execute(
                """
                SELECT
                    `策略ID`, `商品upc`, `商家SKU`, `品牌`,
                    `通用名称`, `单盒规格`, `活动盒数`, `京东价`
                FROM `jd_subsidy_sku`
                WHERE `记录日期` = %s
                ORDER BY `品牌`, `通用名称`, `活动盒数`
                LIMIT 20
                """,
                (args.record_date,),
            )
            sample = [
                {
                    "strategy_id": row[0],
                    "product_upc": row[1],
                    "merchant_sku": row[2],
                    "brand": row[3],
                    "generic_name": row[4],
                    "single_box_spec": row[5],
                    "activity_box_count": row[6],
                    "jd_price": str(row[7]),
                }
                for row in cursor.fetchall()
            ]
    finally:
        connection.close()

    print(
        json.dumps(
            {
                "rows": int(totals[0]),
                "distinct_strategy_ids": int(totals[1]),
                "product_upc_with_suffix": int(totals[2] or 0),
                "merchant_sku_quantity_mismatches": int(totals[3] or 0),
                "blank_jd_price": int(totals[4] or 0),
                "sample": sample,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
