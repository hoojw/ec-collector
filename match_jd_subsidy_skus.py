#!/usr/bin/env python3
"""Match JD subsidy drugs to store SKUs and local costs conservatively."""

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


MATCH_TABLE = "jd_subsidy_sku_cost_daily"
DEFAULT_CATALOG_TABLE = "jd_subsidy_catalog"
BRAND_RE = re.compile(r"^\s*[\[【]([^\]】]+)[\]】]")
NUMBER_UNIT_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*(μg|ug|mcg|微克|mg|毫克|g|克|ml|毫升|l|升|%|iu|单位)",
    re.IGNORECASE,
)
PACKAGE_UNIT_RE = re.compile(
    r"([0-9]+)\s*(片|粒|袋|支|瓶|丸|贴|枚|管|包|颗|张)",
    re.IGNORECASE,
)
PACKAGE_MULTIPLIER_RE = re.compile(
    r"\*\s*([0-9]+)\s*(板|盒|瓶|袋|支|管)",
    re.IGNORECASE,
)


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
    return re.sub(r"[\[\]【】()（）]", "", normalize_text(value))


def normalize_name(value) -> str:
    return normalize_text(value)


def normalize_spec(value) -> str:
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
    return text


def extract_brand(title: str) -> str:
    match = BRAND_RE.search(title or "")
    return match.group(1).strip() if match else ""


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def strength_tokens(value: str) -> set[str]:
    tokens = set()
    for match in NUMBER_UNIT_RE.finditer(normalize_text(value)):
        amount = Decimal(match.group(1))
        unit = match.group(2).lower()
        if unit in {"μg", "ug", "mcg", "微克"}:
            tokens.add(f"mass_mg:{decimal_text(amount / Decimal('1000'))}")
        elif unit in {"mg", "毫克"}:
            tokens.add(f"mass_mg:{decimal_text(amount)}")
        elif unit in {"g", "克"}:
            tokens.add(f"mass_mg:{decimal_text(amount * Decimal('1000'))}")
        elif unit in {"ml", "毫升"}:
            tokens.add(f"volume_ml:{decimal_text(amount)}")
        elif unit in {"l", "升"}:
            tokens.add(f"volume_ml:{decimal_text(amount * Decimal('1000'))}")
        elif unit == "%":
            tokens.add(f"percent:{decimal_text(amount)}")
        else:
            tokens.add(f"unit:{decimal_text(amount)}")
    return tokens


def package_signature(value: str) -> tuple[str, int] | None:
    text = normalize_text(value)
    match = PACKAGE_UNIT_RE.search(text)
    if not match:
        return None
    total = int(match.group(1))
    unit = match.group(2)
    for multiplier in PACKAGE_MULTIPLIER_RE.finditer(text[match.end() :]):
        total *= int(multiplier.group(1))
    return unit, total


def load_catalogs(paths: list[Path]) -> list[dict]:
    entries = []
    seen = set()
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for entry in payload.get("entries", []):
            key = (
                str(entry.get("p") or ""),
                str(entry.get("s") or ""),
                str(entry.get("u") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            title = str(entry.get("t") or "")
            entries.append(
                {
                    "store_id": key[0],
                    "merchant_sku": key[1],
                    "upc": key[2],
                    "title": title,
                    "brand": extract_brand(title),
                    "generic_name": str(entry.get("n") or ""),
                    "specification": str(entry.get("c") or ""),
                }
            )
    return entries


def load_jd_drugs(connection, table: str) -> list[dict]:
    """Read the current activity catalog.

    The catalog is a single overwritten snapshot, so no date filter is applied.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT `药品ID`, `品牌名`, `通用名称`, `规格`
            FROM `{table}`
            GROUP BY `药品ID`, `品牌名`, `通用名称`, `规格`
            ORDER BY `药品ID`
            """
        )
        return [
            {
                "drug_id": str(row[0]),
                "brand": str(row[1]),
                "generic_name": str(row[2]),
                "specification": str(row[3]),
            }
            for row in cursor.fetchall()
        ]


def decimal_or_none(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def load_costs(connection) -> dict[str, tuple[Decimal, str]]:
    costs: dict[str, tuple[Decimal, str]] = {}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT `商品upc`, `成本价`, `成本底价` "
            "FROM `history`.`产品低价明细_去重`"
        )
        for upc, cost, floor in cursor.fetchall():
            key = str(upc or "").strip()
            if not key:
                continue
            selected = decimal_or_none(floor) or decimal_or_none(cost)
            if selected is not None:
                source = "产品低价明细_去重.成本底价" if decimal_or_none(floor) else "产品低价明细_去重.成本价"
                costs[key] = (selected, source)

        cursor.execute(
            """
            SELECT `货品编码`, `商家编码（新）`, `商家编码（旧）`, `主条码`, `成本价`
            FROM `history`.`保民成本价`
            """
        )
        for product_code, new_code, old_code, barcode, cost in cursor.fetchall():
            selected = decimal_or_none(cost)
            if selected is None:
                continue
            for value in (product_code, new_code, old_code, barcode):
                key = str(value or "").strip()
                if key and key not in costs:
                    costs[key] = (selected, "保民成本价.成本价")
    return costs


def match_one(drug: dict, store_entries: list[dict]) -> dict:
    brand_key = normalize_brand(drug["brand"])
    name_key = normalize_name(drug["generic_name"])
    spec_key = normalize_spec(drug["specification"])
    jd_tokens = strength_tokens(drug["specification"])
    jd_package = package_signature(drug["specification"])

    name_matches = [
        entry
        for entry in store_entries
        if normalize_name(entry["generic_name"]) == name_key
    ]
    brand_matches = [
        entry
        for entry in name_matches
        if normalize_brand(entry["brand"]) == brand_key
    ]
    exact_matches = [
        entry
        for entry in brand_matches
        if normalize_spec(entry["specification"]) == spec_key
    ]

    if len(exact_matches) == 1:
        selected = exact_matches[0]
        return {
            "status": "exact",
            "score": 1000,
            "candidate_count": 1,
            "selected": selected,
        }
    if len(exact_matches) > 1:
        return {
            "status": "ambiguous_exact",
            "score": 1000,
            "candidate_count": len(exact_matches),
            "selected": None,
        }

    scored = []
    for entry in brand_matches:
        candidate_tokens = strength_tokens(entry["specification"])
        candidate_package = package_signature(entry["specification"])
        if (
            jd_package is None
            or candidate_package is None
            or jd_package != candidate_package
        ):
            continue
        shared = jd_tokens & candidate_tokens
        if not shared:
            continue
        score = len(shared) * 100 - len(jd_tokens ^ candidate_tokens)
        scored.append((score, entry))
    scored.sort(key=lambda item: item[0], reverse=True)

    if scored:
        best_score = scored[0][0]
        best = [entry for score, entry in scored if score == best_score]
        if len(best) == 1:
            return {
                "status": "compatible_strength",
                "score": best_score,
                "candidate_count": len(scored),
                "selected": best[0],
            }
        return {
            "status": "ambiguous_strength",
            "score": best_score,
            "candidate_count": len(best),
            "selected": None,
        }

    if brand_matches:
        status = "spec_mismatch"
        candidates = len(brand_matches)
    elif name_matches:
        status = "brand_mismatch"
        candidates = len(name_matches)
    else:
        status = "unmatched"
        candidates = 0
    return {
        "status": status,
        "score": 0,
        "candidate_count": candidates,
        "selected": None,
    }


def ensure_table(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{MATCH_TABLE}` (
                `记录日期` DATE NOT NULL,
                `京东药品ID` VARCHAR(50) NOT NULL,
                `品牌名` VARCHAR(255) NOT NULL,
                `通用名称` VARCHAR(255) NOT NULL,
                `京东规格` VARCHAR(255) NOT NULL,
                `店铺ID` VARCHAR(50) NOT NULL,
                `商家SKU` VARCHAR(100) NOT NULL,
                `美团UPC` VARCHAR(50) NOT NULL,
                `店铺商品名称` VARCHAR(500) NOT NULL,
                `店铺规格` VARCHAR(255) NOT NULL,
                `匹配状态` VARCHAR(50) NOT NULL,
                `匹配分数` INT NOT NULL,
                `候选数` INT NOT NULL,
                `成本价` DECIMAL(12,4) NULL,
                `成本来源` VARCHAR(100) NOT NULL,
                PRIMARY KEY (`记录日期`, `京东药品ID`, `店铺ID`),
                KEY `idx_jd_match_status` (`记录日期`, `匹配状态`),
                KEY `idx_jd_match_upc` (`美团UPC`)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cursor.execute(f"SHOW COLUMNS FROM `{MATCH_TABLE}`")
        existing = {row[0] for row in cursor.fetchall()}
        if "店铺商品名称" not in existing:
            cursor.execute(
                f"ALTER TABLE `{MATCH_TABLE}` "
                "ADD COLUMN `店铺商品名称` VARCHAR(500) NOT NULL DEFAULT '' AFTER `美团UPC`"
            )
        if "店铺规格" not in existing:
            cursor.execute(
                f"ALTER TABLE `{MATCH_TABLE}` "
                "ADD COLUMN `店铺规格` VARCHAR(255) NOT NULL DEFAULT '' AFTER `店铺商品名称`"
            )


def refresh_matches(
    connection,
    record_date: dt.date,
    drugs: list[dict],
    catalog: list[dict],
    costs: dict[str, tuple[Decimal, str]],
) -> Counter:
    entries_by_store: dict[str, list[dict]] = defaultdict(list)
    for entry in catalog:
        entries_by_store[entry["store_id"]].append(entry)

    rows = []
    statuses = Counter()
    for drug in drugs:
        for store_id, store_entries in sorted(entries_by_store.items()):
            result = match_one(drug, store_entries)
            selected = result["selected"]
            upc = selected["upc"] if selected else ""
            cost, cost_source = costs.get(upc, (None, "")) if upc else (None, "")
            statuses[result["status"]] += 1
            rows.append(
                (
                    record_date,
                    drug["drug_id"],
                    drug["brand"],
                    drug["generic_name"],
                    drug["specification"],
                    store_id,
                    selected["merchant_sku"] if selected else "",
                    upc,
                    selected["title"] if selected else "",
                    selected["specification"] if selected else "",
                    result["status"],
                    result["score"],
                    result["candidate_count"],
                    cost,
                    cost_source,
                )
            )

    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM `{MATCH_TABLE}` WHERE `记录日期` = %s",
            (record_date,),
        )
        cursor.executemany(
            f"""
            INSERT INTO `{MATCH_TABLE}` (
                `记录日期`, `京东药品ID`, `品牌名`, `通用名称`, `京东规格`,
                `店铺ID`, `商家SKU`, `美团UPC`, `店铺商品名称`, `店铺规格`,
                `匹配状态`, `匹配分数`, `候选数`, `成本价`, `成本来源`
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            """,
            rows,
        )
    return statuses


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--record-date", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--catalog", action="append", required=True, type=Path)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    catalog_table = (config.get("tables") or {}).get(
        "jd_subsidy_catalog", DEFAULT_CATALOG_TABLE
    )
    if not catalog_table.replace("_", "").isalnum():
        raise ValueError(f"unsafe JD catalog table: {catalog_table!r}")
    connection = pymysql.connect(**config["mysql"], autocommit=False)
    try:
        drugs = load_jd_drugs(connection, catalog_table)
        catalog = load_catalogs(args.catalog)
        costs = load_costs(connection)
        ensure_table(connection)
        statuses = refresh_matches(
            connection,
            args.record_date,
            drugs,
            catalog,
            costs,
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
                "jd_drugs": len(drugs),
                "catalog_entries": len(catalog),
                "statuses": dict(statuses),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
