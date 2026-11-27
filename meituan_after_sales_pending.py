#!/usr/bin/env python3
"""Collect Meituan after-sales orders and refresh unfinished historical cases."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import socket
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import pymysql
import requests
import websocket

from workbench import qualified as qualified_workbench
from workbench import target as workbench_target


TZ = ZoneInfo("Asia/Shanghai")
TARGET_URL = "https://yiyao.meituan.com/main/frame#/page/b2c/pc/after-sales"
LIST_PATH = "/health/b2c/fulfillment/refund/list/search"
COUNT_PATH = "/health/b2c/fulfillment/refund/list/count"
DETAIL_PATH = "/health/b2c/fulfillment/refund/detail/query"
MAIN_TABLE = "ops_after_sale_review_daily"
NODE_TABLE = "ops_after_sale_review_node"
DEFAULT_CLIENT = "保民"
DEFAULT_OWNER = "待维护"
DEFAULT_PLATFORM = "美团"
CAPTURE_TARGET_ATTEMPTS = 2
CAPTURE_LOAD_ATTEMPTS = 2
CAPTURE_WAIT_SECONDS = 15


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

    def close(self) -> None:
        try:
            self.ws.close()
        finally:
            try:
                requests.get(f"{self.base}/json/close/{self.tab_id}", timeout=10)
            except Exception:
                pass

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
            return message.get("result")

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
        remote = result.get("result", {})
        if "value" not in remote:
            raise RuntimeError(remote.get("description") or remote)
        return remote["value"]

    def capture_templates(self) -> tuple[dict, dict]:
        self.call("Network.enable")
        self.call("Network.setCacheDisabled", {"cacheDisabled": True})
        self.call("Page.enable")
        requests_seen: dict[str, dict] = {}
        responses_seen: dict[str, dict] = {}
        self.ws.settimeout(1)
        try:
            for load_attempt in range(CAPTURE_LOAD_ATTEMPTS):
                # Navigation acknowledgements can legitimately take longer than
                # the one-second polling interval used for Network events.  A
                # slow acknowledgement must not fail the whole collection before
                # the request-capture retry loop has even started.
                self.ws.settimeout(60)
                try:
                    if load_attempt == 0:
                        self.call("Page.navigate", {"url": TARGET_URL})
                    else:
                        self.call("Page.reload", {"ignoreCache": True})
                finally:
                    self.ws.settimeout(1)
                deadline = time.monotonic() + CAPTURE_WAIT_SECONDS
                while time.monotonic() < deadline and len(responses_seen) < 2:
                    try:
                        message = json.loads(self.ws.recv())
                    except (socket.timeout, websocket.WebSocketTimeoutException):
                        continue
                    method = message.get("method")
                    params = message.get("params") or {}
                    if method == "Network.requestWillBeSent":
                        request = params.get("request") or {}
                        path = urlsplit(request.get("url") or "").path
                        if path in (LIST_PATH, COUNT_PATH):
                            requests_seen[params["requestId"]] = {
                                "path": path,
                                "url": request.get("url"),
                                "method": request.get("method"),
                                "body": json.loads(request.get("postData") or "{}")
                                if request.get("postData")
                                else None,
                            }
                    elif (
                        method == "Network.responseReceived"
                        and params.get("requestId") in requests_seen
                    ):
                        responses_seen[params["requestId"]] = params.get("response") or {}
                if {item["path"] for item in requests_seen.values()} >= {LIST_PATH, COUNT_PATH}:
                    break
        finally:
            self.ws.settimeout(60)

        by_path = {item["path"]: item for item in requests_seen.values()}
        missing = {LIST_PATH, COUNT_PATH} - set(by_path)
        if not missing:
            # Observing both authenticated application requests is stronger
            # evidence than exact UI copy, which Meituan changes frequently.
            return by_path[LIST_PATH], by_path[COUNT_PATH]

        login_ok = self.evaluate(
            """
            (() => {
              const frame=document.querySelector('iframe#hashframe');
              const text=(frame && frame.contentDocument && frame.contentDocument.body
                ? frame.contentDocument.body.innerText : document.body.innerText) || '';
              return {
                ok:text.includes('售后订单概览') && text.includes('售后订单查询'),
                challenge:/验证码|风险|访问受限/.test(text.slice(0,4000))
              };
            })()
            """
        )
        if not login_ok.get("ok"):
            # A genuinely challenged/login wall never renders the after-sales
            # overview copy.  When the overview DID render, stray words such as
            # a "风险"-labelled row inside a large refund list must not be
            # mistaken for an anti-bot challenge.
            detail = "is challenged" if login_ok.get("challenge") else "is not logged in"
            raise RuntimeError(f"Meituan after-sales page {detail}")
        raise RuntimeError(
            "required after-sales requests not observed after "
            f"{CAPTURE_LOAD_ATTEMPTS} loads: {sorted(missing)}"
        )

    def fetch_json(self, url: str, method: str, body: dict | None = None) -> dict:
        options: dict = {"method": method, "credentials": "include"}
        if body is not None:
            options["headers"] = {"Content-Type": "application/json"}
            options["body"] = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        expression = f"""
        (async () => {{
          const response=await fetch({json.dumps(url)}, {json.dumps(options, ensure_ascii=False)});
          return await response.text();
        }})()
        """
        raw = self.evaluate(expression)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("Meituan after-sales API returned non-JSON content") from error
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Meituan after-sales API error: code={payload.get('code')} message={payload.get('message')}"
            )
        return payload


def load_config(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def workbench_store_settings(config: dict) -> dict[str, str]:
    store = config.get("meituan_store") or {}
    return {
        "client": str(store.get("client") or DEFAULT_CLIENT).strip(),
        "owner": str(store.get("owner") or DEFAULT_OWNER).strip(),
        "platform": str(store.get("platform") or DEFAULT_PLATFORM).strip(),
    }


def write_state(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def datetime_from_millis(value) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        timestamp = int(value)
        if abs(timestamp) >= 100_000_000_000:
            timestamp /= 1000
        return dt.datetime.fromtimestamp(timestamp, TZ).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def process_node_time(value) -> dt.datetime | None:
    parsed = datetime_from_millis(value)
    if parsed:
        return parsed
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def safe_application_reason(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"(?<!\d)1\d{10}(?!\d)", "[已脱敏手机号]", text)
    return text[:500]


def normalize_node(title: object, value: object) -> tuple[str, str, dt.datetime] | None:
    raw_title = str(title or "").strip()
    node_time = process_node_time(value)
    if not raw_title or not node_time:
        return None
    if re.search(r"(?<!\d)1\d{10}(?!\d)", raw_title) or any(
        marker in raw_title for marker in ("收件人", "退货地址", "联系电话")
    ):
        raise ValueError("after-sales node title contains personal information")
    if raw_title.startswith("顾客"):
        subject, action = "顾客", raw_title[2:]
    elif raw_title.startswith("商家"):
        subject, action = "商家", raw_title[2:]
    else:
        subject, action = "系统", raw_title
    action = action.split("，", 1)[0].strip()
    if action == "系统自动退款":
        action = "自动退款"
    if not action:
        raise ValueError("after-sales node action is empty")
    return subject, action[:128], node_time


def build_detail_url(list_url: str, order_id: str, after_sale_id: str) -> str:
    parts = urlsplit(list_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    store_id = query.get("wmPoiId") or query.get("poiId") or ""
    query.update(
        {
            "orderViewId": order_id,
            "refundViewId": after_sale_id,
            "poiId": store_id,
            "bizScene": query.get("bizScene") or "pc",
            "sceneType": query.get("sceneType") or "103",
            "yodaReady": query.get("yodaReady") or "h5",
            "csecplatform": query.get("csecplatform") or "4",
            "csecversion": query.get("csecversion") or "4.3.0",
        }
    )
    return urlunsplit((parts.scheme, parts.netloc, DETAIL_PATH, urlencode(query), ""))


def decimal_amount(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValueError("refund amount is invalid")


def normalize_after_sale_status(value: object) -> str:
    parts = [part.strip() for part in str(value or "").split(",") if part.strip()]
    if not parts:
        raise ValueError("after-sales row is missing status description")
    final_status = parts[-1]
    interception_status = next(
        (part for part in reversed(parts[:-1]) if part.startswith("拦截")),
        None,
    )
    normalized = (
        f"{interception_status},{final_status}"
        if interception_status
        else final_status
    )
    return normalized[:64]


def normalize_order(item: dict, detail_payload: dict) -> tuple[dict, list[tuple]]:
    order = item.get("refundOrder") or {}
    after_sale_id = str(order.get("refundViewId") or "").strip()
    if not after_sale_id:
        raise ValueError("after-sales row is missing after-sale ID")
    apply_time = datetime_from_millis(order.get("applyTime"))
    if not apply_time:
        raise ValueError("after-sales row is missing application time")
    detail = ((detail_payload or {}).get("data") or {}).get("refundDetailView") or {}
    products = []
    for product in order.get("applyRefundProducts") or []:
        name = str(product.get("productName") or "").strip()
        spec = str(product.get("spec") or "").strip()
        quantity = str(product.get("count") or "").strip()
        if name:
            products.append(" ".join(part for part in (name, spec, f"x{quantity}" if quantity else "") if part))
    service_type = order.get("serviceType")
    refund_type = detail.get("refundTypeDesc")
    if not refund_type:
        refund_type = "部分退" if int(order.get("partRefund") or 0) else "全部退"
    after_sale_status = normalize_after_sale_status(
        order.get("b2cDisplayRefundStatusDesc")
        or order.get("b2cRefundStatusDesc")
        or detail.get("refundStatusDesc")
    )
    main_row = {
        "apply_date": apply_time.date(),
        "after_sale_id": after_sale_id,
        "after_sale_status": after_sale_status,
        "product_info": "；".join(products) or None,
        "refund_amount": decimal_amount(order.get("refundMoney") or order.get("merchantRefundAmount")),
        "after_sale_type": detail.get("serviceTypeDesc") or {1: "仅退款", 2: "退货退款"}.get(service_type),
        "shipment_status": detail.get("orderLogisticStatusDesc") or order.get("b2cOrderMainStatusDesc"),
        "refund_type": refund_type,
        "apply_reason": safe_application_reason(order.get("applyReason")),
        "apply_time": apply_time,
    }
    nodes = []
    for raw_node in detail.get("refundProcessNodeList") or []:
        node = normalize_node(raw_node.get("title"), raw_node.get("time"))
        if node:
            nodes.append((after_sale_id, *node))
    if len(set(nodes)) != len(nodes):
        nodes = list(dict.fromkeys(nodes))
    merchant_audits = [node for node in nodes if node[1] == "商家" and node[2].startswith(("通过", "驳回"))]
    system_audits = [node for node in nodes if node[1] == "系统" and "自动" in node[2]]
    if merchant_audits:
        latest = max(merchant_audits, key=lambda node: node[3])
        main_row["audit_result"] = latest[2]
        main_row["merchant_audit_time"] = latest[3]
    else:
        main_row["audit_result"] = None
        main_row["merchant_audit_time"] = None
    main_row["system_audit_time"] = max(system_audits, key=lambda node: node[3])[3] if system_audits else None
    return main_row, nodes


def fetch_complete_list(page: CdpPage, list_template: dict, base: dict) -> list[dict]:
    last_issue = None
    for attempt in range(2):
        rows, expected_total, page_number = [], None, 1
        while True:
            body = dict(base)
            body.update({"page": page_number, "size": 100})
            data = page.fetch_json(list_template["url"], "POST", body).get("data") or {}
            total = int(data.get("total") or 0)
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                last_issue = "total changed during pagination"
                break
            content = data.get("content")
            if not isinstance(content, list):
                raise RuntimeError("after-sales response has no content list")
            rows.extend(content)
            if page_number >= int(data.get("totalPage") or 0):
                break
            page_number += 1
        if last_issue is None and len(rows) == expected_total:
            return rows
        last_issue = last_issue or f"total={expected_total} rows={len(rows)}"
        if attempt == 0:
            time.sleep(1)
    raise RuntimeError(f"after-sales pagination incomplete: {last_issue}")


def collect_window(
    page: CdpPage, list_template: dict, lookback_days: int
) -> tuple[list[dict], dt.date]:
    if lookback_days < 1:
        raise ValueError("lookback days must be at least 1")
    now = dt.datetime.now(TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + dt.timedelta(days=1) - dt.timedelta(seconds=1)
    window_start = day_start - dt.timedelta(days=lookback_days - 1)
    common = {"sortCondition": 1, "ascOrder": False}
    window_base = dict(list_template.get("body") or {})
    window_base.update(
        {
            **common,
            "queryCondition": 0,
            "b2cRefundStatusList": [],
            "startTime": int(window_start.timestamp()),
            "endTime": int(day_end.timestamp()),
        }
    )
    window_rows = fetch_complete_list(page, list_template, window_base)
    return window_rows, day_start.date()


def ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS `{MAIN_TABLE}` (
              `日期` DATE NOT NULL,
              `售后单号` VARCHAR(64) NOT NULL,
              `售后状态` VARCHAR(64) NOT NULL,
              `商品信息` TEXT NULL,
              `退款金额` DECIMAL(12,2) NULL,
              `售后类型` VARCHAR(64) NULL,
              `发货状态` VARCHAR(128) NULL,
              `退款类型` VARCHAR(64) NULL,
              `申请原因` VARCHAR(500) NULL,
              `申请时间` DATETIME NULL,
              PRIMARY KEY (`日期`, `售后单号`),
              KEY `idx_after_sale_status` (`日期`, `售后状态`)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""
        )
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS `{NODE_TABLE}` (
              `售后单号` VARCHAR(64) NOT NULL,
              `节点主体` VARCHAR(8) NOT NULL,
              `节点操作` VARCHAR(128) NOT NULL,
              `节点时间` DATETIME NOT NULL,
              PRIMARY KEY (`售后单号`, `节点主体`, `节点操作`, `节点时间`)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""
        )
        cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (MAIN_TABLE,))
        columns = {row[0] for row in cur.fetchall()}
        for name, definition in (("售后审核结果", "VARCHAR(128) NULL"), ("商家审核时间", "DATETIME NULL"), ("系统审核时间", "DATETIME NULL")):
            if name not in columns:
                cur.execute(f"ALTER TABLE `{MAIN_TABLE}` ADD COLUMN `{name}` {definition}")


def load_historical_active_ids(
    conn, apply_date: dt.date, lookback_days: int
) -> set[str]:
    window_start = apply_date - dt.timedelta(days=lookback_days - 1)
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT `售后单号` FROM `{MAIN_TABLE}`
                WHERE `日期` >= %s AND `日期` < %s
                  AND SUBSTRING_INDEX(`售后状态`, ',', -1)
                      NOT IN ('退款成功', '退款驳回', '售后取消')""",
            (window_start, apply_date),
        )
        return {str(row[0]) for row in cur.fetchall()}


def persist(
    conn,
    apply_date: dt.date,
    today_rows: list[dict],
    historical_rows: list[dict],
    nodes: list[tuple],
) -> None:
    ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute(f"SELECT `售后单号` FROM `{MAIN_TABLE}` WHERE `日期`=%s", (apply_date,))
        refreshed_ids = {str(row[0]) for row in cur.fetchall()}
        refreshed_ids.update(row["after_sale_id"] for row in historical_rows)
        if refreshed_ids:
            marks = ",".join(["%s"] * len(refreshed_ids))
            cur.execute(
                f"DELETE FROM `{NODE_TABLE}` WHERE `售后单号` IN ({marks})",
                tuple(refreshed_ids),
            )
        cur.execute(f"DELETE FROM `{MAIN_TABLE}` WHERE `日期`=%s", (apply_date,))
        main_rows = [*today_rows, *historical_rows]
        if main_rows:
            cur.executemany(
                f"""INSERT INTO `{MAIN_TABLE}`
                (`日期`,`售后单号`,`售后状态`,`商品信息`,`退款金额`,`售后类型`,
                 `发货状态`,`退款类型`,`申请原因`,`申请时间`,`售后审核结果`,`商家审核时间`,`系统审核时间`)
                VALUES (%(apply_date)s,%(after_sale_id)s,%(after_sale_status)s,%(product_info)s,
                        %(refund_amount)s,%(after_sale_type)s,%(shipment_status)s,%(refund_type)s,
                        %(apply_reason)s,%(apply_time)s,%(audit_result)s,%(merchant_audit_time)s,%(system_audit_time)s)
                ON DUPLICATE KEY UPDATE
                  `售后状态`=VALUES(`售后状态`),
                  `商品信息`=VALUES(`商品信息`),
                  `退款金额`=VALUES(`退款金额`),
                  `售后类型`=VALUES(`售后类型`),
                  `发货状态`=VALUES(`发货状态`),
                  `退款类型`=VALUES(`退款类型`),
                  `申请原因`=VALUES(`申请原因`),
                  `申请时间`=VALUES(`申请时间`),
                  `售后审核结果`=VALUES(`售后审核结果`),
                  `商家审核时间`=VALUES(`商家审核时间`),
                  `系统审核时间`=VALUES(`系统审核时间`)""",
                main_rows,
            )
        if nodes:
            cur.executemany(
                f"INSERT INTO `{NODE_TABLE}` (`售后单号`,`节点主体`,`节点操作`,`节点时间`) VALUES (%s,%s,%s,%s)",
                nodes,
            )


def review_duration_hours(nodes: list[tuple]) -> tuple[float | None, float | None]:
    grouped: dict[str, list[tuple]] = {}
    for node in nodes:
        grouped.setdefault(node[0], []).append(node)
    initial, final = [], []
    for timeline in grouped.values():
        timeline.sort(key=lambda node: node[3])
        applications = [node for node in timeline if node[1] == "顾客" and node[2].startswith("申请")]
        merchant = [node for node in timeline if node[1] == "商家" and node[2].startswith(("通过", "驳回"))]
        logistics = [node for node in timeline if node[1] == "顾客" and node[2] == "提交退货物流单号"]
        if applications:
            target = next((node for node in merchant if node[3] > applications[0][3]), None)
            if target:
                initial.append((target[3] - applications[0][3]).total_seconds() / 3600)
        if logistics:
            target = next((node for node in merchant if node[2] == "通过全额退款申请" and node[3] > logistics[0][3]), None)
            if target:
                final.append((target[3] - logistics[0][3]).total_seconds() / 3600)
    return (round(sum(initial) / len(initial), 2) if initial else None, round(sum(final) / len(final), 2) if final else None)


def persist_workbench_pending_metric(
    conn, config: dict, record_date: dt.date, pending: int, imminent: int, nodes: list[tuple]
) -> int:
    """Upsert the current overview counts returned by the after-sales page."""
    store = workbench_store_settings(config)
    schema, table = workbench_target(config)
    target = qualified_workbench(schema, table)
    initial_hours, final_hours = review_duration_hours(nodes)
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {target} (`日期`,`客户`,`商家负责人`,`平台`,`待商家审核数量`,`即将自动退售后单`,`退货退款初审时间`,`退货退款终审时间`)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              `商家负责人`=VALUES(`商家负责人`),
              `待商家审核数量`=VALUES(`待商家审核数量`),
              `即将自动退售后单`=VALUES(`即将自动退售后单`),
              `退货退款初审时间`=COALESCE(VALUES(`退货退款初审时间`),`退货退款初审时间`),
              `退货退款终审时间`=COALESCE(VALUES(`退货退款终审时间`),`退货退款终审时间`)""",
            (record_date, store["client"], store["owner"], store["platform"], pending, imminent, initial_hours, final_hours),
        )
    return pending


def open_page_with_templates(port: int) -> tuple[CdpPage, dict, dict]:
    """Capture templates with one fresh-target retry within a 60-second budget."""
    last_error: Exception | None = None
    for attempt in range(CAPTURE_TARGET_ATTEMPTS):
        page = CdpPage(port)
        try:
            list_template, count_template = page.capture_templates()
            return page, list_template, count_template
        except Exception as error:
            page.close()
            last_error = error
            if attempt < CAPTURE_TARGET_ATTEMPTS - 1:
                time.sleep(2)
    raise RuntimeError(
        f"Meituan after-sales template capture failed after {CAPTURE_TARGET_ATTEMPTS} targets: {last_error}"
    )


def run(args: argparse.Namespace) -> dict:
    config = load_config(args.config)
    started = dt.datetime.now(TZ)
    if args.state_file:
        write_state(args.state_file, {"state": "running", "started_at": started.isoformat()})
    page = None
    try:
        page, list_template, count_template = open_page_with_templates(args.port)
        overview_payload = page.fetch_json(
            count_template["url"], count_template["method"], count_template["body"]
        )
        overview = overview_payload.get("data") or {}
        try:
            pending_review_count = int(overview["pendingReviewCount"])
            auto_refund_count = int(overview["autoRefundCount"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Meituan after-sales overview did not expose review and auto-refund counts") from error
        window_items, apply_date = collect_window(
            page, list_template, args.status_lookback_days
        )
        conn = None
        historical_active_ids: set[str] = set()
        if not args.dry_run:
            mysql = config["mysql"]
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
                ensure_tables(conn)
                historical_active_ids = load_historical_active_ids(
                    conn, apply_date, args.status_lookback_days
                )
            except Exception:
                conn.close()
                raise

        today_items, historical_items = [], []
        seen_ids = set()
        for item in window_items:
            order = item.get("refundOrder") or {}
            after_sale_id = str(order.get("refundViewId") or "").strip()
            item_apply_time = datetime_from_millis(order.get("applyTime"))
            if not after_sale_id or not item_apply_time:
                if conn is not None:
                    conn.close()
                raise ValueError("after-sales list row is missing an ID or application time")
            if after_sale_id in seen_ids:
                if conn is not None:
                    conn.close()
                raise RuntimeError("after-sales window response contains duplicate IDs")
            seen_ids.add(after_sale_id)
            if item_apply_time.date() == apply_date:
                today_items.append(item)
            elif after_sale_id in historical_active_ids:
                historical_items.append(item)

        main_rows, nodes = [], []
        for item in [*today_items, *historical_items]:
            order = item.get("refundOrder") or {}
            order_id = str(order.get("orderViewId") or order.get("orderId") or "")
            after_sale_id = str(order.get("refundViewId") or "")
            if not order_id or not after_sale_id:
                raise ValueError("after-sales list row is missing an ID")
            detail = page.fetch_json(
                build_detail_url(list_template["url"], order_id, after_sale_id), "GET"
            )
            main_row, order_nodes = normalize_order(item, detail)
            main_rows.append(main_row)
            nodes.extend(order_nodes)
            if args.detail_delay:
                time.sleep(args.detail_delay)
        if len({row["after_sale_id"] for row in main_rows}) != len(main_rows):
            if conn is not None:
                conn.close()
            raise RuntimeError("refreshed after-sales response contains duplicate IDs")
        if len(set(nodes)) != len(nodes):
            if conn is not None:
                conn.close()
            raise RuntimeError("today after-sales nodes contain duplicates")
        today_rows = [row for row in main_rows if row["apply_date"] == apply_date]
        historical_rows = [row for row in main_rows if row["apply_date"] != apply_date]
        pending_merchant_review_count = None
        if conn is not None:
            try:
                persist(conn, apply_date, today_rows, historical_rows, nodes)
                pending_merchant_review_count = persist_workbench_pending_metric(
                    conn, config, apply_date, pending_review_count, auto_refund_count, nodes
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        result = {
            "state": "success",
            "apply_date": apply_date.isoformat(),
            "started_at": started.isoformat(),
            "finished_at": dt.datetime.now(TZ).isoformat(),
            "today_after_sale_count": len(today_rows),
            "historical_active_found": len(historical_items),
            "historical_active_missing": len(historical_active_ids)
            - len(historical_items),
            "historical_status_refreshed": len(historical_rows),
            "status_counts": {
                status: sum(row["after_sale_status"] == status for row in today_rows)
                for status in sorted({row["after_sale_status"] for row in today_rows})
            },
            "node_count": len(nodes),
            "pending_merchant_review_count": pending_merchant_review_count,
            "imminent_auto_refund_count": auto_refund_count,
            "dry_run": bool(args.dry_run),
        }
        if args.state_file:
            write_state(args.state_file, result)
        return result
    except Exception as error:
        if args.state_file:
            write_state(
                args.state_file,
                {
                    "state": "failed",
                    "started_at": started.isoformat(),
                    "finished_at": dt.datetime.now(TZ).isoformat(),
                    "message": str(error),
                },
            )
        raise
    finally:
        if page is not None:
            page.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Meituan after-sales orders requiring merchant review"
    )
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--state-file", type=pathlib.Path)
    parser.add_argument("--detail-delay", type=float, default=0.5)
    parser.add_argument("--status-lookback-days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
