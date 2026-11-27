#!/usr/bin/env python3
"""Collect JD 商智 商品明细 (per-SPU sales) rows for one store — by listening only.

Source page: ``https://jdsz.jd.com/szweb/view/product/productDetail.html``
             (京东商智 → 商品 → 商品明细)

How the data is obtained
------------------------
The page keeps its state in the query string as
``productDetailUrlParams`` — an LZ-string of
``{"proType":"spu","datepickerValue":{...}}``.  The SPA decompresses it on
startup and issues its own ``productTable.ajax`` call for exactly that window,
so the collector does three things and nothing else:

1. mint a URL carrying the window it wants (``lzstring_uri``, verified to
   reproduce JD's own 172-character parameter byte for byte);
2. navigate to it and let the page make its own request;
3. read that response off the wire with ``Network.getResponseBody``.

No request is ever constructed by the collector — no ``fetch`` replay, no
re-signed headers.  That matters because ``szgateway.jd.com`` rejects calls that
do not carry the ``user-mnp``/``user-mup``/``uuid`` triple the page issued
(``-402``/``-407 不安全的请求``), and because one page-issued request per store
and cycle is the smallest possible footprint.

The first paint is driven by the URL, not by the browser clock: shifting the
browser's clock does not move the window, which is why the window has to be
minted rather than inherited from whatever link the operator happened to copy.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import pathlib
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
import websocket

import esale_product
from jd_product360_analysis import LOGIN_MARKERS, NO_AUTH_MARKERS, NoPermissionError
from lzstring_uri import compress_to_encoded_uri_component


TZ = ZoneInfo("Asia/Shanghai")
COLLECTOR_VERSION = 2
PAGE_URL = "https://jdsz.jd.com/szweb/view/product/productDetail.html"
TABLE_API_PATTERN = "/api/lowcode/productDetail/table/productTable.ajax"
SUMMARY_SPU_ID = "合计"

# dateType values the gateway accepts for completed periods.  Mixed combinations
# such as {"dateType": "d7", "realtime": true} are rejected with code 10001, so
# realtime stays off.
DIMENSIONS = {"d7": 7, "d30": 30}

# Pause between two dimensions of the same run, so 商智 does not see two
# page-issued requests land back to back in one session.
INTER_DIMENSION_DELAY = 8.0

# Socket timeout for a CDP command reply.  ``pump`` polls with a 1-second timeout
# and restores this before every ``call``; without that a reply slower than one
# poll interval would be mistaken for a dead connection.
COMMAND_TIMEOUT = 30.0

# Indicator codes the 商品明细 table asks for; the constants below name the
# columns the rendered table shows instead of leaking the opaque codes.
AMOUNT = "jdr_sch_trade_deal_ord_ord_amt_sz_trade_deal_snapshot"
QUANTITY = "jdr_sch_trade_deal_ord_sku_qtty_sz_trade_deal_snapshot"
CONVERSION_RATE = "fo_jdr_sch_industry_deal_rate"
PAGE_VIEWS = "jdr_sch_traffic_brow_sku__page_qtty_traffic_plat_item_di_sz_bsg"
VISITORS = "jdr_sch_traffic_brow_sku__page_cnt_traffic_plat_item_di_sz_bsg"
INDICATORS = [AMOUNT, QUANTITY, CONVERSION_RATE, PAGE_VIEWS, VISITORS]

TWO_PLACES = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")


def window_for(date_type: str, today: dt.date) -> dict[str, str]:
    """Completed-period window: ends yesterday, compared with the previous span."""
    days = DIMENSIONS[date_type]
    end = today - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    compare_end = start - dt.timedelta(days=1)
    compare_start = compare_end - dt.timedelta(days=days - 1)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "compare_start": compare_start.isoformat(),
        "compare_end": compare_end.isoformat(),
    }


def mint_url(date_type: str, window: dict[str, str]) -> str:
    """Build the product-detail URL whose first paint asks for ``window``."""
    state = {
        "proType": "spu",
        "datepickerValue": {
            "currentKey": date_type,
            "currentValue": [window["start"], window["end"]],
            "compareKey": "hb",
            "compareValue": [window["compare_start"], window["compare_end"]],
        },
    }
    plain = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
    return f"{PAGE_URL}?productDetailUrlParams={compress_to_encoded_uri_component(plain)}"


class ProductDetailPage:
    """CDP tab that navigates to the product-detail page and reads its own XHR.

    Only the page's request is ever observed; the tab reports the request body so
    the caller can assert the page honoured the minted window, and the response
    body the page received.
    """

    def __init__(self, port: int, url: str):
        self.base = f"http://127.0.0.1:{port}"
        response = requests.put(f"{self.base}/json/new?{quote('about:blank', safe='')}", timeout=15)
        if response.status_code == 405:
            response = requests.get(f"{self.base}/json/new?{quote('about:blank', safe='')}", timeout=15)
        response.raise_for_status()
        self.target = response.json()
        self.ws = websocket.create_connection(self.target["webSocketDebuggerUrl"], timeout=COMMAND_TIMEOUT)
        self.message_id = 0
        self.request_id: str | None = None
        self.request: dict | None = None
        self.status: int | None = None
        self.finished = False
        self.last_body_error: str | None = None
        # A 500-row product table can exceed CDP's per-resource in-memory
        # buffer.  Once evicted, getResponseBody reports that the resource is
        # unavailable even though responseReceived/loadingFinished succeeded.
        self.call(
            "Network.enable",
            {"maxTotalBufferSize": 100_000_000, "maxResourceBufferSize": 25_000_000},
        )
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.navigate", {"url": url})

    def call(self, method: str, params: dict | None = None) -> dict:
        """Send a command, buffering any events that arrive before its reply.

        ``pump`` narrows the socket timeout to keep polling responsive; it is
        restored here first, because a command reply may legitimately take longer
        than one polling interval (a navigation or a slow evaluate).
        """
        self.ws.settimeout(COMMAND_TIMEOUT)
        self.message_id += 1
        request_id = self.message_id
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})
            self._record(message)

    def _record(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            if self.request_id is None and TABLE_API_PATTERN in (request.get("url") or ""):
                self.request_id = params.get("requestId")
                self.request = request
        elif method == "Network.responseReceived":
            if params.get("requestId") == self.request_id:
                self.status = (params.get("response") or {}).get("status")
        elif method == "Network.loadingFinished":
            if params.get("requestId") == self.request_id:
                self.finished = True

    def pump(self, seconds: float) -> None:
        self.ws.settimeout(1.0)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                raw = self.ws.recv()
            except Exception:
                continue
            try:
                message = json.loads(raw)
            except Exception:
                continue
            self._record(message)

    def evaluate(self, expression: str):
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True, "timeout": 60000},
        )
        remote = result.get("result") or {}
        if "value" not in remote:
            raise RuntimeError(remote.get("description") or "JD product-detail page returned no value")
        return remote["value"]

    def text(self) -> str:
        return self.evaluate("document.body && document.body.innerText || ''")

    def store_name(self) -> str | None:
        """Read the store name the 商智 header renders next to ``POP``."""
        lines = [line.strip() for line in self.text().splitlines()]
        for index, line in enumerate(lines):
            if line == "帮助":
                for candidate in lines[index + 1 : index + 4]:
                    if candidate and candidate != "POP":
                        return candidate
        return None

    def response_body(self) -> dict | None:
        """The page's own response body, or ``None`` while it is still buffering."""
        try:
            result = self.call("Network.getResponseBody", {"requestId": self.request_id})
        except RuntimeError as error:
            self.last_body_error = str(error)
            return None
        body = result.get("body") or ""
        if result.get("base64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def wait_for_table(self, *, timeout: float = 90) -> dict:
        """Return the JSON the page received, or raise after ``timeout`` seconds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(1.0)
            if self.request_id is None:
                text = self.text()
                if any(marker in text for marker in LOGIN_MARKERS):
                    raise RuntimeError("JD 商智 product-detail requires login or verification")
                if any(marker in text for marker in NO_AUTH_MARKERS):
                    raise NoPermissionError("JD 商智 is not authorized for the current JD account")
                continue
            if error := table_error(self):
                raise error
            if self.finished:
                payload = self.response_body()
                if payload is not None:
                    return payload
                # A request can be captured before the portal redirects to an
                # authentication/entitlement page. Do not hide this as a CDP
                # buffer timeout merely because a request ID already exists.
                text = self.text()
                if any(marker in text for marker in LOGIN_MARKERS):
                    raise RuntimeError("JD product detail requires login or verification")
                if any(marker in text for marker in NO_AUTH_MARKERS):
                    raise NoPermissionError("JD product detail is not authorized for this account")
        if self.request_id is None:
            raise RuntimeError("JD product-detail page never issued its own productTable.ajax request")
        detail = f": {self.last_body_error}" if self.last_body_error else ""
        raise RuntimeError(f"JD product-detail response body never became available{detail}")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass
        try:
            requests.get(f"{self.base}/json/close/{self.target['id']}", timeout=10)
        except requests.RequestException:
            pass


def table_error(page: ProductDetailPage) -> RuntimeError | None:
    """Surface a non-200 page response instead of waiting for a body that never comes."""
    if page.status is not None and page.status != 200:
        return RuntimeError(f"JD product-table HTTP error: {page.status}")
    return None


def request_body(request: dict) -> dict:
    try:
        parsed = json.loads(request.get("postData") or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def assert_window(request: dict, window: dict[str, str], date_type: str) -> None:
    """Fail loudly if the page asked for a different window than the minted URL."""
    body = request_body(request)
    actual = (body.get("dateType"), body.get("startDate"), body.get("endDate"))
    expected = (date_type, window["start"], window["end"])
    if actual != expected:
        raise RuntimeError(
            f"JD product-detail page ignored the requested window: asked for {actual}, expected {expected}"
        )


def request_page_size(request: dict) -> int | None:
    size = request_body(request).get("pageSize")
    try:
        return int(size) if size else None
    except (TypeError, ValueError):
        return None


def decimal_or_none(value: object, *, place: Decimal) -> Decimal | None:
    if value is None or str(value).strip() in {"", "-", "--"}:
        return None
    try:
        return Decimal(str(value)).quantize(place)
    except InvalidOperation as error:
        raise ValueError(f"invalid JD product-detail value: {value!r}") from error


def integer_or_none(value: object) -> int | None:
    if value is None or str(value).strip() in {"", "-", "--"}:
        return None
    try:
        return int(Decimal(str(value)))
    except InvalidOperation as error:
        raise ValueError(f"invalid JD product-detail quantity: {value!r}") from error


def item_id_from_url(pro_url: object) -> str | None:
    match = re.search(r"/item\.jd\.com/(\d+)\.html", str(pro_url or ""))
    return match.group(1) if match else None


def normalize_row(row: dict) -> dict:
    return {
        "spu_id": str(row["spu_id"]),
        "item_id": item_id_from_url(row.get("pro_url")),
        "item_num": str(row.get("item_num") or "").strip() or None,
        "name": str(row.get("name") or "").strip(),
        "quantity": integer_or_none(row.get(QUANTITY)),
        "amount": decimal_or_none(row.get(AMOUNT), place=TWO_PLACES),
        "conversion_rate": decimal_or_none(row.get(CONVERSION_RATE), place=SIX_PLACES),
        "page_views": integer_or_none(row.get(PAGE_VIEWS)),
        "visitors": integer_or_none(row.get(VISITORS)),
        "quantity_prev": integer_or_none(row.get(f"{QUANTITY}##compareValue")),
        "amount_prev": decimal_or_none(row.get(f"{AMOUNT}##compareValue"), place=TWO_PLACES),
        "cate_1": row.get("cate_1"),
        "cate_2": row.get("cate_2"),
        "cate_3": row.get("cate_3"),
    }


def parse_table_response(payload: dict, *, page_size: int | None = None) -> tuple[list[dict], dict]:
    """Return the product rows plus the ``合计`` summary row.

    A row without a SPU id is only acceptable for the summary row; any other
    unidentified row means the response structure changed, which fails the whole
    batch instead of silently dropping products.  When the page's own request
    declared a ``pageSize``, a full page is treated as truncation.
    """
    if payload.get("code") is not None:
        raise RuntimeError(f"JD product-detail API error: code={payload.get('code')} msg={payload.get('msg')}")
    header = payload.get("header") or {}
    if header.get("code") != 0:
        raise RuntimeError(f"JD product-detail API error: code={header.get('code')} desc={header.get('desc')}")
    data = (payload.get("body") or {}).get("data")
    if not isinstance(data, list) or not data:
        raise RuntimeError("JD product-detail response contains no data rows")

    rows: list[dict] = []
    summary: dict | None = None
    for raw in data:
        if not isinstance(raw, dict):
            raise RuntimeError("JD product-detail response row is not an object")
        spu_id = str(raw.get("spu_id") or "").strip()
        if not spu_id or spu_id == "None":
            raise RuntimeError("JD product-detail response row has no SPU id")
        if spu_id == SUMMARY_SPU_ID or "$summary" in raw:
            summary = normalize_row(raw)
            continue
        normalized = normalize_row(raw)
        if not normalized["name"]:
            raise RuntimeError(f"JD product-detail row {normalized['spu_id']} has no product name")
        rows.append(normalized)
    if summary is None:
        raise RuntimeError("JD product-detail response has no summary row")
    if page_size is not None and len(data) >= page_size:
        raise RuntimeError(f"JD product-detail response may be truncated: {len(data)} rows for pageSize={page_size}")
    return rows, summary


def collect(port: int, date_type: str = "d7") -> dict:
    if date_type not in DIMENSIONS:
        raise ValueError(f"unsupported JD product-detail dimension: {date_type}")
    today = dt.datetime.now(TZ).date()
    window = window_for(date_type, today)
    page = ProductDetailPage(port, mint_url(date_type, window))
    try:
        payload = page.wait_for_table()
        request = page.request or {}
        assert_window(request, window, date_type)
        rows, summary = parse_table_response(payload, page_size=request_page_size(request))
        return {
            "store": page.store_name(),
            "dimension": date_type,
            "window": window,
            "rows": rows,
            "summary": summary,
        }
    finally:
        page.close()


def sales_rows(collected: dict) -> list[dict]:
    """Shape one dimension's results for ``esale_product``: 唯一编码 is the JD SPU id."""
    return [
        {"code": row["spu_id"], "name": row["name"], "quantity": row["quantity"], "amount": row["amount"]}
        for row in collected["rows"]
    ]


def merge_dimensions(dimension_rows: dict[str, list[dict]]) -> list[dict]:
    """Fold several dimensions into one row per SPU carrying both windows.

    ``dimension_rows`` maps a dimension code (``d7``/``d30``) to that dimension's
    ``sales_rows`` output.  A SPU that only appears in one dimension still gets a
    row: the other window stays NULL, which is honest — a product with no sales in
    a window is absent from that window's table rather than reported as zero.

    ``销量`` carries the shorter window and ``30天销量`` the 30-day one, matching
    how the Meituan feed names the columns.
    """
    short_field = {"d7": "quantity", "d1": "quantity"}
    merged: dict[str, dict] = {}
    for dimension, rows in dimension_rows.items():
        for row in rows:
            entry = merged.setdefault(
                row["code"],
                {"code": row["code"], "name": row["name"]},
            )
            if dimension == "d30":
                entry["quantity_30"] = row.get("quantity")
            elif dimension in short_field:
                entry["quantity"] = row.get("quantity")
                entry["amount"] = row.get("amount")

    def order(entry: dict) -> tuple:
        return (-(entry.get("quantity") or 0), -(entry.get("quantity_30") or 0), entry["name"])

    return sorted(merged.values(), key=order)


def collected_today(state_file: pathlib.Path, today: dt.date, dimensions: list[str]) -> bool:
    """True when this store's snapshot for ``today`` has already been taken.

    The collector rides a cycle that also runs every ten minutes, so the daily
    cadence lives in this state file rather than in the timer: reopening the
    page 144 times a day would be pointless load on 商智.
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
        and list(state.get("dimensions") or []) == dimensions
    )


def write_state(state_file: pathlib.Path, payload: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_name(state_file.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(temporary, state_file)


def parse_dimensions(value: str) -> list[str]:
    """Split ``--dimension`` into an ordered, de-duplicated list of date types.

    ``d7,d30`` collects both windows in one run so a single ``esale_product``
    slice ends up carrying 销量 and 30天销量 for the same SPU.  Each dimension is
    still exactly one page-issued request, which is what keeps the 商智 risk
    footprint minimal.
    """
    dimensions = [part.strip() for part in str(value).split(",") if part.strip()]
    if not dimensions:
        raise ValueError("--dimension must name at least one dimension")
    unknown = sorted(set(dimensions) - set(DIMENSIONS))
    if unknown:
        raise ValueError(f"unsupported JD product-detail dimension(s): {unknown}")
    seen: list[str] = []
    for dimension in dimensions:
        if dimension not in seen:
            seen.append(dimension)
    return seen


def run(args: argparse.Namespace) -> dict:
    if args.persist and not args.config:
        raise ValueError("--persist requires --config")
    dimensions = parse_dimensions(args.dimension)
    collected_at = dt.datetime.now(TZ).replace(microsecond=0)
    if args.state_file and collected_today(args.state_file, collected_at.date(), dimensions):
        return {
            "state": "skipped",
            "collected_at": collected_at.isoformat(),
            "port": args.port,
            "reason": f"already collected on {collected_at.date().isoformat()}",
            "state_file": str(args.state_file),
        }
    collected: dict[str, dict] = {}
    for index, dimension in enumerate(dimensions):
        if index:
            # Two page-issued requests in one session is still a tiny footprint,
            # but 商智's risk control is sensitive to back-to-back calls (a rapid
            # burst previously produced a -402 wobble).  A short pause between
            # dimensions keeps a d7,d30 run comfortably under that threshold.
            time.sleep(INTER_DIMENSION_DELAY)
        collected[dimension] = collect(args.port, dimension)
    first = collected[dimensions[0]]
    dimension_rows = {dimension: sales_rows(payload) for dimension, payload in collected.items()}
    rows = merge_dimensions(dimension_rows) if len(dimensions) > 1 else dimension_rows[dimensions[0]]
    result: dict = {
        "state": "dry_run",
        "collected_at": collected_at.isoformat(),
        "port": args.port,
        "store": first["store"],
        "dimensions": dimensions,
        "windows": {dimension: payload["window"] for dimension, payload in collected.items()},
        "product_rows": len(rows),
        "quantity_total": sum(row.get("quantity") or 0 for row in rows),
        "quantity_30_total": sum(row.get("quantity_30") or 0 for row in rows),
        "amount_total": str(sum((row.get("amount") or Decimal("0") for row in rows), Decimal("0"))),
        "out": str(args.out) if args.out else None,
        "persisted": None,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "platform": "京东",
                    "store": first["store"],
                    "dimensions": dimensions,
                    "windows": result["windows"],
                    "collected_at": collected_at.isoformat(),
                    "summaries": {dimension: payload["summary"] for dimension, payload in collected.items()},
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
        config = json.loads(args.config.read_text(encoding="utf-8"))
        client, platform = esale_product.settings(config)
        schema = esale_product.schema_of(config)
        connection = esale_product.connect(config, schema=schema)
        try:
            esale_product.ensure_table(connection, schema=schema)
            result["persisted"] = esale_product.replace(
                connection,
                schema=schema,
                client=client,
                platform=platform,
                day=collected_at.date(),
                rows=rows,
                # Default pruning is correct here: this one run already carries
                # both windows (销量 from d7, 30天销量 from d30), so it owns every
                # column of the 京东 slice and may drop whatever it no longer lists.
                # The Meituan feeds are the opposite case — see their prune=False.
            )
        finally:
            connection.close()
        result["state"] = "persisted"
    if args.state_file:
        write_state(
            args.state_file,
            {
                "state": result["state"],
                "date": collected_at.date().isoformat(),
                "collector_version": COLLECTOR_VERSION,
                "store": first["store"],
                "dimensions": dimensions,
                "windows": result["windows"],
                "product_rows": len(rows),
                "persisted": result["persisted"],
                "collected_at": collected_at.isoformat(),
            },
        )
        result["state_file"] = str(args.state_file)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect JD 商智 product-detail sales rows by listening")
    parser.add_argument("--port", required=True, type=int, help="CDP port of the logged-in store browser")
    parser.add_argument("--dimension", default="d7",
                        help="comma-separated dimensions: d7 = 近7天 (default), d30 = 近30天; "
                             "e.g. d7,d30 collects both into 销量 and 30天销量")
    parser.add_argument("--config", type=pathlib.Path, help="store config.json, required by --persist")
    parser.add_argument("--persist", action="store_true", help="overwrite this store's esale_product slice")
    parser.add_argument("--state-file", type=pathlib.Path,
                        help="skip the run when this file already records today's snapshot")
    parser.add_argument("--out", type=pathlib.Path, help="also write the collected rows to this JSON file")
    return parser.parse_args()


def main() -> int:
    try:
        result = run(parse_args())
    except NoPermissionError as error:
        print(json.dumps({"state": "no_permission", "detail": str(error)}, ensure_ascii=False, indent=2))
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
