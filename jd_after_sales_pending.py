#!/usr/bin/env python3
"""Collect JD after-sales data into JD-only review and node tables.

2026-11 改版说明（务必先读，否则会误以为「采集器又坏了」）
--------------------------------------------------------------
京东自主售后页已迁到 micro-app 微前端，页面里**不再有**公开的 webpack
运行时：顶层 `window.webpackChunkafter_sale` 消失，微应用沙箱 window 里也
找不到含 `getIndependentAfsListData` 的模块。旧版靠 dispatcher 调接口的
方式已整体失效，报错形如 `JD after-sales list operation returned invalid
data; keys=[]`。

现改为「让页面自己发请求 + CDP 抓响应」：

* 列表：navigate 触发的首屏请求 + 点击分页（分页是 Vue 组件，
  `el.click()` 不生效，必须 scrollIntoView 后发真实鼠标事件；
  数字页在 `ul.jd-pager li.number`，跳 5 页在 `li.btn-quicknext`）。
* 不再调详情/节点接口：列表行本身已含 `serviceOrderBaseFacetDTO
  .afsApplyTime`、`serviceOrderStatusFacetDTO`、`refundFacetDTO`、
  `wareFacetDTOList`、`customerExceptFacetDTO.customerExpectName`，
  够填 `jd_after_sale_review_daily` 全部 10 列。节点表本轮不再采集
  （persist() 仍会按日期清空当日节点，保持「当日重写」语义）。
* **禁止**在页面内 fetch 重放请求：sff.jd.com 会返回
  `code=312 请求异常`（风控），只有页面自身发出的请求才带齐签名头。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import socket
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pymysql
import requests
import websocket

TZ = ZoneInfo("Asia/Shanghai")
LIST_URL = "https://shop.jd.com/jdm/trade/after-sale/independent-after-sale/list?tabCode=all"
# 列表接口（sff 网关上按 api 参数区分），只用于在抓包时匹配请求
LIST_API = "serviceOrderQueryDsmService.page"
MAIN_TABLE = "jd_after_sale_review_daily"
NODE_TABLE = "jd_after_sale_review_node"
# 该页面实测首屏要 18 秒左右才渲染出表格，超时给足
FIRST_PAGE_TIMEOUT = 60.0
PAGE_TIMEOUT = 45.0


class CdpPage:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        response = requests.put(f"{self.base}/json/new?{quote('about:blank', safe='')}", timeout=15)
        if response.status_code == 405:
            response = requests.get(f"{self.base}/json/new?{quote('about:blank', safe='')}", timeout=15)
        response.raise_for_status()
        target = response.json()
        self.tab_id = target["id"]
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=60)
        self.message_id = 0
        # CDP 事件可能在等待命令回复期间到达，必须留住：列表请求往往在
        # Page.navigate 返回之前就发出了。
        self.events: list[dict] = []

    def close(self) -> None:
        try:
            self.ws.close()
        finally:
            try:
                requests.get(f"{self.base}/json/close/{self.tab_id}", timeout=10)
            except requests.RequestException:
                pass

    def call(self, method: str, params: dict | None = None) -> dict:
        self.message_id += 1
        payload = {"id": self.message_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.ws.send(json.dumps(payload))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != self.message_id:
                if message.get("method"):
                    self.events.append(message)
                continue
            if "error" in message:
                raise RuntimeError(message["error"])
            return message.get("result", {})

    def next_event(self) -> dict:
        if self.events:
            return self.events.pop(0)
        return json.loads(self.ws.recv())

    def evaluate(self, expression: str):
        response = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": 60000,
            },
        )
        result = response.get("result") or {}
        if "value" not in result:
            raise RuntimeError(result.get("description") or result)
        return result["value"]

    def load_list(self) -> None:
        self.call("Network.enable", {"maxTotalBufferSize": 80_000_000})
        self.call("Page.enable")
        self.call("Page.navigate", {"url": LIST_URL})

    def wait_list_payload(self, timeout: float = PAGE_TIMEOUT) -> dict | None:
        """等待一次列表接口响应，返回其 data（含 content / totalNum）。"""
        matched: set[str] = set()
        responded: set[str] = set()
        deadline = time.monotonic() + timeout
        old_timeout = self.ws.gettimeout()
        self.ws.settimeout(0.5)
        try:
            while time.monotonic() < deadline:
                try:
                    message = self.next_event()
                except (socket.timeout, websocket.WebSocketTimeoutException):
                    continue
                params = message.get("params") or {}
                method = message.get("method")
                request_id = params.get("requestId")
                if method == "Network.requestWillBeSent":
                    url = (params.get("request") or {}).get("url") or ""
                    if LIST_API in url:
                        matched.add(request_id)
                elif method == "Network.responseReceived" and request_id in matched:
                    responded.add(request_id)
                elif method == "Network.loadingFinished" and request_id in responded:
                    try:
                        body = self.call(
                            "Network.getResponseBody", {"requestId": request_id}
                        ).get("body") or ""
                        payload = json.loads(body)
                    except (RuntimeError, json.JSONDecodeError):
                        continue
                    data = payload.get("data") if isinstance(payload, dict) else None
                    if isinstance(data, dict) and isinstance(data.get("content"), list):
                        return data
            return None
        finally:
            self.ws.settimeout(old_timeout)

    def pager_state(self) -> tuple[list[str], str]:
        raw = self.evaluate(
            """(()=>{
              const lis = [...document.querySelectorAll('ul.jd-pager li.number')];
              const active = document.querySelector('ul.jd-pager li.is-active.number');
              return JSON.stringify({
                nums: lis.map(e => (e.innerText || '').trim()),
                active: active ? (active.innerText || '').trim() : ''
              });
            })()"""
        )
        state = json.loads(raw)
        return state.get("nums") or [], state.get("active") or ""

    def _real_click(self, selector: str, text: str | None) -> bool:
        """滚动到元素并发真实鼠标事件（Vue 分页组件不响应 el.click()）。"""
        locate = """(()=>{
          const text = %s;
          const els = [...document.querySelectorAll(%s)];
          const el = text === null
            ? els[0]
            : els.find(e => (e.innerText || '').trim() === text);
          if (!el) return null;
          el.scrollIntoView({block: 'center', inline: 'center'});
          return 'ok';
        })()""" % (json.dumps(text, ensure_ascii=False), json.dumps(selector))
        if self.evaluate(locate) != "ok":
            return False
        time.sleep(0.5)
        rect = self.evaluate(
            """(()=>{
              const text = %s;
              const els = [...document.querySelectorAll(%s)];
              const el = text === null
                ? els[0]
                : els.find(e => (e.innerText || '').trim() === text);
              if (!el) return null;
              const r = el.getBoundingClientRect();
              return [Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)];
            })()""" % (json.dumps(text, ensure_ascii=False), json.dumps(selector))
        )
        if not rect:
            return False
        x, y = int(rect[0]), int(rect[1])
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        time.sleep(0.15)
        self.call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        time.sleep(0.08)
        self.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        return True

    def goto_page(self, target: int, attempts: int = 10) -> bool:
        """点到指定页；目标页不在可见区先点「向后 5 页」。"""
        for _ in range(attempts):
            nums, active = self.pager_state()
            text = str(target)
            if text in nums:
                if active == text:
                    return True
                return self._real_click("ul.jd-pager li.number", text)
            if not self._real_click("ul.jd-pager li.btn-quicknext", None):
                return False
            time.sleep(1.2)
        return False


def millis_to_datetime(value) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(int(value) / 1000, TZ).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def decimal_amount(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("JD after-sales refund amount is invalid") from error


def status_from(item: dict) -> str:
    value = item.get("serviceOrderStatusFacetDTO") or {}
    final = str(
        value.get("serviceOrderMainStateName") or value.get("afsServiceStatusName") or ""
    ).strip()
    stage = str(value.get("afsServiceStateName") or "").strip()
    if not final:
        raise ValueError("JD after-sales status is missing")
    return (f"{stage},{final}" if "拦截" in stage and stage not in final else final)[:64]


def normalize_type(item: dict) -> str | None:
    """客户期望。列表行把名称放在 customerExceptFacetDTO，详情接口才放在
    customerApplyFacetDTO，两个都试。"""
    for key in ("customerExceptFacetDTO", "customerApplyFacetDTO"):
        customer = str(((item.get(key) or {}).get("customerExpectName")) or "").strip()
        if not customer:
            continue
        if customer == "退款":
            return "仅退款"
        if customer == "退货":
            return "退货退款"
        return customer
    return None


def normalize_row(item: dict) -> dict:
    base = item.get("serviceOrderBaseFacetDTO") or {}
    apply_time = millis_to_datetime(base.get("afsApplyTime"))
    after_sale_id = str(item.get("afsServiceId") or "").strip()
    if not apply_time or not after_sale_id:
        raise ValueError("JD after-sales row is missing ID or application time")
    products = []
    for product in item.get("wareFacetDTOList") or []:
        name = str(product.get("wareName") or "").strip()
        spec = str(product.get("size") or "").strip()
        count = product.get("wareNum")
        if name:
            products.append(
                " ".join(
                    value
                    for value in (name, spec, f"x{count}" if count not in (None, "") else "")
                    if value
                )
            )
    reason_facet = (item.get("customerApplyFacetDTO") or {}).get("applyReasonFacetDTO") or {}
    reason = reason_facet.get("applyReasonCid2Name") or reason_facet.get("applyReasonCid1Name")
    refund = item.get("refundFacetDTO") or {}
    return {
        "apply_date": apply_time.date(),
        "after_sale_id": after_sale_id,
        "after_sale_status": status_from(item),
        "product_info": "；".join(products) or None,
        "refund_amount": decimal_amount(
            (item.get("serviceExtFacetDTO") or {}).get("actualPayTotalAmount")
        ),
        "after_sale_type": normalize_type(item),
        "shipment_status": (item.get("deliveryWareFacetDTO") or {}).get("deliveryWareStateName"),
        # 列表行没有 refundTypeName（旧版取自详情接口），退化为退款操作名，
        # 语义是「谁做了什么退款操作」而不是「取件方式」。
        "refund_type": refund.get("refundOperateTypeName"),
        "apply_reason": str(reason).strip()[:500] if reason else None,
        "apply_time": apply_time,
    }


def persist(conn, apply_date: dt.date, rows: list[dict], nodes: list[tuple]) -> None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT `售后单号` FROM `{MAIN_TABLE}` WHERE `日期`=%s", (apply_date,))
        old_ids = [row[0] for row in cur.fetchall()]
        if old_ids:
            cur.execute(
                f"DELETE FROM `{NODE_TABLE}` WHERE `售后单号` IN ({','.join(['%s'] * len(old_ids))})",
                old_ids,
            )
        cur.execute(f"DELETE FROM `{MAIN_TABLE}` WHERE `日期`=%s", (apply_date,))
        if rows:
            cur.executemany(
                f"INSERT INTO `{MAIN_TABLE}` (`日期`,`售后单号`,`售后状态`,`商品信息`,`退款金额`,"
                f"`售后类型`,`发货状态`,`退款类型`,`申请原因`,`申请时间`) VALUES "
                f"(%(apply_date)s,%(after_sale_id)s,%(after_sale_status)s,%(product_info)s,"
                f"%(refund_amount)s,%(after_sale_type)s,%(shipment_status)s,%(refund_type)s,"
                f"%(apply_reason)s,%(apply_time)s)",
                rows,
            )
        if nodes:
            cur.executemany(
                f"INSERT INTO `{NODE_TABLE}` (`售后单号`,`节点主体`,`节点操作`,`节点时间`) "
                f"VALUES (%s,%s,%s,%s)",
                nodes,
            )


def collect_pages(page: CdpPage) -> tuple[list[dict], int]:
    """翻完全部页，返回去重后的列表行与接口给的 totalNum。"""
    page.load_list()
    first = page.wait_list_payload(timeout=FIRST_PAGE_TIMEOUT)
    if first is None:
        text = str(page.evaluate("document.body.innerText.slice(0,600)"))
        if "登录" in text or "验证码" in text:
            raise RuntimeError("JD after-sales page is not logged in or is challenged")
        raise RuntimeError("JD after-sales list request was not observed on first load")

    total = int(first.get("totalNum") or 0)
    items = list(first.get("content") or [])
    seen = {str(item.get("afsServiceId")) for item in items if item.get("afsServiceId")}
    page_size = len(items) or 10
    pages = max(1, -(-total // page_size))

    for index in range(2, pages + 1):
        if len(items) >= total:
            break
        if not page.goto_page(index):
            raise RuntimeError(f"JD after-sales pager has no reachable page {index}")
        payload = page.wait_list_payload(timeout=PAGE_TIMEOUT)
        if payload is None:
            raise RuntimeError(f"JD after-sales page {index} returned no response")
        grabbed = payload.get("content") or []
        if not grabbed:
            raise RuntimeError(f"JD after-sales page {index} returned no rows")
        for item in grabbed:
            key = str(item.get("afsServiceId") or "")
            if key and key not in seen:
                seen.add(key)
                items.append(item)
    return items, total


def run(args: argparse.Namespace) -> dict:
    now = dt.datetime.now(TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    page = CdpPage(args.port)
    try:
        items, total = collect_pages(page)
    finally:
        page.close()

    # 页面默认查询窗口含历史单，只保留本次运行当天的申请。
    rows = []
    for item in items:
        row = normalize_row(item)
        if row["apply_date"] != day_start.date():
            continue
        rows.append(row)
    if len({row["after_sale_id"] for row in rows}) != len(rows):
        raise RuntimeError("JD after-sales list has duplicate service IDs")

    nodes: list[tuple] = []
    result = {
        "state": "success",
        "apply_date": day_start.date().isoformat(),
        "list_total": total,
        "list_rows": len(items),
        "after_sale_count": len(rows),
        "node_count": len(nodes),
        "status_counts": {
            status: sum(row["after_sale_status"] == status for row in rows)
            for status in sorted({row["after_sale_status"] for row in rows})
        },
    }
    if not args.dry_run:
        config = json.loads(args.config.read_text(encoding="utf-8"))
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
            persist(conn, day_start.date(), rows, nodes)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--port", type=int, default=9533)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
