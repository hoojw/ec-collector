#!/usr/bin/env python3
"""Collect Meituan subsidy activity JSON directly into the existing result table."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import subprocess
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pymysql
import requests
import websocket


LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LIST_URL = (
    "https://yiyao.meituan.com/main/frame#/pages/health/marketing/"
    "low_price_campaign/list?tab=subsidyProduct&filterType=0"
)
LIST_API = "/api/health/activity/apply/b/activity/apply/lst/sku/list?source=1"
LIST_FILTER_TYPES = {1: 1, 0: 0}
OUTPUT_TABLE_DEFAULT = "subsidy_unsubsidized_daily"
NODE_TABLE_DEFAULT = "subsidy_product_nodes"
FOCUS_TABLE_DEFAULT = "excel_数据1_8a8013f1fb"
RESPONSE_TABLE_DEFAULT = "subsidy_focus_response_summary"
NODE_COLUMNS = (
    "upc",
    "活动类型",
    "操作",
    "原因",
    "时间",
)
OUTPUT_COLUMNS = (
    "记录日期",
    "商品upc",
    "商品名称",
    "门槛价格",
    "售价",
    "活动优惠金额",
    "补贴状态",
    "rejecttime",
    "活动类型",
    "门槛盒数",
    "补贴承担",
)
ACTIVITY_TYPES = {17: "折扣", 29: "多盒"}
TARGET_UNAPPROVED_REASON = "价格不符合补贴要求"
TZ = ZoneInfo("Asia/Shanghai")


def quote_ident(name: str) -> str:
    if not name:
        raise ValueError("identifier cannot be empty")
    return "`" + name.replace("`", "``") + "`"


def load_config(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def docker_json(template: str):
    output = subprocess.run(
        ["docker", "inspect", "mysql-de", "--format", template],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return json.loads(output)


def dataease_connection_config() -> dict:
    environment = docker_json("{{json .Config.Env}}")
    values = dict(item.split("=", 1) for item in environment if "=" in item)
    networks = docker_json("{{json .NetworkSettings.Networks}}")
    host = next(iter(networks.values()))["IPAddress"]
    return {
        "host": host,
        "port": 3306,
        "user": "root",
        "password": values["MYSQL_ROOT_PASSWORD"],
        "database": "dataease",
        "charset": "utf8mb4",
    }


def load_focus_upcs(table: str) -> list[str]:
    table_q = quote_ident(table)
    conn = pymysql.connect(**dataease_connection_config())
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT `upc` FROM {table_q} WHERE `upc` IS NOT NULL")
            return [str(int(row[0])) for row in cur.fetchall()]
    finally:
        conn.close()


def load_configured_focus_upcs(tables: dict) -> list[str]:
    table = tables.get("focus_products", FOCUS_TABLE_DEFAULT)
    return load_focus_upcs(table) if table else []


def decimal_value(value, *, required: bool, field: str) -> Decimal | None:
    if value is None or value == "":
        if required:
            raise ValueError(f"missing required field: {field}")
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid numeric field {field}: {value!r}") from error


def datetime_from_epoch(value, *, field: str) -> dt.datetime | None:
    if value is None or value == "":
        return None
    try:
        timestamp = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid timestamp field {field}: {value!r}") from error
    if timestamp >= Decimal("100000000000"):
        timestamp /= 1000
    try:
        return dt.datetime.fromtimestamp(float(timestamp), TZ).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError(f"invalid timestamp field {field}: {value!r}") from error


def normalize_record(record: dict, subsidy_status: int, record_date: dt.date) -> tuple:
    """Map one API record without applying any price comparison."""
    if subsidy_status not in (0, 1):
        raise ValueError(f"invalid subsidy status: {subsidy_status}")

    upc = str(record.get("upcCode") or record.get("skuCode") or "").strip()
    name = str(record.get("skuName") or "").strip()
    if not upc or not name:
        raise ValueError(f"record is missing UPC or name: {record.get('id')!r}")

    act_type = int(record.get("actType")) if record.get("actType") is not None else None
    activity = ACTIVITY_TYPES.get(act_type)
    if activity is None:
        raise ValueError(f"unsupported activity type: {record.get('actType')!r}")

    threshold_price = decimal_value(
        record.get("minDiscountPrice"), required=True, field="minDiscountPrice"
    )
    sale_price = decimal_value(record.get("currentPrice"), required=False, field="currentPrice")
    reject_time = datetime_from_epoch(record.get("rejectTime"), field="rejectTime")
    if act_type == 17:
        activity_discount = decimal_value(record.get("mtPrice"), required=False, field="mtPrice")
        box_count = 1
        merchant_burden = Decimal("0")
    else:
        activity_discount = decimal_value(
            record.get("reduceAmount"), required=False, field="reduceAmount"
        )
        raw_box_count = record.get("levelItemNumber")
        try:
            box_count = int(raw_box_count)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid multi-box levelItemNumber: {raw_box_count!r}") from error
        if box_count <= 0:
            raise ValueError(f"invalid multi-box levelItemNumber: {raw_box_count!r}")

        merchant_burden = decimal_value(
            record.get("singleItemPoiCharge"), required=False, field="singleItemPoiCharge"
        )
        if merchant_burden is None:
            total_burden = decimal_value(record.get("poiCharge"), required=False, field="poiCharge")
            if total_burden is None:
                raise ValueError(f"multi-box merchant burden is missing: {record.get('id')!r}")
            merchant_burden = total_burden / box_count

    return (
        record_date,
        upc,
        name,
        threshold_price,
        sale_price,
        activity_discount,
        subsidy_status,
        reject_time,
        activity,
        box_count,
        merchant_burden,
    )


def record_key(row: tuple) -> tuple:
    return (row[1], row[2], row[3], row[8])


def should_collect(record: dict, subsidy_status: int) -> bool:
    try:
        raw_status = int(record.get("status"))
        if subsidy_status == 1 and raw_status != 1:
            return False
    except (TypeError, ValueError):
        return False
    if subsidy_status == 0:
        rejection = (record.get("rej") or [{}])[0]
        reason = rejection.get("rejText") if isinstance(rejection, dict) else None
        return reason == TARGET_UNAPPROVED_REASON
    return True


def validate_rows(rows: list[tuple]) -> None:
    seen: set[tuple] = set()
    for row in rows:
        key = record_key(row)
        if key in seen:
            raise ValueError(f"duplicate product/activity record: {key!r}")
        seen.add(key)


def deduplicate_rows(rows: list[tuple]) -> list[tuple]:
    """Collapse exact platform duplicates, but reject conflicting duplicates.

    The source occasionally returns two different API ids for the same business
    record.  The destination primary key cannot represent both.  Dropping an
    exact duplicate is lossless; choosing between rows whose non-key values
    differ would be guessing, so that remains a whole-batch error.
    """
    unique: list[tuple] = []
    by_key: dict[tuple, tuple] = {}
    for row in rows:
        key = record_key(row)
        previous = by_key.get(key)
        if previous is None:
            by_key[key] = row
            unique.append(row)
        elif previous == row:
            logging.warning("Skipping exact duplicate Meituan product/activity record: %r", key)
        else:
            raise ValueError(f"conflicting duplicate product/activity record: {key!r}")
    return unique


class CdpPage:
    def __init__(self, port: int, url: str):
        self.base = f"http://127.0.0.1:{port}"
        self.message_id = 0
        self.tab_id = None
        self.ws = None
        last_error: Exception | None = None
        for attempt in range(3):
            target = None
            try:
                # Every collector owns a disposable target, so its navigation
                # and cleanup cannot alter the browser's landing-page target.
                encoded = quote(url, safe="")
                response = requests.put(f"{self.base}/json/new?{encoded}", timeout=15)
                if response.status_code == 405:
                    response = requests.get(f"{self.base}/json/new?{encoded}", timeout=15)
                response.raise_for_status()
                target = response.json()
                self.tab_id = target["id"]
                self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=20)
                self.call("Runtime.enable")
                time.sleep(2)
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
        raise RuntimeError(f"Meituan subsidy CDP target failed after 3 attempts: {last_error}")

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


def fetch_page(tab: CdpPage, subsidy_status: int, page_no: int, page_size: int) -> dict:
    if subsidy_status not in LIST_FILTER_TYPES:
        raise ValueError(f"unsupported subsidy status: {subsidy_status}")
    request_body = {
        "sortType": 1,
        "filterType": LIST_FILTER_TYPES[subsidy_status],
        "sortField": 40,
        "actTypes": [17, 29],
        "medicineTypeList": [],
        "pageNo": page_no,
        "pageSize": page_size,
    }
    body_json = json.dumps(request_body, ensure_ascii=False, separators=(",", ":"))
    expression = f"""
    (async () => {{
      const response = await fetch({json.dumps(LIST_API)}, {{
        method: 'POST',
        credentials: 'include',
        headers: {{'Content-Type': 'application/json'}},
        body: {json.dumps(body_json)}
      }});
      const text = await response.text();
      return JSON.stringify({{httpStatus: response.status, text}});
    }})()
    """
    raw = tab.run_js(expression)
    if not isinstance(raw, str):
        raise RuntimeError(f"Meituan API returned non-text result: {raw!r}")
    envelope = json.loads(raw)
    if envelope.get("httpStatus") != 200:
        raise RuntimeError(f"Meituan API HTTP error: {envelope}")
    payload = json.loads(envelope.get("text") or "")
    if payload.get("code") != 0:
        raise RuntimeError(f"Meituan API business error: {payload}")
    return payload


def collect_records(
    tab: CdpPage, subsidy_status: int, record_date: dt.date, page_size: int
) -> list[tuple]:
    rows: list[tuple] = []
    raw_count = 0
    page_no = 1
    expected_total: int | None = None
    seen_api_ids: set[str] = set()
    while page_no <= 1000:
        payload = fetch_page(tab, subsidy_status, page_no, page_size)
        data = payload.get("data") or {}
        records = data.get("data")
        if not isinstance(records, list):
            raise ValueError(f"Meituan API page has no data list: page={page_no}")
        total = data.get("total")
        if expected_total is None:
            try:
                expected_total = int(total)
            except (TypeError, ValueError) as error:
                raise ValueError(f"Meituan API page has invalid total: {total!r}") from error
        elif int(total) != expected_total:
            raise ValueError("Meituan API total changed during pagination")

        if not records:
            if raw_count < expected_total:
                raise ValueError(
                    f"Meituan API returned an early empty page: status={subsidy_status}, "
                    f"got={raw_count}, expected={expected_total}"
                )
            break

        for record in records:
            raw_count += 1
            api_id = str(record.get("id") or "")
            if api_id and api_id in seen_api_ids:
                raise ValueError(f"Meituan API returned duplicate record id: {api_id}")
            if api_id:
                seen_api_ids.add(api_id)
            if not should_collect(record, subsidy_status):
                continue
            try:
                rows.append(normalize_record(record, subsidy_status, record_date))
            except (TypeError, ValueError) as error:
                logging.warning(
                    "Skipping invalid Meituan product: id=%s upc=%s act_type=%s reason=%s",
                    api_id or "-",
                    record.get("upcCode") or record.get("skuCode") or "-",
                    record.get("actType"),
                    error,
                )

        if raw_count >= expected_total:
            if raw_count != expected_total:
                raise ValueError(
                    f"Meituan API returned more rows than total: got={raw_count}, expected={expected_total}"
                )
            break
        page_no += 1
    else:
        raise ValueError("Meituan API pagination exceeded 1000 pages")

    if expected_total is None or raw_count != expected_total:
        raise ValueError(
            f"Meituan API pagination incomplete: status={subsidy_status}, "
            f"got={raw_count}, expected={expected_total}"
        )
    return rows


def ensure_output_table(conn, table: str) -> None:
    table_q = quote_ident(table)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {table_q} ("
            "`记录日期` DATE NOT NULL, "
            "`商品upc` VARCHAR(50) NOT NULL, "
            "`商品名称` VARCHAR(500) NOT NULL, "
            "`门槛价格` DECIMAL(10,2) NOT NULL, "
            "`售价` DECIMAL(10,2) NULL, "
            "`活动优惠金额` DECIMAL(10,2) NULL, "
            "`补贴状态` TINYINT UNSIGNED NOT NULL DEFAULT 0, "
            "`rejecttime` DATETIME NULL, "
            "`活动类型` VARCHAR(10) NOT NULL, "
            "`门槛盒数` INT UNSIGNED NOT NULL, "
            "`补贴承担` DECIMAL(10,2) NOT NULL, "
            "PRIMARY KEY (`记录日期`, `商品upc`, `商品名称`, `门槛价格`, `活动类型`)"
            ") DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
        columns = {row[0] for row in cur.fetchall()}
        if "售价" not in columns:
            cur.execute(f"ALTER TABLE {table_q} ADD COLUMN `售价` DECIMAL(10,2) NULL AFTER `门槛价格`")
        if "活动优惠金额" not in columns:
            if "补贴价格" in columns:
                cur.execute(
                    f"ALTER TABLE {table_q} CHANGE COLUMN `补贴价格` "
                    "`活动优惠金额` DECIMAL(10,2) NULL"
                )
            else:
                cur.execute(
                    f"ALTER TABLE {table_q} ADD COLUMN `活动优惠金额` "
                    "DECIMAL(10,2) NULL AFTER `售价`"
                )
        if "补贴状态" not in columns:
            cur.execute(
                f"ALTER TABLE {table_q} ADD COLUMN `补贴状态` "
                "TINYINT UNSIGNED NOT NULL DEFAULT 0 AFTER `活动优惠金额`"
            )
        if "rejecttime" not in columns:
            cur.execute(
                f"ALTER TABLE {table_q} ADD COLUMN `rejecttime` "
                "DATETIME NULL AFTER `补贴状态`"
            )
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
        final_columns = {row[0] for row in cur.fetchall()}
    missing = set(OUTPUT_COLUMNS) - final_columns
    if missing:
        raise ValueError(f"{table} is missing required columns: {sorted(missing)}")


def refresh_output_table(conn, table: str, record_date: dt.date, rows: list[tuple]) -> None:
    validate_rows(rows)
    table_q = quote_ident(table)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {table_q} WHERE `记录日期` = %s", (record_date,))
        if rows:
            cur.executemany(
                f"INSERT INTO {table_q} ("
                "`记录日期`, `商品upc`, `商品名称`, `门槛价格`, `售价`, `活动优惠金额`, `补贴状态`, "
                "`rejecttime`, `活动类型`, `门槛盒数`, `补贴承担`) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )


def ensure_node_table(conn, table: str, output_table: str) -> None:
    table_q = quote_ident(table)
    output_q = quote_ident(output_table)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {table_q} ("
            "`upc` VARCHAR(50) NOT NULL, "
            "`活动类型` VARCHAR(10) NOT NULL, "
            "`操作` VARCHAR(10) NOT NULL, "
            "`原因` VARCHAR(50) NULL, "
            "`时间` DATETIME NOT NULL, "
            "PRIMARY KEY (`upc`, `活动类型`, `操作`, `时间`)"
            ") DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
        columns = {row[0] for row in cur.fetchall()}
        if "活动类型" not in columns:
            cur.execute(
                f"ALTER TABLE {table_q} ADD COLUMN `活动类型` VARCHAR(10) NULL AFTER `upc`"
            )
        if "原因" not in columns:
            cur.execute(f"ALTER TABLE {table_q} ADD COLUMN `原因` VARCHAR(50) NULL AFTER `操作`")
        for column in ("变动前售价", "变动后售价", "变动前门槛价格", "变动后门槛价格"):
            if column in columns:
                cur.execute(f"ALTER TABLE {table_q} DROP COLUMN `{column}`")
        cur.execute(
            f"UPDATE {table_q} AS node JOIN ("
            f"SELECT `商品upc`, `rejecttime`, `补贴状态`, MIN(`活动类型`) AS `活动类型` "
            f"FROM {output_q} WHERE `rejecttime` IS NOT NULL "
            "GROUP BY `商品upc`, `rejecttime`, `补贴状态` "
            "HAVING COUNT(DISTINCT `活动类型`) = 1"
            ") AS source ON source.`商品upc`=node.`upc` "
            "AND source.`rejecttime`=node.`时间` "
            "AND ((source.`补贴状态`=1 AND node.`操作`='入补') "
            "OR (source.`补贴状态`=0 AND node.`操作`='出补')) "
            "SET node.`活动类型`=source.`活动类型` WHERE node.`活动类型` IS NULL"
        )
        cur.execute(f"UPDATE {table_q} SET `活动类型`='未知' WHERE `活动类型` IS NULL")
        cur.execute(f"ALTER TABLE {table_q} MODIFY COLUMN `活动类型` VARCHAR(10) NOT NULL")
        cur.execute(
            "SELECT column_name FROM information_schema.statistics "
            "WHERE table_schema=DATABASE() AND table_name=%s AND index_name='PRIMARY' "
            "ORDER BY seq_in_index",
            (table,),
        )
        primary_columns = [row[0] for row in cur.fetchall()]
        desired_primary = ["upc", "活动类型", "操作", "时间"]
        if primary_columns != desired_primary:
            cur.execute(
                f"ALTER TABLE {table_q} DROP PRIMARY KEY, "
                "ADD PRIMARY KEY (`upc`, `活动类型`, `操作`, `时间`)"
            )
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
        columns = {row[0] for row in cur.fetchall()}
    required = set(NODE_COLUMNS)
    missing = required - columns
    if missing:
        raise ValueError(f"{table} is missing required columns: {sorted(missing)}")


def ensure_response_table(conn, table: str) -> None:
    table_q = quote_ident(table)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {table_q} ("
            "`平均入补响应分钟` DECIMAL(14,2) NULL"
            ") DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=%s ORDER BY ordinal_position",
            (table,),
        )
        columns = [row[0] for row in cur.fetchall()]
    if columns != ["平均入补响应分钟"]:
        raise ValueError(f"unexpected {table} columns: {columns}")


def calculate_focus_response_average(
    conn,
    node_table: str,
    focus_upcs: list[str],
) -> Decimal | None:
    if not focus_upcs:
        return None
    node_q = quote_ident(node_table)
    placeholders = ",".join(["%s"] * len(focus_upcs))
    with conn.cursor() as cur:
        cur.execute(
            "WITH R AS ("
            "SELECT I.`upc`, I.`活动类型`, I.`时间` AS `入补时间`, "
            "MAX(O.`时间`) AS `上次出补时间` "
            f"FROM {node_q} AS I "
            f"INNER JOIN {node_q} AS O ON O.`upc`=I.`upc` "
            "AND O.`活动类型`=I.`活动类型` "
            "AND O.`操作`='出补' AND O.`时间`<I.`时间` "
            "WHERE I.`操作`='入补' AND I.`原因` LIKE '%%售价降低%%' "
            f"AND I.`upc` IN ({placeholders}) "
            "GROUP BY I.`upc`, I.`活动类型`, I.`时间`"
            ") SELECT AVG(TIMESTAMPDIFF(MINUTE, `上次出补时间`, `入补时间`)) "
            "FROM R WHERE `上次出补时间` IS NOT NULL",
            tuple(focus_upcs),
        )
        value = cur.fetchone()[0]
    return value


def refresh_response_summary(conn, table: str, average_minutes: Decimal | None) -> None:
    table_q = quote_ident(table)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {table_q}")
        cur.execute(
            f"INSERT INTO {table_q} (`平均入补响应分钟`) VALUES (%s)",
            (average_minutes,),
        )


def load_latest_snapshot(conn, output_table: str) -> dict[tuple[str, str], dict]:
    output_q = quote_ident(output_table)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT `商品upc`, `活动类型`, `售价`, `门槛价格`, `补贴状态` "
            f"FROM {output_q} WHERE `记录日期`=(SELECT MAX(`记录日期`) FROM {output_q})"
        )
        rows = cur.fetchall()
    snapshot = {}
    for upc, activity, sale_price, threshold_price, status in rows:
        key = (str(upc), str(activity))
        if key in snapshot:
            raise ValueError(f"latest subsidy snapshot has duplicate key: {key!r}")
        snapshot[key] = {
            "sale_price": sale_price,
            "threshold_price": threshold_price,
            "status": int(status),
        }
    return snapshot


def detect_price_triggered_nodes(
    previous: dict[tuple[str, str], dict],
    current_rows: list[tuple],
    observed_at: dt.datetime,
) -> list[tuple]:
    current = {}
    for row in current_rows:
        key = (str(row[1]), str(row[8]))
        if key in current:
            raise ValueError(f"current subsidy snapshot has duplicate key: {key!r}")
        current[key] = {
            "sale_price": row[4],
            "threshold_price": row[3],
            "status": int(row[6]),
        }

    nodes = []
    event_time = observed_at.astimezone(TZ).replace(tzinfo=None, microsecond=0)
    for key, after in current.items():
        before = previous.get(key)
        if before is None:
            continue
        before_sale = before["sale_price"]
        after_sale = after["sale_price"]
        before_threshold = before["threshold_price"]
        after_threshold = after["threshold_price"]
        operation = None
        reasons = []
        if before["status"] == 0 and after["status"] == 1:
            operation = "入补"
            if before_sale is not None and after_sale is not None and after_sale < before_sale:
                reasons.append("售价降低")
            if (
                before_threshold is not None
                and after_threshold is not None
                and after_threshold > before_threshold
            ):
                reasons.append("门槛价格升高")
        elif before["status"] == 1 and after["status"] == 0:
            operation = "出补"
            if before_sale is not None and after_sale is not None and after_sale > before_sale:
                reasons.append("售价升高")
            if (
                before_threshold is not None
                and after_threshold is not None
                and after_threshold < before_threshold
            ):
                reasons.append("门槛价格降低")
        if operation and reasons:
            nodes.append(
                (
                    key[0],
                    key[1],
                    operation,
                    "、".join(reasons),
                    event_time,
                )
            )
    return nodes


def insert_nodes(conn, node_table: str, nodes: list[tuple]) -> int:
    if not nodes:
        return 0
    node_q = quote_ident(node_table)
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT IGNORE INTO {node_q} ("
            "`upc`, `活动类型`, `操作`, `原因`, `时间`) "
            "VALUES (%s, %s, %s, %s, %s)",
            nodes,
        )
        return cur.rowcount


def write_state(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run(args: argparse.Namespace) -> dict:
    config = load_config(args.config)
    record_date = args.record_date or dt.datetime.now(TZ).date()
    state_file = args.state_file
    start = dt.datetime.now(TZ)
    if state_file:
        write_state(
            state_file,
            {"state": "running", "started_at": start.isoformat(), "record_date": record_date.isoformat()},
        )

    tab = CdpPage(args.port, LIST_URL)
    try:
        approved = collect_records(tab, 1, record_date, args.page_size)
        unapproved = collect_records(tab, 0, record_date, args.page_size)
        rows = deduplicate_rows(approved + unapproved)
        validate_rows(rows)
        result = {
            "state": "success",
            "record_date": record_date.isoformat(),
            "started_at": start.isoformat(),
            "finished_at": dt.datetime.now(TZ).isoformat(),
            "approved": len(approved),
            "unapproved": len(unapproved),
            "total": len(rows),
        }
        if not args.dry_run:
            mysql_config = config["mysql"]
            output_table = config.get("tables", {}).get("unsubsidized_daily", OUTPUT_TABLE_DEFAULT)
            node_table = config.get("tables", {}).get(
                "subsidy_product_nodes", NODE_TABLE_DEFAULT
            )
            tables = config.get("tables", {})
            response_table = config.get("tables", {}).get(
                "subsidy_focus_response_summary", RESPONSE_TABLE_DEFAULT
            )
            focus_upcs = load_configured_focus_upcs(tables)
            conn = pymysql.connect(
                host=mysql_config["host"],
                port=int(mysql_config["port"]),
                user=mysql_config["user"],
                password=mysql_config["password"],
                database=mysql_config["database"],
                charset=mysql_config.get("charset", "utf8mb4"),
                autocommit=False,
            )
            try:
                ensure_output_table(conn, output_table)
                ensure_node_table(conn, node_table, output_table)
                ensure_response_table(conn, response_table)
                previous = load_latest_snapshot(conn, output_table)
                detected_nodes = detect_price_triggered_nodes(
                    previous, rows, dt.datetime.now(TZ)
                )
                result["nodes_inserted"] = insert_nodes(conn, node_table, detected_nodes)
                refresh_output_table(conn, output_table, record_date, rows)
                average_minutes = calculate_focus_response_average(
                    conn, node_table, focus_upcs
                )
                refresh_response_summary(conn, response_table, average_minutes)
                result["focus_response_average_minutes"] = (
                    str(average_minutes) if average_minutes is not None else None
                )
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
                    "record_date": record_date.isoformat(),
                    "started_at": start.isoformat(),
                    "finished_at": dt.datetime.now(TZ).isoformat(),
                    "message": str(error),
                },
            )
        raise
    finally:
        tab.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Meituan subsidy activities from the logged-in browser API.")
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--record-date", type=dt.date.fromisoformat)
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--state-file", type=pathlib.Path)
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
