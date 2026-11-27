"""Collect Meituan B2C product-analysis metrics into the esale_system workbench."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pymysql

from meituan_business_report import CdpPage, load_config, write_state
from workbench import qualified as qualified_workbench
from workbench import refresh_yesterday_week_ratio
from workbench import target as workbench_target


TZ = ZoneInfo("Asia/Shanghai")
TARGET_URL = "https://yiyao.meituan.com/main/frame#/page/analysis/product/b2c"
BASE_PATH = "/api/page/bizdata/b2c/productAnalysis"
TABLE = "esale_system"
CLIENT = "保民"
OWNER = "待维护"
PLATFORM = "美团"
TIRZEPATIDE = "替尔泊肽"
STORE_ID = "32581900"
CHART_MODELS = {"commodity_sale": 465050980, "on_sale": 108058106}


def store_settings(config: dict) -> dict[str, str]:
    store = config.get("meituan_store") or {}
    return {
        "poi_id": str(store.get("poi_id") or STORE_ID).strip(),
        "client": str(store.get("client") or CLIENT).strip(),
        "owner": str(store.get("owner") or OWNER).strip(),
        "platform": str(store.get("platform") or PLATFORM).strip(),
    }


def compact(value: dt.date) -> str:
    return value.strftime("%Y%m%d")


def period_params(now: dt.datetime) -> dict[str, dict[str, str]]:
    yesterday = now.date() - dt.timedelta(days=1)
    start_30 = yesterday - dt.timedelta(days=29)
    compare_30_end = start_30 - dt.timedelta(days=1)
    compare_30_start = compare_30_end - dt.timedelta(days=29)
    daily_compare = yesterday - dt.timedelta(days=1)
    return {
        "near_30": {
            "dateType": "last_30_days",
            "startDate": compact(start_30),
            "endDate": compact(yesterday),
            "compareStartDate": compact(compare_30_start),
            "compareEndDate": compact(compare_30_end),
        },
        "daily": {
            "dateType": "dt",
            "startDate": compact(yesterday),
            "endDate": compact(yesterday),
            "compareStartDate": compact(daily_compare),
            "compareEndDate": compact(daily_compare),
        },
    }


def parse_decimal(value: object, *, field: str) -> Decimal:
    text = str(value if value is not None else "").strip().replace(",", "")
    if not text:
        raise ValueError(f"missing product-analysis field {field}")
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"invalid product-analysis field {field}: {value!r}") from error


def parse_decimal_or_none(value: object, *, field: str) -> Decimal | None:
    if value is None or str(value).strip() in {"", "-", "--"}:
        return None
    return parse_decimal(value, field=field)


def parse_percent(value: object, *, field: str) -> Decimal | None:
    if value is None or str(value).strip() in {"", "-", "--"}:
        return None
    text = str(value).strip().replace("%", "")
    try:
        return Decimal(text) / Decimal("100")
    except InvalidOperation as error:
        raise ValueError(f"invalid product-analysis percent {field}: {value!r}") from error


def open_analysis(page: CdpPage) -> None:
    page.call("Page.enable")
    page.call("Runtime.enable")
    page.call("Page.navigate", {"url": TARGET_URL})
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = page.evaluate(
            """
            (() => {
              const frame = document.querySelector('iframe#hashframe');
              const text = (frame && frame.contentDocument && frame.contentDocument.body
                ? frame.contentDocument.body.innerText : '') || '';
              return {
                ready: text.includes('商品分析') && text.includes('成交金额'),
                challenged: /验证码|风险|访问受限/.test(text.slice(0, 4000))
              };
            })()
            """
        )
        if state.get("challenged"):
            raise RuntimeError("Meituan product-analysis page is challenged")
        if state.get("ready"):
            return
        time.sleep(1)
    raise RuntimeError("Meituan product-analysis page did not finish loading")


def fetch_json(page: CdpPage, path: str, params: dict[str, object]) -> dict:
    url = f"{path}?{urlencode(params)}"
    envelope = json.loads(
        page.evaluate(
            f"""
            (async () => {{
              const response = await fetch({json.dumps(url)}, {{credentials: 'include'}});
              return JSON.stringify({{status: response.status, body: await response.text()}});
            }})()
            """
        )
    )
    if envelope.get("status") != 200:
        raise RuntimeError(f"Meituan product-analysis HTTP error: {envelope.get('status')}")
    try:
        payload = json.loads(envelope.get("body") or "")
    except json.JSONDecodeError as error:
        raise RuntimeError("Meituan product-analysis API returned non-JSON content") from error
    if payload.get("code") != 0:
        raise RuntimeError(
            f"Meituan product-analysis API error: code={payload.get('code')} msg={payload.get('msg')}"
        )
    return payload


def post_json(page: CdpPage, path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False)
    envelope = json.loads(
        page.evaluate(
            f"""
            (async () => {{
              const response = await fetch({json.dumps(path)}, {{
                method: 'POST', credentials: 'include',
                headers: {{'Content-Type': 'application/json'}}, body: {json.dumps(body)}
              }});
              return JSON.stringify({{status: response.status, body: await response.text()}});
            }})()
            """
        )
    )
    if envelope.get("status") != 200:
        raise RuntimeError(f"Meituan product-analysis HTTP error: {envelope.get('status')}")
    try:
        result = json.loads(envelope.get("body") or "")
    except json.JSONDecodeError as error:
        raise RuntimeError("Meituan product-analysis API returned non-JSON content") from error
    if result.get("code") != 0:
        raise RuntimeError(
            f"Meituan product-analysis API error: code={result.get('code')} msg={result.get('msg') or result.get('message')}"
        )
    return result


def fetch_sku_rows(page: CdpPage, params: dict[str, str], *, audit: dict | None = None) -> list[dict]:
    """Fetch every SKU row for one period, tolerating the cold-start settle window.

    The on-demand Chrome is restarted for each collection cycle, and the Meituan
    SPA populates its backend state asynchronously after the DOM renders. During
    that window skuRank/detail can answer inconsistently -- an empty result set
    despite a non-zero total, or a totalSum that differs between calls. Both are
    transient and clear once the app has settled.

    Each attempt walks the pages from 1 and requires the collected row count to
    match a stable totalSum. Any inconsistency (a non-zero total with no rows, or
    a totalSum that changes mid-walk) aborts the attempt and retries from page 1
    after a short pause. This deliberately does not try to interpret which of two
    conflicting totals is correct: a real mismatch is a backend bug and must raise
    rather than be silently resolved into a possibly-truncated result.
    """
    last_error: str | None = None
    for attempt in range(1, 6):
        try:
            if audit is None:
                return _walk_sku_pages(page, params)
            attempt_audit = {}
            rows = _walk_sku_pages(page, params, audit=attempt_audit)
            audit.clear()
            audit.update(attempt_audit)
            audit["attempts"] = attempt
            return rows
        except _PaginationUnsettled as error:
            last_error = str(error)
            if attempt < 5:
                time.sleep(2)
    raise RuntimeError(f"Meituan product-analysis SKU pagination did not settle: {last_error}")


class _PaginationUnsettled(RuntimeError):
    """Raised when skuRank/detail answers inconsistently during the settle window."""


def _walk_sku_pages(page: CdpPage, params: dict[str, str], *, audit: dict | None = None) -> list[dict]:
    """One full pagination walk; raises _PaginationUnsettled on a transient mismatch."""
    rows: list[dict] = []
    page_num = 1
    expected_total: int | None = None
    page_counts = []
    while True:
        payload = fetch_json(
            page,
            f"{BASE_PATH}/skuRank/detail",
            {
                **params,
                "keyword": "",
                "skuCategoryIds": "",
                "productStatus": "",
                "isNmdCharge": "",
                "pageNum": page_num,
                "pageSize": 100,
            },
        )
        data = payload.get("data") or {}
        page_rows = data.get("currentPageData")
        if not isinstance(page_rows, list):
            raise RuntimeError("Meituan product-analysis SKU response has no currentPageData list")
        total = data.get("totalSum")
        try:
            total = int(total)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid product-analysis totalSum: {total!r}") from error

        if expected_total is None:
            expected_total = total
            # A non-zero total with no rows means the backend has not settled yet.
            if total > 0 and not page_rows:
                raise _PaginationUnsettled(f"page 1 empty with totalSum={total}")
        elif total != expected_total:
            raise _PaginationUnsettled(
                f"totalSum changed from {expected_total} to {total} at page {page_num}"
            )

        page_counts.append({"page": page_num, "rows": len(page_rows), "expected_total": total})
        rows.extend(page_rows)
        if len(rows) >= expected_total:
            break
        if not page_rows:
            # A page came back empty before we reached the reported total. During the
            # settle window this is the same transient symptom as a shifting total,
            # so let the caller retry rather than failing the whole collection.
            raise _PaginationUnsettled(
                f"page {page_num} empty at {len(rows)}/{expected_total}"
            )
        page_num += 1

    if len(rows) != expected_total:
        raise RuntimeError(f"Meituan product-analysis SKU pagination incomplete: {len(rows)}/{expected_total}")
    if audit is not None:
        audit.update(expected_rows=expected_total, actual_rows=len(rows), pages=page_counts, complete=True)
    return rows


def chart_values(rows: object, required_codes: set[str]) -> dict[str, dict]:
    if not isinstance(rows, list):
        raise RuntimeError("Meituan product-analysis chart response is not a list")
    values = {str(row.get("indexCode")): row for row in rows if isinstance(row, dict)}
    missing = sorted(required_codes - values.keys())
    if missing:
        raise RuntimeError(f"Meituan product-analysis chart is missing metrics: {missing}")
    return values


def summarize_period(
    sale_rows: object, on_sale_rows: object, sku_rows: list[dict]
) -> dict[str, Decimal | None]:
    sale = chart_values(sale_rows, {"gtv_amt", "sold_sku_cnt", "onsale_sku_sold_rate"})
    on_sale = chart_values(on_sale_rows, {"onsale_sku_cnt", "onsale_stock_sku_cnt"})
    amount = parse_decimal_or_none(sale["gtv_amt"].get("value"), field="gtv_amt.value")
    ring_ratio = parse_percent(sale["gtv_amt"].get("compareValue"), field="gtv_amt.compareValue")
    average_sold_skus = parse_decimal_or_none(sale["sold_sku_cnt"].get("value"), field="sold_sku_cnt.value")
    average_on_sale_skus = parse_decimal_or_none(on_sale["onsale_sku_cnt"].get("value"), field="onsale_sku_cnt.value")
    average_available_skus = parse_decimal_or_none(
        on_sale["onsale_stock_sku_cnt"].get("value"), field="onsale_stock_sku_cnt.value"
    )
    product_turnover_rate = parse_percent(
        sale["onsale_sku_sold_rate"].get("value"), field="onsale_sku_sold_rate.value"
    )
    if average_sold_skus is not None and average_on_sale_skus is not None and average_sold_skus > average_on_sale_skus:
        raise ValueError("average sold SKU count exceeds average on-sale SKU count")
    tirzepatide_amount = None
    if amount is not None:
        tirzepatide_amount = sum(
            (
                parse_decimal(row.get("gtvAmt"), field="sku.gtvAmt")
                for row in sku_rows
                if TIRZEPATIDE in str(row.get("productName") or "")
            ),
            Decimal("0"),
        )
    if tirzepatide_amount is not None and tirzepatide_amount > amount:
        raise ValueError("tirzepatide amount exceeds total transaction amount")
    return {
        "amount": amount,
        "ring_ratio": ring_ratio,
        "average_sold_skus": average_sold_skus,
        "average_on_sale_skus": average_on_sale_skus,
        "average_available_skus": average_available_skus,
        "product_turnover_rate": product_turnover_rate,
        "tirzepatide_amount": tirzepatide_amount,
        "non_tirzepatide_ratio": (
            (amount - tirzepatide_amount) / amount
            if amount is not None and amount != 0 and tirzepatide_amount is not None else None
        ),
    }


def collect(now: dt.datetime, port: int, poi_id: str = STORE_ID, *, on_period=None) -> dict:
    page = CdpPage(port)
    try:
        open_analysis(page)
        results = {}
        for label, params in period_params(now).items():
            sale_chart = post_json(
                page,
                "/api/page/bizdata/report/getChart",
                {"modelId": CHART_MODELS["commodity_sale"], "requestParams": {"poiId": poi_id, **params}},
            ).get("data")
            on_sale_chart = post_json(
                page,
                "/api/page/bizdata/report/getChart",
                {"modelId": CHART_MODELS["on_sale"], "requestParams": {"poiId": poi_id, **params}},
            ).get("data")
            if on_period is None:
                results[label] = summarize_period(sale_chart, on_sale_chart, fetch_sku_rows(page, params))
            else:
                audit = {}
                sku_rows = fetch_sku_rows(page, params, audit=audit)
                results[label] = summarize_period(sale_chart, on_sale_chart, sku_rows)
                on_period(label, {"params": params, "sale_chart": sale_chart,
                    "on_sale_chart": on_sale_chart, "sku_rows": sku_rows,
                    "metrics": results[label], "pagination": audit,
                    "observed_at": dt.datetime.now(TZ).isoformat()})
        return results
    finally:
        page.close()


def validate_table(conn, table: str = TABLE, *, schema: str | None = None) -> None:
    schema = schema or (conn.db.decode() if isinstance(conn.db, bytes) else conn.db)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
        columns = {row[0] for row in cur.fetchall()}
    required = {"日期", "客户", "商家负责人", "平台", "昨日交易额", "商品动销率", "经营商品数量", "未动销商品数量", "本月累计交易额", "近30日环比", "昨日周环比", "非替尔泊肽销售金额占比"}
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(f"{table} is missing required columns: {missing}")


def persist(
    conn,
    metrics: dict,
    record_date: dt.date,
    *,
    table: str = TABLE,
    client: str = CLIENT,
    owner: str = OWNER,
    platform: str = PLATFORM,
    schema: str = "adjust_price",
) -> None:
    near_30 = metrics["near_30"]
    daily = metrics["daily"]
    row = {
        "record_date": record_date,
        "client": client,
        "owner": owner,
        "platform": platform,
        "daily_amount": daily["amount"],
        "product_turnover_rate": near_30["product_turnover_rate"],
        "operating_product_count": near_30["average_on_sale_skus"],
        "non_moving_product_count": (
            near_30["average_on_sale_skus"] - near_30["average_sold_skus"]
            if near_30["average_on_sale_skus"] is not None and near_30["average_sold_skus"] is not None else None
        ),
        "near_30_amount": near_30["amount"],
        "ring_ratio": near_30["ring_ratio"],
        "day_ring_ratio": daily["ring_ratio"],
        "non_tirzepatide_ratio": near_30["non_tirzepatide_ratio"],
    }
    with conn.cursor() as cur:
        target_table = qualified_workbench(schema, table)
        cur.execute(
            f"""INSERT INTO {target_table}
            (`日期`,`客户`,`商家负责人`,`平台`,`昨日交易额`,`商品动销率`,`经营商品数量`,`未动销商品数量`,
             `本月累计交易额`,`近30日环比`,`日环比平台`,`非替尔泊肽销售金额占比`)
            VALUES (%(record_date)s,%(client)s,%(owner)s,%(platform)s,%(daily_amount)s,%(product_turnover_rate)s,
                    %(operating_product_count)s,%(non_moving_product_count)s,%(near_30_amount)s,
                    %(ring_ratio)s,%(day_ring_ratio)s,%(non_tirzepatide_ratio)s)
            ON DUPLICATE KEY UPDATE
              `商家负责人`=VALUES(`商家负责人`),
              `昨日交易额`=VALUES(`昨日交易额`),
              `商品动销率`=VALUES(`商品动销率`),
              `经营商品数量`=VALUES(`经营商品数量`),
              `未动销商品数量`=VALUES(`未动销商品数量`),
              `本月累计交易额`=VALUES(`本月累计交易额`),
              `近30日环比`=VALUES(`近30日环比`),
              `日环比平台`=VALUES(`日环比平台`),
              `非替尔泊肽销售金额占比`=VALUES(`非替尔泊肽销售金额占比`)""",
            row,
        )
    refresh_yesterday_week_ratio(
        conn,
        schema=schema,
        table=table,
        record_date=record_date,
        client=client,
        platform=platform,
    )


def run(args: argparse.Namespace) -> dict:
    started = dt.datetime.now(TZ).replace(microsecond=0)
    if args.state_file:
        write_state(args.state_file, {"state": "running", "started_at": started.isoformat()})
    config = load_config(args.config)
    settings = store_settings(config)
    if not settings["poi_id"]:
        raise ValueError("meituan_store.poi_id is required")
    workbench_schema, table = workbench_target(config)
    metrics = collect(started, args.port, settings["poi_id"])
    result = {
        "state": "success",
        "collected_at": started.isoformat(),
        "near_30_transaction_amount": str(metrics["near_30"]["amount"]) if metrics["near_30"]["amount"] is not None else None,
        "near_30_tirzepatide_amount": str(metrics["near_30"]["tirzepatide_amount"]) if metrics["near_30"]["tirzepatide_amount"] is not None else None,
        "daily_transaction_amount": str(metrics["daily"]["amount"]) if metrics["daily"]["amount"] is not None else None,
        "daily_tirzepatide_amount": str(metrics["daily"]["tirzepatide_amount"]) if metrics["daily"]["tirzepatide_amount"] is not None else None,
        "poi_id": settings["poi_id"],
    }
    if not args.dry_run:
        mysql = config["mysql"]
        conn = pymysql.connect(
            host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
            password=mysql["password"], database=mysql["database"],
            charset=mysql.get("charset", "utf8mb4"), autocommit=False,
        )
        try:
            validate_table(conn, table, schema=workbench_schema)
            persist(
                conn,
                metrics,
                started.date(),
                table=table,
                client=settings["client"],
                owner=settings["owner"],
                platform=settings["platform"],
                schema=workbench_schema,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    if args.state_file:
        write_state(args.state_file, result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Meituan product-analysis workbench metrics")
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--state-file", type=pathlib.Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
