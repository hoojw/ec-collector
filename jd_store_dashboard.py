"""Collect JD home-dashboard and product-list metrics from an authenticated store browser."""

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

from jd_store_sku_web_collect import PRODUCT_CODE, collect_page_rows
from workbench import qualified as qualified_workbench
from workbench import target as workbench_target


TZ = ZoneInfo("Asia/Shanghai")
HOME_URL = "https://shop.jd.com/jdm/home"
LOGIN_MARKERS = ("账号登录", "扫码登录", "统一登录", "京东验证", "请输入验证码")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
PRICE = re.compile(r"¥\s*([0-9]+(?:\.[0-9]+)?)")


def quote_identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe table identifier: {value!r}")
    return f"`{value}`"


def decimal_or_none(value: str | None) -> Decimal | None:
    if value is None or value.strip() in {"", "-", "--"}:
        return None
    try:
        return Decimal(value.replace("¥", "").replace(",", "").replace("%", "").strip())
    except InvalidOperation as error:
        raise ValueError(f"invalid JD dashboard number: {value!r}") from error


def integer_or_none(value: str | None) -> int | None:
    number = decimal_or_none(value)
    if number is None:
        return None
    if number != number.to_integral_value():
        raise ValueError(f"non-integer JD dashboard count: {value!r}")
    return int(number)


def label_value(text: str, label: str) -> str | None:
    match = re.search(rf"(?:^|\n){re.escape(label)}\n([^\n]+)", text)
    return match.group(1).strip() if match else None


def business_metric(text: str, label: str) -> tuple[str | None, str | None]:
    match = re.search(
        rf"(?:^|\n){re.escape(label)}\n([^\n]+)\n较昨日\n([^\n]+)", text
    )
    return (match.group(1).strip(), match.group(2).strip()) if match else (None, None)


def parse_home(text: str) -> dict:
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if any(marker in text for marker in LOGIN_MARKERS):
        raise RuntimeError("JD store home requires login or verification")
    name_match = re.search(r"(?:^|\n)([^\n]+旗舰店)\nPOP(?:\n|$)", text)
    if not name_match:
        raise RuntimeError("JD store home did not expose the official shop name")
    metrics = {}
    for key, label, kind in [
        ("transaction_amount", "成交金额", decimal_or_none),
        ("transaction_orders", "成交单量", integer_or_none),
        ("transaction_customers", "成交客户数", integer_or_none),
        ("store_visitors", "店铺访客数", integer_or_none),
        ("store_views", "店铺浏览量", integer_or_none),
        ("transaction_product_units", "成交商品件数", integer_or_none),
    ]:
        value, comparison = business_metric(text, label)
        metrics[key] = kind(value)
        metrics[f"{key}_comparison"] = comparison if comparison not in {None, "-", "--"} else None
    counts = {
        "pending_after_sales": integer_or_none(label_value(text, "待审核售后")),
        "receiving_after_sales": integer_or_none(label_value(text, "待收货售后")),
        "processing_after_sales": integer_or_none(label_value(text, "待处理售后")),
        "eligible_activities_home": integer_or_none(label_value(text, "可报名活动")),
        "pending_promotions": integer_or_none(label_value(text, "待审核促销")),
        "expiring_promotions": integer_or_none(label_value(text, "即将到期促销")),
        "price_risks": integer_or_none(label_value(text, "价格风险")),
    }
    star_match = re.search(r"店铺星级\n([0-9]+(?:\.[0-9]+)?)", text)
    return {
        "official_shop_name": name_match.group(1).strip(),
        "store_star": Decimal(star_match.group(1)) if star_match else None,
        **metrics,
        **counts,
    }


def parse_product_rows(pages: list[list[str]]) -> list[dict]:
    rows = []
    for page in pages:
        for text in page:
            product = PRODUCT_CODE.search(text)
            if not product:
                continue
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            score_match = re.search(r"信息分\s*(\d+)", text)
            sales_match = re.search(r"\n(\d+)\n好评率", text)
            prices = PRICE.findall(text)
            name = next(
                (line for line in lines if "商品编码：" not in line and not line.startswith("信息分")),
                None,
            )
            if not name or not score_match or not sales_match or not prices:
                # A single malformed listing must not block the aggregate
                # information-score count.  The caller retains a strict
                # threshold for how many parent rows may be skipped.
                print(
                    f"WARNING: JD product {product.group(1)} is missing analysis fields; skipping it",
                    file=sys.stderr,
                )
                continue
            rows.append({
                "product_code": product.group(1),
                "product_name": name,
                "price": Decimal(prices[0]),
                "information_score": int(score_match.group(1)),
                "sales_30d": int(sales_match.group(1)),
                "is_tirzepatide": "替尔泊肽" in name,
            })
    if len({row["product_code"] for row in rows}) != len(rows):
        raise ValueError("JD product analysis contains duplicate product codes")
    return rows


class Page:
    def __init__(self, port: int, url: str):
        self.base = f"http://127.0.0.1:{port}"
        response = requests.put(f"{self.base}/json/new?{quote(url, safe='')}", timeout=15)
        response.raise_for_status()
        self.target = response.json()
        self.ws = websocket.create_connection(self.target["webSocketDebuggerUrl"], timeout=20)
        self.message_id = 0

    def evaluate(self, expression: str):
        self.message_id += 1
        request_id = self.message_id
        self.ws.send(json.dumps({
            "id": request_id,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != request_id:
                continue
            remote = message.get("result", {}).get("result", {})
            if "value" not in remote:
                raise RuntimeError(remote.get("description") or "JD page returned no value")
            return remote["value"]

    def close(self):
        try:
            self.ws.close()
        finally:
            try:
                requests.get(f"{self.base}/json/close/{self.target['id']}", timeout=10)
            except requests.RequestException:
                pass


def collect_home(port: int) -> dict:
    last_error: Exception | None = None
    for attempt in range(3):
        page = None
        try:
            page = Page(port, HOME_URL)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                state = page.evaluate("({url:location.href,text:document.body&&document.body.innerText||''})")
                text = state.get("text", "")
                if any(marker in text for marker in LOGIN_MARKERS):
                    raise RuntimeError("JD store home requires login or verification")
                if (
                    "/jdm/home" in state.get("url", "")
                    and "经营数据" in text
                    and "店铺浏览量" in text
                    and "价格风险" in text
                ):
                    return parse_home(text)
                time.sleep(1)
            raise RuntimeError("JD store home did not finish loading")
        except RuntimeError as error:
            if "requires login or verification" in str(error):
                raise
            last_error = error
        except (requests.RequestException, websocket.WebSocketException, OSError) as error:
            last_error = error
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
        if attempt < 2:
            time.sleep(2)
    raise RuntimeError(f"JD store home failed after 3 attempts: {last_error}")


def store_settings(config: dict) -> dict:
    store = config.get("store") or config.get("jd_store") or {}
    value = {
        "client": str(store.get("client") or "").strip(),
        "owner": str(store.get("owner") or "待维护").strip(),
        "platform": str(store.get("platform") or "京东").strip(),
    }
    if not value["client"]:
        raise ValueError("JD store config requires store.client")
    return value


def persist(config: dict, record_date: dt.date, collected_at: dt.datetime, home: dict | None, products: list[dict] | None) -> None:
    tables = config.get("tables") or {}
    dashboard_table = quote_identifier(tables.get("jd_business_report", "jd_business_report_daily"))
    product_table = quote_identifier(tables.get("jd_product_analysis", "jd_product_analysis_daily"))
    workbench_schema, workbench_name = workbench_target(config)
    workbench_table = qualified_workbench(workbench_schema, workbench_name)
    store = store_settings(config)
    mysql = config["mysql"]
    conn = pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]), user=mysql["user"], password=mysql["password"],
        database=mysql["database"], charset=mysql.get("charset", "utf8mb4"), autocommit=False,
    )
    try:
        with conn.cursor() as cursor:
            if home is not None:
                cursor.execute(
                    f"""INSERT INTO {dashboard_table} (
                      `日期`,`采集时间`,`店铺名称`,`成交金额`,`成交金额较昨日`,`成交单量`,`成交单量较昨日`,
                      `成交客户数`,`成交客户数较昨日`,`店铺访客数`,`店铺访客数较昨日`,`店铺浏览量`,`店铺浏览量较昨日`,
                      `成交商品件数`,`成交商品件数较昨日`,`待审核售后`,`待收货售后`,`待处理售后`,
                      `可报名活动`,`待审核促销`,`即将到期促销`,`价格风险`,`店铺星级`
                    ) VALUES ({','.join(['%s'] * 23)}) ON DUPLICATE KEY UPDATE
                      `采集时间`=VALUES(`采集时间`),`店铺名称`=VALUES(`店铺名称`),
                      `成交金额`=VALUES(`成交金额`),`成交金额较昨日`=VALUES(`成交金额较昨日`),
                      `成交单量`=VALUES(`成交单量`),`成交单量较昨日`=VALUES(`成交单量较昨日`),
                      `成交客户数`=VALUES(`成交客户数`),`成交客户数较昨日`=VALUES(`成交客户数较昨日`),
                      `店铺访客数`=VALUES(`店铺访客数`),`店铺访客数较昨日`=VALUES(`店铺访客数较昨日`),
                      `店铺浏览量`=VALUES(`店铺浏览量`),`店铺浏览量较昨日`=VALUES(`店铺浏览量较昨日`),
                      `成交商品件数`=VALUES(`成交商品件数`),`成交商品件数较昨日`=VALUES(`成交商品件数较昨日`),
                      `待审核售后`=VALUES(`待审核售后`),`待收货售后`=VALUES(`待收货售后`),
                      `待处理售后`=VALUES(`待处理售后`),`可报名活动`=VALUES(`可报名活动`),
                      `待审核促销`=VALUES(`待审核促销`),`即将到期促销`=VALUES(`即将到期促销`),
                      `价格风险`=VALUES(`价格风险`),`店铺星级`=VALUES(`店铺星级`)""",
                    (
                        record_date, collected_at, home["official_shop_name"],
                        home["transaction_amount"], home["transaction_amount_comparison"],
                        home["transaction_orders"], home["transaction_orders_comparison"],
                        home["transaction_customers"], home["transaction_customers_comparison"],
                        home["store_visitors"], home["store_visitors_comparison"],
                        home["store_views"], home["store_views_comparison"],
                        home["transaction_product_units"], home["transaction_product_units_comparison"],
                        home["pending_after_sales"], home["receiving_after_sales"], home["processing_after_sales"],
                        home["eligible_activities_home"], home["pending_promotions"], home["expiring_promotions"],
                        home["price_risks"], home["store_star"],
                    ),
                )
                cursor.execute(
                    f"""INSERT INTO {workbench_table} (`日期`,`客户`,`商家负责人`,`平台`,`待商家审核数量`)
                    VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                    `商家负责人`=VALUES(`商家负责人`),`待商家审核数量`=VALUES(`待商家审核数量`)""",
                    (record_date, store["client"], store["owner"], store["platform"], home["pending_after_sales"]),
                )
            if products is not None:
                cursor.execute(f"DELETE FROM {product_table} WHERE `记录日期`=%s", (record_date,))
                if products:
                    cursor.executemany(
                        f"""INSERT INTO {product_table}
                        (`记录日期`,`商品编码`,`商品名称`,`当前价格`,`信息分`,`近30天销量`,`是否替尔泊肽`,`采集时间`)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        [
                            (record_date, row["product_code"], row["product_name"], row["price"],
                             row["information_score"], row["sales_30d"], int(row["is_tirzepatide"]), collected_at)
                            for row in products
                        ],
                    )
                total = len(products)
                nonmoving = sum(row["sales_30d"] == 0 for row in products)
                turnover = (Decimal(total - nonmoving) / Decimal(total)) if total else None
                low_score = sum(row["information_score"] < 90 for row in products)
                cursor.execute(
                    f"""INSERT INTO {workbench_table}
                    (`日期`,`客户`,`商家负责人`,`平台`,`商品动销率`,`经营商品数量`,`未动销商品数量`,`低于90分商品数`)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                    `商家负责人`=VALUES(`商家负责人`),`商品动销率`=VALUES(`商品动销率`),
                    `经营商品数量`=VALUES(`经营商品数量`),`未动销商品数量`=VALUES(`未动销商品数量`),
                    `低于90分商品数`=VALUES(`低于90分商品数`)""",
                    (record_date, store["client"], store["owner"], store["platform"], turnover, total, nonmoving, low_score),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run(args: argparse.Namespace) -> dict:
    record_date = args.record_date or dt.datetime.now(TZ).date()
    collected_at = dt.datetime.now(TZ).replace(microsecond=0)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    home = None if args.products_only else collect_home(args.port)
    products = None
    if not args.home_only:
        # This workbench task uses only the parent product rows (information
        # score and 30-day sales); SKU expansion belongs to the independent
        # SKU snapshot collector and must not gate these aggregate metrics.
        expected, pages = collect_page_rows(
            args.port, click_delay_ms=round(args.click_delay_seconds * 1000), expand_skus=False
        )
        products = parse_product_rows(pages)
        missing = expected - len(products)
        allowed_skips = max(1, expected // 20)
        if missing < 0 or missing > allowed_skips:
            raise RuntimeError(
                f"JD product analysis incomplete beyond safe threshold: {len(products)}/{expected}"
            )
    if not args.dry_run:
        persist(config, record_date, collected_at, home, products)
    summary = {
        "state": "success", "record_date": record_date.isoformat(), "dry_run": args.dry_run,
        "official_shop_name": home.get("official_shop_name") if home else None,
        "business": home,
        "product_count": len(products) if products is not None else None,
        "nonmoving_product_count": sum(row["sales_30d"] == 0 for row in products) if products is not None else None,
        "low_score_product_count": sum(row["information_score"] < 90 for row in products) if products is not None else None,
    }
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect JD store home and product-analysis metrics")
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--record-date", type=dt.date.fromisoformat)
    parser.add_argument("--click-delay-seconds", type=float, default=3.0)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--home-only", action="store_true")
    group.add_argument("--products-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.click_delay_seconds < 1:
        parser.error("--click-delay-seconds must be at least 1")
    return args


if __name__ == "__main__":
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2, default=str))
