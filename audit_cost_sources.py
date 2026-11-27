#!/usr/bin/env python3
"""Inspect local subsidy cost-source table columns and small samples."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pymysql


TABLES = [
    ("history", "产品低价明细_去重"),
    ("history", "保民成本价"),
    ("history", "保民5月销售量前100_upc"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    connection = pymysql.connect(**config["mysql"])
    output = []
    try:
        with connection.cursor() as cursor:
            for schema, table in TABLES:
                qualified = f"`{schema}`.`{table}`"
                cursor.execute(f"DESCRIBE {qualified}")
                columns = [row[0] for row in cursor.fetchall()]
                cursor.execute(f"SELECT * FROM {qualified} LIMIT 3")
                sample = [
                    dict(zip(columns, row))
                    for row in cursor.fetchall()
                ]
                output.append(
                    {
                        "table": f"{schema}.{table}",
                        "columns": columns,
                        "sample": sample,
                    }
                )
    finally:
        connection.close()
    print(json.dumps(output, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
