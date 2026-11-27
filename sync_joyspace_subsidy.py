#!/usr/bin/env python3
"""Sync the JoySpace JD subsidy catalog into local MySQL."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import pathlib
import tempfile
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import pymysql

from read_joyspace_sheets import RuntimeConnection, find_page


SHEET_NAME = "小官补补贴清单"
EXPECTED_HEADERS = [
    "大盘排序",
    "策略ID",
    "药品ID",
    "品牌名",
    "通用名称",
    "规格",
    "生产厂家",
    "批准文号",
    "二级类目",
    "三级类目",
    "类目组",
    "采销",
    "是否支持百补",
    "盒数",
    "前几个低价补贴",
    "价格下限",
    "价格上限",
    "实际入围最高价",
    "实际入围最低价",
    "券信息",
    "券到期时间",
]
HEADER_ALIASES = {
    "大盘排序": ("大盘排序", "大盘顺序"),
    "前几个低价补贴": ("前几个低价补贴", "推荐数量"),
    # 2026-11-17 版本起，京东在表头给这两列加了口径后缀（数据本身未变，
    # 仍是入围后的到手价）。表头改名曾让整轮同步在读取 23178 行后中止。
    "实际入围最高价": (
        "实际入围最高价",
        "实际入围最高价（算上小官补后到手价）",
    ),
    "实际入围最低价": (
        "实际入围最低价",
        "实际入围最低价（算上小官补后到手价）",
    ),
}
# Daily history: one complete partition per record date, kept for audit.
TABLE_NAME = "jd_subsidy_catalog_daily"
# Current activity catalog: a single overwritten snapshot.  Downstream metric
# jobs read this table instead of "today's partition", so a late or missing
# JoySpace sync no longer blocks the JD activity columns for the whole day.
CURRENT_TABLE_NAME = "jd_subsidy_catalog"
TZ = ZoneInfo("Asia/Shanghai")


def quote_ident(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def excel_column(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def normalize_row(row: list, width: int) -> list[str]:
    values = list(row or [])
    values.extend([""] * (width - len(values)))
    return ["" if value is None else str(value).strip() for value in values[:width]]


def remap_sheet_rows(
    source_headers: list[str],
    source_rows: list[list[str]],
) -> tuple[list[str], list[list[str]]]:
    source_indexes: list[int] = []
    missing_headers: list[str] = []
    ambiguous_headers: list[str] = []
    for expected in EXPECTED_HEADERS:
        aliases = HEADER_ALIASES.get(expected, (expected,))
        matches = [
            index
            for index, source_header in enumerate(source_headers)
            if source_header in aliases
        ]
        if not matches:
            missing_headers.append(expected)
        elif len(matches) > 1:
            ambiguous_headers.append(expected)
        else:
            source_indexes.append(matches[0])
    if missing_headers or ambiguous_headers:
        details = []
        if missing_headers:
            details.append(f"missing required headers: {missing_headers}")
        if ambiguous_headers:
            details.append(f"ambiguous required headers: {ambiguous_headers}")
        raise ValueError("; ".join(details))

    mapped_rows = []
    for source_row in source_rows:
        normalized = normalize_row(source_row, len(source_headers))
        mapped = [normalized[index] for index in source_indexes]
        if any(mapped):
            mapped_rows.append(mapped)
    return list(EXPECTED_HEADERS), mapped_rows


def read_sheet(connection: RuntimeConnection, chunk_size: int) -> tuple[list[str], list[list[str]]]:
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
        % json.dumps(SHEET_NAME, ensure_ascii=False)
    )
    row_count = int(metadata["rows"])
    column_count = int(metadata["columns"])

    rows: list[list[str]] = []
    last_column = excel_column(column_count)
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
                json.dumps(SHEET_NAME, ensure_ascii=False),
                json.dumps(f"A{start}:{last_column}{end}"),
            )
        )
        chunk = values if isinstance(values, list) else []
        rows.extend(normalize_row(row, column_count) for row in chunk)
        logging.info("Read JoySpace rows %s-%s", start, end)

    if not rows:
        raise ValueError(f"{SHEET_NAME} returned no rows")
    return remap_sheet_rows(rows[0], rows[1:])


def write_snapshot(
    output_dir: pathlib.Path,
    record_date: dt.date,
    headers: list[str],
    rows: list[list[str]],
) -> pathlib.Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "jd_subsidy_catalog_latest.json"
    payload = {
        "record_date": record_date.isoformat(),
        "sheet_name": SHEET_NAME,
        "headers": headers,
        "rows": rows,
    }
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=output_dir,
        prefix=output_path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as file:
        json.dump(payload, file, ensure_ascii=False)
        file.write("\n")
        temporary_path = pathlib.Path(file.name)
    temporary_path.replace(output_path)
    return output_path


def decimal_or_none(value: str):
    if not value or value == "无入选":
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def int_or_none(value: str):
    try:
        return int(Decimal(value))
    except (InvalidOperation, ValueError):
        return None


_CATALOG_BODY = """
    `大盘排序` INT NULL,
    `策略ID` VARCHAR(50) NOT NULL,
    `药品ID` VARCHAR(50) NOT NULL,
    `品牌名` VARCHAR(255) NOT NULL,
    `通用名称` VARCHAR(255) NOT NULL,
    `规格` VARCHAR(255) NOT NULL,
    `生产厂家` VARCHAR(500) NOT NULL,
    `批准文号` VARCHAR(100) NOT NULL,
    `二级类目` VARCHAR(100) NOT NULL,
    `三级类目` VARCHAR(100) NOT NULL,
    `类目组` VARCHAR(100) NOT NULL,
    `采销` VARCHAR(100) NOT NULL,
    `是否支持百补` VARCHAR(20) NOT NULL,
    `盒数` INT NULL,
    `前几个低价补贴` INT NULL,
    `价格下限` DECIMAL(12,2) NULL,
    `价格上限` DECIMAL(12,2) NULL,
    `实际入围最高价` VARCHAR(50) NOT NULL,
    `实际入围最低价` VARCHAR(50) NOT NULL,
    `券信息` VARCHAR(500) NOT NULL,
    `券到期时间` VARCHAR(50) NOT NULL,
"""


def ensure_table(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {quote_ident(TABLE_NAME)} (
                `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                `记录日期` DATE NOT NULL,{_CATALOG_BODY}
                PRIMARY KEY (`id`),
                KEY `idx_jd_subsidy_record_date` (`记录日期`),
                KEY `idx_jd_subsidy_drug` (`药品ID`),
                KEY `idx_jd_subsidy_match` (`品牌名`, `通用名称`, `规格`)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        # The current catalog keeps the date as provenance only (同步日期 =
        # the business day of the last successful sync).  Downstream jobs must
        # never filter it by date, which is why these columns are named
        # differently from the daily history table.
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {quote_ident(CURRENT_TABLE_NAME)} (
                `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                `同步日期` DATE NOT NULL,
                `更新时间` DATETIME NOT NULL,{_CATALOG_BODY}
                PRIMARY KEY (`id`),
                KEY `idx_jd_subsidy_catalog_sync_date` (`同步日期`),
                KEY `idx_jd_subsidy_catalog_drug` (`药品ID`),
                KEY `idx_jd_subsidy_catalog_match` (`品牌名`, `通用名称`, `规格`)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )


def _business_values(rows: list[list[str]]) -> list[tuple]:
    return [
        (
            int_or_none(row[0]),
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
            row[11],
            row[12],
            int_or_none(row[13]),
            int_or_none(row[14]),
            decimal_or_none(row[15]),
            decimal_or_none(row[16]),
            row[17],
            row[18],
            row[19],
            row[20],
        )
        for row in rows
    ]


def _replace_rows(connection, table: str, columns: list[str], values: list[tuple], delete_sql: str, delete_params: tuple) -> None:
    column_sql = ", ".join(quote_ident(column) for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    insert_sql = f"INSERT INTO {quote_ident(table)} ({column_sql}) VALUES ({placeholders})"
    with connection.cursor() as cursor:
        cursor.execute(delete_sql, delete_params)
        cursor.executemany(insert_sql, values)


def refresh_table(connection, record_date: dt.date, rows: list[list[str]]) -> None:
    """Write the daily history partition and overwrite the current catalog."""
    business = _business_values(rows)

    daily_columns = ["记录日期", *EXPECTED_HEADERS]
    _replace_rows(
        connection,
        TABLE_NAME,
        daily_columns,
        [(record_date, *values) for values in business],
        f"DELETE FROM {quote_ident(TABLE_NAME)} WHERE `记录日期` = %s",
        (record_date,),
    )

    current_columns = ["同步日期", "更新时间", *EXPECTED_HEADERS]
    updated_at = dt.datetime.now(TZ).replace(tzinfo=None)
    _replace_rows(
        connection,
        CURRENT_TABLE_NAME,
        current_columns,
        [(record_date, updated_at, *values) for values in business],
        f"DELETE FROM {quote_ident(CURRENT_TABLE_NAME)}",
        (),
    )


def load_mysql_config(path: pathlib.Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    allowed = {"host","port","user","password","database","charset","connect_timeout"}
    return {k:v for k,v in config["mysql"].items() if k in allowed}


def validate_catalog_completeness(
    connection, record_date: dt.date, rows: list[list[str]]
) -> int:
    """Reject a half-rendered worksheet before it can replace a good day."""
    box_index = EXPECTED_HEADERS.index("盒数")
    populated_box_count = sum(
        int_or_none(row[box_index]) is not None for row in rows
    )
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT COUNT(*)
            FROM {quote_ident(TABLE_NAME)}
            WHERE `记录日期`=(
              SELECT MAX(`记录日期`) FROM {quote_ident(TABLE_NAME)}
              WHERE `记录日期` < %s
            )
              AND `盒数` IS NOT NULL
            """,
            (record_date,),
        )
        previous_populated_box_count = int(cursor.fetchone()[0])
    minimum = max(1, math.ceil(previous_populated_box_count * 0.5))
    if populated_box_count < minimum:
        raise RuntimeError(
            "JoySpace subsidy sheet is incomplete: "
            f"populated 盒数 rows {populated_box_count}, expected at least {minimum} "
            f"from previous snapshot {previous_populated_box_count}"
        )
    return populated_box_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9533)
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        default=pathlib.Path("/opt/subsidy/joyspace/inbox"),
    )
    parser.add_argument("--chunk-size", type=int, default=250)
    parser.add_argument("--record-date", type=dt.date.fromisoformat)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    record_date = args.record_date or dt.datetime.now(
        ZoneInfo("Asia/Shanghai")
    ).date()
    page = find_page(args.port)
    runtime = RuntimeConnection(page["webSocketDebuggerUrl"])
    headers, rows = read_sheet(runtime, args.chunk_size)
    if json.loads(args.config.read_text(encoding="utf-8"))["mysql"].get("dw_database")=="ec_dw":
        from joyspace_direct import publish
        print(json.dumps(publish(headers, rows, record_date), ensure_ascii=False, default=str))
        return


    connection = pymysql.connect(
        **load_mysql_config(args.config),
        autocommit=False,
    )
    try:
        ensure_table(connection)
        populated_box_count = validate_catalog_completeness(
            connection, record_date, rows
        )
        snapshot = write_snapshot(args.output_dir, record_date, headers, rows)
        refresh_table(connection, record_date, rows)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(
        json.dumps(
            {
                "record_date": record_date.isoformat(),
                "sheet": SHEET_NAME,
                "rows": len(rows),
                "populated_box_count": populated_box_count,
                "snapshot": str(snapshot),
                "table": TABLE_NAME,
                "current_table": CURRENT_TABLE_NAME,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
