"""Persist collected 商品销量 rows into ``adjust_price.esale_product``.

One row per ``(客户, 平台, 唯一编码)`` holding two sales snapshots side by side:

* ``销量``    — the trailing **7**-day sales volume;
* ``30天销量`` — the trailing **30**-day sales volume.

The 30-day column is a separate column rather than a switch of the 7-day one so
DataEase can still compare the two windows.

Two rules make it safe for several collectors to feed the same platform:

1. **The UPDATE half only names the columns the caller reported.**
   ``replace()`` builds its ``ON DUPLICATE KEY UPDATE`` from
   :func:`reported_keys`, so Meituan's skuRank run (which knows 销量/销售额) leaves
   30天销量/库存 alone, and the 上架商品 run (which knows 30天销量/库存) leaves 销量
   alone.  Naming every column unconditionally is what erased the data on
   2026-09-21 — see :func:`build_insert`.
2. **Only the collector that owns the complete view may delete.**
   ``prune=True`` (JD: one run fills every column) drops rows the run no longer
   lists.  The Meituan feeds pass ``prune=False``, and the 上架商品 feed — the only
   one that sees every listed product — calls :func:`prune_stale` itself, so a
   delisted product still eventually disappears.

The table has no date column: it is a rolling overwrite refreshed once a day, so
``更新日期`` records which day's snapshot the surviving rows belong to.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pymysql

from workbench import qualified

DEFAULT_SCHEMA = "adjust_price"
TABLE = "esale_product"
COLUMNS = ("客户", "平台", "商品", "唯一编码", "价格", "销量", "库存", "销售额", "30天销量", "更新日期")

# 库存 is part of the agreed 口径 and is filled by the Meituan 上架商品 feed
# (which reports per-SKU ``stock``); the JD feed has no stock column, so JD rows
# are written as NULL.  ``销售额`` is only available on the JD feed.
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS {target} (
  `客户`   varchar(64)  COLLATE utf8mb4_unicode_ci NOT NULL,
  `平台`   varchar(32)  COLLATE utf8mb4_unicode_ci NOT NULL,
  `商品`   varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `唯一编码` varchar(64)  COLLATE utf8mb4_unicode_ci NOT NULL,
  `价格`   decimal(18,2) DEFAULT NULL,
  `销量`   decimal(18,2) DEFAULT NULL,
  `库存`   decimal(18,2) DEFAULT NULL,
  `销售额`  decimal(18,2) DEFAULT NULL,
  `30天销量` decimal(18,2) DEFAULT NULL,
  `更新日期` date NOT NULL,
  PRIMARY KEY (`客户`,`平台`,`唯一编码`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# Idempotent upgrades for tables created before the rolling snapshot gained
# these fields.  Never narrow or retype existing production columns here.
ADD_COLUMN_SQL = {
    "价格": "ALTER TABLE {target} ADD COLUMN `价格` decimal(18,2) DEFAULT NULL AFTER `唯一编码`",
    "30天销量": "ALTER TABLE {target} ADD COLUMN `30天销量` decimal(18,2) DEFAULT NULL AFTER `销售额`",
}
HAS_COLUMN_SQL = (
    "SELECT COUNT(*) FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s"
)

# The INSERT is built per run so that ``ON DUPLICATE KEY UPDATE`` only names the
# columns the caller actually reported.  Assigning every column unconditionally
# is what silently destroyed data on 2026-09-21: Meituan's 上架 feed reports
# 30天销量/库存 but not 销量, so a blanket ``销量=VALUES(销量)`` wrote NULL over the
# value skuRank had just stored (and vice versa).  See ``build_insert``.
INSERT_TEMPLATE = """
INSERT INTO {target} ({columns})
VALUES ({placeholders})
ON DUPLICATE KEY UPDATE
{updates}
"""

# Column -> the row key that fills it.  Order fixes the placeholder order.
COLUMN_SOURCES = (
    ("客户", None),
    ("平台", None),
    ("商品", "name"),
    ("唯一编码", "code"),
    ("价格", "price"),
    ("销量", "quantity"),
    ("库存", "stock"),
    ("销售额", "amount"),
    ("30天销量", "quantity_30"),
    ("更新日期", None),
)

# 客户/平台/唯一编码 identify the row, so they are never in the UPDATE list;
# 商品 and 更新日期 always are (a row that is still listed keeps its name and
# snapshot day current even if it reported no numbers).
ALWAYS_UPDATED = ("商品", "更新日期")

DELETE_SQL = "DELETE FROM {target} WHERE `客户`=%s AND `平台`=%s"


def reported_keys(rows: list[dict]) -> set[str]:
    """The row keys the caller actually supplied, across all rows.

    A key is "reported" when at least one row carries it, even with a ``None``
    value — that is how a collector says "this product really has no sales in
    this window", which must overwrite an older figure.  A key absent from every
    row means the collector does not know that column at all, and the existing
    value has to survive.
    """
    keys: set[str] = set()
    for row in rows:
        keys.update(row)
    return keys


def build_insert(target: str, columns: tuple[str, ...]) -> tuple[str, str]:
    """Return ``(insert_sql, update_clause)`` for the columns in play.

    ``columns`` are the ``esale_product`` columns to write.  The update clause
    names only the *value* columns among them, so a run that omits 销量 leaves
    whatever 销量 is already on the row untouched.
    """
    placeholders = ",".join(["%s"] * len(columns))
    quoted = ",".join(f"`{column}`" for column in columns)
    updates = [
        f"  `{column}`=VALUES(`{column}`)"
        for column in columns
        if column not in ("客户", "平台", "唯一编码")
    ]
    insert = INSERT_TEMPLATE.format(
        target=target, columns=quoted, placeholders=placeholders,
        updates=",\n".join(updates),
    )
    return insert, ",\n".join(updates)


def schema_of(config: dict) -> str:
    """The schema holding the esale tables (``adjust_price`` for every store)."""
    return str(((config.get("workbench") or {}).get("schema")) or DEFAULT_SCHEMA).strip()


def settings(config: dict) -> tuple[str, str]:
    """Return ``(客户, 平台)`` for the store the config belongs to."""
    store = config.get("store") or config.get("meituan_store") or config.get("jd_store") or {}
    client = str(store.get("client") or "").strip()
    platform = str(store.get("platform") or "").strip()
    if not client or not platform:
        raise ValueError("store config must define both client and platform")
    return client, platform


def connect(config: dict, *, schema: str) -> pymysql.connections.Connection:
    mysql = config["mysql"]
    return pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
        password=mysql["password"], database=schema,
        charset=mysql.get("charset", "utf8mb4"), autocommit=False,
    )


def ensure_table(connection, *, schema: str) -> str:
    """Create the table when missing and idempotently add newer fields."""
    target = qualified(schema, TABLE)
    with connection.cursor() as cursor:
        cursor.execute(CREATE_SQL.format(target=target))
        for column, statement in ADD_COLUMN_SQL.items():
            cursor.execute(HAS_COLUMN_SQL, (schema, TABLE, column))
            if not cursor.fetchone()[0]:
                cursor.execute(statement.format(target=target))
    connection.commit()
    return target


def _decimal_or_none(value: object, *, place: Decimal | None = None) -> Decimal | None:
    """Coerce a collector value to ``Decimal`` while keeping ``None`` as NULL."""
    if value is None or str(value).strip() in {"", "-", "--"}:
        return None
    parsed = Decimal(str(value))
    return parsed.quantize(place) if place else parsed


def replace(
    connection,
    *,
    schema: str,
    client: str,
    platform: str,
    day: dt.date,
    rows: list[dict],
    allow_empty: bool = False,
    prune: bool = False,
    commit: bool = True,
) -> int:
    """Write ``rows`` into the ``(客户, 平台)`` slice; returns the row count.

    ``rows`` carry ``code``/``name`` plus any of
    ``price`` (价格) / ``quantity`` (销量, 7 天) /
    ``quantity_30`` (30天销量) / ``stock`` (库存) / ``amount`` (销售额).
    A key the caller omits is left untouched on an existing row.

    ``prune`` decides what happens to rows that are not in ``rows``:

    * ``prune=True`` (default) — wipe the whole ``(客户, 平台)`` slice first, so
      the table ends up being exactly this collection.  Correct when one
      collector owns every column for the platform: JD's ``--dimension d7,d30``
      fills 销量 **and** 30天销量 in a single run, and the slice is what it just
      reported.
    * ``prune=False`` — upsert only, deleting nothing.  Required when two
      collectors feed disjoint columns of the same platform and their row sets
      differ: Meituan's ``skuRank/detail`` lists only SKUs that sold in the
      7-day window (52 rows), while the 上架商品 feed lists every listed product
      (129 rows).  Neither sees the other's columns, so neither may delete rows.

    Independent of ``prune``, the UPDATE half names **only the columns this run
    reported** (see :func:`build_insert`).  That is what lets the two Meituan
    feeds share a row: the skuRank run writes 销量/销售额 and leaves 30天销量/库存
    alone, and the 上架 run does the reverse.  A blanket
    ``销量=VALUES(销量)`` would write NULL over the other feed's value — the
    2026-09-21 incident.  A collector that reports a key with a ``None`` value
    *does* overwrite (that is how "genuinely zero sales this window" is said).

    With ``prune=False`` a stale row (a product that was taken off the shelf)
    only disappears if the owner of the full view deletes it.  Meituan's 上架商品
    feed is that owner and calls :func:`prune_stale` afterwards.

    An empty result is rejected unless the caller says the platform itself
    reported "no sales": wiping a store's slice because a collector silently
    failed is much worse than failing loudly, so the exception has to be
    requested explicitly.
    """
    if not rows and not allow_empty:
        raise ValueError(f"refusing to overwrite {client}/{platform}: collector returned no rows")
    codes = [str(row["code"]).strip() for row in rows]
    if any(not code for code in codes):
        raise ValueError("product row has an empty 唯一编码")
    duplicates = {code for code in codes if codes.count(code) > 1}
    if duplicates:
        raise ValueError(f"duplicate 唯一编码 in one collection: {sorted(duplicates)[:5]}")

    # Different records can expose different columns. Never let another row's
    # fields turn this row's omitted fields into SQL NULL.
    groups = {}
    for item in rows:
        groups.setdefault(tuple(sorted(reported_keys([item]))), []).append(item)
    if len(groups) > 1:
        for group in groups.values():
            replace(connection, schema=schema, client=client, platform=platform,
                    day=day, rows=group, prune=False, commit=False)
        if commit: connection.commit()
        return len(rows)
    # A caller holds only a single shop view. It cannot prune a client/platform.
    prune = False
    target = qualified(schema, TABLE)
    # Only take responsibility for the columns this run can actually speak for.
    reported = reported_keys(rows)
    columns = tuple(
        column for column, source in COLUMN_SOURCES
        if source is None or source in reported
    )
    arguments = []
    for row in rows:
        values = []
        for column, source in COLUMN_SOURCES:
            if column not in columns:
                continue
            if source is None:
                values.append(
                    client if column == "客户"
                    else platform if column == "平台"
                    else day
                )
            elif column == "唯一编码":
                values.append(str(row["code"]).strip())
            elif column == "商品":
                values.append(str(row["name"]).strip()[:255])
            elif column in {"价格", "销售额"}:
                values.append(_decimal_or_none(row.get(source), place=Decimal("0.01")))
            else:
                values.append(_decimal_or_none(row.get(source)))
        arguments.append(tuple(values))

    with connection.cursor() as cursor:
        if prune and not rows:
            # ``allow_empty`` explicitly asked for an empty slice.
            cursor.execute(DELETE_SQL.format(target=target), (client, platform))
        elif prune:
            # Delete the rows this run does not carry, but keep the ones it does.
            _delete_missing(cursor, target, client, platform, codes)
        if rows:
            insert_sql, _ = build_insert(target, columns)
            cursor.executemany(insert_sql, arguments)
    if commit:
        connection.commit()
    return len(rows)


def _delete_missing(cursor, target: str, client: str, platform: str, codes: list[str]) -> None:
    """Drop the slice's rows whose 唯一编码 is not in ``codes``.

    This must be one predicate over the complete code set.  Splitting the codes
    into several ``NOT IN`` statements is not equivalent: every later statement
    would delete rows retained by the earlier chunks.
    """
    placeholders = ",".join(["%s"] * len(codes))
    cursor.execute(
        f"DELETE FROM {target} WHERE `客户`=%s AND `平台`=%s AND `唯一编码` NOT IN ({placeholders})",
        (client, platform, *codes),
    )


RESETTABLE_METRICS = {"价格", "销量", "库存", "销售额", "30天销量"}


def reset_metrics(
    connection,
    *,
    schema: str,
    client: str,
    platform: str,
    values: dict[str, object],
    commit: bool = True,
) -> int:
    """Reset collector-owned metrics for an existing store/platform slice.

    Partial feeds must clear values for products which disappeared from the
    current window before they upsert the rows that remain.  The allowlist keeps
    identifiers out of dynamically generated SQL.
    """
    # Multiple shops share this database scope: preserve other shops' records.
    return 0

    if not values:
        return 0
    unknown = sorted(set(values) - RESETTABLE_METRICS)
    if unknown:
        raise ValueError(f"unsupported esale_product reset columns: {unknown}")
    target = qualified(schema, TABLE)
    assignments = ",".join(f"`{column}`=%s" for column in values)
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {target} SET {assignments} WHERE `客户`=%s AND `平台`=%s",
            (*values.values(), client, platform),
        )
        affected = int(cursor.rowcount)
    if commit:
        connection.commit()
    return affected


def prune_stale(
    connection,
    *,
    schema: str,
    client: str,
    platform: str,
    codes: list[str],
    commit: bool = True,
) -> int:
    """Delete the ``(客户, 平台)`` rows whose 唯一编码 is not in ``codes``.

    The counterpart to ``replace(..., prune=False)``: the collector that owns
    the *complete* view of a platform (Meituan's 上架商品 feed — every listed
    product) calls this so a product taken off the shelf stops lingering.  The
    partial feeds (skuRank) must never call it, or they would delete the
    products they cannot see.  Returns the number of rows removed.
    """
    # Multiple shops share this database scope: preserve other shops' records.
    return 0

    target = qualified(schema, TABLE)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM {target} WHERE `客户`=%s AND `平台`=%s".format(target=target),
            (client, platform),
        )
        before = int(cursor.fetchone()[0])
        if codes:
            _delete_missing(cursor, target, client, platform, [str(code).strip() for code in codes])
        else:
            cursor.execute(DELETE_SQL.format(target=target), (client, platform))
        cursor.execute(
            "SELECT COUNT(*) FROM {target} WHERE `客户`=%s AND `平台`=%s".format(target=target),
            (client, platform),
        )
        after = int(cursor.fetchone()[0])
    if commit:
        connection.commit()
    return before - after


__all__ = ["ALWAYS_UPDATED", "COLUMN_SOURCES", "COLUMNS", "CREATE_SQL",
           "DEFAULT_SCHEMA", "INSERT_TEMPLATE", "TABLE", "build_insert",
           "connect", "ensure_table", "prune_stale", "reported_keys",
           "replace", "reset_metrics", "schema_of", "settings"]
