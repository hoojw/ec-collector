#!/usr/bin/env python3
"""Load Meituan's daily eligible-unsubsidized list into a fixed MySQL table.

This service intentionally does not calculate cost, freight, profit, or price
recommendations.  Those calculations are maintained in DataEase.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl
import pymysql


LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
OUTPUT_TABLE_DEFAULT = "subsidy_unsubsidized_daily"
OUTPUT_COLUMNS = ("记录日期", "商品upc", "商品名称", "门槛价格", "活动类型", "门槛盒数", "补贴承担")
EXTENDED_OUTPUT_COLUMNS = OUTPUT_COLUMNS[:4] + ("售价", "补贴状态") + OUTPUT_COLUMNS[4:]
REQUIRED_COLUMNS = ("商品upc", "商品名称", "活动类型", "未补贴原因", "价格参考")
TARGET_REASON = "价格不符合补贴要求"
ACTIVITY_RULE_TEST_TABLE_DEFAULT = "subsidy_unsubsidized_daily_test"
ACTIVITY_RULE_TEST_COLUMNS = ("记录日期", "商品upc", "商品名称", "活动类型", "门槛价格", "门槛盒数", "补贴承担")
ACTIVITY_RULE_REQUIRED_COLUMNS = REQUIRED_COLUMNS + ("补贴收益",)
ACTIVITY_RULE_TYPES = {"折扣活动": "折扣", "多盒补贴": "多盒"}


def quote_ident(name: str) -> str:
    if not name:
        raise ValueError("identifier cannot be empty")
    return "`" + name.replace("`", "``") + "`"


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def detect_encoding(path: Path) -> str:
    sample = path.read_bytes()[:65536]
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8-sig"


def normalize_cell(value) -> str:
    return "" if value is None else str(value).strip()


def clean_header(values: list[str]) -> list[str]:
    header = [value.strip().replace("\ufeff", "") for value in values]
    if not all(header):
        raise ValueError("Input header contains empty column names")
    if len(set(header)) != len(header):
        raise ValueError("Input header contains duplicate column names")
    return header


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", encoding=detect_encoding(path), newline="") as file:
        reader = csv.reader(file)
        try:
            header = clean_header(next(reader))
        except StopIteration as error:
            raise ValueError(f"{path} is empty") from error

        rows = []
        for row in reader:
            if not row or all(cell == "" for cell in row):
                continue
            rows.append((row + [""] * len(header))[: len(header)])
    return header, rows


def read_xlsx(path: Path) -> tuple[list[str], list[list[str]]]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        sheet.reset_dimensions()
        raw_rows = list(sheet.iter_rows(values_only=True))
        required = {"商品名称", "货号", "价格参考", "活动类型", "未补贴原因"}
        default_header = [
            "商品upc", "商品名称", "货号", "SKUID", "门店名称", "门店id", "活动类型",
            "未补贴原因", "价格参考", "补贴收益", "撤销补贴时间", "补贴状态",
        ]
        header_index = None
        header: list[str] = []
        has_header = False

        for index, raw_row in enumerate(raw_rows[:50]):
            candidate = [normalize_cell(value) for value in raw_row]
            if required.issubset(set(candidate)):
                header_index = index
                header = clean_header(candidate)
                has_header = True
                break

        if header_index is None:
            for index, raw_row in enumerate(raw_rows):
                candidate = [normalize_cell(value) for value in raw_row]
                if len(candidate) >= len(default_header) and any("补贴门槛" in value for value in candidate):
                    header_index = index
                    header = default_header
                    break

        if header_index is None:
            raise ValueError("Cannot find a recognizable Meituan unsubsidized header row")

        rows = []
        for raw_row in raw_rows[header_index + 1 if has_header else header_index :]:
            row = [normalize_cell(value) for value in raw_row]
            if row and any(row):
                rows.append((row + [""] * len(header))[: len(header)])
        return header, rows
    finally:
        workbook.close()


def format_price(value) -> str:
    if value is None or value == "":
        return ""
    return format(float(value), ".2f").rstrip("0").rstrip(".")


def read_campaign_json(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    pages = payload.get("pages") if isinstance(payload, dict) and "pages" in payload else payload
    if isinstance(pages, dict):
        pages = [pages]
    if not isinstance(pages, list):
        raise ValueError("Campaign JSON must be a response object, page list, or object with pages")

    rows = []
    for page in pages:
        records = page.get("data", {}).get("data") if isinstance(page, dict) else None
        if not isinstance(records, list):
            raise ValueError("Campaign JSON page is missing data.data")
        for record in records:
            if not isinstance(record, dict):
                continue
            rejection = (record.get("rej") or [{}])[0]
            rows.append([
                normalize_cell(record.get("upcCode")),
                normalize_cell(record.get("skuName")),
                "折扣活动" if record.get("actType") == 17 else "多盒补贴",
                normalize_cell(rejection.get("rejText") if isinstance(rejection, dict) else ""),
                f"补贴门槛:¥{format_price(record.get('minDiscountPrice'))};我的价格:¥{format_price(record.get('currentPrice'))}",
                normalize_cell(
                    record.get("subsidyBenefit")
                    or record.get("benefitDescription")
                    or record.get("benefitDesc")
                ),
            ])
    return list(ACTIVITY_RULE_REQUIRED_COLUMNS), rows


def read_input_file(path: Path) -> tuple[list[str], list[list[str]]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return read_csv(path)
    if suffix in (".xlsx", ".xlsm"):
        return read_xlsx(path)
    if suffix == ".json":
        return read_campaign_json(path)
    raise ValueError(f"Unsupported input file type: {path.suffix}")


def parse_threshold_price(value: str) -> float | None:
    match = re.search(r"补贴门槛:\s*¥\s*([0-9]+(?:\.[0-9]+)?)", value)
    return float(match.group(1)) if match else None


def parse_activity_rule(activity: str, benefit_description: str) -> tuple[str, int, float] | None:
    """Return normalized activity type, threshold boxes, and per-item merchant burden."""
    if activity == "折扣活动":
        return "折扣", 1, 0.0
    if activity != "多盒补贴":
        return None

    threshold_match = re.search(r"每满\s*(\d+)\s*件", benefit_description)
    burden_match = re.search(r"商家承担\s*([0-9]+(?:\.[0-9]+)?)\s*元\s*/\s*件", benefit_description)
    if not threshold_match or not burden_match:
        return None
    return "多盒", int(threshold_match.group(1)), float(burden_match.group(1))


def select_activity_rule_rows(columns: list[str], rows: list[list[str]]) -> list[tuple[str, str, str, float, int, float]]:
    positions = {name: columns.index(name) for name in ACTIVITY_RULE_REQUIRED_COLUMNS if name in columns}
    missing = [name for name in ACTIVITY_RULE_REQUIRED_COLUMNS if name not in positions]
    if missing:
        raise ValueError("Input is missing required columns: " + ", ".join(missing))

    selected: list[tuple[str, str, str, float, int, float]] = []
    seen: set[tuple[str, str, str, float, int, float]] = set()
    for row in rows:
        activity = row[positions["活动类型"]]
        if activity not in ACTIVITY_RULE_TYPES or row[positions["未补贴原因"]] != TARGET_REASON:
            continue

        upc = row[positions["商品upc"]].strip()
        name = row[positions["商品名称"]].strip()
        threshold_price = parse_threshold_price(row[positions["价格参考"]])
        rule = parse_activity_rule(activity, row[positions["补贴收益"]])
        if not upc or not name or threshold_price is None:
            logging.warning("Skipping incomplete eligible record: upc=%r name=%r", upc, name)
            continue
        if rule is None:
            logging.warning("Skipping multi-box record with unparseable benefit description: upc=%r", upc)
            continue

        normalized_activity, threshold_boxes, merchant_burden = rule
        item = (upc, name, normalized_activity, threshold_price, threshold_boxes, merchant_burden)
        if item not in seen:
            seen.add(item)
            selected.append(item)
    return selected


def ensure_output_table(conn, table: str) -> None:
    table_q = quote_ident(table)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {table_q} ("
            "`记录日期` DATE NOT NULL, "
            "`商品upc` VARCHAR(50) NOT NULL, "
            "`商品名称` VARCHAR(500) NOT NULL, "
            "`门槛价格` DECIMAL(10,2) NOT NULL, "
            "`售价` DECIMAL(10,2) NULL, "
            "`补贴状态` TINYINT UNSIGNED NOT NULL DEFAULT 0, "
            "`活动类型` VARCHAR(10) NOT NULL, "
            "`门槛盒数` INT UNSIGNED NOT NULL, "
            "`补贴承担` DECIMAL(10,2) NOT NULL, "
            "PRIMARY KEY (`记录日期`, `商品upc`, `商品名称`, `门槛价格`, `活动类型`)"
            ") DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s ORDER BY ordinal_position",
            (table,),
        )
        actual_columns = tuple(row[0] for row in cur.fetchall())
        old_columns = ("记录日期", "商品upc", "商品名称", "门槛价格")
        if actual_columns == old_columns:
            cur.execute(
                f"ALTER TABLE {table_q} "
                "ADD COLUMN `活动类型` VARCHAR(10) NOT NULL DEFAULT '折扣' AFTER `门槛价格`, "
                "ADD COLUMN `门槛盒数` INT UNSIGNED NOT NULL DEFAULT 1 AFTER `活动类型`, "
                "ADD COLUMN `补贴承担` DECIMAL(10,2) NOT NULL DEFAULT 0.00 AFTER `门槛盒数`, "
                "DROP PRIMARY KEY, "
                "ADD PRIMARY KEY (`记录日期`, `商品upc`, `商品名称`, `门槛价格`, `活动类型`)"
            )
        if "售价" not in actual_columns:
            cur.execute(f"ALTER TABLE {table_q} ADD COLUMN `售价` DECIMAL(10,2) NULL AFTER `门槛价格`")
        if "补贴状态" not in actual_columns:
            cur.execute(
                f"ALTER TABLE {table_q} ADD COLUMN `补贴状态` TINYINT UNSIGNED NOT NULL DEFAULT 0 AFTER `售价`"
            )
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s ORDER BY ordinal_position",
            (table,),
        )
        actual_columns = tuple(row[0] for row in cur.fetchall())
    if not set(EXTENDED_OUTPUT_COLUMNS).issubset(actual_columns):
        raise ValueError(f"{table} is missing required columns {EXTENDED_OUTPUT_COLUMNS}; found {actual_columns}")


def refresh_output_table(
    conn,
    table: str,
    record_date: date,
    rows: list[tuple[str, str, str, float, int, float]],
) -> None:
    table_q = quote_ident(table)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {table_q} WHERE `记录日期` = %s", (record_date,))
        if rows:
            cur.executemany(
                f"INSERT INTO {table_q} ("
                "`记录日期`, `商品upc`, `商品名称`, `活动类型`, `门槛价格`, `门槛盒数`, `补贴承担`"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [(record_date, *row) for row in rows],
            )


def ensure_activity_rule_test_table(conn, table: str) -> None:
    table_q = quote_ident(table)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {table_q} ("
            "`记录日期` DATE NOT NULL, "
            "`商品upc` VARCHAR(50) NOT NULL, "
            "`商品名称` VARCHAR(500) NOT NULL, "
            "`活动类型` VARCHAR(10) NOT NULL, "
            "`门槛价格` DECIMAL(10,2) NOT NULL, "
            "`门槛盒数` INT UNSIGNED NOT NULL, "
            "`补贴承担` DECIMAL(10,2) NOT NULL, "
            "PRIMARY KEY (`记录日期`, `商品upc`, `商品名称`, `活动类型`, `门槛价格`)"
            ") DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s ORDER BY ordinal_position",
            (table,),
        )
        actual_columns = tuple(row[0] for row in cur.fetchall())
    if actual_columns != ACTIVITY_RULE_TEST_COLUMNS:
        raise ValueError(f"{table} must have exactly {ACTIVITY_RULE_TEST_COLUMNS}, found {actual_columns}")


def refresh_activity_rule_test_table(
    conn,
    table: str,
    record_date: date,
    rows: list[tuple[str, str, str, float, int, float]],
) -> None:
    table_q = quote_ident(table)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {table_q} WHERE `记录日期` = %s", (record_date,))
        if rows:
            cur.executemany(
                f"INSERT INTO {table_q} ("
                "`记录日期`, `商品upc`, `商品名称`, `活动类型`, `门槛价格`, `门槛盒数`, `补贴承担`"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [(record_date, *row) for row in rows],
            )


def run_pipeline(config_path: Path, input_path: Path, record_date: date) -> None:
    config = load_config(config_path)
    mysql_config = config["mysql"]
    output_table = config.get("tables", {}).get("unsubsidized_daily", OUTPUT_TABLE_DEFAULT)
    conn = pymysql.connect(
        host=mysql_config["host"],
        port=int(mysql_config["port"]),
        user=mysql_config["user"],
        password=mysql_config["password"],
        database=mysql_config["database"],
        charset=mysql_config.get("charset", "utf8mb4"),
        autocommit=False,
    )
    try:
        columns, rows = read_input_file(input_path)
        selected = select_activity_rule_rows(columns, rows)
        ensure_output_table(conn, output_table)
        refresh_output_table(conn, output_table, record_date, selected)
        conn.commit()
        logging.info("Refreshed %s for %s with %s rows", output_table, record_date, len(selected))
    except Exception:
        conn.rollback()
        logging.exception("Daily unsubsidized refresh failed; rolled back")
        raise
    finally:
        conn.close()


def run_activity_rule_test_pipeline(config_path: Path, input_path: Path, record_date: date) -> None:
    config = load_config(config_path)
    mysql_config = config["mysql"]
    output_table = config.get("tables", {}).get("unsubsidized_daily_test", ACTIVITY_RULE_TEST_TABLE_DEFAULT)
    conn = pymysql.connect(
        host=mysql_config["host"],
        port=int(mysql_config["port"]),
        user=mysql_config["user"],
        password=mysql_config["password"],
        database=mysql_config["database"],
        charset=mysql_config.get("charset", "utf8mb4"),
        autocommit=False,
    )
    try:
        columns, rows = read_input_file(input_path)
        selected = select_activity_rule_rows(columns, rows)
        ensure_activity_rule_test_table(conn, output_table)
        refresh_activity_rule_test_table(conn, output_table, record_date, selected)
        conn.commit()
        logging.info("Refreshed %s for %s with %s rows", output_table, record_date, len(selected))
    except Exception:
        conn.rollback()
        logging.exception("Activity-rule test refresh failed; rolled back")
        raise
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh the DataEase daily unsubsidized source table.")
    parser.add_argument("--config", required=True, type=Path, help="Path to JSON config file.")
    parser.add_argument("--input", required=True, type=Path, help="Daily Meituan CSV, XLSX, or JSON file.")
    parser.add_argument("--record-date", type=date.fromisoformat, help="Record date in YYYY-MM-DD; defaults to today in Asia/Shanghai.")
    parser.add_argument(
        "--activity-rules-test",
        action="store_true",
        help="Build the test table that includes discount and multi-box activity rules.",
    )
    parser.add_argument("--log-file", type=Path, help="Optional log file path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    handlers = [logging.StreamHandler()]
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=handlers)
    record_date = args.record_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if args.activity_rules_test:
        run_activity_rule_test_pipeline(args.config, args.input, record_date)
    else:
        run_pipeline(args.config, args.input, record_date)


if __name__ == "__main__":
    main()
