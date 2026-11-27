#!/usr/bin/env python3
"""Collect Meituan B2C per-SKU sales rows (商品销量) for one store.

Source page: ``https://yiyao.meituan.com/main/frame#/page/analysis/product/b2c``
             (商家后台 → 经营分析 → 商品分析 → 商品明细)
Source API :  ``GET /api/page/bizdata/b2c/productAnalysis/skuRank/detail``

Two endpoints live behind that table and they are not interchangeable:

* ``skuRank/realtime/detail`` ignores ``compareDate`` and always returns today's
  live snapshot (verified 2026-09-20: 09-10 / 09-13 / 09-19 return byte-identical
  rows), so it cannot be used for a historical window;
* ``skuRank/detail`` honours ``dateType`` + ``startDate``/``endDate`` (verified:
  近7天 → 54 rows, 昨天 → 28 rows, 近30天 → 86 rows).

This collector therefore uses ``skuRank/detail``.  Rows only cover SKUs that sold
in the window, and the sales volume is ``gtvProdCnt`` (成交商品件数).

The request carries Meituan's ``mtgsig`` signature, so it cannot be replayed
outside the browser: the collector issues it from inside the already-logged-in
page through CDP, which is the mechanism ``meituan_product_analysis.py`` uses.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import esale_product
from meituan_business_report import CdpPage
from meituan_product_analysis import BASE_PATH, open_analysis


TZ = ZoneInfo("Asia/Shanghai")
COLLECTOR_VERSION = 2
DETAIL_PATH = f"{BASE_PATH}/skuRank/detail"
PAGE_SIZE = 100
QUANTITY_COLUMN = "gtvProdCnt"

# 美团 B2C 商品明细在窗口刚跨日（尤其 00:00 前后）尚未结算，``skuRank/detail``
# 会以 ``code=0`` + 0 行回答一个其实有成交的窗口。门店周期每 10 分钟一轮、
# 每天的第一轮固定落在 00:00，因此销量采集必须跳过凌晨，等平台侧结算完成后再跑。
# 06:00 之后窗口稳定（实测 09:47 / 10:39 / 17:49 均正常返回）。
EARLIEST_HOUR = 6

# dateType codes of the B2C 商品明细 date picker, one per supported window.
WINDOWS = {"d1": ("dt", 1), "d7": ("last_7_days", 7), "d30": ("last_30_days", 30)}

# Columns of one ``skuRank/detail`` row, confirmed against the live table.  The
# ``*Tb`` comparison columns are absent when the page has no comparison period,
# so only the always-present ones are required; a missing required column fails
# the run instead of silently writing null sales volumes.
REQUIRED_COLUMNS = (
    "skuCode",
    "upcCode",
    "productName",
    "productStatus",
    "gtvProdCnt",
    "gtvAmt",
    "gtvUserCnt",
    "gtvUnitPrice",
    "discountPrice",
    "stock",
    "secondCategoryName",
    "thirdCategoryName",
)

TWO_PLACES = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")


def window_for(window: str, today: dt.date) -> dict[str, str]:
    """Completed-period window parameters, matching the page's own requests."""
    date_type, days = WINDOWS[window]
    end = today - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    compare_end = start - dt.timedelta(days=1)
    compare_start = compare_end - dt.timedelta(days=days - 1)
    compact = "%Y%m%d"
    return {
        "dateType": date_type,
        "startDate": start.strftime(compact),
        "endDate": end.strftime(compact),
        "compareStartDate": compare_start.strftime(compact),
        "compareEndDate": compare_end.strftime(compact),
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def request_params(window_params: dict[str, str], page_num: int) -> dict[str, object]:
    return {
        "dateType": window_params["dateType"],
        "startDate": window_params["startDate"],
        "endDate": window_params["endDate"],
        "compareStartDate": window_params["compareStartDate"],
        "compareEndDate": window_params["compareEndDate"],
        "keyword": "",
        "skuCategoryIds": "",
        "productStatus": "",
        "isNmdCharge": "",
        "pageNum": page_num,
        "pageSize": PAGE_SIZE,
        "yodaReady": "h5",
        "csecplatform": "4",
        "csecversion": "4.3.0",
    }


def decimal_or_none(value: object, *, place: Decimal | None = None) -> Decimal | None:
    if value is None or str(value).strip() in {"", "-", "--"}:
        return None
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except InvalidOperation as error:
        raise ValueError(f"invalid Meituan product-analysis value: {value!r}") from error
    return parsed.quantize(place) if place else parsed


def percent_or_none(value: object) -> Decimal | None:
    """Parse the ``*Tb`` comparison columns, which arrive as ``'7,600.00%'``."""
    if value is None or str(value).strip() in {"", "-", "--"}:
        return None
    text = str(value).replace(",", "").replace("%", "").strip()
    try:
        return (Decimal(text) / Decimal("100")).quantize(SIX_PLACES)
    except InvalidOperation as error:
        raise ValueError(f"invalid Meituan product-analysis percent: {value!r}") from error


def fetch_json(page: CdpPage, params: dict[str, object]) -> dict:
    envelope = json.loads(
        page.evaluate(
            f"""
            (async () => {{
              const response = await fetch({json.dumps(f"{DETAIL_PATH}?{urlencode(params)}")}, {{credentials: 'include'}});
              return JSON.stringify({{status: response.status, body: await response.text()}});
            }})()
            """
        )
    )
    if envelope.get("status") != 200:
        raise RuntimeError(f"Meituan skuRank HTTP error: {envelope.get('status')}")
    try:
        return json.loads(envelope.get("body") or "")
    except json.JSONDecodeError as error:
        raise RuntimeError("Meituan skuRank API returned non-JSON content") from error


def fetch_rows(page: CdpPage, window_params: dict[str, str]) -> list[dict]:
    rows: list[dict] = []
    page_num = 1
    expected_total: int | None = None
    while True:
        payload = fetch_json(page, request_params(window_params, page_num))
        if payload.get("code") != 0:
            raise RuntimeError(f"Meituan skuRank API error: code={payload.get('code')} msg={payload.get('msg')}")
        data = payload.get("data") or {}
        page_rows = data.get("currentPageData")
        if not isinstance(page_rows, list):
            raise RuntimeError("Meituan skuRank response has no currentPageData list")
        total = data.get("totalSum")
        try:
            total = int(total)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid Meituan skuRank totalSum: {total!r}") from error
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RuntimeError("Meituan skuRank total changed during pagination")
        rows.extend(page_rows)
        if len(rows) >= expected_total:
            break
        if not page_rows:
            raise RuntimeError("Meituan skuRank pagination ended early")
        page_num += 1
    if len(rows) != expected_total:
        raise RuntimeError(f"Meituan skuRank pagination incomplete: {len(rows)}/{expected_total}")
    return rows


def required_count(value):
    result=decimal_or_none(value)
    if result is None or not result.is_finite() or result<0 or result!=result.to_integral_value():
        raise ValueError('Required product count is missing or invalid')
    return int(result)


def normalize_row(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise RuntimeError("Meituan skuRank row is not an object")
    missing = sorted(set(REQUIRED_COLUMNS) - raw.keys())
    if missing:
        raise RuntimeError(
            f"Meituan skuRank row is missing expected columns {missing}; available: {sorted(raw.keys())}"
        )
    # 美团把 ``skuCode`` 改成了空串（2026-09-29 实测：skuCode=""，实际标识挪到
    # ``productId``，如 "50134509026"）。唯一编码因此按「UPC → productId → skuCode」
    # 依次回退：UPC 能跨平台对齐（上架 feed 也用 upcCode），productId 保证即便
    # 无 UPC 的行也有稳定身份。三者全空才算真异常。
    upc = str(raw.get("upcCode") or "").strip()
    product_id = str(raw.get("productId") or "").strip()
    sku_code = str(raw.get("skuCode") or "").strip()
    identity = upc or product_id or sku_code
    if not identity:
        raise RuntimeError(
            "Meituan skuRank row has no identity (upcCode/productId/skuCode all empty)"
        )
    name = str(raw.get("productName") or "").strip()
    if not name:
        raise RuntimeError(f"Meituan skuRank row {identity} has no product name")
    return {
        "sku_code": sku_code or product_id,
        "upc_code": upc or None,
        "product_id": product_id or None,
        "product_name": name,
        "quantity": required_count(raw["gtvProdCnt"]),
        "amount": decimal_or_none(raw["gtvAmt"], place=TWO_PLACES),
        "buyers": int(decimal_or_none(raw["gtvUserCnt"]) or 0),
        "unit_price": decimal_or_none(raw["gtvUnitPrice"], place=TWO_PLACES),
        "discount_price": decimal_or_none(raw["discountPrice"], place=TWO_PLACES),
        "stock": raw["stock"],
        "status": raw["productStatus"],
        "sub_category": raw.get("secondCategoryName"),
        "leaf_category": raw.get("thirdCategoryName"),
        "quantity_tb": percent_or_none(raw.get("gtvProdCntTb")),
        "amount_tb": percent_or_none(raw.get("gtvAmtTb")),
    }


def collect(port: int, window: str) -> dict:
    collected_at = dt.datetime.now(TZ).replace(microsecond=0)
    window_params = window_for(window, collected_at.date())
    page = CdpPage(port)
    try:
        open_analysis(page)
        rows = [normalize_row(raw) for raw in fetch_rows(page, window_params)]
    finally:
        page.close()
    rows.sort(key=lambda row: (-row["quantity"], row["product_name"]))
    return {
        "collected_at": collected_at.isoformat(),
        "window": {
            "code": window,
            "date_type": window_params["dateType"],
            "start": window_params["start"],
            "end": window_params["end"],
        },
        "rows": rows,
    }


def sales_rows(collected: dict) -> list[dict]:
    """Shape the results for ``esale_product``.

    唯一编码 prefers the UPC because it is the code that travels across
    platforms; SKUs without one fall back to the store's own SKU code so every
    row still has an identity.
    """
    return [
        {
            "code": row["upc_code"] or row["sku_code"],
            "name": row["product_name"],
            "quantity": row["quantity"],
            "amount": row["amount"],
        }
        for row in collected["rows"]
    ]


def collected_today(state_file: pathlib.Path, today: dt.date) -> bool:
    """True when this store's snapshot for ``today`` has already been taken.

    The collector rides cycles that also run every ten minutes, so the daily
    cadence lives in this state file rather than in the timer, matching how
    ``meituan_product_analysis.py`` gates its own once-a-day collection.
    """
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        state.get("state") == "persisted"
        and state.get("persisted") is not None
        and state.get("target_database") == "ec_dw"
        and str(state.get("date") or "") == today.isoformat()
        and int(state.get("collector_version") or 0) == COLLECTOR_VERSION
    )


def write_state(state_file: pathlib.Path, payload: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_name(state_file.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(temporary, state_file)


def run(args: argparse.Namespace) -> dict:
    if args.persist and not args.config:
        raise ValueError("--persist requires --config")
    now = dt.datetime.now(TZ).replace(microsecond=0)
    if args.state_file and collected_today(args.state_file, now.date()):
        return {
            "state": "skipped",
            "collected_at": now.isoformat(),
            "port": args.port,
            "reason": f"already collected on {now.date().isoformat()}",
            "state_file": str(args.state_file),
        }
    # 凌晨窗口未结算，接口会把「有成交」答成 0 行。与其采一次假空结果，不如让
    # 当天的前几轮直接跳过，等 06:00 之后再真正采集（state 门控此时也不写）。
    if now.hour < EARLIEST_HOUR:
        return {
            "state": "deferred",
            "collected_at": now.isoformat(),
            "port": args.port,
            "reason": (
                f"before {EARLIEST_HOUR:02d}:00 local; Meituan's window is not settled yet"
            ),
            "earliest_hour": EARLIEST_HOUR,
        }
    collected = collect(args.port, args.window)
    rows = collected["rows"]
    result: dict = {
        "state": "dry_run",
        "collected_at": collected["collected_at"],
        "port": args.port,
        "window": collected["window"],
        "sku_rows": len(rows),
        "quantity_field": QUANTITY_COLUMN,
        "quantity_total": sum(row["quantity"] for row in rows),
        "amount_total": str(sum((row["amount"] or Decimal("0") for row in rows), Decimal("0"))),
        "out": str(args.out) if args.out else None,
        "persisted": None,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "platform": "美团",
                    "window": collected["window"],
                    "collected_at": collected["collected_at"],
                    "quantity_field": QUANTITY_COLUMN,
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=1,
                default=str,
            ),
            encoding="utf-8",
        )
        result["state"] = "success"
    if args.persist and not rows:
        # ⚠ 窗口结算期的假空结果必须完全丢弃：既不清零已落库的 销量/销售额，
        # 也不写状态门控。2026-09-23 起连续 7 天「销量全为 0」就是这么来的——
        # 00:00 那一轮拿到 0 行，reset_metrics 抹平全店销量，state 又把当天关掉，
        # 于是当天再无任何补偿机会。等下一轮（≥10 分钟后）窗口结算好再采。
        result["state"] = "empty_window"
        result["persisted"] = None
        result["gate"] = "unarmed"
        result["note"] = (
            "collector returned no rows for the window; skipped the write entirely "
            "and left the daily gate unarmed so a later cycle retries"
        )
        return result
    if args.persist:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        client, platform = esale_product.settings(config)
        schema = esale_product.schema_of(config)
        connection = esale_product.connect(config, schema=schema)
        try:
            esale_product.ensure_table(connection, schema=schema)
            # skuRank is a partial view containing only products with sales in
            # the current 7-day window.  Clear the previous window first so a
            # product that drops out does not retain stale sales indefinitely.
            result["reset"] = esale_product.reset_metrics(
                connection,
                schema=schema,
                client=client,
                platform=platform,
                values={"销量": 0, "销售额": Decimal("0.00")},
                commit=False,
            )
            result["persisted"] = esale_product.replace(
                connection,
                schema=schema,
                client=client,
                platform=platform,
                day=dt.date.fromisoformat(collected["collected_at"][:10]),
                rows=sales_rows(collected),
                # Every row that *did* arrive is a real sale.  Reaching here with
                # rows means the window was settled, so ``allow_empty`` never has
                # to excuse an empty slice — the empty case returned above.
                allow_empty=False,
                # This feed is the *partial* view — only SKUs that sold in the
                # 7-day window.  The 上架商品 feed writes the same rows' 30天销量
                # and 库存, so pruning here would delete the columns it cannot
                # see.  Stale-row cleanup is the 上架 feed's job (prune_stale).
                prune=False,
                commit=False,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result["state"] = "persisted"
    if args.state_file:
        write_state(
            args.state_file,
            {
                "state": result["state"],
                "date": now.date().isoformat(),
                "collector_version": COLLECTOR_VERSION,
                "window": collected["window"],
                "sku_rows": len(rows),
                "persisted": result["persisted"],
                "collected_at": collected["collected_at"],
            },
        )
        result["state_file"] = str(args.state_file)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Meituan B2C per-SKU sales rows")
    parser.add_argument("--port", required=True, type=int, help="CDP port of the logged-in store browser")
    parser.add_argument("--window", default="d7", choices=sorted(WINDOWS), help="d1 = 昨天, d7 = 近7天 (default), d30 = 近30天")
    parser.add_argument("--config", type=pathlib.Path, help="store config.json, required by --persist")
    parser.add_argument("--persist", action="store_true", help="overwrite this store's esale_product slice")
    parser.add_argument("--state-file", type=pathlib.Path,
                        help="skip the run when this file already records today's snapshot")
    parser.add_argument("--out", type=pathlib.Path, help="also write the collected rows to this JSON file")
    return parser.parse_args()


def main() -> int:
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
