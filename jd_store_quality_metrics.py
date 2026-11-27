"""Collect the JD low information-score product count from the inspection page.

The inspection page (``wares-jdm.jd.com/ware/commodity-inspection``) shows one
row per SKU, and the previous implementation read every row to count the scores
below 90 locally.  That meant paging through the shop's entire SKU list — by far
the heaviest request volume in the whole JD collector set, and ``wares-jdm`` is
unstable under it (occasional 601).  It was therefore switched off on
2026-09-20.

The card opens on 「30天有销量商品」, whose tally includes products that are no
longer on sale.  The low-score figure is defined for 「售卖中商品」, so that tab is
selected first.  The page then exposes a score-range filter (``开始分数`` /
``结束分数``), which turns the same number into a single query: pick the tab, set
the range to 0-90, press 查询, and read the pager's ``共 N 条``.  One page load and
one query replaces N paginated page loads.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pymysql

from jd_store_sku_web_collect import Page
from workbench import qualified as qualified_workbench
from workbench import target as workbench_target


TZ = ZoneInfo("Asia/Shanghai")
INSPECTION_URL = "https://wares-jdm.jd.com/ware/commodity-inspection"
# "低于90分" is expressed as the inclusive range 0-90 on the page itself.
LOW_SCORE_MIN = 0
LOW_SCORE_MAX = 90
LOGIN_MARKERS = (
    "账号登录", "扫码登录", "统一登录", "京东验证", "请输入验证码",
    "安全验证", "访问受限",
)
SCORE_ROW = re.compile(
    r"商品编码：(\d+)\s*\nSKU编码：(\d+)\s*\n(?:-|\d+)\s*\n(\d+(?:\.\d+)?)\s*\n(?:较差|良好|优质)"
)

# The two score bounds live on plain <input class="range-input"> elements
# identified only by their placeholder text.  They are framework-controlled, so
# the value has to be written through the native setter and announced with
# input/change events — assigning `.value` directly is ignored.
SCORE_RANGE_JS = r"""
(() => {
  const inputs = Array.from(document.querySelectorAll('input.range-input'));
  const start = inputs.find(n => n.placeholder === '开始分数');
  const end = inputs.find(n => n.placeholder === '结束分数');
  if (!start || !end) return {ok: false, reason: 'score range inputs not found'};
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const set = (node, value) => {
    node.focus();
    setter.call(node, value);
    node.dispatchEvent(new Event('input', {bubbles: true}));
    node.dispatchEvent(new Event('change', {bubbles: true}));
    node.blur();
  };
  set(start, %(low)s);
  set(end, %(high)s);
  return {ok: true, start: start.value, end: end.value};
})()
"""

QUERY_BUTTON_JS = r"""
(() => {
  const vis = (n) => { if (!n) return false; const r = n.getBoundingClientRect();
    return !!(r.width && r.height); };
  const button = Array.from(document.querySelectorAll('button, .jd-button, [role=button]'))
    .filter(n => vis(n) && (n.innerText || '').trim() === '查询')[0];
  if (!button) return null;
  button.scrollIntoView({block: 'center', inline: 'nearest'});
  const rect = button.getBoundingClientRect();
  return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};
})()
"""

# The card offers two scopes: 「30天有销量商品」(default) and 「售卖中商品」.
# The low-score figure is only defined for the on-sale scope, so that tab is
# selected first — otherwise the count silently includes off-sale products.
ON_SALE_TAB_PREFIX = "售卖中商品"
ON_SALE_TAB_JS = r"""
(() => {
  const vis = (n) => { if (!n) return false; const r = n.getBoundingClientRect();
    return !!(r.width && r.height); };
  const tab = Array.from(document.querySelectorAll('.score-card__tab'))
    .filter(n => vis(n) && (n.innerText || '').trim().startsWith(%(prefix)s))[0];
  if (!tab) return null;
  tab.scrollIntoView({block: 'center', inline: 'nearest'});
  const rect = tab.getBoundingClientRect();
  return {text: (tab.innerText || '').trim(), x: rect.x + rect.width / 2,
          y: rect.y + rect.height / 2};
})()
"""


def store_settings(config: dict) -> dict[str, str]:
    store = config.get("store") or {}
    client = str(store.get("client") or "").strip()
    if not client:
        raise ValueError("JD store config requires store.client")
    return {
        "client": client,
        "owner": str(store.get("owner") or "待维护").strip(),
        "platform": str(store.get("platform") or "京东").strip(),
    }


def parse_scores(pages: list[str] | list[list[str]]) -> dict[str, float]:
    """Read per-SKU scores out of rendered inspection rows.

    Kept for offline verification against a captured page; the scheduled path
    uses the score-range filter instead of reading rows.
    """
    scores: dict[str, float] = {}
    for page in pages:
        text = "\n".join(page) if isinstance(page, list) else page
        for product_code, sku_id, score_text in SCORE_ROW.findall(text):
            value = float(score_text)
            previous = scores.get(sku_id)
            if previous is not None and previous != value:
                raise RuntimeError(f"JD SKU {sku_id} exposed conflicting information scores")
            scores[sku_id] = value
    return scores


def score_range_expression(low: int, high: int) -> str:
    return SCORE_RANGE_JS % {"low": json.dumps(str(low)), "high": json.dumps(str(high))}


def _state(page: Page) -> dict:
    # The pager's own total element is the authoritative count.  Scanning the
    # page for "共 N 条" also matches unrelated counters (the tab headers carry
    # their own product tallies), which is how an earlier revision reported 224
    # instead of the filtered result.
    return page.evaluate(r"""(() => {
      const text = document.body && document.body.innerText || '';
      const totalNode = document.querySelector('.jd-pagination__total');
      const match = totalNode && (totalNode.innerText.match(/(\d+)/) || [])[1];
      const pager = document.querySelector('.jd-pagination__sizes');
      const size = pager && (pager.innerText.match(/(\d+)\s*条\s*\/\s*页/) || [])[1];
      const tab = Array.from(document.querySelectorAll('.score-card__tab'))
        .find(node => (node.innerText || '').trim().startsWith('售卖中商品'));
      return {text, total: match ? Number(match) : null, pageSize: size || null,
              onSaleTab: tab ? (tab.innerText || '').trim() : null};
    })()""")


def _mouse_click(page: Page, coordinates: dict) -> None:
    # Travel and dwell instead of firing a press/release pair in the same
    # millisecond; see human_input.py for why the event shape matters.
    center_x = coordinates.get("x", 0) + coordinates.get("width", 0) / 2
    center_y = coordinates.get("y", 0) + coordinates.get("height", 0) / 2
    page.human.click(center_x, center_y)


def select_on_sale_expression() -> str:
    # ensure_ascii=False keeps the tab label readable in the generated JS
    # (escaped \uXXXX works but makes debugging the expression painful).
    return ON_SALE_TAB_JS % {
        "prefix": json.dumps(ON_SALE_TAB_PREFIX, ensure_ascii=False)
    }


def _read_settled_total(page: Page, settle_seconds: float = 5.0, polls: int = 3) -> int:
    """Wait for the filtered count, then confirm it has stopped changing.

    The pager starts at 0, so a query whose result happens to equal the previous
    reading cannot be detected by difference; poll until two reads agree.
    """
    time.sleep(settle_seconds)
    previous: int | None = None
    for _ in range(polls):
        total = _state(page).get("total")
        if total is None:
            raise RuntimeError("JD commodity-inspection pager exposed no total")
        if previous is not None and int(total) == previous:
            return int(total)
        previous = int(total)
        time.sleep(1)
    if previous is None:
        raise RuntimeError("JD commodity-inspection pager exposed no total")
    return previous


def collect_low_score_count(
    port: int, low: int = LOW_SCORE_MIN, high: int = LOW_SCORE_MAX
) -> int:
    """Return how many on-sale SKUs the inspection filter reports in the range."""
    page = Page(port, INSPECTION_URL, force_new=True)
    try:
        page.call("Runtime.enable")
        state = None
        for _ in range(30):
            state = _state(page)
            if any(marker in state["text"] for marker in LOGIN_MARKERS):
                raise RuntimeError("JD browser login or verification is required")
            # The heading is rendered before the scope tabs during a cold
            # browser start. Wait for the actual control without reloading or
            # issuing another platform query.
            if "商品信息体检" in state["text"] and page.evaluate(select_on_sale_expression()):
                break
            time.sleep(1)
        else:
            raise RuntimeError("JD commodity-inspection page did not render its filter form")

        # Scope first: the card opens on 「30天有销量商品」, whose tally includes
        # products that are no longer on sale.
        tab = page.evaluate(select_on_sale_expression())
        if not tab:
            raise RuntimeError("JD commodity-inspection on-sale tab not found")
        _mouse_click(page, tab)
        time.sleep(3)

        filled = page.evaluate(score_range_expression(low, high))
        if not filled.get("ok"):
            raise RuntimeError(f"JD commodity-inspection score filter missing: {filled}")

        button = page.evaluate(QUERY_BUTTON_JS)
        if not button:
            raise RuntimeError("JD commodity-inspection query button not found")
        _mouse_click(page, button)

        total = _read_settled_total(page)
        if any(marker in _state(page)["text"] for marker in LOGIN_MARKERS):
            raise RuntimeError("JD browser login or verification is required")
        return total
    finally:
        page.close()


def persist(conn, config: dict, record_date: dt.date, low_score_count: int) -> None:
    store = store_settings(config)
    schema, table = workbench_target(config)
    target = qualified_workbench(schema, table)
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {target} (`日期`,`客户`,`商家负责人`,`平台`,`低于90分商品数`)
            VALUES (%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              `商家负责人`=VALUES(`商家负责人`),
              `低于90分商品数`=VALUES(`低于90分商品数`)""",
            (record_date, store["client"], store["owner"], store["platform"], low_score_count),
        )


def run(args: argparse.Namespace) -> dict:
    config = json.loads(args.config.read_text(encoding="utf-8"))
    record_date = args.record_date or dt.datetime.now(TZ).date()
    low_score_count = collect_low_score_count(args.port)
    if not args.dry_run:
        mysql = config["mysql"]
        conn = pymysql.connect(
            host=mysql["host"], port=int(mysql["port"]), user=mysql["user"], password=mysql["password"],
            database=mysql["database"], charset=mysql.get("charset", "utf8mb4"), autocommit=False,
        )
        try:
            persist(conn, config, record_date, low_score_count)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {
        "state": "success",
        "record_date": record_date.isoformat(),
        "scope": ON_SALE_TAB_PREFIX,
        "score_range": [LOW_SCORE_MIN, LOW_SCORE_MAX],
        "low_score_product_count": low_score_count,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect JD low information-score product count")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--record-date", type=dt.date.fromisoformat)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2))
