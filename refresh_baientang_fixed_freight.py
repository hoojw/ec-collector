#!/usr/bin/env python3
"""Refresh Baientang's physical fixed-freight tables."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pymysql


TABLE_DDL = """
CREATE TABLE IF NOT EXISTS `{schema}`.`{table}` (
    `货品名称` LONGTEXT NULL,
    `货品编号` BIGINT NULL,
    `订购盒数` BIGINT NULL,
    `订单数量` BIGINT NULL,
    `总订单数量` BIGINT NULL,
    `出现概率百分比` DECIMAL(27,8) NULL,
    `档位总运费` DECIMAL(27,8) NULL,
    `单盒平均运费` DECIMAL(27,8) NULL,
    `订单平均运费` DECIMAL(27,8) NULL,
    KEY `idx_goods_box` (`货品编号`, `订购盒数`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=Path, default=Path("/opt/subsidy/baientang/config.json")
    )
    parser.add_argument("--average-freight", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))["mysql"]
    connection = pymysql.connect(
        host=config["host"],
        port=int(config["port"]),
        user=config["user"],
        password=config["password"],
        charset="utf8mb4",
        autocommit=False,
    )
    targets = (
        ("baientang", "fixed_freight"),
        ("adjust_price", "bi_baientang_freight"),
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("DROP VIEW IF EXISTS `adjust_price`.`bi_baientang_freight`")
            for schema, table in targets:
                cursor.execute(TABLE_DDL.format(schema=schema, table=table))
                cursor.execute(f"TRUNCATE TABLE `{schema}`.`{table}`")
                cursor.execute(
                    f"""INSERT INTO `{schema}`.`{table}`
                        (`货品名称`, `货品编号`, `订购盒数`, `订单数量`,
                         `总订单数量`, `出现概率百分比`, `档位总运费`,
                         `单盒平均运费`, `订单平均运费`)
                        SELECT
                            p.`商品名称`,
                            CAST(p.`商品upc` AS UNSIGNED),
                            d.`订购盒数`,
                            d.`订单数量`,
                            d.`总订单数量`,
                            d.`出现概率百分比`,
                            CAST(d.`订单数量` * %s AS DECIMAL(27,8)),
                            CAST(%s / NULLIF(d.`订购盒数`, 0) AS DECIMAL(27,8)),
                            CAST(%s AS DECIMAL(27,8))
                        FROM (
                            SELECT `商品upc`, MAX(`商品名称`) AS `商品名称`
                            FROM `baientang`.`subsidy_unsubsidized_daily`
                            WHERE `商品upc` REGEXP '^[0-9]+$'
                            GROUP BY `商品upc`
                        ) p
                        CROSS JOIN (
                            SELECT
                                f.`订购盒数`,
                                SUM(f.`订单数量`) AS `订单数量`,
                                totals.`总订单数量`,
                                CAST(
                                    SUM(f.`订单数量`) /
                                    NULLIF(totals.`总订单数量`, 0)
                                    AS DECIMAL(27,8)
                                ) AS `出现概率百分比`
                            FROM `dataease`.`excel_数据1_d2c7507960` f
                            CROSS JOIN (
                                SELECT SUM(`订单数量`) AS `总订单数量`
                                FROM `dataease`.`excel_数据1_d2c7507960`
                            ) totals
                            GROUP BY f.`订购盒数`, totals.`总订单数量`
                        ) d""",
                    (args.average_freight,) * 3,
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print("Baientang fixed freight refreshed in baientang and adjust_price")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
