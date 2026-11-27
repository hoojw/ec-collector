#!/usr/bin/env python3
"""Build the single-table JD subsidy dataset consumed by DataEase."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path

import pymysql


DEFAULT_SOURCE_TABLE = "jd_subsidy_sku"
DEFAULT_CATALOG_TABLE = "jd_subsidy_catalog"
DEFAULT_OUTPUT_TABLE = "jd_subsidy_daily"
DEFAULT_COST_SCHEMA = "dataease"
DEFAULT_COST_TABLE = "excel_Result 1_1aa17d71d5"
DEFAULT_FREIGHT_SCHEMA = "dataease"
DEFAULT_FREIGHT_TABLE = "excel_Result 1_d2c7507960"
IDENTIFIER_RE = re.compile(r"^[^`\x00]+$")


def quote_identifier(value: str) -> str:
    if not value or not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"Invalid SQL identifier: {value!r}")
    return f"`{value}`"


def qualified_name(schema: str | None, table: str) -> str:
    if schema:
        return f"{quote_identifier(schema)}.{quote_identifier(table)}"
    return quote_identifier(table)


def table_settings(config: dict) -> dict[str, str]:
    tables = config.get("tables", {})
    return {
        "source_table": tables.get(
            "jd_subsidy_sku",
            DEFAULT_SOURCE_TABLE,
        ),
        "catalog_table": tables.get(
            "jd_subsidy_catalog",
            DEFAULT_CATALOG_TABLE,
        ),
        "output_table": tables.get(
            "jd_subsidy_daily",
            DEFAULT_OUTPUT_TABLE,
        ),
        "cost_schema": tables.get(
            "jd_cost_schema",
            DEFAULT_COST_SCHEMA,
        ),
        "cost_table": tables.get(
            "jd_cost_table",
            DEFAULT_COST_TABLE,
        ),
        "freight_schema": tables.get(
            "jd_freight_schema",
            DEFAULT_FREIGHT_SCHEMA,
        ),
        "freight_table": tables.get(
            "jd_freight_table",
            DEFAULT_FREIGHT_TABLE,
        ),
    }


def ensure_output_table(connection, output_table: str) -> None:
    output = quote_identifier(output_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {output} (
                `记录日期` DATE NOT NULL,
                `策略ID` VARCHAR(50) NOT NULL,
                `商品upc` VARCHAR(100) NOT NULL,
                `商家SKU` VARCHAR(100) NOT NULL,
                `品牌` VARCHAR(255) NOT NULL,
                `通用名称` VARCHAR(255) NOT NULL,
                `单盒规格` VARCHAR(255) NOT NULL,
                `活动盒数` INT NOT NULL,
                `购买件数` INT NOT NULL,
                `实际发出盒数` INT NOT NULL,
                `京东价` DECIMAL(12,2) NOT NULL,
                `价格下限` DECIMAL(12,2) NULL,
                `价格上限` DECIMAL(12,2) NULL,
                `实际入围最高价` VARCHAR(50) NOT NULL,
                `实际入围最低价` VARCHAR(50) NOT NULL,
                `成本价` DECIMAL(27,8) NULL,
                `成本底价` DECIMAL(27,8) NULL,
                `货品编号` VARCHAR(20) NULL,
                `订购盒数` BIGINT NULL,
                `订单数量` BIGINT NULL,
                `总订单数量` BIGINT NULL,
                `出现概率百分比` DECIMAL(27,8) NULL,
                `策略场景权重` DECIMAL(18,8) NULL,
                `档位总运费` DECIMAL(27,8) NULL,
                `单盒平均运费` DECIMAL(27,8) NULL,
                `订单平均运费` DECIMAL(27,8) NULL,
                PRIMARY KEY (`记录日期`, `策略ID`, `购买件数`),
                KEY `idx_jd_subsidy_daily_upc` (`记录日期`, `商品upc`),
                KEY `idx_jd_subsidy_daily_merchant` (`记录日期`, `商家SKU`)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )


def refresh_output(
    connection,
    record_date: dt.date,
    settings: dict[str, str],
) -> dict[str, int]:
    source = quote_identifier(settings["source_table"])
    catalog = quote_identifier(settings["catalog_table"])
    output = quote_identifier(settings["output_table"])
    cost = qualified_name(
        settings["cost_schema"],
        settings["cost_table"],
    )
    freight = qualified_name(
        settings["freight_schema"],
        settings["freight_table"],
    )

    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT COUNT(*) FROM {source} WHERE `记录日期` = %s",
            (record_date,),
        )
        source_rows = int(cursor.fetchone()[0])
        if source_rows == 0:
            raise RuntimeError(
                f"No {settings['source_table']} rows for {record_date}"
            )

        cursor.execute(
            f"DELETE FROM {output} WHERE `记录日期` = %s",
            (record_date,),
        )
        cursor.execute(
            f"""
            INSERT INTO {output} (
                `记录日期`, `策略ID`, `商品upc`, `商家SKU`, `品牌`,
                `通用名称`, `单盒规格`, `活动盒数`,
                `购买件数`, `实际发出盒数`, `京东价`,
                `价格下限`, `价格上限`,
                `实际入围最高价`, `实际入围最低价`,
                `成本价`, `成本底价`, `货品编号`, `订购盒数`,
                `订单数量`, `总订单数量`, `出现概率百分比`,
                `策略场景权重`,
                `档位总运费`, `单盒平均运费`, `订单平均运费`
            )
            SELECT
                activity.`记录日期`,
                activity.`策略ID`,
                activity.`商品upc`,
                activity.`商家SKU`,
                activity.`品牌`,
                activity.`通用名称`,
                activity.`单盒规格`,
                activity.`活动盒数`,
                COALESCE(
                    CAST(
                        freight.`订购盒数` / activity.`活动盒数`
                        AS UNSIGNED
                    ),
                    1
                ),
                COALESCE(
                    freight.`订购盒数`,
                    activity.`活动盒数`
                ),
                activity.`京东价`,
                catalog.`价格下限`,
                catalog.`价格上限`,
                catalog.`实际入围最高价`,
                catalog.`实际入围最低价`,
                cost.`成本价`,
                cost.`成本底价`,
                CONVERT(freight.`货品编号`, CHAR CHARACTER SET utf8mb4),
                freight.`订购盒数`,
                freight.`订单数量`,
                freight.`总订单数量`,
                freight.`出现概率百分比`,
                CAST(freight.`订单数量` AS DECIMAL(27,8))
                    / NULLIF(
                        SUM(freight.`订单数量`) OVER (
                            PARTITION BY
                                activity.`记录日期`,
                                activity.`策略ID`
                        ),
                        0
                    ),
                freight.`档位总运费`,
                freight.`单盒平均运费`,
                freight.`订单平均运费`
            FROM {source} activity
            INNER JOIN {catalog} catalog
                ON activity.`策略ID` = catalog.`策略ID`
            LEFT JOIN (
                SELECT
                    CONVERT(`商品upc`, CHAR CHARACTER SET utf8mb4)
                        COLLATE utf8mb4_unicode_ci AS `商品upc`,
                    `成本价`,
                    `成本底价`
                FROM (
                    SELECT
                        `商品upc`,
                        `成本价`,
                        `成本底价`,
                        ROW_NUMBER() OVER (
                            PARTITION BY `商品upc`
                            ORDER BY `序号` DESC
                        ) AS cost_rank
                    FROM {cost}
                    WHERE `商品upc` IS NOT NULL
                ) ranked_cost
                WHERE cost_rank = 1
            ) cost
                ON activity.`商品upc` = cost.`商品upc`
            LEFT JOIN {freight} freight
                ON activity.`商品upc` =
                    CONVERT(
                        freight.`货品编号`,
                        CHAR CHARACTER SET utf8mb4
                    ) COLLATE utf8mb4_unicode_ci
               AND freight.`订购盒数` >= activity.`活动盒数`
               AND MOD(
                    freight.`订购盒数`,
                    activity.`活动盒数`
               ) = 0
            WHERE activity.`记录日期` = %s
            """,
            (record_date,),
        )

        cursor.execute(
            f"""
            SELECT
                COUNT(*) AS output_rows,
                SUM(`成本价` IS NOT NULL OR `成本底价` IS NOT NULL)
                    AS cost_rows,
                SUM(`订单平均运费` IS NOT NULL) AS freight_rows
            FROM {output}
            WHERE `记录日期` = %s
            """,
            (record_date,),
        )
        output_rows, cost_rows, freight_rows = cursor.fetchone()
        output_rows = int(output_rows)
        if output_rows < source_rows:
            raise RuntimeError(
                "JD subsidy scenarios lost source strategies: "
                f"source={source_rows}, output={output_rows}"
            )

        cursor.execute(
            f"""
            SELECT COUNT(DISTINCT `策略ID`)
            FROM {output}
            WHERE `记录日期` = %s
            """,
            (record_date,),
        )
        output_strategies = int(cursor.fetchone()[0])
        if output_strategies != source_rows:
            raise RuntimeError(
                "JD subsidy strategy count changed during scenario expansion: "
                f"source={source_rows}, output={output_strategies}"
            )

    return {
        "source_rows": source_rows,
        "output_strategies": output_strategies,
        "output_rows": output_rows,
        "cost_rows": int(cost_rows or 0),
        "cost_insufficient_rows": output_rows - int(cost_rows or 0),
        "freight_rows": int(freight_rows or 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--record-date",
        required=True,
        type=dt.date.fromisoformat,
    )
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    settings = table_settings(config)
    connection = pymysql.connect(**config["mysql"], autocommit=False)
    try:
        ensure_output_table(connection, settings["output_table"])
        metrics = refresh_output(connection, args.record_date, settings)
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
                "output_table": settings["output_table"],
                **metrics,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
