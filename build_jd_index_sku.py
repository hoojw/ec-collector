#!/usr/bin/env python3
"""Match JoySpace index-trade catalog against this store's JD listings.

Produces a per-date "eligible products" list: index SKUs from the two JoySpace
sheets (已有指数商品清单 / 指数补贴品) that map to currently-listed products in
this store, using the same brand + generic-name + specification matching as
build_jd_subsidy_sku.py (but without box-count, which the index sheets lack).

The source of truth is the JoySpace workbook synced by sync_joyspace_index.py;
the old jd_index_trade_collect.py (CDP scrape of the store's own bid page) is
no longer used.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
from collections import Counter
from zoneinfo import ZoneInfo

import pymysql

from build_jd_subsidy_sku import (
    base_listing_spec,
    brands_match,
    decimal_or_none,
    extract_brand,
    normalize_name,
    spec_score,
)

TZ = ZoneInfo("Asia/Shanghai")
OUTPUT_TABLE = "jd_index_sku"


def load_index_catalog(connection, record_date: dt.date) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT `指数SKU名称`,`标准品名`,`品牌`,`通用名`,`规格`,`二级类目`,`指数药品ID`,`指数SKUID编号`
            FROM `adjust_price`.`jd_index_catalog`
            WHERE `同步日期`=%s
            """,
            (record_date,),
        )
        return [
            {
                "name": str(r[0] or ""),
                "standard_name": str(r[1] or ""),
                "brand": str(r[2] or ""),
                "generic_name": str(r[3] or ""),
                "specification": str(r[4] or ""),
                "category": str(r[5] or ""),
                "drug_id": str(r[6] or ""),
                "index_sku": str(r[7] or ""),
            }
            for r in cursor.fetchall()
        ]


def load_subsidy_flags(connection, record_date: dt.date) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT `指数sku`,`是否有竞价`
            FROM `adjust_price`.`jd_index_subsidy`
            WHERE `同步日期`=%s
            """,
            (record_date,),
        )
        return {str(r[0] or ""): str(r[1] or "") for r in cursor.fetchall()}


def resolve_listing_date(connection, record_date: dt.date) -> dt.date:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT MAX(`记录日期`) FROM `jd_store_sku_normalized_daily` WHERE `记录日期`<=%s",
            (record_date,),
        )
        row = cursor.fetchone()
    if not row or row[0] is None:
        raise RuntimeError(f"no listing snapshot on or before {record_date}")
    return row[0]


def load_listings(connection, listing_date: dt.date) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT `SKUID`,`商品编码`,`商品名称`,`商家SKU原值`,`基础UPC`,
                   `销售属性`,`品牌`,`京东价`,`商品状态`,`解析状态`
            FROM `jd_store_sku_normalized_daily`
            WHERE `记录日期`=%s
            ORDER BY `商品编码`,`SKUID`
            """,
            (listing_date,),
        )
        return [
            {
                "sku_id": str(r[0]),
                "product_code": str(r[1]),
                "product_name": str(r[2]),
                "merchant_sku": str(r[3]),
                "base_upc": str(r[4]),
                "sales_attribute": str(r[5]),
                "brand": str(r[6]),
                "jd_price": decimal_or_none(r[7]),
                "product_status": str(r[8]),
                "parse_status": str(r[9]),
            }
            for r in cursor.fetchall()
        ]


def family_score(index_item: dict, listing: dict) -> int:
    title_brand = extract_brand(listing["product_name"])
    if not (
        brands_match(index_item["brand"], listing["brand"])
        or brands_match(index_item["brand"], title_brand)
    ):
        return 0
    if normalize_name(index_item["generic_name"]) not in normalize_name(
        listing["product_name"]
    ):
        return 0
    return spec_score(index_item["specification"], base_listing_spec(listing["sales_attribute"]))


def match_rows(index_rows: list[dict], listing_rows: list[dict], flags: dict[str, str]):
    listings_by_product: dict[str, list[dict]] = {}
    for listing in listing_rows:
        listings_by_product.setdefault(listing["product_code"], []).append(listing)

    output = []
    statuses = Counter()
    for item in index_rows:
        product_scores = {}
        for product_code, product_listings in listings_by_product.items():
            score = max(family_score(item, listing) for listing in product_listings)
            if score:
                product_scores[product_code] = score
        if not product_scores:
            statuses["product_not_matched"] += 1
            continue

        best_score = max(product_scores.values())
        best_products = {
            code for code, score in product_scores.items() if score == best_score
        }
        eligible = [
            listing
            for code in best_products
            for listing in listings_by_product[code]
            if listing["product_status"] == "上架"
            and listing["parse_status"] == "ok"
            and listing["merchant_sku"]
            and listing["base_upc"]
            and listing["jd_price"] is not None
        ]
        if not eligible:
            statuses["not_listed"] += 1
            continue

        merchant_skus = {l["merchant_sku"] for l in eligible}
        if len(merchant_skus) != 1:
            statuses["ambiguous_merchant_sku"] += 1
            continue
        prices = {l["jd_price"] for l in eligible}
        if len(prices) != 1:
            statuses["ambiguous_jd_price"] += 1
            continue
        base_upcs = {l["base_upc"] for l in eligible}
        if len(base_upcs) != 1:
            statuses["ambiguous_base_upc"] += 1
            continue

        selected = eligible[0]
        output.append(
            (
                item["index_sku"],
                item["drug_id"],
                item["brand"],
                item["generic_name"],
                item["specification"],
                selected["base_upc"],
                selected["merchant_sku"],
                selected["jd_price"],
                flags.get(item["index_sku"], ""),
                item["name"],
            )
        )
        statuses["matched"] += 1
    return output, statuses


def ensure_table(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `adjust_price`.`{OUTPUT_TABLE}` (
                `记录日期` DATE NOT NULL,
                `指数SKUID编号` VARCHAR(100) NOT NULL,
                `指数药品ID` VARCHAR(100) NOT NULL,
                `品牌` VARCHAR(255) NOT NULL,
                `通用名` VARCHAR(255) NOT NULL,
                `规格` VARCHAR(255) NOT NULL,
                `商品upc` VARCHAR(100) NOT NULL,
                `商家SKU` VARCHAR(100) NOT NULL,
                `京东价` DECIMAL(12,2) NOT NULL,
                `是否有竞价` VARCHAR(50) NOT NULL DEFAULT '',
                `指数SKU名称` VARCHAR(500) NOT NULL DEFAULT '',
                PRIMARY KEY (`记录日期`, `指数SKUID编号`, `商家SKU`),
                KEY `idx_index_sku_upc` (`记录日期`, `商品upc`)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )


def load_mysql_config(path: pathlib.Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))["mysql"]
    allowed = {"host", "port", "user", "password", "database", "charset"}
    return {k: v for k, v in config.items() if k in allowed}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=pathlib.Path,
                        help="central config (adjust_price)")
    parser.add_argument("--record-date", type=dt.date.fromisoformat)
    parser.add_argument("--store", required=True, help="store key (for logging only)")
    args = parser.parse_args()

    record_date = args.record_date or dt.datetime.now(TZ).date()
    central = pymysql.connect(**load_mysql_config(args.config), autocommit=False)
    try:
        listing_date = resolve_listing_date(central, record_date)
        index_rows = load_index_catalog(central, record_date)
        flags = load_subsidy_flags(central, record_date)
        listings = load_listings(central, listing_date)

        rows, statuses = match_rows(index_rows, listings, flags)
        ensure_table(central)
        with central.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM `adjust_price`.`{OUTPUT_TABLE}` WHERE `记录日期`=%s",
                (record_date,),
            )
            cursor.executemany(
                f"INSERT INTO `adjust_price`.`{OUTPUT_TABLE}` "
                "(`记录日期`,`指数SKUID编号`,`指数药品ID`,`品牌`,`通用名`,`规格`,"
                "`商品upc`,`商家SKU`,`京东价`,`是否有竞价`,`指数SKU名称`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [(record_date, *row) for row in rows],
            )
        central.commit()
        print(
            json.dumps(
                {
                    "record_date": record_date.isoformat(),
                    "store": args.store,
                    "listing_date": listing_date.isoformat(),
                    "index_rows": len(index_rows),
                    "listings": len(listings),
                    "matched": statuses.get("matched", 0),
                    "statuses": dict(statuses),
                },
                ensure_ascii=False,
            )
        )
    except Exception:
        central.rollback()
        raise
    finally:
        central.close()


if __name__ == "__main__":
    main()
