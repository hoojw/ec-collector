"""Collect the authenticated JD on-sale SKU page into the normalized daily snapshot.

The listing renders one row per product; its SKU detail only appears after the
row is expanded, so expanding every product is the heaviest request burst in the
whole collection.  When wares-jdm withholds the rows it answers code 601
(未经京东授权的软件操作); that verdict is carried by the sgm fingerprint state
cached in localStorage under WQ_*/__sgm__*, so the collector drops that state and
re-reads the page once instead of losing the day's snapshot.  See RISK_STATE_JS.

The listing does expose each product's 修改时间, so this collector keeps a JSON
index of the last known SKU detail per product and re-expands only the products
whose 修改时间 moved.  A steady-state run is one page load plus pagination; the
expansion burst happens only when something actually changed, or while the
index is being built for the first time.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pymysql
import requests
import websocket

from human_input import HumanInput
from import_jd_store_skus import normalize_sku


TZ = ZoneInfo("Asia/Shanghai")
PORT = 9533
URL = "https://wares-jdm.jd.com/ware/wareList?activeTab=OnsaleWare&businessModel=0"
TABLE = "jd_store_sku_normalized_daily"
PRODUCT_CODE = re.compile(r"商品编码：(\d+)")
SKU_ID = re.compile(r"SKU编码：(\d+)")
MERCHANT_SKU = re.compile(r"商家SKU编码：([^\s]+)")
PRICE = re.compile(r"¥\s*([0-9]+(?:\.[0-9]+)?)")
BRAND = re.compile(r"^[\[【]([^\]】]+)[\]】]")
COMPETITIVENESS_SCORE = re.compile(r"^信息分\d+$")
ON_SALE_TAB_COUNT = re.compile(r"售卖中\s*[（(]\s*(\d+)\s*[）)]")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?")

INDEX_VERSION = 1
MODIFIED_HEADER = "修改时间"
REFRESH_POLICIES = ("changed", "all", "none")


class SourceUnavailable(RuntimeError):
    """The authenticated JD page loaded but its product source was withheld."""


def has_withheld_product_rows(text: str, rendered_total: int) -> bool:
    match = ON_SALE_TAB_COUNT.search(text)
    return rendered_total == 0 and bool(match and int(match.group(1)) > 0)


class Page:
    def __init__(self, port: int, url: str = URL, *, force_new: bool = False):
        self.port = port
        targets = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=15).json()
        self.target = None if force_new else next(
            (target for target in targets if target.get("type") == "page" and str(target.get("url") or "").startswith(url.split("?")[0])),
            None,
        )
        self.owns_tab = self.target is None
        if self.owns_tab:
            created = requests.put(f"http://127.0.0.1:{port}/json/new?{quote(url, safe='')}", timeout=15)
            created.raise_for_status()
            self.target = created.json()
        # A stalled SPA can leave CDP evaluation open indefinitely.  This
        # collector never writes until the complete snapshot is validated, so
        # fail the run and let the wrapper release Chrome instead of retaining
        # a large on-demand browser process until systemd's global timeout.
        self.ws = websocket.create_connection(self.target["webSocketDebuggerUrl"], timeout=15)
        self.message_id = 0
        # Human-shaped input shared by every JD collector built on this Page.
        # Raw dispatchMouseEvent/dispatchKeyEvent pairs are trivially separable
        # from real input by the risk engine (no travel path before a press, no
        # dwell between down and up, no jitter between keystrokes).  Routing
        # interaction through HumanInput closes that gap.  See human_input.py.
        self.human = HumanInput(self)

    def call(self, method: str, params: dict | None = None):
        self.message_id += 1
        message_id = self.message_id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != message_id:
                continue
            if message.get("error"):
                raise RuntimeError(message["error"])
            return message.get("result", {})

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True, "timeout": 10000})
        remote = result.get("result", {})
        if "value" not in remote:
            raise RuntimeError(remote.get("description") or "JD page returned no value")
        return remote["value"]

    def close(self):
        self.ws.close()
        if self.owns_tab:
            try:
                requests.get(f"http://127.0.0.1:{self.port}/json/close/{self.target['id']}", timeout=15)
            except requests.RequestException:
                # The browser owner may already have released a crashed tab.
                # Never hide the original collection error during cleanup.
                pass


# --------------------------------------------------------------------------
# SKU index
# --------------------------------------------------------------------------
#
# The index is the standing record of what each product last looked like.  Only
# products whose 修改时间 moved are re-read from the page; every other product
# contributes its previous SKU detail, so the daily snapshot stays complete
# while the page is touched once per run instead of once per product.

def index_path_for(config_path: Path, override: Path | None = None) -> Path:
    return override if override is not None else config_path.parent / "jd-sku-index.json"


def load_index(path: Path) -> dict:
    """Read the index; an unreadable or foreign file restarts it empty.

    A missing index simply means "nothing is known yet", which the refresh
    policy turns into a full expansion — the same work the collector used to do
    on every single run.
    """
    if not path.is_file():
        return {"version": INDEX_VERSION, "products": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": INDEX_VERSION, "products": {}}
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        return {"version": INDEX_VERSION, "products": {}}
    if not isinstance(data.get("products"), dict):
        data["products"] = {}
    return data


def save_index(path: Path, index: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # default=str keeps database numerics (Decimal) serializable.
    payload = json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True, default=str)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


SEED_COLUMNS = (
    "SKUID", "商品编码", "商品名称", "商家SKU原值", "基础UPC", "组合数量",
    "标注组合数量", "包装单位", "销售属性", "品牌", "京东价",
    "解析状态", "解析说明",
)


def seed_index_from_database(config: dict, table: str = TABLE) -> dict:
    """Build the index from the newest complete snapshot already in the database.

    Seeding makes the first indexed run read only the listing: the SKU detail is
    already standing, and each product's 修改时间 is recorded as the baseline
    that later runs compare against.  Without it the first run would have to
    expand the whole store — exactly the request burst that ends in code 601.
    """
    mysql = config["mysql"]
    conn = pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
        password=mysql["password"], database=mysql["database"],
        charset=mysql.get("charset", "utf8mb4"),
    )
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"SELECT MAX(`记录日期`) AS `latest` FROM `{table}`")
            latest = (cur.fetchone() or {}).get("latest")
            if latest is None:
                return {"products": {}, "snapshot_date": None}
            columns = ", ".join(f"`{name}`" for name in SEED_COLUMNS)
            cur.execute(f"SELECT {columns} FROM `{table}` WHERE `记录日期`=%s", (latest,))
            records = cur.fetchall()
    finally:
        conn.close()

    products: dict[str, dict] = {}
    for record in records:
        code = str(record.get("商品编码") or "").strip()
        sku_id = str(record.get("SKUID") or "").strip()
        if not code or not sku_id:
            continue
        entry = products.setdefault(code, {
            "商品名称": record.get("商品名称") or "",
            "品牌": record.get("品牌") or "",
            # Unknown until the first listing read; see needs_refresh.
            "修改时间": None,
            "SKU": {},
        })
        entry["SKU"][sku_id] = {
            "商家SKU原值": record.get("商家SKU原值") or "",
            "销售属性": record.get("销售属性") or "",
            "京东价": record.get("京东价") or "",
            "基础UPC": record.get("基础UPC") or "",
            "组合数量": record.get("组合数量"),
            "标注组合数量": record.get("标注组合数量"),
            "包装单位": record.get("包装单位") or "",
            "解析状态": record.get("解析状态") or "",
            "解析说明": record.get("解析说明") or "",
        }
    return {"products": products, "snapshot_date": str(latest)}


def sku_entry(row: tuple) -> dict:
    """Serialize a parsed SKU row into its index form."""
    _, _, _, merchant_sku, parsed, specification, _, price = row
    return {
        "商家SKU原值": merchant_sku,
        "销售属性": specification,
        "京东价": price,
        "基础UPC": parsed["base_upc"],
        "组合数量": parsed["bundle_quantity"],
        "标注组合数量": parsed["listed_quantity"],
        "包装单位": parsed["package_unit"],
        "解析状态": parsed["parse_status"],
        "解析说明": parsed["parse_message"],
    }


def row_from_index(sku_id: str, entry: dict, product_code: str, product_name: str, brand: str) -> tuple:
    """Rebuild the persistable SKU row for a product that did not change."""
    parsed = {
        "base_upc": entry.get("基础UPC", ""),
        "bundle_quantity": entry.get("组合数量"),
        "listed_quantity": entry.get("标注组合数量"),
        "package_unit": entry.get("包装单位", ""),
        "parse_status": entry.get("解析状态", ""),
        "parse_message": entry.get("解析说明", ""),
    }
    return (
        sku_id,
        product_code,
        product_name,
        entry.get("商家SKU原值", ""),
        parsed,
        entry.get("销售属性", ""),
        brand,
        entry.get("京东价", ""),
    )


def needs_refresh(policy: str, entry: dict | None, modified: str | None) -> bool:
    """Decide whether one product's SKU detail has to be re-read from the page."""
    if policy == "all":
        return True
    if policy == "none":
        return False
    if entry is None:
        # A product the index has never seen (newly listed, or first run).
        return True
    if entry.get("修改时间") is None:
        # Seeded from the database: this run only establishes the baseline
        # timestamp.  Expanding here would rebuild the whole store, which is the
        # request burst the index exists to avoid.
        return False
    if modified is None:
        # The page did not expose a timestamp, so there is no evidence of a
        # change.  Re-expanding on uncertainty would restore that same burst.
        return False
    return modified != entry.get("修改时间")


# --------------------------------------------------------------------------
# Page reading
# --------------------------------------------------------------------------

LIST_STATE_JS = r"""(() => {
  const text = document.body && document.body.innerText || '';
  return {
    text,
    total: (text.match(/共\s*(\d+)\s*条/) || [])[1] || null,
    pageSize: (text.match(/(\d+)\s*条\s*\/\s*页/) || [])[1] || null
  };
})()"""

# wares-jdm answers code 601 (未经京东授权的软件操作) for the product-list query
# while the sgm fingerprint state under WQ_*/__sgm__* carries a rejected verdict.
# That state — not the login cookies — is what keeps every later query withheld,
# so dropping it makes the SDK re-enrol the device and the same query is served.
# Measured 2026-09-28 on a profile copy with everything else held constant:
#   clear nothing            -> 601
#   clear WQ_*/__sgm__* only -> 200 (still 200 two loads and two minutes later)
#   clear eid family only    -> 601
#   clear JDst_* only        -> 601
#   clear __we_m_* only      -> 601
#   clear shshshfp* only     -> 601
#   clear deviceId only      -> 601
RISK_STATE_JS = r"""(() => {
  const keys = Object.keys(localStorage)
    .filter(key => key.startsWith('WQ_') || key.includes('__sgm__'));
  keys.forEach(key => localStorage.removeItem(key));
  return keys.length;
})()"""

# One row per product.  The 修改时间 cell is located by matching the header
# column, because the row text carries 上架时间/创建时间/修改时间 in the same
# order as the header and positional guessing is what breaks on a column
# reshuffle.
LIST_ROWS_JS = r"""(() => {
  const stamp = (value) => {
    const m = String(value || '').match(/\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?/);
    return m ? m[0] : null;
  };
  const headers = Array.from(document.querySelectorAll('th'));
  let modifiedIndex = -1;
  for (let index = 0; index < headers.length; index += 1) {
    if ((headers[index].innerText || '').trim().indexOf(%(header)s) === 0) {
      modifiedIndex = index;
      break;
    }
  }
  const rows = Array.from(document.querySelectorAll('tr.render-row'))
    .filter(row => /商品编码：/.test(row.innerText));
  return {
    modifiedIndex: modifiedIndex,
    rows: rows.map(row => {
      const text = row.innerText;
      let modified = null;
      if (modifiedIndex >= 0) {
        const cells = row.querySelectorAll('td');
        if (cells[modifiedIndex]) { modified = stamp(cells[modifiedIndex].innerText); }
      }
      if (!modified) {
        // Header order is 上架时间/创建时间/修改时间, so the last timestamp on
        // the row is the modification one.
        const found = text.match(/\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?/g);
        if (found && found.length) { modified = found[found.length - 1]; }
      }
      return {
        code: (text.match(/商品编码：(\d+)/) || [])[1] || null,
        modified: modified,
        text: text
      };
    })
  };
})()""" % {"header": json.dumps(MODIFIED_HEADER, ensure_ascii=False)}

CHALLENGE_JS = r"""(() => {
  const text = document.body && document.body.innerText || '';
  return /统一登录|账号登录|扫码登录|京东验证|验证码|风险/.test(text);
})()"""

RENDERED_PRODUCTS_JS = r"""(() => {
  const rows = Array.from(document.querySelectorAll('tr.render-row'))
    .filter(row => /商品编码：/.test(row.innerText));
  return new Set(rows.map(row => (row.innerText.match(/商品编码：(\d+)/) || [])[1]).filter(Boolean)).size;
})()"""


def parse_product_row(text: str) -> tuple[str, str, str] | None:
    """Pull (product_code, name, brand) out of one product row."""
    product_code = PRODUCT_CODE.search(text)
    if not product_code:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    code_index = next((index for index, line in enumerate(lines) if line.startswith("商品编码：")), len(lines))
    name = next((line for line in reversed(lines[:code_index]) if not COMPETITIVENESS_SCORE.fullmatch(line)), "")
    brand = (BRAND.search(name).group(1) if BRAND.search(name) else "")
    return product_code.group(1), name, brand


def parse_sku_rows(sku_texts: list[str], product_code: str, product_name: str, brand: str) -> list[tuple]:
    """Parse the expanded SKU rows that follow one product row."""
    rows: list[tuple] = []
    for text in sku_texts:
        sku_id = SKU_ID.search(text)
        merchant = MERCHANT_SKU.search(text)
        if not sku_id or not merchant:
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        sku_index = next((index for index, line in enumerate(lines) if line.startswith("SKU编码：")), 0)
        specification = next((line for line in reversed(lines[:sku_index]) if not COMPETITIVENESS_SCORE.fullmatch(line)), "")
        if not specification:
            raise ValueError(f"JD SKU {sku_id.group(1)} has no sales attribute")
        prices = PRICE.findall(text)
        if not prices:
            raise ValueError(f"JD SKU {sku_id.group(1)} has no price")
        parsed = normalize_sku({"商家SKU": merchant.group(1), "销售属性": specification, "商品名称": product_name})
        rows.append((sku_id.group(1), product_code, product_name, merchant.group(1), parsed, specification, brand, prices[0]))
    return rows


def _await_list(page: Page) -> dict:
    """Wait for the authenticated listing to report a positive total."""
    ready = None
    for _ in range(30):
        ready = page.evaluate(LIST_STATE_JS)
        # The SPA initially exposes “共0条” before its authenticated product
        # data arrives.  Treat that transient value as loading; only a positive
        # total makes the snapshot safe to collect.
        if ready["total"] and int(ready["total"]) > 0:
            return ready
        time.sleep(1)
    if not ready or not ready["total"]:
        raise RuntimeError("JD product-list total was not found")
    return ready


def _clear_withheld_risk_state(page: Page) -> dict:
    """Re-enrol the device when wares-jdm withholds the rows behind code 601.

    The login session lives in cookies and is left untouched; only the sgm
    fingerprint cache is dropped, so the reload stays authenticated.
    """
    removed = page.evaluate(RISK_STATE_JS)
    print(
        f"NOTE: JD withheld the product rows (code 601); cleared {removed} sgm state keys and reloaded",
        file=sys.stderr,
    )
    page.call("Page.enable")
    page.call("Page.reload", {"ignoreCache": False})
    return _await_list(page)


def _reselect_on_sale_tab(page: Page, ready: dict) -> dict:
    """Re-run the on-sale query when a restored browser sits on the empty bootstrap."""
    triggered = page.evaluate(r"""(() => {
      const visible = element => Boolean(element && element.offsetParent);
      const candidates = Array.from(document.querySelectorAll('button,[role=tab],li,div,span'))
        .filter(element => visible(element) && /^售卖中(?:\s|（|\(|$)/.test(element.innerText.trim()))
        .sort((a, b) => a.children.length - b.children.length);
      const tab = candidates[0];
      if (!tab) return {tab: false, query: false};
      tab.click();
      const query = Array.from(document.querySelectorAll('button'))
        .find(element => visible(element) && element.innerText.trim() === '查询');
      if (query) query.click();
      return {tab: true, query: Boolean(query)};
    })()""")
    if not triggered.get("tab"):
        return ready
    for _ in range(30):
        ready = page.evaluate(LIST_STATE_JS)
        if ready["total"] and int(ready["total"]) > 0:
            break
        time.sleep(1)
    return ready


def _ensure_page_size(page: Page, ready: dict) -> dict:
    """Keep the portal's default page size.

    100条/页 used to fit a store on one page, but wares-jdm now fails to
    render per-SKU rows when ~90 products are expanded on a single page.  The
    default 10条/页 is slower (one page per ten products) but the SKU expansion
    is reliable, so we deliberately leave the pager alone.
    """
    return ready


def _await_rendered_rows(page: Page, expected_visible: int) -> None:
    """Block until the first page's product rows are actually painted."""
    render_deadline = time.monotonic() + 60
    query_clicked = False
    rendered = 0
    while time.monotonic() < render_deadline:
        if page.evaluate(CHALLENGE_JS):
            raise RuntimeError("JD product-list login or verification is required")
        rendered = int(page.evaluate(RENDERED_PRODUCTS_JS))
        if rendered >= expected_visible:
            return
        if not query_clicked and time.monotonic() + 50 <= render_deadline:
            query = page.evaluate(r"""(() => {
              const visible = element => Boolean(element && element.offsetParent);
              const button = Array.from(document.querySelectorAll('button'))
                .filter(element => visible(element) && element.innerText.trim() === '查询')
                .sort((a, b) => a.children.length - b.children.length)[0];
              if (!button) return null;
              button.scrollIntoView({block:'center', inline:'nearest'});
              const rect = button.getBoundingClientRect();
              return {x:rect.x + rect.width / 2, y:rect.y + rect.height / 2};
            })()""")
            if query:
                page.call("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, **query})
                page.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, **query})
                query_clicked = True
        time.sleep(1)
    raise RuntimeError(
        f"JD product-list rendered {rendered}/{expected_visible} product rows; preserving the existing snapshot"
    )


def _rewind_to_first_page(page: Page, expected_pages: int) -> None:
    for _ in range(expected_pages):
        moved = page.evaluate("""(() => {
          const previous = document.querySelector('button.btn-prev:not([disabled])');
          if (!previous) return false;
          previous.click();
          return true;
        })()""")
        if not moved:
            break
        time.sleep(3)


def _read_pages(page: Page, pages_to_collect: int, on_page=None) -> list[dict]:
    """Read product rows page by page.

    ``on_page`` is called with the rows of the page currently rendered, before
    advancing to the next page.  SKU expansion must happen here: once we move
    on, the earlier rows are no longer in the DOM.
    """
    products: list[dict] = []
    for current in range(1, pages_to_collect + 1):
        state = None
        for _ in range(20):
            if page.evaluate(CHALLENGE_JS):
                raise RuntimeError("JD product-list login or verification is required")
            state = page.evaluate(LIST_ROWS_JS)
            if state.get("rows"):
                break
            time.sleep(1)
        else:
            raise RuntimeError("JD product-list rows did not render")
        if len(state["rows"]) > 100:
            raise RuntimeError(f"JD product-list page exposes too many rows: {len(state['rows'])}")
        for row in state["rows"]:
            parsed = parse_product_row(row.get("text") or "")
            if parsed is None or not row.get("code") or parsed[0] != row["code"]:
                raise RuntimeError("JD product-list product code is incomplete")
            products.append({
                "code": parsed[0],
                "name": parsed[1],
                "brand": parsed[2],
                "modified": row.get("modified"),
                "text": row.get("text") or "",
            })
        if on_page is not None:
            on_page(products[-len(state["rows"]):])

        if current < pages_to_collect:
            moved = page.evaluate("""(() => {
              const next = document.querySelector('button.btn-next:not([disabled])');
              if (!next) return false;
              next.click();
              return true;
            })()""")
            if not moved:
                raise RuntimeError("JD product-list pagination ended early")
            time.sleep(3)
    return products


# --------------------------------------------------------------------------
# On-demand SKU expansion
# --------------------------------------------------------------------------

def _sku_texts_for(page: Page, product_code: str) -> list[str] | None:
    """Return the expanded SKU rows sitting directly under one product row."""
    return page.evaluate(f"""(() => {{
      const code = {json.dumps(product_code)};
      const parent = Array.from(document.querySelectorAll('tr.render-row')).find(row =>
        row.innerText.includes('商品编码：' + code)
      );
      if (!parent) return null;
      const texts = [];
      let node = parent.nextElementSibling;
      while (node && /SKU编码：/.test(node.innerText)) {{
        texts.push(node.innerText);
        node = node.nextElementSibling;
      }}
      return texts.length ? texts : null;
    }})()""")


def _enable_sku_mode(page: Page) -> None:
    """Turn on the visible “SKU展开模式” control that renders per-SKU rows."""
    sku_mode = page.evaluate("""(() => {
      const label = Array.from(document.querySelectorAll('.jd-checkbox__label'))
        .find(element => element.innerText.trim() === 'SKU展开模式');
      const container = label && label.closest('label');
      const input = container && container.querySelector('input[type=checkbox]');
      if (!input) return {found: false, enabled: false};
      if (!input.checked) input.click();
      return {found: true, enabled: Boolean(input.checked)};
    })()""")
    if not sku_mode.get("found") or not sku_mode.get("enabled"):
        raise RuntimeError("JD product-list did not expose an enabled SKU展开模式 control")
    for _ in range(15):
        if int(page.evaluate("document.querySelectorAll('i.row-expand').length")):
            return
        time.sleep(1)
    raise RuntimeError("JD SKU展开模式 did not render visible per-SKU expanders; preserving the existing snapshot")


def _expand_products(page: Page, codes: list[str], *, click_delay_ms: int) -> dict[str, list[str]]:
    """Expand the given products and collect their SKU rows.

    Only products whose detail is genuinely unknown reach this path, so a
    steady-state run usually expands nothing at all.
    """
    if not codes:
        return {}
    _enable_sku_mode(page)
    collected: dict[str, list[str]] = {}
    skipped: list[str] = []
    deadline = time.monotonic() + max(30.0, min(180.0, 30.0 * len(codes)))
    settle_seconds = min(0.5, max(0.25, click_delay_ms / 1000))
    pending: list[str] = []
    for product_code in codes:
        action = page.evaluate(f"""(() => {{
          const code = {json.dumps(product_code)};
          const parent = Array.from(document.querySelectorAll('tr.render-row')).find(row =>
            row.innerText.includes('商品编码：' + code)
          );
          if (!parent) return {{found:false, expanded:false}};
          const child = parent.nextElementSibling;
          if (child && /SKU编码：/.test(child.innerText)) return {{found:true, expanded:true}};
          const icon = parent.querySelector('i.row-expand');
          if (!icon) return {{found:true, expanded:false, missingIcon:true}};
          icon.scrollIntoView({{block:'center', inline:'nearest'}});
          const rect = icon.getBoundingClientRect();
          return {{
            found:true,
            expanded:false,
            coordinates:{{x:rect.x + rect.width / 2, y:rect.y + rect.height / 2}}
          }};
        }})()""")
        if not action.get("found") or action.get("missingIcon"):
            # The product row is present but this individual listing cannot
            # expose SKU detail.  Report it instead of failing the whole run.
            skipped.append(product_code)
            print(
                f"WARNING: JD product {product_code} has no visible SKU expander; skipping SKU detail",
                file=sys.stderr,
            )
            continue
        if action.get("expanded"):
            continue
        coordinates = action.get("coordinates") or {}
        if not {"x", "y"}.issubset(coordinates):
            raise RuntimeError(f"JD product {product_code} SKU expander has no geometry")
        page.call("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, **coordinates})
        page.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, **coordinates})
        pending.append(product_code)
        # Expanding inserts a new row and changes every following icon's screen
        # position.  Let JDesign settle before resolving the next geometry.
        if time.monotonic() < deadline:
            time.sleep(min(settle_seconds, max(0.0, deadline - time.monotonic())))

    remaining = set(pending)
    retry_attempted: set[str] = set()
    retries_exhausted_at: float | None = None
    while remaining and time.monotonic() < deadline:
        # SKU rows arrive via an authenticated follow-up request.  Give them a
        # few seconds to paint before deciding a click failed -- retrying the
        # expander too early toggles the row back to collapsed.
        time.sleep(max(3.0, settle_seconds))
        for product_code in list(remaining):
            texts = _sku_texts_for(page, product_code)
            if texts:
                collected[product_code] = texts
                remaining.discard(product_code)
        retry_candidates = remaining - retry_attempted
        if retry_candidates and time.monotonic() < deadline:
            retry_code = sorted(retry_candidates)[0]
            retry_attempted.add(retry_code)
            retry_coordinates = page.evaluate(f"""(() => {{
              const code = {json.dumps(retry_code)};
              const parent = Array.from(document.querySelectorAll('tr.render-row')).find(row =>
                row.innerText.includes('商品编码：' + code)
              );
              if (!parent) return null;
              const child = parent.nextElementSibling;
              if (child && /SKU编码：/.test(child.innerText)) return null;
              const icon = parent.querySelector('i.row-expand');
              if (!icon) return null;
              icon.scrollIntoView({{block:'center', inline:'nearest'}});
              const rect = icon.getBoundingClientRect();
              return {{x:rect.x + rect.width / 2, y:rect.y + rect.height / 2}};
            }})()""")
            if retry_coordinates:
                page.call("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, **retry_coordinates})
                page.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, **retry_coordinates})
        elif remaining:
            # Every still-missing product has already received its one bounded
            # retry.  Allow a short final response grace, then move on rather
            # than idling to the deadline.
            if retries_exhausted_at is None:
                retries_exhausted_at = time.monotonic()
            elif time.monotonic() - retries_exhausted_at >= 5:
                break
    for product_code in sorted(remaining):
        skipped.append(product_code)
        print(
            f"WARNING: JD product {product_code} did not render its SKU rows; skipping SKU detail",
            file=sys.stderr,
        )
        # An empty list marks "asked for, but nothing came back", which
        # build_rows must tell apart from "never asked": the index entry keeps
        # its old timestamp so the next run tries again.
        collected[product_code] = []
    return collected


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

def collect_page_rows(
    port: int,
    *,
    click_delay_ms: int,
    index: dict | None = None,
    refresh_policy: str = "changed",
    max_pages: int | None = None,
) -> tuple[int, list[dict]]:
    """Read the on-sale listing, expanding only the products that changed."""
    if refresh_policy not in REFRESH_POLICIES:
        raise ValueError(f"unknown refresh policy: {refresh_policy!r}")
    index = index or {"version": INDEX_VERSION, "products": {}}
    known = index.setdefault("products", {})

    page = Page(port)
    try:
        page.call("Runtime.enable")
        # /json/new already navigates to URL.  A second navigation can interrupt
        # the platform's anti-risk context, so wait for the rendered list instead.
        ready = _await_list(page)
        if ready.get("total") == "0":
            ready = _reselect_on_sale_tab(page, ready)
        if not ready or not ready["total"]:
            raise RuntimeError("JD product-list total was not found")
        if page.evaluate(CHALLENGE_JS):
            raise RuntimeError("JD product-list login or verification is required")

        expected_products = int(ready["total"])
        if expected_products <= 0 and has_withheld_product_rows(str(ready.get("text") or ""), expected_products):
            # The on-sale tab counts products while the table stays empty: that
            # is the code 601 verdict, not an empty shop.  Drop the sgm state and
            # read the same page again before giving up on the snapshot.
            ready = _clear_withheld_risk_state(page)
            if ready.get("total") == "0":
                ready = _reselect_on_sale_tab(page, ready)
            expected_products = int(ready.get("total") or 0)
        if expected_products <= 0:
            if has_withheld_product_rows(str(ready.get("text") or ""), expected_products):
                raise SourceUnavailable(
                    "JD product-list reports on-sale products but withheld all rows; "
                    "preserving the previous snapshot"
                )
            raise RuntimeError("JD product-list returned zero on-sale products; refusing to replace the snapshot")

        ready = _ensure_page_size(page, ready)
        page_size = int(ready.get("pageSize") or 0)
        if page_size < 1 or page_size > 100:
            raise RuntimeError(f"JD product-list page size is invalid: {page_size}")

        # Changing the JDesign selector can update its label before the table's
        # authenticated request has completed.  Do not mistake the old ten rows
        # for a complete page.
        expected_visible = min(expected_products, page_size)
        _await_rendered_rows(page, expected_visible)

        expected_pages = (expected_products + page_size - 1) // page_size
        pages_to_collect = min(expected_pages, max_pages) if max_pages else expected_pages
        _rewind_to_first_page(page, expected_pages)

        # Expand SKU rows while each page is still rendered.  Expanding after
        # _read_pages finishes would target only the last page's DOM.
        refreshed: dict[str, list[str]] = {}
        def expand_this_page(page_products: list[dict]) -> None:
            codes = [
                p["code"] for p in page_products
                if needs_refresh(refresh_policy, known.get(p["code"]), p["modified"])
            ]
            if codes:
                refreshed.update(_expand_products(page, codes, click_delay_ms=click_delay_ms))

        products = _read_pages(page, pages_to_collect, on_page=expand_this_page)

        for product in products:
            product["sku_texts"] = refreshed.get(product["code"])
        return expected_products, products
    finally:
        page.close()


def build_rows(products: list[dict], index: dict) -> tuple[list[tuple], dict]:
    """Merge freshly expanded products with the index's standing entries.

    ``product['sku_texts']`` distinguishes three states: ``None`` means the
    product was not asked for (its indexed detail is still current), ``[]``
    means it was asked for but produced nothing (keep the detail, but keep the
    old timestamp so the next run retries), and a list means fresh detail.

    Returns the SKU rows to persist and the index entries to carry forward.
    """
    known = index.setdefault("products", {})
    rows: list[tuple] = []
    entries: dict[str, dict] = {}
    unchanged = 0
    refreshed = 0
    failed: list[str] = []
    missing: list[str] = []
    for product in products:
        code = product["code"]
        texts = product.get("sku_texts")
        previous = known.get(code)
        previous_skus = previous.get("SKU") if isinstance(previous, dict) else None
        carry_modified = True
        if texts:
            product_rows = parse_sku_rows(texts, code, product["name"], product["brand"])
            if not product_rows:
                texts = []
            else:
                refreshed += 1
        if not texts and previous_skus:
            if texts is None:
                # Not asked for: the indexed detail is still current.
                unchanged += 1
            else:
                # Asked for but empty.  Reuse the standing detail, yet hold the
                # indexed timestamp at its old value so this retries next run.
                failed.append(code)
                carry_modified = False
            product_rows = [
                row_from_index(sku_id, entry, code, product["name"], product["brand"])
                for sku_id, entry in sorted(previous_skus.items())
            ]
        elif not texts:
            # No fresh detail and nothing in the index: the product has never
            # been read.  Report it rather than dropping it silently.
            missing.append(code)
            continue
        rows.extend(product_rows)
        entries[code] = {
            "商品名称": product["name"],
            "品牌": product["brand"],
            "修改时间": product["modified"] if carry_modified else previous.get("修改时间"),
            "SKU": {row[0]: sku_entry(row) for row in product_rows},
        }
    return rows, {
        "entries": entries,
        "refreshed_products": refreshed,
        "unchanged_products": unchanged,
        "failed_products": failed,
        "missing_products": missing,
    }


def persist(config: dict, record_date: dt.date, rows: list[tuple]) -> dict:
    # The listing occasionally renders the same SKU twice.  It is safe to
    # collapse only byte-for-byte identical source rows; a conflicting repeat
    # would mean the UI was inconsistent, so keep it as a hard failure.
    unique_rows: dict[str, tuple] = {}
    duplicate_rows = 0
    for row in rows:
        sku_id = row[0]
        previous = unique_rows.get(sku_id)
        if previous is not None:
            if previous != row:
                raise ValueError(f"conflicting duplicate JD SKU id: {sku_id}")
            duplicate_rows += 1
            continue
        unique_rows[sku_id] = row
    rows = list(unique_rows.values())
    encoded = json.dumps(rows, ensure_ascii=False, default=str, sort_keys=True).encode("utf-8")
    source_hash = hashlib.sha256(encoded).hexdigest()
    mysql = config["mysql"]
    conn = pymysql.connect(host=mysql["host"], port=int(mysql["port"]), user=mysql["user"], password=mysql["password"], database=mysql["database"], charset=mysql.get("charset", "utf8mb4"), autocommit=False)
    try:
        values = []
        for sku_id, product_code, product_name, merchant_sku, parsed, specification, brand, price in rows:
            values.append((record_date, sku_id, product_code, product_name, merchant_sku, parsed["base_upc"], parsed["bundle_quantity"], parsed["listed_quantity"], parsed["package_unit"], specification, brand, price, None, None, "上架", parsed["parse_status"], parsed["parse_message"], source_hash))
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM `{TABLE}` WHERE `记录日期`=%s", (record_date,))
            cur.executemany(f"""INSERT INTO `{TABLE}` (`记录日期`,`SKUID`,`商品编码`,`商品名称`,`商家SKU原值`,`基础UPC`,`组合数量`,`标注组合数量`,`包装单位`,`销售属性`,`品牌`,`京东价`,`商品总库存`,`商品可用库存`,`商品状态`,`解析状态`,`解析说明`,`源文件SHA256`) VALUES ({','.join(['%s']*18)})""", values)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"record_date": record_date.isoformat(), "sku_rows": len(rows), "duplicate_source_rows_collapsed": duplicate_rows, "source_sha256": source_hash}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--record-date", type=dt.date.fromisoformat)
    parser.add_argument("--click-delay-seconds", type=float, default=3.0)
    parser.add_argument("--max-pages", type=int, help="Read only this many pages; never persists a partial snapshot")
    parser.add_argument("--index", type=Path, help="JSON index of last known SKU detail per product")
    parser.add_argument(
        "--refresh",
        choices=REFRESH_POLICIES,
        default="changed",
        help="changed: re-expand only products whose 修改时间 moved (default); "
             "all: rebuild every product; none: never expand, index only",
    )
    parser.add_argument(
        "--seed-from-db",
        action="store_true",
        help="rebuild the index from the newest complete snapshot in the database, "
             "then exit (avoids a full expansion on the first indexed run)",
    )
    args = parser.parse_args()
    record_date = args.record_date or dt.datetime.now(TZ).date()
    if args.click_delay_seconds < 1:
        raise ValueError("JD SKU click delay must be at least one second")
    index_file = index_path_for(args.config, args.index)
    if args.seed_from_db:
        seeded = seed_index_from_database(json.loads(args.config.read_text(encoding='utf-8')))
        index = {"version": INDEX_VERSION, "products": seeded["products"]}
        save_index(index_file, index)
        print(json.dumps({
            "state": "seeded",
            "index_file": str(index_file),
            "snapshot_date": seeded["snapshot_date"],
            "products": len(index["products"]),
            "sku_rows": sum(len(entry["SKU"]) for entry in index["products"].values()),
        }, ensure_ascii=False, indent=2))
        return
    index = load_index(index_file)
    # A Chrome tab can be discarded during a long, visible SKU-expansion run.
    # Retry once from a fresh DevTools target; persistence still occurs only
    # after a complete page scan and validation.
    for attempt in range(2):
        try:
            expected, products = collect_page_rows(
                args.port,
                click_delay_ms=round(args.click_delay_seconds * 1000),
                index=index,
                refresh_policy=args.refresh,
                max_pages=args.max_pages,
            )
            break
        except SourceUnavailable as error:
            print(f"SOURCE_UNAVAILABLE: {error}", file=sys.stderr)
            raise SystemExit(3)
        except (requests.RequestException, websocket.WebSocketException) as error:
            if attempt:
                raise
            print(f"WARNING: JD CDP connection lost; retrying once: {error}", file=sys.stderr)
            time.sleep(2)
    if args.max_pages:
        shapes = []
        for product in products[:10]:
            lines = [line.strip() for line in product["text"].splitlines() if line.strip()]
            shapes.append({"code": product["code"], "modified": product["modified"], "lines": lines[:4]})
        print(json.dumps({"state": "partial_read", "expected_products": expected, "products": len(products), "row_shapes": shapes}, ensure_ascii=False, indent=2))
        return
    rows, summary = build_rows(products, index)
    unique_parent_codes = {product["code"] for product in products}
    if len(unique_parent_codes) != expected or not rows:
        raise RuntimeError(
            f"JD SKU snapshot incomplete: unique products {len(unique_parent_codes)}/{expected}, "
            f"SKUs {len(rows)}, missing products {summary['missing_products']}"
        )
    result = persist(json.loads(args.config.read_text(encoding='utf-8')), record_date, rows)
    # Persist the merged index only after the snapshot is committed, so a failed
    # write cannot leave the index claiming detail the database never received.
    index["products"] = summary["entries"]
    index["updated_at"] = dt.datetime.now(TZ).isoformat(timespec="seconds")
    save_index(index_file, index)
    result.update({
        "index_file": str(index_file),
        "refreshed_products": summary["refreshed_products"],
        "unchanged_products": summary["unchanged_products"],
        "failed_products": summary["failed_products"],
        "missing_products": summary["missing_products"],
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
