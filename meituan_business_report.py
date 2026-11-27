#!/usr/bin/env python3
"""Collect Meituan business-report metrics through the logged-in browser session."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import pymysql
import requests
import websocket


TZ = ZoneInfo("Asia/Shanghai")
TARGET_URL = "https://yiyao.meituan.com/main/frame#/page/analysis/home/b2c"
CORE_PATH = "/api/page/bizdata/manageHome/single/b/core"
TABLE_DEFAULT = "ops_business_report"


class CdpPage:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        response = requests.put(
            f"{self.base}/json/new?{quote('about:blank', safe='')}", timeout=15
        )
        if response.status_code == 405:
            response = requests.get(
                f"{self.base}/json/new?{quote('about:blank', safe='')}", timeout=15
            )
        response.raise_for_status()
        target = response.json()
        self.tab_id = target["id"]
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=60)
        self.message_id = 0

    def call(self, method: str, params: dict | None = None):
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
            return message.get("result") or {}

    def evaluate(self, expression: str):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": 60000,
            },
        )
        remote = result.get("result") or {}
        if "value" not in remote:
            raise RuntimeError(remote.get("description") or remote)
        return remote["value"]

    def open_report(self) -> None:
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.navigate", {"url": TARGET_URL})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = self.evaluate(
                """
                (() => {
                  const frame=document.querySelector('iframe#hashframe');
                  const text=(frame && frame.contentDocument && frame.contentDocument.body
                    ? frame.contentDocument.body.innerText : '') || '';
                  return {
                    ready:text.includes('经营报告') && text.includes('成交金额'),
                    challenged:/验证码|风险|访问受限/.test(text.slice(0,4000))
                  };
                })()
                """
            )
            if state.get("challenged"):
                raise RuntimeError("Meituan business report is challenged")
            if state.get("ready"):
                return
            time.sleep(1)
        raise RuntimeError("Meituan business report did not finish loading")

    def fetch_json(self, path: str, params: dict) -> dict:
        url = f"{path}?{urlencode(params)}"
        expression = f"""
        (async () => {{
          const response=await fetch({json.dumps(url)}, {{credentials:'include'}});
          return JSON.stringify({{status:response.status, body:await response.text()}});
        }})()
        """
        envelope = json.loads(self.evaluate(expression))
        if envelope.get("status") != 200:
            raise RuntimeError(f"Meituan business-report HTTP error: {envelope.get('status')}")
        try:
            payload = json.loads(envelope.get("body") or "")
        except json.JSONDecodeError as error:
            raise RuntimeError("Meituan business-report API returned non-JSON content") from error
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Meituan business-report API error: code={payload.get('code')} msg={payload.get('msg')}"
            )
        return payload

    def close(self) -> None:
        try:
            self.ws.close()
        finally:
            try:
                requests.get(f"{self.base}/json/close/{self.tab_id}", timeout=10)
            except Exception:
                pass


def load_config(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def report_periods(now: dt.datetime) -> list[dict]:
    today = now.date()
    yesterday = today - dt.timedelta(days=1)
    previous_day = yesterday - dt.timedelta(days=1)
    near_7_end = yesterday
    near_7_start = near_7_end - dt.timedelta(days=6)
    compare_near_7_end = near_7_start - dt.timedelta(days=1)
    compare_near_7_start = compare_near_7_end - dt.timedelta(days=6)

    def compact(value: dt.date) -> str:
        return value.strftime("%Y%m%d")

    return [
        {
            "label": "实时",
            "dateType": "realTime",
            "beginTime": compact(today),
            "endTime": compact(today),
            "compareBeginTime": compact(yesterday),
            "compareEndTime": compact(yesterday),
        },
        {
            "label": "按日",
            "dateType": "dt",
            "beginTime": compact(yesterday),
            "endTime": compact(yesterday),
            "compareBeginTime": compact(previous_day),
            "compareEndTime": compact(previous_day),
        },
        {
            "label": "近7天",
            "dateType": "last_7_days",
            "beginTime": compact(near_7_start),
            "endTime": compact(near_7_end),
            "compareBeginTime": compact(compare_near_7_start),
            "compareEndTime": compact(compare_near_7_end),
        },
    ]


def decimal_metric(data: dict, section: str, field: str) -> Decimal:
    value = (data.get(section) or {}).get(field)
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"invalid business-report metric {section}.{field}: {value!r}") from error


def integer_metric(data: dict, section: str, field: str) -> int:
    value = decimal_metric(data, section, field)
    if value != value.to_integral_value():
        raise ValueError(f"non-integer business-report metric {section}.{field}: {value!r}")
    return int(value)


def normalize_report(label: str, payload: dict) -> tuple:
    data = payload.get("data") or {}
    return (
        label,
        decimal_metric(data, "dealAmount", "amount"),
        integer_metric(data, "transactionVolume", "turnover"),
        decimal_metric(data, "singleAveragePrice", "averagePrice"),
        decimal_metric(data, "refundAmount", "amount"),
        integer_metric(data, "allRefundOrderQuantity", "turnover"),
        integer_metric(data, "refundOrderQuantity", "turnover"),
    )


def quote_ident(name: str) -> str:
    if not name:
        raise ValueError("identifier cannot be empty")
    return "`" + name.replace("`", "``") + "`"


def ensure_table(conn, table: str) -> None:
    table_q = quote_ident(table)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {table_q} ("
            "`时间` VARCHAR(64) NOT NULL, "
            "`成交金额` DECIMAL(18,2) NOT NULL, "
            "`成交订单量` INT UNSIGNED NOT NULL, "
            "`单均价` DECIMAL(18,2) NOT NULL, "
            "`退款金额` DECIMAL(18,2) NOT NULL, "
            "`退款订单量(全部退款+部分退款)` INT UNSIGNED NOT NULL, "
            "`退款订单量(全部退款)` INT UNSIGNED NOT NULL, "
            "PRIMARY KEY (`时间`)"
            ") DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s ORDER BY ordinal_position",
            (table,),
        )
        columns = [row[0] for row in cur.fetchall()]
    expected = [
        "时间",
        "成交金额",
        "成交订单量",
        "单均价",
        "退款金额",
        "退款订单量(全部退款+部分退款)",
        "退款订单量(全部退款)",
    ]
    if columns != expected:
        raise ValueError(f"unexpected {table} columns: {columns}")


def write_rows(conn, table: str, rows: list[tuple]) -> None:
    labels = [row[0] for row in rows]
    if labels != ["实时", "按日", "近7天"]:
        raise ValueError("business report replacement requires real-time, daily, and last-7-days rows")
    table_q = quote_ident(table)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {table_q}")
        cur.executemany(
            f"INSERT INTO {table_q} ("
            "`时间`, `成交金额`, `成交订单量`, `单均价`, `退款金额`, "
            "`退款订单量(全部退款+部分退款)`, `退款订单量(全部退款)`) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            rows,
        )


def write_state(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def run(args: argparse.Namespace) -> dict:
    now = dt.datetime.now(TZ).replace(microsecond=0)
    page = CdpPage(args.port)
    try:
        page.open_report()
        rows = []
        for period in report_periods(now):
            params = {"businessType": 2, "storeType": 1, **{k: v for k, v in period.items() if k != "label"}}
            rows.append(normalize_report(period["label"], page.fetch_json(CORE_PATH, params)))
    finally:
        page.close()

    result = {
        "state": "success",
        "collected_at": now.isoformat(),
        "rows": len(rows),
        "periods": [row[0] for row in rows],
    }
    if not args.dry_run:
        config = load_config(args.config)
        mysql = config["mysql"]
        table = config.get("tables", {}).get("meituan_business_report", TABLE_DEFAULT)
        conn = pymysql.connect(
            host=mysql["host"],
            port=int(mysql["port"]),
            user=mysql["user"],
            password=mysql["password"],
            database=mysql["database"],
            charset=mysql.get("charset", "utf8mb4"),
            autocommit=False,
        )
        try:
            ensure_table(conn, table)
            write_rows(conn, table, rows)
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
    parser = argparse.ArgumentParser(description="Sync Meituan business-report metrics")
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--state-file", type=pathlib.Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    result = run(parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
