#!/usr/bin/env python3
"""Collect Meituan subsidy (subsidized / unsubsidized / unlisted) by store via the
already-logged-in browser API, and persist three daily tables plus a per-store
historical lowest-price table.

Data sets (filterType):
  1 = 已补贴 (subsidized), 0 = 未补贴 (unsubsidized), 2 = 未建品 (unlisted)

Only one signed-in browser target is used; no credentials are persisted.
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pymysql, requests, websocket

TZ = ZoneInfo("Asia/Shanghai")
TARGET_URL = (
    "https://yiyao.meituan.com/main/frame#/pages/health/marketing/"
    "low_price_campaign/list"
)
LIST_API = "/api/health/activity/apply/b/activity/apply/lst/sku/list?source=1"
FILTER = {"subsidized": 1, "unsubsidized": 0, "unlisted": 2}
DATASETS = ("subsidized", "unsubsidized", "unlisted")

TABLES = {
    "subsidized": "mt_subsidy_subsidized_daily",
    "unsubsidized": "mt_subsidy_unsubsidized_daily",
    "unlisted": "mt_subsidy_unlisted_daily",
}
LOWEST_TABLE = "mt_subsidy_lowest_price"

COLS = {
    "subsidized": [
        "数据日期", "店铺", "poi_id", "poi_name", "upc", "sku_id", "sku_name", "类别",
        "活动类型", "活动名称", "补贴状态", "原价", "当前售价", "补贴门槛", "补贴后价",
        "单件补贴", "商家承担", "门槛盒数", "补贴范围", "活动开始", "活动结束",
        "活动时间段", "活动周期", "reject_time",
    ],
    "unsubsidized": [
        "数据日期", "店铺", "poi_id", "poi_name", "upc", "sku_id", "sku_name", "类别",
        "活动类型", "活动名称", "未补贴原因", "补贴门槛", "我的价格", "至少再降",
        "预估补贴", "商家承担", "门槛盒数", "reject_time",
    ],
    "unlisted": [
        "数据日期", "店铺", "poi_id", "poi_name", "upc", "sku_name", "类别",
        "活动类型", "活动名称", "建议定价", "预估补贴", "门槛盒数", "活动开始", "活动结束",
    ],
}
ACT_TYPE = {17: "单盒补贴", 29: "多盒补贴"}


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def decimal_value(v):
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def epoch_to_dt(v):
    if v in (None, "", 0):
        return None
    try:
        ts = int(v)
        if ts >= 100_000_000_000:
            ts //= 1000
        return dt.datetime.fromtimestamp(ts, TZ).replace(tzinfo=None)
    except (ValueError, OverflowError, OSError):
        return None


def parse_act_config(v):
    if not v:
        return {}
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return {}


class CdpPage:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        url = quote(TARGET_URL, safe='')
        r = requests.put(f"{self.base}/json/new?{url}", timeout=15)
        if r.status_code == 405:
            r = requests.get(f"{self.base}/json/new?{url}", timeout=15)
        r.raise_for_status()
        t = r.json()
        self.tab_id = t["id"]
        self.ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=60)
        self.mid = 0
        self.call("Runtime.enable")
        time.sleep(2)

    def close(self):
        try:
            self.ws.close()
        finally:
            try:
                requests.get(f"{self.base}/json/close/{self.tab_id}", timeout=10)
            except Exception:
                pass

    def call(self, method, params=None):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") != self.mid:
                continue
            if "error" in m:
                raise RuntimeError(m["error"])
            return m.get("result", {})

    def ev(self, expression):
        r = self.call("Runtime.evaluate", {
            "expression": expression, "awaitPromise": True,
            "returnByValue": True, "timeout": 60000})
        remote = r.get("result", {})
        if "value" not in remote:
            raise RuntimeError(remote.get("description") or str(remote))
        return remote["value"]


def fetch_page(page: CdpPage, filter_type: int, page_no: int, page_size: int) -> dict:
    body = json.dumps({
        "sortType": 1, "filterType": filter_type, "sortField": 40,
        "actTypes": [17, 29], "medicineTypeList": [],
        "pageNo": page_no, "pageSize": page_size,
    }, ensure_ascii=False, separators=(",", ":"))
    expr = (
        "(async()=>{const r=await fetch(%s,{method:'POST',credentials:'include',"
        "headers:{'Content-Type':'application/json'},body:%s});"
        "return await r.text();})()" % (json.dumps(LIST_API), json.dumps(body))
    )
    raw = page.ev(expr)
    payload = json.loads(raw)
    if payload.get("code") != 0:
        raise RuntimeError(f"Meituan API business error: {str(payload)[:300]}")
    return payload.get("data") or {}


def normalize(rec: dict, dataset: str, client: str, day: str) -> dict:
    cfg = parse_act_config(rec.get("actConfig"))
    common = {
        "数据日期": day, "店铺": client,
        "poi_id": str(rec.get("poiId") or ""),
        "poi_name": rec.get("poiName") or "",
        "upc": str(rec.get("upcCode") or ""),
        "sku_id": str(rec.get("skuId") or ""),
        "sku_name": rec.get("skuName") or "",
        "类别": rec.get("cate1Name") or ("处方药" if rec.get("medicineType") == 2 else None),
        "活动类型": ACT_TYPE.get(rec.get("actType")),
        "活动名称": rec.get("actName") or "",
    }
    start = epoch_to_dt(cfg.get("startTime"))
    end = epoch_to_dt(cfg.get("endTime"))
    if dataset == "subsidized":
        common.update({
            "补贴状态": rec.get("statusDesc") or "",
            "原价": decimal_value(rec.get("orgPrice")),
            "当前售价": decimal_value(rec.get("currentPrice")),
            "补贴门槛": decimal_value(rec.get("minDiscountPrice")),
            "补贴后价": decimal_value(rec.get("discountPrice")),
            "单件补贴": decimal_value(rec.get("mtPrice")),
            "商家承担": decimal_value(rec.get("poiCharge")),
            "门槛盒数": rec.get("levelItemNumber") or 1,
            "补贴范围": rec.get("subsideCityDesc") or "",
            "活动开始": start, "活动结束": end,
            "活动时间段": cfg.get("period"),
            "活动周期": cfg.get("weeksTime"),
            "reject_time": epoch_to_dt(rec.get("rejectTime")),
        })
    elif dataset == "unsubsidized":
        rej = rec.get("rej")
        reason = None
        if isinstance(rej, list) and rej and isinstance(rej[0], dict):
            reason = rej[0].get("rejText")
        threshold = decimal_value(rec.get("minDiscountPrice"))
        my_price = decimal_value(rec.get("currentPrice"))
        need_drop = (my_price - threshold) if (my_price is not None and threshold is not None) else None
        common.update({
            "未补贴原因": reason,
            "补贴门槛": threshold,
            "我的价格": my_price,
            "至少再降": need_drop,
            "预估补贴": decimal_value(rec.get("mtPrice")),
            "商家承担": decimal_value(rec.get("poiCharge")),
            "门槛盒数": rec.get("levelItemNumber") or 1,
            "reject_time": epoch_to_dt(rec.get("rejectTime")),
        })
    else:
        common.update({
            "建议定价": decimal_value(rec.get("minDiscountPrice")),
            "预估补贴": decimal_value(rec.get("mtPrice")),
            "门槛盒数": rec.get("levelItemNumber") or 1,
            "活动开始": start, "活动结束": end,
        })
    return common


def collect_dataset(page: CdpPage, dataset: str, client: str, day: str,
                    page_size: int, max_pages: int) -> list[dict]:
    filter_type = FILTER[dataset]
    rows: list[dict] = []
    seen_ids: set[str] = set()
    expected_total: int | None = None
    for page_no in range(1, max_pages + 1):
        data = fetch_page(page, filter_type, page_no, page_size)
        records = data.get("data")
        if not isinstance(records, list):
            raise ValueError(f"{dataset} page {page_no} has no data list")
        total = data.get("total")
        if expected_total is None:
            expected_total = int(total)
        # total may fluctuate slightly between pages; anchor on first page
        if not records:
            break
        for rec in records:
            rid = str(rec.get("id") or "")
            if rid and rid in seen_ids:
                continue
            if rid:
                seen_ids.add(rid)
            upc = str(rec.get("upcCode") or "")
            if not upc:
                continue
            rows.append(normalize(rec, dataset, client, day))
        if len(rows) >= expected_total:
            break
    # 列表总数在翻页间可能小幅波动; 以实际抓到的唯一记录为准, 记录与首页总数的差值
    return rows


def price_observations(rows_by_dataset: dict[str, list[dict]], day: str, client: str) -> list[dict]:
    """Collect every observed selling/handler price for the historical-lowest table.

    Sources that represent an actual price a buyer pays or the store sells at:
      - subsidized: 当前售价 (pre-subsidy selling price) and 补贴后价 (after-subsidy)
      - unsubsidized: 我的价格 (current selling price)
    Unlisted 建议定价 is a suggestion, not an observed selling price, so excluded.
    """
    obs: dict[tuple[str, str], dict] = {}
    sources = (
        ("subsidized", "当前售价", "当前售价"),
        ("subsidized", "补贴后价", "补贴后价"),
        ("unsubsidized", "我的价格", "我的价格"),
    )
    for dataset, field, label in sources:
        for r in rows_by_dataset.get(dataset, []):
            price = r.get(field)
            if price is None:
                continue
            key = (r["店铺"] or client, r["upc"])
            if key not in obs or price < obs[key]["最低售价"]:
                obs[key] = {
                    "店铺": key[0], "upc": r["upc"], "sku_name": r["sku_name"],
                    "最低售价": price, "价格来源": label,
                    "首次观测日期": day, "最近观测日期": day,
                }
    return list(obs.values())


DDL_NUM = "DECIMAL(12,2)"
DDL_INT = "INT UNSIGNED"


def column_ddl(col: str) -> str:
    if col in ("数据日期", "活动开始", "活动结束", "reject_time", "首次观测日期", "最近观测日期"):
        return "DATETIME" if col in ("活动开始", "活动结束", "reject_time") else "DATE"
    if col in ("原价", "当前售价", "补贴门槛", "补贴后价", "单件补贴", "商家承担",
               "我的价格", "至少再降", "预估补贴", "建议定价", "最低售价"):
        return DDL_NUM
    if col in ("门槛盒数",):
        return DDL_INT
    if col in ("poi_id", "upc", "sku_id"):
        return "VARCHAR(50)"
    return "VARCHAR(500)"


def ensure_tables(conn):
    with conn.cursor() as cur:
        for dataset, table in TABLES.items():
            defs = [f"`{c}` {column_ddl(c)} NULL" for c in COLS[dataset]]
            cols_sql = ", ".join(defs)
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {quote_ident(table)} ("
                f"`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, {cols_sql}, "
                f"PRIMARY KEY (`id`), "
                f"KEY `idx_store_day` (`店铺`, `数据日期`), KEY `idx_upc` (`upc`)"
                f") DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
            )
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {quote_ident(LOWEST_TABLE)} ("
            f"`店铺` VARCHAR(100) NOT NULL, `upc` VARCHAR(50) NOT NULL, "
            f"`sku_name` VARCHAR(500) NULL, `最低售价` {DDL_NUM} NULL, "
            f"`价格来源` VARCHAR(50) NULL, `首次观测日期` DATE NULL, `最近观测日期` DATE NULL, "
            f"PRIMARY KEY (`店铺`, `upc`), KEY `idx_upc` (`upc`)"
            f") DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )


def refresh_daily(conn, dataset: str, day: str, client: str, rows: list[dict]):
    table = TABLES[dataset]
    cols = COLS[dataset]
    col_sql = ", ".join(f"`{c}`" for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {quote_ident(table)} WHERE `数据日期`=%s AND `店铺`=%s",
            (day, client))
        if rows:
            cur.executemany(
                f"INSERT INTO {quote_ident(table)} ({col_sql}) VALUES ({placeholders})",
                [tuple(r.get(c) for c in cols) for r in rows])


def upsert_lowest(conn, observations: list[dict]):
    if not observations:
        return 0
    # 与库内已有最低价比较, 仅当观测价更低时更新, 保留最早记录日期
    with conn.cursor() as cur:
        cur.execute(f"SELECT `店铺`, `upc`, `最低售价`, `首次观测日期` FROM {quote_ident(LOWEST_TABLE)}")
        existing = {(r[0], r[1]): (r[2], r[3]) for r in cur.fetchall()}
    to_write = []
    for o in observations:
        key = (o["店铺"], o["upc"])
        old = existing.get(key)
        if old is not None and old[0] is not None:
            if o["最低售价"] >= old[0]:
                # 价格未创新低, 只刷新最近观测日期
                cur_date = o["最近观测日期"]
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE {quote_ident(LOWEST_TABLE)} SET `最近观测日期`=%s "
                        f"WHERE `店铺`=%s AND `upc`=%s", (cur_date, key[0], key[1]))
                continue
            o["首次观测日期"] = old[1] or o["首次观测日期"]
        to_write.append((
            o["店铺"], o["upc"], o["sku_name"], o["最低售价"], o["价格来源"],
            o["首次观测日期"], o["最近观测日期"]))
    if to_write:
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {quote_ident(LOWEST_TABLE)} ("
                f"`店铺`,`upc`,`sku_name`,`最低售价`,`价格来源`,`首次观测日期`,`最近观测日期`) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s) "
                f"ON DUPLICATE KEY UPDATE "
                f"`sku_name`=VALUES(`sku_name`), `最低售价`=VALUES(`最低售价`), "
                f"`价格来源`=VALUES(`价格来源`), `首次观测日期`=VALUES(`首次观测日期`), "
                f"`最近观测日期`=VALUES(`最近观测日期`)", to_write)
    return len(to_write)


def connect_mysql(cfg: dict):
    return pymysql.connect(
        host=cfg["host"], port=int(cfg["port"]), user=cfg["user"],
        password=cfg["password"], database=cfg["database"],
        charset=cfg.get("charset", "utf8mb4"), autocommit=False)


def main():
    ap = argparse.ArgumentParser(description="Collect Meituan subsidy data by store.")
    ap.add_argument("--port", type=int, required=True, help="CDP port of the store browser")
    ap.add_argument("--client", help="store/client name; defaults to store.client in --config")
    ap.add_argument("--date", default=dt.datetime.now(TZ).strftime("%Y-%m-%d"))
    ap.add_argument("--config", default="/opt/subsidy/config.json")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--max-pages", type=int, default=100)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8"))
    client = args.client or (cfg.get("store") or {}).get("client")
    if not client:
        raise SystemExit("--client is required when config has no store.client")

    page = CdpPage(args.port)
    result = {"client": client, "date": args.date, "port": args.port,
              "collected_at": dt.datetime.now(TZ).isoformat(), "datasets": {}}
    try:
        for ds in DATASETS:
            rows = collect_dataset(page, ds, client, args.date,
                                   args.page_size, args.max_pages)
            result["datasets"][ds] = rows
    finally:
        page.close()

    obs = price_observations(result["datasets"], args.date, client)
    summary = {"client": client, "date": args.date,
               "collected_at": result["collected_at"],
               "counts": {ds: len(result["datasets"][ds]) for ds in DATASETS},
               "lowest_price_updates": len(obs)}
    if not args.dry_run:
        conn = connect_mysql(cfg["mysql"])
        try:
            ensure_tables(conn)
            for ds in DATASETS:
                refresh_daily(conn, ds, args.date, client, result["datasets"][ds])
            updated = upsert_lowest(conn, obs)
            conn.commit()
            summary["lowest_price_updates"] = updated
            summary["db_written"] = True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        summary["db_written"] = False
        for ds in DATASETS:
            summary.setdefault("samples", {})[ds] = result["datasets"][ds][:2]
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
