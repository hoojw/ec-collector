#!/usr/bin/env python3
"""Collect Meituan SKU-rank real-time TOP3 lowest discount price by UPC.

Source API: /api/page/bizdata/b2c/productAnalysis/skuRank/realtime/detail
Result table: adjust_price.mt_Top3LowestDiscountPrice
              (upc, Top3LowestDiscountPrice, updated_at)

The result table keeps only the latest observed value per UPC. Each run upserts
every collected UPC, so rows written today are overwritten by tomorrow's run and
updated_at records when the current value was last observed.
The collector never invents a price: a record whose UPC or price is missing or
unparsable fails the whole run instead of writing a partial snapshot.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, quote
from zoneinfo import ZoneInfo

import pymysql
import requests
import websocket


LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
TZ = ZoneInfo("Asia/Shanghai")
TARGET_URL = "https://yiyao.meituan.com/main/frame#/page/analysis/product/b2c"
DETAIL_PATH = "/api/page/bizdata/b2c/productAnalysis/skuRank/realtime/detail"
OUTPUT_SCHEMA_DEFAULT = "adjust_price"
OUTPUT_TABLE_DEFAULT = "mt_Top3LowestDiscountPrice"
PRICE_FIELD = "gtvSoldTop3LowestDiscountPrice"
UPC_FIELD = "upcCode"


def quote_ident(name: str) -> str:
    if not name:
        raise ValueError("identifier cannot be empty")
    return "`" + name.replace("`", "``") + "`"


def load_config(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def compact(value: dt.date) -> str:
    return value.strftime("%Y%m%d")


def compare_date_for(now: dt.datetime) -> dt.date:
    """The API's compareDate is the previous day."""
    return now.date() - dt.timedelta(days=1)


def parse_price(value: object, *, field: str) -> Decimal:
    text = str(value or "").strip().replace(",", "")
    if not text:
        raise ValueError(f"missing {field}")
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"invalid {field}: {value!r}") from error


class CdpPage:
    """Disposable CDP target so navigation cannot disturb the landing page."""

    def __init__(self, port: int, url: str):
        self.base = f"http://127.0.0.1:{port}"
        self.message_id = 0
        self.tab_id = None
        self.ws = None
        last_error: Exception | None = None
        for attempt in range(3):
            target = None
            try:
                encoded = quote(url, safe="")
                response = requests.put(f"{self.base}/json/new?{encoded}", timeout=15)
                if response.status_code == 405:
                    response = requests.get(f"{self.base}/json/new?{encoded}", timeout=15)
                response.raise_for_status()
                target = response.json()
                self.tab_id = target["id"]
                self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=20)
                self.call("Runtime.enable")
                time.sleep(3)
                return
            except (requests.RequestException, websocket.WebSocketException, OSError) as error:
                last_error = error
                if self.ws is not None:
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    self.ws = None
                if target is not None:
                    try:
                        requests.get(f"{self.base}/json/close/{target['id']}", timeout=10)
                    except requests.RequestException:
                        pass
                if attempt < 2:
                    time.sleep(2)
        raise RuntimeError(f"Meituan TOP3 CDP target failed after 3 attempts: {last_error}")

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
        if self.tab_id is not None:
            try:
                requests.get(f"{self.base}/json/close/{self.tab_id}", timeout=10)
            except requests.RequestException:
                pass

    def call(self, method: str, params=None):
        self.message_id += 1
        payload = {"id": self.message_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.ws.send(json.dumps(payload))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != self.message_id:
                continue
            if "error" in message:
                raise RuntimeError(message["error"])
            return message.get("result")

    def run_js(self, expression: str):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": 60000,
            },
        )
        remote = result.get("result", {})
        if "value" in remote:
            return remote["value"]
        raise RuntimeError(remote.get("description") or remote)


def fetch_page(tab: CdpPage, compare_date: dt.date, page_num: int, page_size: int) -> dict:
    params = {
        "compareDate": compact(compare_date),
        "pageNum": page_num,
        "pageSize": page_size,
        "keyword": "",
        "skuCategoryIds": "",
        "productStatus": "",
        "yodaReady": "h5",
        "csecplatform": "4",
        "csecversion": "4.3.0",
    }
    url = f"{DETAIL_PATH}?{urlencode(params)}"
    raw = tab.run_js(
        f"""
        (async () => {{
          const response = await fetch({json.dumps(url)}, {{credentials:'include'}});
          const text = await response.text();
          return JSON.stringify({{httpStatus: response.status, text}});
        }})()
        """
    )
    if not isinstance(raw, str):
        raise RuntimeError(f"Meituan TOP3 API returned non-text result: {raw!r}")
    envelope = json.loads(raw)
    if envelope.get("httpStatus") != 200:
        raise RuntimeError(f"Meituan TOP3 API HTTP error: {envelope.get('httpStatus')}")
    try:
        payload = json.loads(envelope.get("text") or "")
    except json.JSONDecodeError as error:
        raise RuntimeError("Meituan TOP3 API returned non-JSON content") from error
    if payload.get("code") != 0:
        raise RuntimeError(
            f"Meituan TOP3 API business error: code={payload.get('code')} "
            f"msg={payload.get('message') or payload.get('msg')}"
        )
    return payload


def normalize_rows(records: list[dict]) -> list[tuple[str, Decimal]]:
    rows: list[tuple[str, Decimal]] = []
    for record in records:
        upc = str(record.get(UPC_FIELD) or "").strip()
        if not upc:
            logging.warning(
                "Skipping invalid Meituan TOP3 product: product_id=%r reason=missing %s",
                record.get("productId"),
                UPC_FIELD,
            )
            continue
        try:
            price = parse_price(record.get(PRICE_FIELD), field=PRICE_FIELD)
        except ValueError as error:
            logging.warning(
                "Skipping invalid Meituan TOP3 product: product_id=%r upc=%s reason=%s",
                record.get("productId"),
                upc,
                error,
            )
            continue
        rows.append((upc, price))
    return rows


def collect_rows(tab: CdpPage, compare_date: dt.date, page_size: int) -> list[tuple[str, Decimal]]:
    rows: list[tuple[str, Decimal]] = []
    seen_upcs: set[str] = set()
    received_records = 0
    page_num = 1
    expected_total: int | None = None
    while page_num <= 1000:
        payload = fetch_page(tab, compare_date, page_num, page_size)
        data = payload.get("data") or {}
        records = data.get("currentPageData")
        if not isinstance(records, list):
            raise RuntimeError("Meituan TOP3 API page has no currentPageData list")
        total = data.get("totalSum")
        try:
            total = int(total)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid TOP3 totalSum: {total!r}") from error
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RuntimeError("Meituan TOP3 total changed during pagination")

        received_records += len(records)
        for row in normalize_rows(records):
            if row[0] in seen_upcs:
                raise ValueError(f"duplicate UPC in TOP3 response: {row[0]!r}")
            seen_upcs.add(row[0])
            rows.append(row)

        if received_records >= expected_total:
            break
        if not records:
            raise RuntimeError(
                f"Meituan TOP3 pagination ended early: got={received_records}, expected={expected_total}"
            )
        page_num += 1
    else:
        raise RuntimeError("Meituan TOP3 pagination exceeded 1000 pages")

    if expected_total is None or received_records != expected_total:
        raise RuntimeError(
            f"Meituan TOP3 pagination incomplete: got={received_records}, expected={expected_total}"
        )
    return rows


def ensure_table(conn, table: str) -> None:
    table_q = quote_ident(table)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {table_q} ("
            "`upc` VARCHAR(32) NOT NULL, "
            "`Top3LowestDiscountPrice` DECIMAL(10,2) NULL, "
            "`updated_at` DATETIME NULL, "
            "PRIMARY KEY (`upc`)"
            ") DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
        columns = {row[0] for row in cur.fetchall()}
        if "updated_at" not in columns:
            cur.execute(
                f"ALTER TABLE {table_q} ADD COLUMN `updated_at` DATETIME NULL "
                "AFTER `Top3LowestDiscountPrice`"
            )
            columns.add("updated_at")
    missing = {"upc", "Top3LowestDiscountPrice", "updated_at"} - columns
    if missing:
        raise ValueError(f"{table} is missing required columns: {sorted(missing)}")


def upsert_rows(conn, table: str, rows: list[tuple[str, Decimal]], observed_at: dt.datetime) -> int:
    if not rows:
        return 0
    table_q = quote_ident(table)
    payload = [(upc, price, observed_at) for upc, price in rows]
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {table_q} (`upc`, `Top3LowestDiscountPrice`, `updated_at`) "
            "VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE `Top3LowestDiscountPrice`=VALUES(`Top3LowestDiscountPrice`), "
            "`updated_at`=VALUES(`updated_at`)",
            payload,
        )
        return cur.rowcount


def write_state(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run(args: argparse.Namespace) -> dict:
    config = load_config(args.config)
    started = dt.datetime.now(TZ).replace(microsecond=0)
    compare_date = compare_date_for(started)
    state_file = args.state_file
    if state_file:
        write_state(
            state_file,
            {
                "state": "running",
                "started_at": started.isoformat(),
                "compare_date": compare_date.isoformat(),
            },
        )
    try:
        tab = CdpPage(args.port, args.url or TARGET_URL)
        try:
            rows = collect_rows(tab, compare_date, args.page_size)
        finally:
            tab.close()
        observed_at = dt.datetime.now(TZ).replace(tzinfo=None, microsecond=0)
        result = {
            "state": "success",
            "compare_date": compare_date.isoformat(),
            "started_at": started.isoformat(),
            "finished_at": dt.datetime.now(TZ).replace(microsecond=0).isoformat(),
            "rows": len(rows),
        }
        if not args.dry_run:
            mysql_config = config["mysql"]
            # The table is platform-shared and always lives in adjust_price,
            # even when the store config points at its own per-store database.
            schema = config.get("tables", {}).get("top3_schema", OUTPUT_SCHEMA_DEFAULT)
            table = config.get("tables", {}).get("top3_lowest_discount_price", OUTPUT_TABLE_DEFAULT)
            conn = pymysql.connect(
                host=mysql_config["host"],
                port=int(mysql_config["port"]),
                user=mysql_config["user"],
                password=mysql_config["password"],
                database=schema,
                charset=mysql_config.get("charset", "utf8mb4"),
                autocommit=False,
            )
            try:
                ensure_table(conn, table)
                result["written"] = upsert_rows(conn, table, rows, observed_at)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        if state_file:
            write_state(state_file, result)
        return result
    except Exception as error:
        if state_file:
            write_state(
                state_file,
                {
                    "state": "failed",
                    "compare_date": compare_date.isoformat(),
                    "started_at": started.isoformat(),
                    "finished_at": dt.datetime.now(TZ).replace(microsecond=0).isoformat(),
                    "message": str(error),
                },
            )
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Meituan TOP3 lowest discount price by UPC from the logged-in browser."
    )
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--state-file", type=pathlib.Path)
    parser.add_argument("--url", default=TARGET_URL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-file", type=pathlib.Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    handlers = [logging.StreamHandler()]
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=handlers)
    result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
