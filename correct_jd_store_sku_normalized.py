#!/usr/bin/env python3
"""Apply user-confirmed corrections to normalized JD store SKU rows."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pymysql


CORRECTIONS = [
    {
        "sku_id": "10222619650172",
        "merchant_sku": "6910853810272*3",
        "base_upc": "6910853810272",
        "bundle_quantity": 3,
    },
    {
        "sku_id": "10222619650173",
        "merchant_sku": "6910853810272*5",
        "base_upc": "6910853810272",
        "bundle_quantity": 5,
    },
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--record-date", required=True, type=dt.date.fromisoformat)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    connection = pymysql.connect(**config["mysql"], autocommit=False)
    try:
        with connection.cursor() as cursor:
            for correction in CORRECTIONS:
                cursor.execute(
                    """
                    UPDATE `jd_store_sku_normalized_daily`
                    SET
                        `商家SKU原值` = %s,
                        `基础UPC` = %s,
                        `组合数量` = %s,
                        `解析状态` = 'ok',
                        `解析说明` = '用户确认修正商家SKU组合数量'
                    WHERE `记录日期` = %s
                      AND `SKUID` = %s
                      AND `标注组合数量` = %s
                    """,
                    (
                        correction["merchant_sku"],
                        correction["base_upc"],
                        correction["bundle_quantity"],
                        args.record_date,
                        correction["sku_id"],
                        correction["bundle_quantity"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"Correction did not update exactly one row: "
                        f"{correction['sku_id']}"
                    )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(
        json.dumps(
            {
                "record_date": args.record_date.isoformat(),
                "corrected_rows": len(CORRECTIONS),
                "sku_ids": [item["sku_id"] for item in CORRECTIONS],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
