"""Refresh JD after-sales dashboard metrics from visible, authenticated pages."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pymysql
import requests
import websocket

from workbench import qualified as qualified_workbench
from workbench import target as workbench_target


TZ = ZoneInfo("Asia/Shanghai")
PORT = 9533
DASHBOARD_URL = "https://shop.jd.com/jdm/shopstar/vane/VaneContainer?routeOutTabName=newShopStar&routeInnerTabName=AfterSaleDashboard"
LIST_URL = "https://shop.jd.com/jdm/trade/after-sale/independent-after-sale/list?tabCode=all"
LOGIN_MARKERS = ("账号登录", "扫码登录", "统一登录", "京东验证", "请输入验证码")
PENDING_COUNT = re.compile(
    r"待审核\s*(?:[（(]\s*(?P<bracketed>\d{1,6})\s*[）)]|(?P<plain>\d{1,6})(?![-\d]))"
)
AUTO_REVIEW_REMAINING = re.compile(
    r"(?:(?P<days>\d+)天)?(?:(?P<hours>\d+)时)?(?:(?P<minutes>\d+)分)?(?:\d+秒)?后自动审核"
)


def visible_text(
    url: str,
    port: int,
    *,
    ready_pattern: re.Pattern[str] | None = None,
    attempts: int = 3,
) -> str:
    base = f"http://127.0.0.1:{port}"
    last_error: Exception | None = None
    for attempt in range(attempts):
        target = None
        ws = None
        try:
            response = requests.put(f"{base}/json/new?{quote(url, safe='')}", timeout=15)
            response.raise_for_status()
            target = response.json()
            ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=20)
            deadline = time.monotonic() + 20
            ready_after = time.monotonic() + 10
            message_id = 0
            latest_text = ""
            while time.monotonic() < deadline:
                message_id += 1
                ws.send(json.dumps({
                    "id": message_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": "document.body&&document.body.innerText||''",
                        "returnByValue": True,
                    },
                }))
                while True:
                    payload = json.loads(ws.recv())
                    if payload.get("id") == message_id:
                        break
                latest_text = str(payload.get("result", {}).get("result", {}).get("value") or "")
                if any(marker in latest_text for marker in LOGIN_MARKERS):
                    raise RuntimeError("JD browser login or verification is required")
                if ready_pattern is not None and ready_pattern.search(latest_text):
                    return latest_text
                if ready_pattern is None and time.monotonic() >= ready_after and latest_text.strip():
                    return latest_text
                time.sleep(1)
            raise RuntimeError("JD page did not expose its required content within 20 seconds")
        except RuntimeError as error:
            if "login or verification" in str(error):
                raise
            last_error = error
        except (requests.RequestException, websocket.WebSocketException, OSError) as error:
            last_error = error
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            if target is not None:
                try:
                    requests.get(f"{base}/json/close/{target['id']}", timeout=10)
                except requests.RequestException:
                    pass
        if attempt < attempts - 1:
            time.sleep(2)
    raise RuntimeError(f"JD page failed after {attempts} attempts: {last_error}")


def parse_dashboard(text: str) -> float | None:
    match = re.search(r"本店售后服务时长\s*([0-9]+(?:\.[0-9]+)?)小时", text)
    if not match:
        # This dashboard card is independently rendered.  A missing card must
        # not discard the verifiable list metrics from the same JD session.
        return None
    return float(match.group(1))


def parse_list(text: str) -> tuple[int, int]:
    pending_match = PENDING_COUNT.search(text)
    if not pending_match:
        raise RuntimeError("JD after-sales list did not expose 待审核 count")
    pending = int(pending_match.group("bracketed") or pending_match.group("plain"))
    remaining_hours = []
    for match in AUTO_REVIEW_REMAINING.finditer(text):
        days = int(match.group("days") or 0)
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        remaining_hours.append(days * 24 + hours + minutes / 60)
    if pending and len(remaining_hours) < pending:
        raise RuntimeError("JD after-sales pending list is paginated; refusing incomplete imminent count")
    return pending, sum(hours <= 12 for hours in remaining_hours)


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


def load_home_pending(config: dict, record_date: dt.date) -> int | None:
    mysql = config["mysql"]
    store = store_settings(config)
    schema, table = workbench_target(config)
    target = qualified_workbench(schema, table)
    conn = pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
        password=mysql["password"], database=mysql["database"],
        charset=mysql.get("charset", "utf8mb4"),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT `待商家审核数量` FROM {target} "
                "WHERE `日期`=%s AND `客户`=%s AND `平台`=%s",
                (record_date, store["client"], store["platform"]),
            )
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None
    finally:
        conn.close()


def persist(
    config: dict,
    record_date: dt.date,
    duration: float | None,
    pending: int | None,
    imminent: int | None,
) -> None:
    mysql = config["mysql"]
    conn = pymysql.connect(host=mysql["host"], port=int(mysql["port"]), user=mysql["user"], password=mysql["password"], database=mysql["database"], charset=mysql.get("charset", "utf8mb4"), autocommit=False)
    try:
        with conn.cursor() as cur:
            store = store_settings(config)
            schema, table = workbench_target(config)
            target = qualified_workbench(schema, table)
            cur.execute(
                f"""INSERT INTO {target}
                (`日期`,`客户`,`商家负责人`,`平台`,`退货退款初审时间`,`待商家审核数量`,`即将自动退售后单`)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  `商家负责人`=VALUES(`商家负责人`),
                  `退货退款初审时间`=COALESCE(VALUES(`退货退款初审时间`), `退货退款初审时间`),
                  `待商家审核数量`=COALESCE(VALUES(`待商家审核数量`), `待商家审核数量`),
                  `即将自动退售后单`=COALESCE(VALUES(`即将自动退售后单`), `即将自动退售后单`)""",
                (record_date, store["client"], store["owner"], store["platform"], round(duration, 2) if duration is not None else None, pending, imminent),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--record-date", type=dt.date.fromisoformat)
    args = parser.parse_args()
    record_date = args.record_date or dt.datetime.now(TZ).date()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    # The service-duration card is optional for some stores.  Wait for the
    # page's normal settled text, then preserve NULL when the card is absent.
    duration = parse_dashboard(visible_text(DASHBOARD_URL, args.port))
    home_pending = load_home_pending(config, record_date)
    if home_pending == 0:
        pending, imminent = 0, 0
    else:
        try:
            pending, imminent = parse_list(
                visible_text(LIST_URL, args.port, ready_pattern=PENDING_COUNT, attempts=1)
            )
            if home_pending is not None and pending != home_pending:
                raise RuntimeError(
                    f"JD after-sales count disagrees with home dashboard: home={home_pending} list={pending}"
                )
        except Exception as error:
            # The independently rendered service-duration card remains valid.
            # The home count is also independently valid.  Preserve the last
            # good imminent count when the detail list changed shape or did
            # not finish rendering, and report a partial success without
            # making every ten-minute store cycle fail.
            persist(config, record_date, duration, home_pending, None)
            print(json.dumps({
                "state": "partial_success",
                "record_date": record_date.isoformat(),
                "after_sale_service_hours": duration,
                "final_review_hours": None,
                "pending": home_pending,
                "imminent": None,
                "detail_error": str(error),
            }, ensure_ascii=False))
            return
    persist(config, record_date, duration, pending, imminent)
    print(json.dumps({"record_date": record_date.isoformat(), "after_sale_service_hours": duration, "final_review_hours": None, "pending": pending, "imminent": imminent}, ensure_ascii=False))


if __name__ == "__main__":
    main()
