"""Resolve the shared e-commerce workbench table."""

from __future__ import annotations

import datetime as dt
import re


DEFAULT_SCHEMA = "adjust_price"
DEFAULT_TABLE = "esale_system"
IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


def quote_identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe database identifier: {value!r}")
    return f"`{value}`"


def target(config: dict) -> tuple[str, str]:
    settings = config.get("workbench") or {}
    tables = config.get("tables") or {}
    schema = str(settings.get("schema") or DEFAULT_SCHEMA).strip()
    table = str(settings.get("table") or tables.get("esale_system") or DEFAULT_TABLE).strip()
    quote_identifier(schema)
    quote_identifier(table)
    return schema, table


def qualified(schema: str, table: str) -> str:
    return f"{quote_identifier(schema)}.{quote_identifier(table)}"


def refresh_yesterday_week_ratio(
    connection,
    *,
    schema: str,
    table: str,
    record_date: dt.date,
    client: str,
    platform: str,
) -> None:
    """Compare yesterday turnover with the same weekday one week earlier."""
    target = qualified(schema, table)
    previous_date = record_date - dt.timedelta(days=7)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE {target} AS current_row
            LEFT JOIN {target} AS previous_row
              ON previous_row.`客户`=current_row.`客户`
             AND previous_row.`平台`=current_row.`平台`
             AND previous_row.`日期`=%s
            SET current_row.`昨日周环比`=CASE
              WHEN current_row.`昨日交易额` IS NULL
                OR previous_row.`昨日交易额` IS NULL
                OR previous_row.`昨日交易额`=0
              THEN NULL
              ELSE (current_row.`昨日交易额`-previous_row.`昨日交易额`)
                   / previous_row.`昨日交易额`
            END
            WHERE current_row.`日期`=%s
              AND current_row.`客户`=%s
              AND current_row.`平台`=%s
            """,
            (previous_date, record_date, client, platform),
        )
