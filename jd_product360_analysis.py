"""Collect JD Product360 daily and trailing-30-day workbench metrics.

The JD home page is intentionally not used for these fields: it presents a
real-time dashboard, whereas ``esale_system`` needs the same completed-period
semantics as Meituan's product-analysis page.  This collector only reads the
rendered, authenticated Product360 UI through CDP.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pymysql
import requests
import websocket

from workbench import qualified as qualified_workbench
from workbench import refresh_yesterday_week_ratio
from workbench import target as workbench_target
from human_input import HumanInput


TZ = ZoneInfo("Asia/Shanghai")
PRODUCT360_URL = "https://jdsz.jd.com/szweb/view/product/product360.html"
# Since the late-September 商智 new-console rollout, 商品明细 is a *separate page*
# (productDetail.html), not a tab inside product360.html.  The left-nav entry is
# an anchor that performs a full navigation; clicking it from the overview page
# destroys the overview renderer and the old same-page "数据明细/商品信息" wait
# times out forever, silently zeroing the tirzepatide ratio.
PRODUCT_DETAIL_URL = "https://jdsz.jd.com/szweb/view/product/productDetail.html"
LOGIN_MARKERS = ("账号登录", "扫码登录", "统一登录", "京东验证", "请输入验证码")
# A sub-account without the Product360 entitlement lands on the noAuth page:
# https://jdsz.jd.com/szweb/view/noAuth/noAuth.html ("子账号无权限").
NO_AUTH_MARKERS = ("暂无商智权限", "子账号无权限", "noAuth", "请授权商智权限")
TIRZEPATIDE = "替尔泊肽"
# 商品明细 page header cells.  The product-detail table always renders these two
# labels once it has finished its first paint, so they are a stable readiness
# marker; the old "数据明细"/"商品信息" pair is no longer present anywhere.
DETAIL_TABLE_MARKERS = ("商品信息", "成交商品件数")
DETAIL_PERIOD_EVENTS = {
    "昨天": "当前时间_昨天",
    "近30天": "当前时间_近30天",
}


class NoPermissionError(RuntimeError):
    """The authenticated JD account cannot access Product360 for this store.

    Raised instead of a generic load failure so that the surrounding cycle can
    skip the analysis stage for the day (exit code 3) rather than retrying and
    polluting the journal with repeated permission errors.
    """

    def __init__(self, message: str = "JD Product360 permission is not granted for this account") -> None:
        super().__init__(message)


def decimal_value(value: str, *, field: str) -> Decimal:
    text = value.strip().replace("¥", "").replace(",", "").replace("%", "")
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"invalid JD Product360 {field}: {value!r}") from error


def percent_value(value: str, *, field: str) -> Decimal:
    return decimal_value(value, field=field) / Decimal("100")


def parse_overview(text: str) -> dict[str, Decimal | None]:
    """Parse the rendered Product360 ``商品概况`` page for one selected period."""
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if any(marker in text for marker in LOGIN_MARKERS):
        raise RuntimeError("JD Product360 requires login or verification")

    def optional(pattern: str) -> str | None:
        match = re.search(pattern, text)
        return match.group(1) if match else None

    def decimal_optional(pattern: str, field: str) -> Decimal | None:
        value = optional(pattern)
        return decimal_value(value, field=field) if value is not None else None

    def percent_optional(pattern: str, field: str) -> Decimal | None:
        value = optional(pattern)
        return percent_value(value, field=field) if value is not None else None

    moving = decimal_optional(r"动销SPU数\n([0-9,]+)\n环比", "moving SPU count")
    listed = decimal_optional(r"上架SPU数\n([0-9,]+)\n环比", "listed SPU count")
    turnover = percent_optional(r"SPU动销率\n([+-]?[0-9.]+%)\n环比", "SPU turnover rate")
    amount = decimal_optional(r"成交金额\n[¥￥]?([0-9,.]+)\n环比", "transaction amount")
    amount_ring = percent_optional(r"成交金额\n[¥￥]?[0-9,.]+\n环比\n([+-]?[0-9.]+%)", "transaction amount ring ratio")
    # Product360 occasionally shows an incomplete zero "上架SPU数" while
    # retaining yesterday's moving count.  Keep the platform values but do
    # not derive a negative non-moving count in ``persist`` below.
    return {
        "moving_spu_count": moving,
        "listed_spu_count": listed,
        "turnover_rate": turnover,
        "transaction_amount": amount,
        "transaction_amount_ring_ratio": amount_ring,
    }


def overview_result_rendered(text: str) -> bool:
    """Return true for either numeric cards or JD's explicit empty result."""
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if re.search(r"成交金额\n[¥￥]?[0-9,.]+\n环比", normalized):
        return True
    return bool(
        "暂无数据" in normalized
        and re.search(r"成交金额\n--?\n环比(?:\n--?)?", normalized)
    )


def detail_table_rendered(text: str) -> bool:
    """Return true once the 商品明细 table has painted its header row.

    Both header labels live in the same header strip, so requiring the pair
    avoids matching the left-nav entry (商品明细) or an unrelated card title.
    """
    return all(marker in text for marker in DETAIL_TABLE_MARKERS)


def parse_detail_rows(text: str) -> list[dict[str, Decimal | str]]:
    """Read the visible Product360 product-detail rows for the active page."""
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    rows = []
    for match in re.finditer(
        r"(?:^|\n)(\d+)\n([^\n]+)\n(?:SPU|SKU):\s*[^\n]+\n[¥￥]([0-9,.]+)",
        normalized,
    ):
        rows.append({"name": match.group(2), "amount": decimal_value(match.group(3), field="detail transaction amount")})
    if not rows:
        raise RuntimeError("JD Product360 product-detail page contains no visible rows")
    return rows


def parse_keyword_rows(text: str) -> list[dict[str, Decimal | str]]:
    """Parse rows after a keyword filter, tolerant of the current renderer.

    The rendered product-detail table intersperses column cells with tabs and
    places a leading tab before the transaction amount, so the historical
    row-number anchored parser can miss everything.  Anchor on the SPU line
    instead and read the first amount that follows it (the current-period
    transaction amount; the comparison amount appears on the next line).
    """
    rows = []
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if TIRZEPATIDE not in line:
            continue
        # A rendered row may carry optional promo/CTA copy between the SPU
        # line and the first amount, so scan forward for the nearest amount
        # instead of requiring the SPU line to be immediately followed by it.
        amount = None
        for offset in range(1, 13):
            candidate_index = index + offset
            if candidate_index >= len(lines):
                break
            amount_match = re.match(r"^[¥￥][ \t]*([0-9][0-9,.]*)$", lines[candidate_index])
            if amount_match:
                amount = decimal_value(amount_match.group(1), field="keyword-filtered transaction amount")
                break
        if amount is not None:
            rows.append({"name": line, "amount": amount})
    if not rows:
        raise RuntimeError("JD Product360 keyword-filtered product-detail page contains no tirzepatide rows")
    return rows


def has_spu_rows(text: str) -> bool:
    """True when the page renders at least one product-detail row block."""
    return bool(re.search(r"(?:^|\n)(?:SPU|SKU):\s*\S+", text))


class Page:
    def __init__(self, port: int, url: str = PRODUCT360_URL):
        self.base = f"http://127.0.0.1:{port}"
        response = requests.put(f"{self.base}/json/new?{quote(url, safe='')}", timeout=15)
        response.raise_for_status()
        self.target = response.json()
        self.ws = websocket.create_connection(self.target["webSocketDebuggerUrl"], timeout=30)
        self.message_id = 0
        # Human-shaped input.  Clicking and typing through this instead of raw
        # dispatch calls keeps the browser's own event stream looking like a
        # hand: a travel path before each press, dwell between down and up, and
        # down/char/up triples with jittered gaps when typing.  See
        # human_input.py for why that matters to the risk engine.
        self.human = HumanInput(self)

    def call(self, method: str, params: dict | None = None) -> dict:
        self.message_id += 1
        request_id = self.message_id
        self.ws.send(json.dumps({
            "id": request_id,
            "method": method,
            "params": params or {},
        }))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(message["error"])
            return message.get("result", {})

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        remote = result.get("result", {})
        if "value" not in remote:
            raise RuntimeError(remote.get("description") or "JD Product360 page returned no value")
        return remote["value"]

    def click_accessible_label(self, label: str, *, role: str | None = None, occurrence: int = 0) -> None:
        """Click controls in Product360's shadow DOM through its AX node."""
        matches = []
        nodes = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            nodes = self.call("Accessibility.getFullAXTree").get("nodes", [])
            matches = [
                node for node in nodes
                if (node.get("name") or {}).get("value") == label
                and node.get("backendDOMNodeId")
                and (role is None or (node.get("role") or {}).get("value") == role)
            ]
            if occurrence < len(matches):
                break
            time.sleep(0.5)
        if occurrence >= len(matches):
            raise RuntimeError(f"JD Product360 accessible control not found: {label!r} (matches={len(matches)})")
        # The label itself is usually StaticText with a zero rectangle.  Walk
        # upward in the AX tree to the rendered list item/button before
        # dispatching the pointer event.
        by_id = {node.get("nodeId"): node for node in nodes}
        geometry = None
        for matched in matches[occurrence:]:
            candidate = matched
            for _ in range(8):
                backend = candidate.get("backendDOMNodeId")
                if backend:
                    resolved = self.call("DOM.resolveNode", {"backendNodeId": backend})
                    object_id = resolved.get("object", {}).get("objectId")
                    if object_id:
                        geometry = self.call("Runtime.callFunctionOn", {
                            "objectId": object_id,
                            "functionDeclaration": "function(){const r=this.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,width:r.width,height:r.height};}",
                            "returnByValue": True,
                        }).get("result", {}).get("value")
                        if geometry and geometry.get("width", 0) > 0 and geometry.get("height", 0) > 0:
                            break
                candidate = by_id.get(candidate.get("parentId"))
                if not candidate:
                    break
            if geometry and geometry.get("width", 0) > 0 and geometry.get("height", 0) > 0:
                break
        if not geometry or geometry.get("width", 0) <= 0 or geometry.get("height", 0) <= 0:
            raise RuntimeError(f"JD Product360 control is not rendered: {label!r}")
        # Travel to the control and press with human dwell rather than firing a
        # press/release pair at the same millisecond.
        center_x = geometry.get("x", 0) + geometry.get("width", 0) / 2
        center_y = geometry.get("y", 0) + geometry.get("height", 0) / 2
        self.human.click(center_x, center_y)

    def click_exact_text(self, text: str, *, occurrence: int = 0) -> None:
        """Click a rendered control by exact visible label using pointer events."""
        # Product360 renders key controls inside shadow roots.  Prefer its
        # accessibility representation and retain the DOM fallback for simple
        # browser builds that expose light-DOM controls.
        try:
            self.click_accessible_label(text, occurrence=occurrence)
            return
        except RuntimeError as accessibility_error:
            fallback_error = accessibility_error
        result = self.evaluate(
            """(() => {
              const label = %s;
              const occurrence = %d;
              const candidates = [...document.querySelectorAll('button,a,div,span')]
                .filter(el => el.offsetParent && el.innerText && el.innerText.trim() === label)
                .filter(el => ![...el.children].some(child => child.offsetParent && child.innerText && child.innerText.trim() === label));
              const el = candidates[occurrence];
              if (!el) return {ok:false, count:candidates.length};
              const rect = el.getBoundingClientRect();
              const options = {bubbles:true, cancelable:true, view:window, clientX:rect.left+rect.width/2, clientY:rect.top+rect.height/2};
              el.dispatchEvent(new MouseEvent('mousedown', options));
              el.dispatchEvent(new MouseEvent('mouseup', options));
              el.dispatchEvent(new MouseEvent('click', options));
              return {ok:true, count:candidates.length};
            })()""" % (json.dumps(text, ensure_ascii=False), occurrence)
        )
        if not result.get("ok"):
            raise RuntimeError(f"{fallback_error}; light-DOM fallback had {result.get('count')} matches") from fallback_error

    def type_keyword_filter(self, keyword: str) -> None:
        """Type a product-title keyword into the detail-table filter and search.

        The 商品明细 page exposes a plain ``input.jd-input`` whose placeholder
        reads 输入商品标题关键词搜索.  Focusing it through the DOM (rather than
        the accessibility tree, which no longer exposes it) and pressing Enter
        is what triggers the title-keyword search; the toolbar 查询 button
        belongs to the period selector and does not apply the keyword.
        """
        focused = self.evaluate(
            """(() => {
              const el = [...document.querySelectorAll('input.jd-input')]
                .find(e => e.offsetParent && (e.placeholder || '').includes('关键词'));
              if (!el) return false;
              el.focus();
              el.value = '';
              el.dispatchEvent(new Event('input', {bubbles: true}));
              return document.activeElement === el;
            })()"""
        )
        if not focused:
            raise RuntimeError("JD Product360 product-detail keyword field was not rendered")
        self.human.type_text(keyword)
        self.human.press_key("Enter", "Enter")

    def click_detail_next(self, current_page: int) -> bool:
        """Advance the product-detail table by one page.

        The new renderer paginates with ``li[data-event-content="点击第N页"]``
        items and hides the pager entirely when everything fits on one page.
        """
        return bool(self.evaluate(
            """(() => {
              const el = document.querySelector(%s);
              if (!el || !el.offsetParent) return false;
              el.click();
              return true;
            })()""" % json.dumps(f'li[data-event-content="点击第{current_page + 1}页"]', ensure_ascii=False)
        ))

    def state(self) -> dict[str, str]:
        return self.evaluate("({url: location.href, text: document.body && document.body.innerText || ''})")

    def close(self) -> None:
        self.ws.close()
        # A renderer crash can close DevTools before cleanup runs.  Cleanup
        # must not conceal that original collection error with a second,
        # irrelevant connection-refused exception.
        try:
            requests.get(f"{self.base}/json/close/{self.target['id']}", timeout=10)
        except requests.RequestException:
            pass


def wait_for(page: Page, predicate, *, description: str, timeout: int = 30) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = page.state()
        text = state.get("text", "")
        url = state.get("url", "")
        # A redirect to the noAuth page is the strongest signal: a sub-account
        # without the entitlement must raise NoPermissionError even when the
        # page copy contains generic login wording.
        if "/noAuth/" in url:
            raise NoPermissionError("JD Product360 is not authorized for the current JD account")
        if any(marker in text for marker in LOGIN_MARKERS):
            raise RuntimeError("JD Product360 requires login or verification")
        if any(marker in text for marker in NO_AUTH_MARKERS):
            raise NoPermissionError("JD Product360 is not authorized for the current JD account")
        if predicate(state):
            return state
        time.sleep(1)
    raise RuntimeError(f"JD Product360 did not finish loading {description}")


def select_period(page: Page, label: str) -> dict[str, str]:
    event_name = {
        "昨天": "当前时间_昨天",
        "近30天": "当前时间_近30天",
    }.get(label)
    if event_name is None:
        raise ValueError(f"unsupported Product360 period: {label}")
    applied = page.evaluate(
        """(() => {
          const period = document.querySelector(%s);
          if (!period) return false;
          period.click();
          const query = [...document.querySelectorAll('button')]
            .find(el => el.offsetParent && el.innerText.trim() === '查询');
          if (!query) return false;
          query.click();
          return true;
        })()""" % json.dumps(f'li[data-event-content="{event_name}"]', ensure_ascii=False)
    )
    if not applied:
        raise RuntimeError(f"JD Product360 period controls were not rendered: {label}")
    today = dt.datetime.now(TZ).date()
    if label == "昨天":
        expected_marker = f"当前：{today - dt.timedelta(days=1):%Y-%m-%d}"
    else:
        end = today - dt.timedelta(days=1)
        start = end - dt.timedelta(days=29)
        expected_marker = f"当前：{start:%Y-%m-%d} ~ {end:%m-%d}"
    wait_for(
        page,
        lambda state: expected_marker in state.get("text", "")
        and "商品概况" in state.get("text", "")
        and overview_result_rendered(state.get("text", "")),
        description=f"period {label}",
    )
    # The date label updates before the Product360 cards.  Reading the cards
    # immediately here can retain the preceding period's amount (for example,
    # "昨天" after the UI already displays "近30天").
    time.sleep(3)
    return page.state()


def select_detail_period(page: Page, label: str) -> dict[str, str]:
    """Apply a completed period on the standalone 商品明细 page.

    The new page keeps the same ``li[data-event-content]`` period selector as
    the overview, so this mirrors ``select_period`` but waits on the detail
    table's own header instead of the overview cards.
    """
    event_name = DETAIL_PERIOD_EVENTS.get(label)
    if event_name is None:
        raise ValueError(f"unsupported Product360 detail period: {label}")
    applied = page.evaluate(
        """(() => {
          const period = document.querySelector(%s);
          if (!period) return false;
          period.click();
          const query = [...document.querySelectorAll('button')]
            .find(el => el.offsetParent && el.innerText.trim() === '查询');
          if (!query) return false;
          query.click();
          return true;
        })()""" % json.dumps(f'li[data-event-content="{event_name}"]', ensure_ascii=False)
    )
    if not applied:
        raise RuntimeError(f"JD Product360 detail period controls were not rendered: {label}")
    today = dt.datetime.now(TZ).date()
    if label == "昨天":
        expected_marker = f"当前：{today - dt.timedelta(days=1):%Y-%m-%d}"
    else:
        end = today - dt.timedelta(days=1)
        start = end - dt.timedelta(days=29)
        expected_marker = f"当前：{start:%Y-%m-%d} ~ {end:%m-%d}"
    wait_for(
        page,
        lambda state: expected_marker in state.get("text", "")
        and detail_table_rendered(state.get("text", "")),
        description=f"detail period {label}",
    )
    # As on the overview, the period label repaints before the table refills.
    time.sleep(3)
    return page.state()


def ensure_all_channel(page: Page) -> None:
    """Product360 may restore an empty channel selector for a fresh profile."""
    text = page.state().get("text", "")
    if "渠道\n请选择" not in text:
        return
    page.click_exact_text("请选择")
    page.click_exact_text("全部")
    wait_for(page, lambda state: "渠道\n全部" in state.get("text", ""), description="all channel")


def collect_tirzepatide_amount(port: int) -> Decimal | None:
    """Total the near-30-day transaction amount of tirzepatide products.

    Runs on its own tab against the standalone 商品明细 page: the left-nav entry
    there is a real anchor navigation, so driving it from the overview tab would
    tear down the renderer the overview columns were just read from.
    """
    page = Page(port, url=PRODUCT_DETAIL_URL)
    try:
        wait_for(
            page,
            lambda state: "商品明细" in state.get("text", "")
            and "近30天" in state.get("text", "")
            and "查询" in state.get("text", ""),
            description="product detail page",
        )
        select_detail_period(page, "近30天")
        # Filter the detail table by the tirzepatide keyword and total the rows.
        # Filtering (rather than walking every page of every product) matches the
        # confirmed business definition and keeps pagination trivially small.
        page.type_keyword_filter(TIRZEPATIDE)
        state = wait_for(
            page,
            lambda candidate: "替尔泊肽" in candidate.get("text", "")
            or not has_spu_rows(candidate.get("text", "")),
            description="keyword-filtered tirzepatide rows or empty result",
            timeout=20,
        )
        rows: list[dict[str, Decimal | str]] = []
        signatures: set[str] = set()
        page_number = 1
        while True:
            text = state["text"]
            if not has_spu_rows(text):
                # A keyword query that matches nothing renders no SPU rows at all.
                return Decimal("0")
            page_rows = parse_keyword_rows(text)
            signature = "|".join(f"{row['name']}:{row['amount']}" for row in page_rows)
            if signature in signatures:
                raise RuntimeError("JD Product360 keyword-filtered pagination repeated a page")
            signatures.add(signature)
            rows.extend(page_rows)
            if not page.click_detail_next(page_number):
                break
            page_number += 1
            state = wait_for(
                page,
                lambda candidate, old=signature: "替尔泊肽" in candidate.get("text", "")
                and "|".join(f"{row['name']}:{row['amount']}" for row in parse_keyword_rows(candidate["text"])) != old,
                description="next keyword-filtered page",
            )
        return sum((row["amount"] for row in rows), Decimal("0"))
    finally:
        page.close()


def _collect_once(port: int) -> dict[str, dict[str, Decimal | None]]:
    page = Page(port)
    try:
        wait_for(
            page,
            lambda state: "商品概况" in state.get("text", "") and "成交金额" in state.get("text", ""),
            description="Product360 overview",
        )
        ensure_all_channel(page)
        daily = parse_overview(select_period(page, "昨天")["text"])
        near_30 = parse_overview(select_period(page, "近30天")["text"])
        tirzepatide_amount = None
        if near_30["transaction_amount"] is not None:
            try:
                tirzepatide_amount = collect_tirzepatide_amount(port)
            except RuntimeError:
                # The overview cards remain usable when Product360's optional
                # product-detail table does not render.  Preserve the unknown
                # non-tirzepatide ratio instead of discarding all core data.
                tirzepatide_amount = None
            if tirzepatide_amount is not None and tirzepatide_amount > near_30["transaction_amount"]:
                # The product-detail table and the overview card do not always
                # agree on scope: a store whose 30-day total only counts on-sale
                # services can still list delisted SPUs with sales in the table.
                # That makes the ratio meaningless, but it must not fail the
                # whole Product360 stage and lose the core business columns --
                # leave the ratio unknown and say so on stderr.
                print(
                    "warning: JD Product360 tirzepatide amount "
                    f"{tirzepatide_amount} exceeds near-30-day total "
                    f"{near_30['transaction_amount']}; non-tirzepatide ratio left unknown",
                    file=sys.stderr,
                )
                tirzepatide_amount = None
        near_30["tirzepatide_amount"] = tirzepatide_amount
        return {"daily": daily, "near_30": near_30}
    finally:
        page.close()


def collect(port: int) -> dict[str, dict[str, Decimal | None]]:
    """Collect from one visible Product360 page, retrying one CDP disconnect.

    The retry is deliberately narrow: a login, permission, parsing, or
    pagination failure remains a failed collection instead of being masked.
    """
    for attempt in range(2):
        try:
            return _collect_once(port)
        except (requests.RequestException, websocket.WebSocketException):
            if attempt:
                raise
            time.sleep(2)
    raise AssertionError("unreachable")


def store_settings(config: dict) -> dict[str, str]:
    store = config.get("store") or config.get("jd_store") or {}
    result = {
        "client": str(store.get("client") or "").strip(),
        "owner": str(store.get("owner") or "待维护").strip(),
        "platform": str(store.get("platform") or "京东").strip(),
    }
    if not result["client"]:
        raise ValueError("JD store config requires store.client")
    return result


def persist(conn, metrics: dict[str, dict[str, Decimal | None]], record_date: dt.date, *, schema: str, table: str, store: dict[str, str]) -> None:
    daily = metrics["daily"]
    near_30 = metrics["near_30"]
    target = qualified_workbench(schema, table)
    row = {
        "date": record_date,
        **store,
        "daily_amount": daily["transaction_amount"],
        "turnover": near_30["turnover_rate"],
        "listed": near_30["listed_spu_count"],
        "nonmoving": (
            near_30["listed_spu_count"] - near_30["moving_spu_count"]
            if near_30["listed_spu_count"] is not None and near_30["moving_spu_count"] is not None
            and near_30["listed_spu_count"] >= near_30["moving_spu_count"] else None
        ),
        "near_30_amount": near_30["transaction_amount"],
        "ring": near_30["transaction_amount_ring_ratio"],
        "non_tirzepatide_ratio": (
            (near_30["transaction_amount"] - near_30["tirzepatide_amount"]) / near_30["transaction_amount"]
            if near_30["transaction_amount"] is not None and near_30["transaction_amount"] != 0
            and near_30["tirzepatide_amount"] is not None else None
        ),
    }
    with conn.cursor() as cursor:
        cursor.execute(
            f"""INSERT INTO {target}
            (`日期`,`客户`,`商家负责人`,`平台`,`昨日交易额`,`商品动销率`,`经营商品数量`,`未动销商品数量`,
             `本月累计交易额`,`近30日环比`,`非替尔泊肽销售金额占比`)
            VALUES (%(date)s,%(client)s,%(owner)s,%(platform)s,%(daily_amount)s,%(turnover)s,%(listed)s,%(nonmoving)s,
                    %(near_30_amount)s,%(ring)s,%(non_tirzepatide_ratio)s)
            ON DUPLICATE KEY UPDATE
              `商家负责人`=VALUES(`商家负责人`), `昨日交易额`=VALUES(`昨日交易额`),
              `商品动销率`=VALUES(`商品动销率`), `经营商品数量`=VALUES(`经营商品数量`),
              `未动销商品数量`=VALUES(`未动销商品数量`), `本月累计交易额`=VALUES(`本月累计交易额`),
              `近30日环比`=VALUES(`近30日环比`), `非替尔泊肽销售金额占比`=VALUES(`非替尔泊肽销售金额占比`)""",
            row,
        )
    refresh_yesterday_week_ratio(
        conn,
        schema=schema,
        table=table,
        record_date=record_date,
        client=store["client"],
        platform=store["platform"],
    )


def run(args: argparse.Namespace) -> dict:
    config = json.loads(args.config.read_text(encoding="utf-8"))
    try:
        metrics = collect(args.port)
    except NoPermissionError:
        # A sub-account without the Product360 entitlement must not fail the
        # whole store cycle nor spam retries; exit code 3 lets the cycle mark
        # the day as analysed and move on.
        return {
            "state": "no_permission",
            "record_date": (args.record_date or dt.datetime.now(TZ).date()).isoformat(),
        }
    result = {
        "state": "success",
        "record_date": (args.record_date or dt.datetime.now(TZ).date()).isoformat(),
        "daily_transaction_amount": str(metrics["daily"]["transaction_amount"]) if metrics["daily"]["transaction_amount"] is not None else None,
        "near_30_transaction_amount": str(metrics["near_30"]["transaction_amount"]) if metrics["near_30"]["transaction_amount"] is not None else None,
        "near_30_tirzepatide_amount": str(metrics["near_30"]["tirzepatide_amount"]) if metrics["near_30"]["tirzepatide_amount"] is not None else None,
    }
    if args.dry_run:
        return result
    schema, table = workbench_target(config)
    mysql = config["mysql"]
    conn = pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]), user=mysql["user"], password=mysql["password"],
        database=mysql["database"], charset=mysql.get("charset", "utf8mb4"), autocommit=False,
    )
    try:
        persist(conn, metrics, args.record_date or dt.datetime.now(TZ).date(), schema=schema, table=table, store=store_settings(config))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect JD Product360 workbench metrics")
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--record-date", type=dt.date.fromisoformat)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    result = run(parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    # Exit 3 means "Product360 not authorised for this account today" so the
    # caller can write the daily marker and skip without failing the cycle.
    sys.exit(3 if result.get("state") == "no_permission" else 0)
