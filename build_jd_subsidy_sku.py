#!/usr/bin/env python3
"""Build exact JD subsidy-to-listed-SKU matches without cost calculations."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pymysql


OUTPUT_TABLE = "jd_subsidy_sku"
DEFAULT_CATALOG_TABLE = "jd_subsidy_catalog"
MEASUREMENT_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*"
    r"(μg|ug|mcg|微克|mg|毫克|g|克|ml|毫升|l|升|kcal|iu|单位|%)",
    re.IGNORECASE,
)
PACKAGE_RE = re.compile(
    r"([0-9]+)\s*(片|粒|袋|支|瓶|丸|贴|枚|管|包|颗|张|泡|揿|喷|吸)",
    re.IGNORECASE,
)
PACKAGE_MULTIPLIER_RE = re.compile(
    r"\*\s*([0-9]+)\s*(板|盒|瓶|袋|支|管)",
    re.IGNORECASE,
)
BUNDLE_SUFFIX_RE = re.compile(r"\s*[0-9]+\s*(盒|瓶)装.*$")
BRAND_RE = re.compile(r"^\s*[\[【]([^\]】]+)[\]】]")


def normalize_text(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = text.translate(
        str.maketrans(
            {
                "×": "*",
                "x": "*",
                "：": ":",
                "／": "/",
                "µ": "μ",
                "（": "(",
                "）": ")",
            }
        )
    )
    return re.sub(r"[\s·•_\-]+", "", text)


def normalize_brand(value) -> str:
    text = normalize_text(value)
    text = re.sub(r"\([a-z0-9 .&+-]+\)", "", text)
    return re.sub(r"[\[\]【】()]", "", text)


def brands_match(left, right) -> bool:
    left_key = normalize_brand(left)
    right_key = normalize_brand(right)
    return bool(left_key and right_key and left_key == right_key)


def normalize_name(value) -> str:
    return re.sub(r"[\[\]【】()（）]", "", normalize_text(value))


def extract_brand(title: str) -> str:
    match = BRAND_RE.search(title or "")
    return match.group(1).strip() if match else ""


def base_listing_spec(value: str) -> str:
    return BUNDLE_SUFFIX_RE.sub("", str(value or "")).strip()


def canonical_spec(value: str) -> str:
    text = normalize_text(value)
    replacements = {
        "微克": "μg",
        "ug": "μg",
        "mcg": "μg",
        "毫克": "mg",
        "克": "g",
        "毫升": "ml",
        "升": "l",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"(?:/)?盒$", "", text)
    text = re.sub(r"/(支|瓶|袋|管)\*1\1$", "", text)
    return text


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def measurement_tokens(value: str) -> tuple[str, ...]:
    tokens = []
    for match in MEASUREMENT_RE.finditer(normalize_text(value)):
        amount = Decimal(match.group(1))
        unit = match.group(2).lower()
        if unit in {"μg", "ug", "mcg", "微克"}:
            token = f"mass_mg:{decimal_text(amount / Decimal('1000'))}"
        elif unit in {"mg", "毫克"}:
            token = f"mass_mg:{decimal_text(amount)}"
        elif unit in {"g", "克"}:
            token = f"mass_mg:{decimal_text(amount * Decimal('1000'))}"
        elif unit in {"ml", "毫升"}:
            token = f"volume_ml:{decimal_text(amount)}"
        elif unit in {"l", "升"}:
            token = f"volume_ml:{decimal_text(amount * Decimal('1000'))}"
        elif unit == "%":
            token = f"percent:{decimal_text(amount)}"
        elif unit == "kcal":
            token = f"kcal:{decimal_text(amount)}"
        else:
            token = f"unit:{decimal_text(amount)}"
        tokens.append(token)
    return tuple(sorted(tokens))


def package_signature(value: str) -> tuple[str, int] | None:
    text = normalize_text(value)
    match = PACKAGE_RE.search(text)
    if not match:
        return None
    total = int(match.group(1))
    unit = match.group(2)
    for multiplier in PACKAGE_MULTIPLIER_RE.finditer(text[match.end() :]):
        total *= int(multiplier.group(1))
    return unit, total


def spec_score(activity_spec: str, listing_spec: str) -> int:
    activity_key = canonical_spec(activity_spec)
    listing_key = canonical_spec(listing_spec)
    if activity_key and activity_key == listing_key:
        return 400

    activity_tokens = measurement_tokens(activity_spec)
    listing_tokens = measurement_tokens(listing_spec)
    if not activity_tokens or activity_tokens != listing_tokens:
        return 0

    activity_package = package_signature(activity_spec)
    listing_package = package_signature(listing_spec)
    if activity_package == listing_package:
        return 300
    if activity_package is None and listing_package is None:
        return 280
    if activity_package is None and listing_package and listing_package[1] == 1:
        return 250
    if listing_package is None and activity_package and activity_package[1] == 1:
        return 250
    return 0


def decimal_or_none(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def catalog_table(config: dict) -> str:
    """Resolve the shared activity catalog table name from config."""
    table = (config.get("tables") or {}).get("jd_subsidy_catalog", DEFAULT_CATALOG_TABLE)
    if not table.replace("_", "").isalnum():
        raise ValueError(f"unsafe JD catalog table: {table!r}")
    return table


def load_activity_rows(connection, table: str) -> list[dict]:
    """Read the current activity catalog.

    The catalog is a single overwritten snapshot maintained by the JoySpace
    sync, so it is deliberately *not* filtered by date: a late or skipped sync
    must not empty the JD activity result for the whole day.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                `策略ID`, `品牌名`, `通用名称`, `规格`, `盒数`
            FROM `{table}`
            ORDER BY `策略ID`
            """
        )
        return [
            {
                "strategy_id": str(row[0]),
                "brand": str(row[1]),
                "generic_name": str(row[2]),
                "specification": str(row[3]),
                "box_count": int(row[4]) if row[4] is not None else None,
            }
            for row in cursor.fetchall()
        ]


def resolve_listing_date(connection, record_date: dt.date) -> dt.date:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT MAX(`记录日期`)
            FROM `jd_store_sku_normalized_daily`
            WHERE `记录日期` <= %s
            """,
            (record_date,),
        )
        row = cursor.fetchone()
    if not row or row[0] is None:
        raise RuntimeError(
            "No JD store SKU snapshot exists on or before "
            f"{record_date}"
        )
    return row[0]


def load_listing_rows(connection, listing_date: dt.date) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                `SKUID`, `商品编码`, `商品名称`, `商家SKU原值`,
                `基础UPC`, `组合数量`, `销售属性`, `品牌`,
                `京东价`, `商品状态`, `解析状态`
            FROM `jd_store_sku_normalized_daily`
            WHERE `记录日期` = %s
            ORDER BY `商品编码`, `SKUID`
            """,
            (listing_date,),
        )
        return [
            {
                "sku_id": str(row[0]),
                "product_code": str(row[1]),
                "product_name": str(row[2]),
                "merchant_sku": str(row[3]),
                "base_upc": str(row[4]),
                "bundle_quantity": int(row[5]) if row[5] is not None else None,
                "sales_attribute": str(row[6]),
                "brand": str(row[7]),
                "jd_price": decimal_or_none(row[8]),
                "product_status": str(row[9]),
                "parse_status": str(row[10]),
            }
            for row in cursor.fetchall()
        ]


def family_score(activity: dict, listing: dict) -> int:
    title_brand = extract_brand(listing["product_name"])
    if not (
        brands_match(activity["brand"], listing["brand"])
        or brands_match(activity["brand"], title_brand)
    ):
        return 0
    if normalize_name(activity["generic_name"]) not in normalize_name(
        listing["product_name"]
    ):
        return 0
    listing_spec = base_listing_spec(listing["sales_attribute"])
    return spec_score(activity["specification"], listing_spec)


def match_rows(
    activity_rows: list[dict],
    listing_rows: list[dict],
) -> tuple[list[tuple], Counter]:
    listings_by_product: dict[str, list[dict]] = defaultdict(list)
    for listing in listing_rows:
        listings_by_product[listing["product_code"]].append(listing)

    output = []
    statuses = Counter()
    for activity in activity_rows:
        if activity["box_count"] is None:
            statuses["missing_activity_box_count"] += 1
            continue

        product_scores = {}
        for product_code, product_listings in listings_by_product.items():
            score = max(
                family_score(activity, listing)
                for listing in product_listings
            )
            if score:
                product_scores[product_code] = score
        if not product_scores:
            statuses["product_not_matched"] += 1
            continue

        best_score = max(product_scores.values())
        best_products = {
            product_code
            for product_code, score in product_scores.items()
            if score == best_score
        }
        eligible = [
            listing
            for product_code in best_products
            for listing in listings_by_product[product_code]
            if listing["bundle_quantity"] == activity["box_count"]
            and listing["product_status"] == "上架"
            and listing["parse_status"] == "ok"
            and listing["merchant_sku"]
            and listing["base_upc"]
            and listing["jd_price"] is not None
        ]
        if not eligible:
            statuses["bundle_not_listed"] += 1
            continue

        merchant_skus = {listing["merchant_sku"] for listing in eligible}
        if len(merchant_skus) != 1:
            statuses["ambiguous_merchant_sku"] += 1
            continue
        prices = {listing["jd_price"] for listing in eligible}
        if len(prices) != 1:
            statuses["ambiguous_jd_price"] += 1
            continue
        base_upcs = {listing["base_upc"] for listing in eligible}
        if len(base_upcs) != 1:
            statuses["ambiguous_base_upc"] += 1
            continue

        selected = eligible[0]
        output.append(
            (
                activity["strategy_id"],
                selected["base_upc"],
                selected["merchant_sku"],
                activity["brand"],
                activity["generic_name"],
                activity["specification"],
                activity["box_count"],
                selected["jd_price"],
            )
        )
        statuses["matched"] += 1
    return output, statuses


def ensure_table(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{OUTPUT_TABLE}` (
                `记录日期` DATE NOT NULL,
                `策略ID` VARCHAR(50) NOT NULL,
                `商品upc` VARCHAR(100) NOT NULL,
                `商家SKU` VARCHAR(100) NOT NULL,
                `品牌` VARCHAR(255) NOT NULL,
                `通用名称` VARCHAR(255) NOT NULL,
                `单盒规格` VARCHAR(255) NOT NULL,
                `活动盒数` INT NOT NULL,
                `京东价` DECIMAL(12,2) NOT NULL,
                PRIMARY KEY (`记录日期`, `策略ID`),
                KEY `idx_jd_subsidy_sku_upc` (`记录日期`, `商品upc`),
                KEY `idx_jd_subsidy_sku_merchant` (`记录日期`, `商家SKU`)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )


def refresh_table(
    connection,
    record_date: dt.date,
    rows: list[tuple],
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM `{OUTPUT_TABLE}` WHERE `记录日期` = %s",
            (record_date,),
        )
        if rows:
            cursor.executemany(
                f"""
                INSERT INTO `{OUTPUT_TABLE}` (
                    `记录日期`, `策略ID`, `商品upc`, `商家SKU`, `品牌`,
                    `通用名称`, `单盒规格`, `活动盒数`,
                    `京东价`
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [(record_date, *row) for row in rows],
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--record-date", required=True, type=dt.date.fromisoformat)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    catalog = catalog_table(config)
    connection = pymysql.connect(**config["mysql"], autocommit=False)
    try:
        activity_rows = load_activity_rows(connection, catalog)
        listing_date = resolve_listing_date(connection, args.record_date)
        listing_rows = load_listing_rows(connection, listing_date)
        rows, statuses = match_rows(activity_rows, listing_rows)
        ensure_table(connection)
        refresh_table(connection, args.record_date, rows)
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
                "listing_record_date": listing_date.isoformat(),
                "catalog_table": catalog,
                "activity_rows": len(activity_rows),
                "listing_rows": len(listing_rows),
                "output_rows": len(rows),
                "output_table": OUTPUT_TABLE,
                "statuses": dict(statuses),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
