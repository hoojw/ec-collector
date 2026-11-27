#!/usr/bin/env python3
"""Collect Meituan's full 上架商品 list (商品列表) for one store — including the
trailing-30-day sales volume.

Source page: ``https://yiyao.meituan.com/main/frame#/page/product/list/single``
             (商家后台 → 商品管理 → 商品列表)
Source API :  ``POST /reuse/health/product/retail/r/searchListPageV2``

Why this collector exists next to ``meituan_product_sales_collect.py``
----------------------------------------------------------------------
``skuRank/detail`` answers with the SKUs that **sold** inside the window, so a
listed-but-idle product is simply absent (保民 近7天 → 52 rows).  This page is the
authoritative 上架商品 registry: it returns **every listed product**, zero-sales
ones included (保民 → 129 rows), which is what lets the same row carry 30天销量,
库存 and 上架状态 at once.

Field mapping (verified against the live response on 2026-09-21)
---------------------------------------------------------------
``salesNumInThirtyDays``  → 30天销量  (the column the operator asked for)
``name``                  → 商品名
``upcCode``               → 唯一编码  (SPU level; falls back to the first SKU's
                            ``wmProductSkus[].upcCode``)
``wmProductSkus[].stock`` → 库存      (summed over SKUs)
``sellCount``             → 近30天销量 as the page itself labels it; identical to
                            ``salesNumInThirtyDays`` in every row probed, so it is
                            deliberately not persisted separately.

``searchListPageV2`` returns no 销售额, so 销售额 is written as NULL.  近7天销量
is not a field of this feed either — it stays NULL here and is filled by
``meituan_product_sales_collect.py``'s own 7-day run.

Like every Meituan endpoint, the call carries ``mtgsig`` and cannot be replayed
outside the browser: the request is issued from inside the logged-in page through
CDP with the page's own signing, exactly as ``meituan_product_analysis.py`` does.
"""

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

import esale_product
from meituan_business_report import CdpPage


TZ = ZoneInfo("Asia/Shanghai")
COLLECTOR_VERSION = 2
LIST_PATH = "https://yiyao.meituan.com/reuse/health/product/retail/r/searchListPageV2"
APP_KEY = "fe_com.sankuai.yiyao.eproduct.manager"
PAGE_SIZE = 100

# The 上架商品 list lives on its own SPA route.  Loading the *analysis* page and
# firing this API from there is not enough: mtgsig is minted per page module, so
# the list module has to be the one that is actually mounted.  Verified on the VM
# — from the analysis page the endpoint answered with non-JSON content for stores
# whose list module was not resident, while this route answered code 0 with the
# full productList.  The hash route alone does not load it either (the shell
# ignores a bare hash change), so the collector navigates normally and then waits
# for the list's own 30天销量 column to appear.
LIST_URL = "https://yiyao.meituan.com/main/frame#/page/product/list/single"

# The list endpoint has no window parameter — 30天销量 is a fixed per-row field.
# ``code`` keeps the shape of the other collectors' ``window`` block so the state
# file stays readable across platforms.
WINDOW = {"code": "d30", "date_type": "last_30_days", "field": "salesNumInThirtyDays"}

# Body of the page's own request, minus ``pageNum``/``pageSize``/``wmPoiId`` which
# vary per store and page.  Confirmed byte-for-byte against the live SPA request.
BASE_PARAMS = {
    "needTag": "0",
    "name": "",
    "brandId": "0",
    "tagId": "0",
    "searchWord": "",
    "state": "0",
    "sortType": "",
    "sortKey": "",
    "saleStatus": "0",
    "limitSale": "0",
    "needCombinationSpu": "-1",
    "noStockAutoClear": "-1",
    "problemType": "1",
    "noSingleDeliveryType": "0",
}

TWO_PLACES = Decimal("0.01")


def decimal_or_none(value: object, *, place: Decimal | None = None) -> Decimal | None:
    if value is None or str(value).strip() in {"", "-", "--"}:
        return None
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except InvalidOperation as error:
        raise ValueError(f"invalid Meituan 商品列表 value: {value!r}") from error
    return parsed.quantize(place) if place else parsed


def poi_id(config: dict) -> str:
    """The ``wmPoiId`` this store's browser is logged into.

    ``meituan_store.poi_id`` is the canonical location; the flat ``store`` section
    is checked too because the 保民 config keeps its identity there.
    """
    for section in ("meituan_store", "store"):
        store = config.get(section) or {}
        value = str(store.get("wm_poi_id") or store.get("poi_id") or "").strip()
        if value:
            return value
    raise ValueError("store config must define meituan_store.poi_id for the 商品列表 feed")


def request_params(poi: str, page_num: int) -> dict[str, str]:
    return {"wmPoiId": poi, "pageNum": str(page_num), "pageSize": str(PAGE_SIZE), **BASE_PARAMS}


def open_product_list(page: CdpPage) -> None:
    """Bring the browser to the 上架商品 list and wait until it is usable.

    Two things have to be true before the API can be called from page context:

    * the product-list module must be mounted, because that is what mints the
      ``mtgsig`` the endpoint checks; and
    * the store's own list must have rendered, which we detect by the 30天销量
      column header this collector depends on.

    Navigating to the hash route is not enough — the SPA shell ignores a bare
    hash change — so this does a full ``Page.navigate`` to the route URL and then
    polls the frame, mirroring ``open_analysis`` in ``meituan_product_analysis``.
    """
    page.call("Page.enable")
    page.call("Runtime.enable")
    page.call("Page.navigate", {"url": LIST_URL})
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = page.evaluate(
            """
            (() => {
              const frame = document.querySelector('iframe#hashframe');
              const text = (frame && frame.contentDocument && frame.contentDocument.body
                ? frame.contentDocument.body.innerText : '') || '';
              return {
                ready: text.includes('30天销量') || text.includes('商品列表'),
                challenged: /验证码|风险|访问受限/.test(text.slice(0, 4000))
              };
            })()
            """
        )
        if state.get("challenged"):
            raise RuntimeError("Meituan 商品列表 page is challenged")
        if state.get("ready"):
            return
        time.sleep(1)
    raise RuntimeError("Meituan 商品列表 page did not finish loading")


def fetch_json(page: CdpPage, params: dict[str, str]) -> dict:
    """Ask the page to POST one page of the list, signing with its own mtgsig."""
    body = urlencode(params)
    envelope = json.loads(
        page.evaluate(
            f"""
            (async () => {{
              const response = await fetch({json.dumps(LIST_PATH)}, {{
                method: 'POST',
                headers: {{
                  'Content-Type': 'application/x-www-form-urlencoded',
                  'M-APPKEY': {json.dumps(APP_KEY)},
                }},
                body: {json.dumps(body)},
                credentials: 'include',
              }});
              return JSON.stringify({{status: response.status, body: await response.text()}});
            }})()
            """
        )
    )
    if envelope.get("status") != 200:
        raise RuntimeError(f"Meituan 商品列表 HTTP error: {envelope.get('status')}")
    try:
        return json.loads(envelope.get("body") or "")
    except json.JSONDecodeError as error:
        raise RuntimeError("Meituan 商品列表 API returned non-JSON content") from error


def fetch_rows(page: CdpPage, poi: str) -> list[dict]:
    """Walk every page of the store's 上架商品 list."""
    rows: list[dict] = []
    page_num = 1
    expected_total: int | None = None
    while True:
        payload = fetch_json(page, request_params(poi, page_num))
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Meituan 商品列表 API error: code={payload.get('code')} msg={payload.get('msg')}"
            )
        data = payload.get("data") or {}
        page_rows = data.get("productList")
        if not isinstance(page_rows, list):
            raise RuntimeError("Meituan 商品列表 response has no productList list")
        total = data.get("totalCount")
        try:
            total = int(total)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid Meituan 商品列表 totalCount: {total!r}") from error
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RuntimeError("Meituan 商品列表 totalCount changed during pagination")
        rows.extend(page_rows)
        if len(rows) >= expected_total:
            break
        if not page_rows:
            raise RuntimeError("Meituan 商品列表 pagination ended early")
        page_num += 1
    if len(rows) != expected_total:
        raise RuntimeError(f"Meituan 商品列表 pagination incomplete: {len(rows)}/{expected_total}")
    return rows


def sku_upc(raw: dict) -> str | None:
    """SPU-level UPC, falling back to the first SKU that carries one."""
    spu_upc = str(raw.get("upcCode") or "").strip()
    if spu_upc:
        return spu_upc
    for sku in raw.get("wmProductSkus") or []:
        candidate = str((sku or {}).get("upcCode") or "").strip()
        if candidate:
            return candidate
    return None


def sku_stock(raw: dict) -> int | None:
    """Total stock across the SPU's SKUs, or ``None`` when no SKU reports one."""
    stocks = [sku.get("stock") for sku in (raw.get("wmProductSkus") or []) if isinstance(sku, dict)]
    present = [s for s in stocks if s is not None]
    if not present:
        return None
    return sum(int(s) for s in present)


def required_count(value):
    result=decimal_or_none(value)
    if result is None or not result.is_finite() or result<0 or result!=result.to_integral_value():
        raise ValueError('Required product count is missing or invalid')
    return int(result)


def normalize_row(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise RuntimeError("Meituan 商品列表 row is not an object")
    identity = str(raw.get("id") or "").strip()
    if not identity:
        raise RuntimeError("Meituan 商品列表 row has no product id")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise RuntimeError(f"Meituan 商品列表 row {identity} has no product name")
    if "salesNumInThirtyDays" not in raw:
        raise RuntimeError(
            f"Meituan 商品列表 row {identity} is missing salesNumInThirtyDays; "
            f"available: {sorted(raw.keys())}"
        )
    return {
        "spu_id": identity,
        "upc_code": sku_upc(raw),
        "product_name": name,
        "sales_30": required_count(raw["salesNumInThirtyDays"]),
        "stock": sku_stock(raw),
        "sell_status": raw.get("sellStatus"),
        "is_stop_sell": raw.get("isStopSell"),
        "medicine_type": raw.get("medicineType"),
        "medicine_type_name": str(raw.get("medicineTypeName") or "").strip() or None,
        "price": decimal_or_none((raw.get("wmProductSkus") or [{}])[0].get("price"), place=TWO_PLACES)
        if raw.get("wmProductSkus")
        else None,
    }


def sales_rows(collected: dict) -> list[dict]:
    """Shape the results for ``esale_product``.

    唯一编码 prefers the UPC because it is the code that travels across
    platforms; a product without one falls back to the Meituan product id so every
    row still has an identity.  销量 (7 天) and 销售额 are left unset on purpose:
    the 商品列表 feed carries neither, and writing NULL keeps them safe from the
    other collector's value.
    """
    return [
        {
            "code": row["upc_code"] or row["spu_id"],
            "name": row["product_name"],
            "price": row.get("price"),
            "quantity_30": row["sales_30"],
            "stock": row["stock"],
        }
        for row in collected["rows"]
    ]


def collect(port: int, config: dict) -> dict:
    collected_at = dt.datetime.now(TZ).replace(microsecond=0)
    poi = poi_id(config)
    page = CdpPage(port)
    try:
        open_product_list(page)
        rows = [normalize_row(raw) for raw in fetch_rows(page, poi)]
    finally:
        page.close()
    rows.sort(key=lambda row: (-row["sales_30"], row["product_name"]))
    return {
        "collected_at": collected_at.isoformat(),
        "poi_id": poi,
        "window": dict(WINDOW),
        "rows": rows,
    }


def collected_today(state_file: pathlib.Path, today: dt.date) -> bool:
    """True when this store's snapshot for ``today`` has already been taken."""
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
    if not args.config:
        raise ValueError("--config is required: the 商品列表 feed needs the store's wmPoiId")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    now = dt.datetime.now(TZ).replace(microsecond=0)
    if args.state_file and collected_today(args.state_file, now.date()):
        return {
            "state": "skipped",
            "collected_at": now.isoformat(),
            "port": args.port,
            "reason": f"already collected on {now.date().isoformat()}",
            "state_file": str(args.state_file),
        }
    collected = collect(args.port, config)
    rows = collected["rows"]
    result: dict = {
        "state": "dry_run",
        "collected_at": collected["collected_at"],
        "port": args.port,
        "poi_id": collected["poi_id"],
        "window": collected["window"],
        "product_rows": len(rows),
        "sales_30_total": sum(row["sales_30"] for row in rows),
        "zero_sales_rows": sum(1 for row in rows if not row["sales_30"]),
        "out": str(args.out) if args.out else None,
        "persisted": None,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "platform": "美团",
                    "poi_id": collected["poi_id"],
                    "window": collected["window"],
                    "collected_at": collected["collected_at"],
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=1,
                default=str,
            ),
            encoding="utf-8",
        )
        result["state"] = "success"
    if args.persist:
        client, platform = esale_product.settings(config)
        schema = esale_product.schema_of(config)
        connection = esale_product.connect(config, schema=schema)
        try:
            esale_product.ensure_table(connection, schema=schema)
            shaped = sales_rows(collected)
            result["persisted"] = esale_product.replace(
                connection,
                schema=schema,
                client=client,
                platform=platform,
                day=dt.date.fromisoformat(collected["collected_at"][:10]),
                rows=shaped,
                # Upsert, never wipe: the skuRank feed fills the same rows' 销量
                # and 销售额, and this collector does not report those columns.
                prune=False,
                commit=False,
            )
            # This feed is the *complete* view (every listed product), so it is
            # the only one entitled to drop rows: a product taken off the shelf
            # must stop lingering in the table.
            result["pruned"] = esale_product.prune_stale(
                connection, schema=schema, client=client, platform=platform,
                codes=[row["code"] for row in shaped],
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
                "poi_id": collected["poi_id"],
                "window": collected["window"],
                "product_rows": len(rows),
                "persisted": result["persisted"],
                "pruned": result.get("pruned"),
                "collected_at": collected["collected_at"],
            },
        )
        result["state_file"] = str(args.state_file)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Meituan's full 上架商品 list with 30天销量"
    )
    parser.add_argument("--port", required=True, type=int, help="CDP port of the logged-in store browser")
    parser.add_argument("--config", type=pathlib.Path, required=True,
                        help="store config.json (supplies store.wm_poi_id and the esale_product identity)")
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
