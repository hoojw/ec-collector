"""Write platform activity-product counts into the daily e-commerce workbench."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
from zoneinfo import ZoneInfo

import pymysql

from workbench import qualified as qualified_workbench
from workbench import target as workbench_target


TZ = ZoneInfo("Asia/Shanghai")
TABLE = "esale_system"
CLIENT = "保民"
OWNER = "待维护"
PLATFORMS = {"美团", "京东"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")

# The known-floor-price count is a customer-level figure owned by Baomin: it
# counts the products in Baomin's DataEase cost spreadsheet that actually carry
# a usable cost.  The column is only ever written for this one customer, so the
# value is pinned here instead of being made configurable per store.
FLOOR_PRICE_CLIENT = "保民"
FLOOR_PRICE_SOURCE_SCHEMA = "dataease"
FLOOR_PRICE_SOURCE_TABLE = "excel_数据1_1aa17d71d5"
FLOOR_PRICE_SOURCE_COLUMN = "成本价"
FLOOR_PRICE_COLUMN = "已知商品底价数量"


def quote_identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe table identifier: {value!r}")
    return f"`{value}`"


def quote_literal_identifier(value: str) -> str:
    """Quote a name that may contain non-ASCII characters.

    DataEase names its Excel-backed tables after the uploaded sheet, so the
    identifiers are Chinese and cannot pass ``IDENTIFIER``.  They are module
    constants rather than user input, but still reject backticks so a malformed
    constant can never escape its quoting.
    """
    if not value or "`" in value or "\x00" in value or "." in value:
        raise ValueError(f"unsafe identifier: {value!r}")
    return f"`{value}`"


def validate_workbench(conn, table: str, *, schema: str | None = None) -> None:
    schema = schema or (conn.db.decode() if isinstance(conn.db, bytes) else conn.db)
    workbench = qualified_workbench(schema, table)
    required = {
        "日期", "客户", "商家负责人", "平台",
        "可报名活动商品数量", "已报活动商品数量", FLOOR_PRICE_COLUMN,
    }
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
        columns = {row[0] for row in cursor.fetchall()}
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(f"{workbench} is missing required columns: {missing}")


def source_is_current(
    conn,
    table: str,
    record_date: dt.date,
    *,
    allow_empty: bool = False,
) -> bool:
    source = quote_identifier(table)
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT MAX(`记录日期`) FROM {source}")
        latest = cursor.fetchone()[0]
    if latest is None and allow_empty:
        return False
    if latest != record_date:
        raise RuntimeError(f"{table} has no complete snapshot for {record_date}; latest is {latest}")
    return True


def meituan_counts(
    conn,
    record_date: dt.date,
    table: str,
    *,
    allow_empty: bool = False,
) -> tuple[int, int]:
    if not source_is_current(conn, table, record_date, allow_empty=allow_empty):
        return 0, 0
    source = quote_identifier(table)
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT COALESCE(SUM(`补贴状态`=0), 0), COALESCE(SUM(`补贴状态`=1), 0) "
            f"FROM {source} WHERE `记录日期`=%s",
            (record_date,),
        )
        eligible, reported = cursor.fetchone()
    return int(eligible), int(reported)


def catalog_is_available(conn, table: str) -> bool:
    """True when the overwritten JoySpace activity catalog has any row.

    The catalog is no longer partitioned per day, so the only way a
    calculation can be wrong is an empty table (first-ever run, or a failed
    sync that rolled back).  Surface that loudly instead of silently writing a
    zero activity count.
    """
    source = quote_identifier(table)
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM {source}")
        return int(cursor.fetchone()[0]) > 0


def known_floor_price_count(conn) -> int:
    """Count products with a usable known cost in the Baomin cost spreadsheet.

    ``dataease`` is DataEase's own metadata database.  The collector account
    only holds SELECT there, which is exactly what this read needs; the count is
    then written back into ``adjust_price.esale_system``, where the account does
    have write access.
    """
    source = ".".join(
        (
            quote_literal_identifier(FLOOR_PRICE_SOURCE_SCHEMA),
            quote_literal_identifier(FLOOR_PRICE_SOURCE_TABLE),
        )
    )
    column = quote_literal_identifier(FLOOR_PRICE_SOURCE_COLUMN)
    with conn.cursor() as cursor:
        # A cost of 0 means "not maintained yet" everywhere else in this
        # project (see the COALESCE(NULLIF(成本底价,0), NULLIF(成本价,0)) idiom),
        # so blank and zero rows must not inflate the count of known prices.
        cursor.execute(
            f"SELECT COUNT(*) FROM {source} "
            f"WHERE {column} IS NOT NULL AND {column} <> 0"
        )
        return int(cursor.fetchone()[0])


def jd_counts(
    conn,
    record_date: dt.date,
    match_table: str,
    catalog_table: str,
    listing_table: str,
    *,
    allow_empty_match: bool = False,
) -> tuple[int, int]:
    match_is_current = source_is_current(
        conn, match_table, record_date, allow_empty=allow_empty_match
    )
    if not catalog_is_available(conn, catalog_table):
        raise RuntimeError(
            f"{catalog_table} is empty; run the JoySpace activity catalog sync first"
        )
    listings = quote_identifier(listing_table)
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT MAX(`记录日期`) FROM {listings} WHERE `记录日期` <= %s",
            (record_date,),
        )
        listing_date = cursor.fetchone()[0]
    if listing_date != record_date:
        raise RuntimeError(
            f"{listing_table} has no current store SKU snapshot for {record_date}; latest is {listing_date}"
        )
    if not match_is_current:
        return 0, 0
    matches = quote_identifier(match_table)
    catalog = quote_identifier(catalog_table)
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
              COUNT(*) AS matched_count,
              COALESCE(SUM(
                CASE
                  WHEN TRIM(c.`实际入围最高价`) REGEXP '^[0-9]+([.][0-9]+)?$'
                   AND m.`京东价` < CAST(TRIM(c.`实际入围最高价`) AS DECIMAL(12,2))
                  THEN 1 ELSE 0
                END
              ), 0) AS reported_count
            FROM {matches} AS m
            INNER JOIN {catalog} AS c
              ON c.`策略ID` = m.`策略ID`
            WHERE m.`记录日期`=%s
            """,
            (record_date,),
        )
        matched, reported = cursor.fetchone()
    if int(reported) > int(matched):
        raise RuntimeError("JD reported-activity count exceeds matched activity-product count")
    return int(matched) - int(reported), int(reported)


def jd_latest_complete_source_date(
    conn,
    record_date: dt.date,
    match_table: str,
    listing_table: str,
) -> dt.date | None:
    """Return the newest date not after ``record_date`` shared by the JD inputs.

    The activity catalog is a single overwritten snapshot now, so only the
    per-day JD inputs (matched SKUs and the store SKU snapshot) must share a
    partition.  Mixing a new SKU snapshot with a stale match result is still
    wrong, so the "newest coherent partition" rule survives for those two.
    The caller writes the resulting counts to today's workbench row and
    returns this date as provenance.
    """
    matches = quote_identifier(match_table)
    listings = quote_identifier(listing_table)
    with conn.cursor() as cursor:
        # Do not JOIN the fact tables merely to compare their dates: joining on
        # date alone forms a very large Cartesian product.  EXISTS asks only
        # whether each candidate partition exists.
        cursor.execute(
            f"""
            SELECT MAX(m.`记录日期`)
            FROM (SELECT DISTINCT `记录日期` FROM {matches} WHERE `记录日期` <= %s) AS m
            WHERE EXISTS (SELECT 1 FROM {listings} AS l WHERE l.`记录日期`=m.`记录日期`)
            """,
            (record_date,),
        )
        value = cursor.fetchone()[0]
    return value


def persist(
    conn,
    record_date: dt.date,
    platform: str,
    eligible: int,
    reported: int,
    *,
    schema: str,
    table: str,
    client: str,
    owner: str,
    floor_price_count: int | None = None,
) -> None:
    """Upsert today's activity row.

    ``floor_price_count`` is Baomin-only.  For every other customer, and for
    every caller that has no cost spreadsheet, the parameter stays ``None`` and
    the existing column value is left untouched: a partial platform update must
    never blank out a figure another module owns.
    """
    workbench = qualified_workbench(schema, table)
    floor_price_column = quote_literal_identifier(FLOOR_PRICE_COLUMN)
    writes_floor_price = client == FLOOR_PRICE_CLIENT and floor_price_count is not None
    # A customer that must not own this column still supplies a placeholder for
    # the VALUES() list (which ON DUPLICATE KEY UPDATE always evaluates); the
    # UPDATE branch then simply omits the assignment, so an unrelated column is
    # never blanked out by another platform's partial write.
    statements = [
        "`商家负责人`=VALUES(`商家负责人`)",
        "`可报名活动商品数量`=VALUES(`可报名活动商品数量`)",
        "`已报活动商品数量`=VALUES(`已报活动商品数量`)",
    ]
    if writes_floor_price:
        statements.append(f"{floor_price_column}=VALUES({floor_price_column})")
    updates = ",\n              ".join(statements)
    stored_floor_price = floor_price_count if writes_floor_price else None
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO {workbench}
              (`日期`,`客户`,`商家负责人`,`平台`,
               `可报名活动商品数量`,`已报活动商品数量`,{floor_price_column})
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              {updates}
            """,
            (record_date, client, owner, platform, eligible, reported, stored_floor_price),
        )


def workbench_settings(config: dict) -> tuple[str, str, str]:
    tables = config.get("tables") or {}
    store = config.get("store") or config.get("meituan_store") or config.get("jd_store") or {}
    table = str(tables.get("esale_system") or TABLE).strip()
    client = str(store.get("client") or CLIENT).strip()
    owner = str(store.get("owner") or OWNER).strip()
    quote_identifier(table)
    if not client:
        raise ValueError("workbench client must not be empty")
    return table, client, owner


def run(args: argparse.Namespace) -> dict:
    record_date = args.record_date or dt.datetime.now(TZ).date()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    tables = config.get("tables") or {}
    workbench_table, client, owner = workbench_settings(config)
    workbench_schema, configured_workbench_table = workbench_target(config)
    if configured_workbench_table != workbench_table:
        raise ValueError("conflicting esale_system workbench table settings")
    mysql = config["mysql"]
    conn = pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]), user=mysql["user"], password=mysql["password"],
        database=mysql["database"], charset=mysql.get("charset", "utf8mb4"), autocommit=False,
    )
    try:
        validate_workbench(conn, workbench_table, schema=workbench_schema)
        if args.platform == "美团":
            eligible, reported = meituan_counts(
                conn,
                record_date,
                tables.get("unsubsidized_daily", "subsidy_unsubsidized_daily"),
                allow_empty=args.allow_empty_current_snapshot,
            )
        else:
            jd_source_date = record_date
            if args.allow_stale_jd_catalog:
                candidate = jd_latest_complete_source_date(
                    conn,
                    record_date,
                    tables.get("jd_subsidy_sku", "jd_subsidy_sku"),
                    tables.get("jd_store_sku_normalized_daily", "jd_store_sku_normalized_daily"),
                )
                if candidate is None:
                    raise RuntimeError("JD activity sources have no complete historical snapshot")
                jd_source_date = candidate
            eligible, reported = jd_counts(
                conn,
                jd_source_date,
                tables.get("jd_subsidy_sku", "jd_subsidy_sku"),
                tables.get("jd_subsidy_catalog", "jd_subsidy_catalog"),
                tables.get("jd_store_sku_normalized_daily", "jd_store_sku_normalized_daily"),
                allow_empty_match=args.allow_empty_current_snapshot,
            )
        floor_price_count = (
            known_floor_price_count(conn) if client == FLOOR_PRICE_CLIENT else None
        )
        persist(
            conn,
            record_date,
            args.platform,
            eligible,
            reported,
            schema=workbench_schema,
            table=workbench_table,
            client=client,
            owner=owner,
            floor_price_count=floor_price_count,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "state": "success",
        "record_date": record_date.isoformat(),
        "platform": args.platform,
        "eligible_activity_product_count": eligible,
        "reported_activity_product_count": reported,
        "known_floor_price_count": floor_price_count,
        "source_record_date": (
            jd_source_date.isoformat() if args.platform == "京东" else record_date.isoformat()
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh e-sale activity-product metrics")
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    parser.add_argument("--record-date", type=dt.date.fromisoformat)
    parser.add_argument(
        "--allow-empty-current-snapshot",
        action="store_true",
        help="treat an empty Meituan source as a valid zero snapshot after a successful same-run collection",
    )
    parser.add_argument(
        "--allow-stale-jd-catalog",
        action="store_true",
        help=(
            "fall back to the newest date shared by the per-day JD inputs when "
            "today's SKU snapshot is missing (the activity catalog itself is a "
            "single overwritten snapshot and needs no date)"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2))
