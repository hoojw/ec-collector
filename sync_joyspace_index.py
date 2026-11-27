#!/usr/bin/env python3
"""Sync JoySpace index-trade sheets (已有指数商品清单 / 指数补贴品) into MySQL.

Data source: the same JoySpace workbook as 小官补, sheets 2 and 3.
Both sheets are read in full through the embedded WPS OpenAPI and stored as
per-date snapshots (history) plus a current snapshot (overwritten), so the
downstream matcher never depends on "today's partition" being present.

This script only reads the workbook and writes adjust_price tables.  It does
not touch the existing jd_index_trade_collect.py (which is no longer used).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
from zoneinfo import ZoneInfo

import pymysql

from read_joyspace_sheets import RuntimeConnection, find_page

TZ = ZoneInfo("Asia/Shanghai")

SHEETS = {
    "已有指数商品清单": {
        "daily": "jd_index_catalog_daily",
        "current": "jd_index_catalog",
        "columns": [
            "指数SKU名称", "标准品名", "品牌", "通用名", "规格",
            "二级类目", "指数药品ID", "指数SKUID编号",
        ],
    },
    "指数补贴品": {
        "daily": "jd_index_subsidy_daily",
        "current": "jd_index_subsidy",
        "columns": [
            "商品名称", "指数sku", "是否有竞价",
        ],
    },
}


def quote_ident(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def excel_column(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def normalize_row(row: list, width: int, expected: list[str]) -> dict[str, str]:
    values = list(row or [])
    values.extend([""] * (width - len(values)))
    return {
        name: "" if values[i] is None else str(values[i]).strip()
        for i, name in enumerate(expected)
    }


def remap_rows(source_headers: list[str], source_rows: list[list], expected: list[str]):
    indexes = []
    missing = []
    for name in expected:
        found = [i for i, h in enumerate(source_headers) if h == name]
        if not found:
            missing.append(name)
        else:
            indexes.append(found[0])
    if missing:
        raise ValueError(f"missing required headers: {missing}")
    result = []
    for row in source_rows:
        cells = list(row or [])
        cells.extend([""] * (len(source_headers) - len(cells)))
        mapped = [cells[i] for i in indexes]
        if any(v not in (None, "") for v in mapped):
            result.append(mapped)
    return result


def read_sheet(connection: RuntimeConnection, sheet_name: str, expected: list[str], chunk_size: int):
    metadata = connection.evaluate(
        """
        (async function () {
          await WPSOpenApi.ready();
          var workbook = await WPSOpenApi.Application.ActiveWorkbook;
          var worksheets = await workbook.Worksheets;
          var sheet = await worksheets.Item(%s);
          var usedRange = await sheet.UsedRange;
          return {
            rows: Number(await (await usedRange.Rows).Count),
            columns: Number(await (await usedRange.Columns).Count)
          };
        })()
        """
        % json.dumps(sheet_name, ensure_ascii=False)
    )
    row_count = int(metadata["rows"])
    column_count = int(metadata["columns"])
    if row_count < 2 or column_count < 1:
        raise ValueError(f"{sheet_name} returned no data rows")

    last_column = excel_column(column_count)
    rows = []
    for start in range(1, row_count + 1, chunk_size):
        end = min(start + chunk_size - 1, row_count)
        values = connection.evaluate(
            """
            (async function () {
              var workbook = await WPSOpenApi.Application.ActiveWorkbook;
              var worksheets = await workbook.Worksheets;
              var sheet = await worksheets.Item(%s);
              var range = await sheet.Range(%s);
              return await range.Value;
            })()
            """
            % (
                json.dumps(sheet_name, ensure_ascii=False),
                json.dumps(f"A{start}:{last_column}{end}"),
            )
        )
        chunk = values if isinstance(values, list) else []
        rows.extend(chunk)
        logging.info("Read %s rows %s-%s", sheet_name, start, end)

    headers = [str(v or "").strip() for v in (rows[0] if rows else [])]
    mapped = remap_rows(headers, rows[1:], expected)
    return mapped


def body_ddl(columns: list[str], index_columns: list[str], prefix: str) -> str:
    parts = [f"  {quote_ident(c)} VARCHAR(500) NOT NULL DEFAULT ''" for c in columns]
    for i, c in enumerate(n for n in columns if n in index_columns):
        parts.append(f"  KEY `{prefix}_{i}` ({quote_ident(c)})")
    return ",\n".join(parts) + ","


def ensure_tables(connection, cfg: dict) -> None:
    cols = cfg["columns"]
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {quote_ident(cfg['daily'])} (
                `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                `记录日期` DATE NOT NULL,
{body_ddl(cols, ["指数药品ID", "指数SKUID编号", "指数sku"], "idx_d")}
                PRIMARY KEY (`id`),
                KEY `idx_record_date` (`记录日期`)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cursor.execute(
            f"""
            CREATE TABLE IF not EXISTS {quote_ident(cfg['current'])} (
                `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                `同步日期` DATE NOT NULL,
                `更新时间` DATETIME NOT NULL,
{body_ddl(cols, ["指数药品ID", "指数SKUID编号", "指数sku"], "idx_c")}
                PRIMARY KEY (`id`),
                KEY `idx_sync_date` (`同步日期`)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )


def refresh_tables(connection, cfg: dict, record_date: dt.date, rows: list[list[str]]) -> int:
    cols = cfg["columns"]
    placeholders = ",".join(["%s"] * len(cols))
    col_list = ",".join(quote_ident(c) for c in cols)
    values = [(record_date, *row) for row in rows]
    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {quote_ident(cfg['daily'])} WHERE `记录日期`=%s",
            (record_date,),
        )
        if values:
            cursor.executemany(
                f"INSERT INTO {quote_ident(cfg['daily'])} (`记录日期`,{col_list}) VALUES (%s,{placeholders})",
                values,
            )
        cursor.execute(f"DELETE FROM {quote_ident(cfg['current'])}", ())
        if values:
            now = dt.datetime.now()
            cursor.executemany(
                f"INSERT INTO {quote_ident(cfg['current'])} (`同步日期`,`更新时间`,{col_list}) "
                f"VALUES (%s,%s,{placeholders})",
                [(record_date, now, *row[1:]) for row in values],
            )
    return len(values)


def load_mysql_config(path: pathlib.Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))["mysql"]
    # 只保留 pymysql.connect 接受的连接参数（config.json 里混有 dw_database 等非连接字段）
    allowed = {"host", "port", "user", "password", "database", "charset"}
    return {k: v for k, v in config.items() if k in allowed}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9533)
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--record-date", type=dt.date.fromisoformat)
    parser.add_argument("--chunk-size", type=int, default=250)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    record_date = args.record_date or dt.datetime.now(TZ).date()

    page = find_page(args.port)
    runtime = RuntimeConnection(page["webSocketDebuggerUrl"])
    datasets = {}
    try:
        for sheet_name, cfg in SHEETS.items():
            datasets[sheet_name] = read_sheet(runtime, sheet_name, cfg["columns"], args.chunk_size)
    finally:
        runtime.websocket.close()

    connection = pymysql.connect(**load_mysql_config(args.config), autocommit=False)
    summary = {}
    try:
        for sheet_name, cfg in SHEETS.items():
            ensure_tables(connection, cfg)
            summary[sheet_name] = refresh_tables(
                connection, cfg, record_date, datasets[sheet_name]
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(json.dumps({"record_date": record_date.isoformat(), "rows": summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
