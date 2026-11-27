#!/usr/bin/env python3
"""Import a JD POP SKU export and normalize merchant SKU bundle semantics."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import openpyxl
import pymysql


RAW_TABLE = "jd_store_sku_daily"
NORMALIZED_TABLE = "jd_store_sku_normalized_daily"
SOURCE_COLUMNS = [
    "SKUID",
    "商品编码",
    "商品名称",
    "商家SKU",
    "销售属性",
    "货号",
    "一级类目",
    "二级类目",
    "三级类目",
    "末级类目",
    "店内分类",
    "品牌",
    "京东价",
    "商品总库存",
    "商品可用库存",
    "商品状态",
    "创建时间",
    "累计评价数",
    "好评度",
    "商品链接",
    "是否预订",
    "短标题",
]
MERCHANT_SKU_RE = re.compile(r"^([0-9]+)(?:\*([0-9]+))?$")
PACKAGE_RE = re.compile(r"([0-9]+)\s*(盒|瓶)装")
RAW_COLUMN_TYPES = {
    "SKUID": "VARCHAR(50)",
    "商品编码": "VARCHAR(50)",
    "商家SKU": "VARCHAR(100)",
}


def quote_ident(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def normalize_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value).strip()


def decimal_or_none(value: str):
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def integer_or_none(value: str):
    if not value:
        return None
    try:
        return int(Decimal(value))
    except (InvalidOperation, ValueError):
        return None


def read_workbook(path: Path) -> list[dict[str, str]]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        rows = sheet.iter_rows(values_only=True)
        try:
            headers = [normalize_cell(value) for value in next(rows)]
        except StopIteration as error:
            raise ValueError(f"{path} is empty") from error
        if headers != SOURCE_COLUMNS:
            raise ValueError(
                "Unexpected JD SKU headers: "
                + json.dumps(headers, ensure_ascii=False)
            )
        result = []
        for source_row_number, values in enumerate(rows, start=2):
            row = [normalize_cell(value) for value in values]
            row.extend([""] * (len(headers) - len(row)))
            if not any(row):
                continue
            item = dict(zip(headers, row[: len(headers)]))
            item["_source_row_number"] = source_row_number
            result.append(item)
        return result
    finally:
        workbook.close()


def normalize_sku(row: dict[str, str]) -> dict:
    raw_sku = row["商家SKU"]
    package_match = PACKAGE_RE.search(row["销售属性"]) or PACKAGE_RE.search(
        row["商品名称"]
    )
    listed_quantity = int(package_match.group(1)) if package_match else None
    package_unit = package_match.group(2) if package_match else ""

    if not raw_sku or raw_sku == "--":
        return {
            "base_upc": "",
            "bundle_quantity": None,
            "listed_quantity": listed_quantity,
            "package_unit": package_unit,
            "parse_status": "missing_merchant_sku",
            "parse_message": "商家SKU为空",
        }

    merchant_match = MERCHANT_SKU_RE.fullmatch(raw_sku)
    if not merchant_match:
        return {
            "base_upc": "",
            "bundle_quantity": None,
            "listed_quantity": listed_quantity,
            "package_unit": package_unit,
            "parse_status": "invalid_merchant_sku",
            "parse_message": "商家SKU不符合 数字 或 数字*数量 格式",
        }

    base_upc = merchant_match.group(1)
    bundle_quantity = (
        int(merchant_match.group(2)) if merchant_match.group(2) else 1
    )
    if listed_quantity is None:
        status = "package_quantity_inferred_from_sku"
        message = "销售属性未标注盒装或瓶装数量"
    elif listed_quantity != bundle_quantity:
        status = "quantity_mismatch"
        message = (
            f"商家SKU数量={bundle_quantity}，"
            f"销售属性数量={listed_quantity}"
        )
    else:
        status = "ok"
        message = ""

    return {
        "base_upc": base_upc,
        "bundle_quantity": bundle_quantity,
        "listed_quantity": listed_quantity,
        "package_unit": package_unit,
        "parse_status": status,
        "parse_message": message,
    }


def ensure_tables(connection) -> None:
    raw_columns = ",\n".join(
        f"{quote_ident(column)} "
        f"{RAW_COLUMN_TYPES.get(column, 'TEXT')} NOT NULL"
        for column in SOURCE_COLUMNS
    )
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{RAW_TABLE}` (
                `记录日期` DATE NOT NULL,
                `源文件` VARCHAR(255) NOT NULL,
                `源文件SHA256` CHAR(64) NOT NULL,
                `源行号` INT NOT NULL,
                {raw_columns},
                `导入时间` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (`记录日期`, `SKUID`),
                KEY `idx_jd_store_raw_product` (`记录日期`, `商品编码`),
                KEY `idx_jd_store_raw_merchant_sku` (`记录日期`, `商家SKU`)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{NORMALIZED_TABLE}` (
                `记录日期` DATE NOT NULL,
                `SKUID` VARCHAR(50) NOT NULL,
                `商品编码` VARCHAR(50) NOT NULL,
                `商品名称` VARCHAR(500) NOT NULL,
                `商家SKU原值` VARCHAR(100) NOT NULL,
                `基础UPC` VARCHAR(100) NOT NULL,
                `组合数量` INT NULL,
                `标注组合数量` INT NULL,
                `包装单位` VARCHAR(20) NOT NULL,
                `销售属性` VARCHAR(500) NOT NULL,
                `品牌` VARCHAR(255) NOT NULL,
                `京东价` DECIMAL(12,2) NULL,
                `商品总库存` INT NULL,
                `商品可用库存` INT NULL,
                `商品状态` VARCHAR(50) NOT NULL,
                `解析状态` VARCHAR(50) NOT NULL,
                `解析说明` VARCHAR(500) NOT NULL,
                `源文件SHA256` CHAR(64) NOT NULL,
                PRIMARY KEY (`记录日期`, `SKUID`),
                KEY `idx_jd_store_normalized_product`
                    (`记录日期`, `商品编码`),
                KEY `idx_jd_store_normalized_upc`
                    (`记录日期`, `基础UPC`, `组合数量`),
                KEY `idx_jd_store_normalized_status`
                    (`记录日期`, `解析状态`)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )


def refresh_tables(
    connection,
    record_date: dt.date,
    source_path: Path,
    source_hash: str,
    rows: list[dict[str, str]],
) -> dict[str, int]:
    raw_columns = [
        "记录日期",
        "源文件",
        "源文件SHA256",
        "源行号",
        *SOURCE_COLUMNS,
    ]
    raw_sql = (
        f"INSERT INTO `{RAW_TABLE}` "
        f"({', '.join(quote_ident(column) for column in raw_columns)}) "
        f"VALUES ({', '.join(['%s'] * len(raw_columns))})"
    )
    normalized_columns = [
        "记录日期",
        "SKUID",
        "商品编码",
        "商品名称",
        "商家SKU原值",
        "基础UPC",
        "组合数量",
        "标注组合数量",
        "包装单位",
        "销售属性",
        "品牌",
        "京东价",
        "商品总库存",
        "商品可用库存",
        "商品状态",
        "解析状态",
        "解析说明",
        "源文件SHA256",
    ]
    normalized_sql = (
        f"INSERT INTO `{NORMALIZED_TABLE}` "
        f"({', '.join(quote_ident(column) for column in normalized_columns)}) "
        f"VALUES ({', '.join(['%s'] * len(normalized_columns))})"
    )

    raw_values = []
    normalized_values = []
    statuses: dict[str, int] = {}
    seen_sku_ids = set()
    for row in rows:
        sku_id = row["SKUID"]
        if not sku_id:
            raise ValueError(f"Source row {row['_source_row_number']} has no SKUID")
        if sku_id in seen_sku_ids:
            raise ValueError(f"Duplicate SKUID in source file: {sku_id}")
        seen_sku_ids.add(sku_id)
        parsed = normalize_sku(row)
        statuses[parsed["parse_status"]] = (
            statuses.get(parsed["parse_status"], 0) + 1
        )
        raw_values.append(
            (
                record_date,
                source_path.name,
                source_hash,
                row["_source_row_number"],
                *(row[column] for column in SOURCE_COLUMNS),
            )
        )
        normalized_values.append(
            (
                record_date,
                sku_id,
                row["商品编码"],
                row["商品名称"],
                row["商家SKU"],
                parsed["base_upc"],
                parsed["bundle_quantity"],
                parsed["listed_quantity"],
                parsed["package_unit"],
                row["销售属性"],
                row["品牌"],
                decimal_or_none(row["京东价"]),
                integer_or_none(row["商品总库存"]),
                integer_or_none(row["商品可用库存"]),
                row["商品状态"],
                parsed["parse_status"],
                parsed["parse_message"],
                source_hash,
            )
        )

    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM `{NORMALIZED_TABLE}` WHERE `记录日期` = %s",
            (record_date,),
        )
        cursor.execute(
            f"DELETE FROM `{RAW_TABLE}` WHERE `记录日期` = %s",
            (record_date,),
        )
        cursor.executemany(raw_sql, raw_values)
        cursor.executemany(normalized_sql, normalized_values)
    return statuses


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--record-date", required=True, type=dt.date.fromisoformat)
    args = parser.parse_args()

    rows = read_workbook(args.input)
    source_hash = hashlib.sha256(args.input.read_bytes()).hexdigest()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    connection = pymysql.connect(**config["mysql"], autocommit=False)
    try:
        ensure_tables(connection)
        statuses = refresh_tables(
            connection,
            args.record_date,
            args.input,
            source_hash,
            rows,
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
                "source_file": str(args.input),
                "source_sha256": source_hash,
                "rows": len(rows),
                "raw_table": RAW_TABLE,
                "normalized_table": NORMALIZED_TABLE,
                "parse_statuses": statuses,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
