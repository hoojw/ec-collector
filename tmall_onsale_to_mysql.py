#!/usr/bin/env python3
"""Persist Tmall on-sale product detail rows (data.on_sale.rows) into MySQL.

Reads the latest usable daily snapshot per store from the receiver's received/
tree, extracts the per-product rows captured by the collector, and upserts them
into a dedicated detail table. Idempotent: re-running replaces that store/date.

Fields are taken verbatim from the native on-sale API response; nested values
are only unwrapped to their displayed scalar (no invented data).
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import pymysql

TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DATA_DIR = pathlib.Path("/opt/subsidy/tmall-ecommerce-data")
TABLE = "tmall_on_sale_daily"
STORE_KEYS = {"default": "保民", "jinghetang": "璟和堂"}
USABLE_STATES = ("success", "partial")


def q(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def parse_slot_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def decimal_of(value):
    if value is None:
        return None
    text = str(value).replace("¥", "").replace(",", "").strip()
    import re
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return Decimal(m.group())
    except InvalidOperation:
        return None


def unwrap(row: dict) -> dict:
    desc = row.get("itemDesc") or {}
    title = None
    for piece in (desc.get("desc") or []):
        if isinstance(piece, dict) and (piece.get("text") or piece.get("copyText")):
            title = piece.get("text") or piece.get("copyText")
            break
    price = None
    mp = row.get("managerPrice")
    if isinstance(mp, dict):
        price = decimal_of(mp.get("currentPrice"))
    elif mp is not None:
        price = decimal_of(mp)
    qty = None
    mq = row.get("managerQuantityNew")
    if isinstance(mq, dict):
        qty = mq.get("text")
    elif mq is not None:
        qty = mq
    monthly = None
    ms = row.get("monthlySoldQuantity")
    if isinstance(ms, dict):
        monthly = decimal_of(ms.get("value"))
    elif ms is not None:
        monthly = decimal_of(ms)
    up = row.get("upShelfDate_m") or {}
    up_value = up.get("value") if isinstance(up, dict) else None
    up_status = (up.get("status") or {}).get("text") if isinstance(up, dict) else None

    def _status_of(e):
        if isinstance(e, dict):
            for k in ("statusText", "statusDesc", "text", "name", "value", "status"):
                v = e.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return None

    # order_list 页每行带商品状态（出售中/仓库中/...）；字段名以接口为准，多候选兜底
    item_status = None
    for key in ("itemStatus", "status", "statusDesc", "statusText", "itemStatusDesc",
                "statusName", "saleStatus", "goodsStatus"):
        item_status = _status_of(row.get(key)) or (row.get(key) if isinstance(row.get(key), str) else None)
        if item_status:
            break
    if not item_status:
        for key in ("itemStatusVo", "statusVo", "statusInfo"):
            v = row.get(key)
            if isinstance(v, dict):
                item_status = _status_of(v)
                if item_status:
                    break
    return {
        "item_id": str(row.get("itemId") or "") or None,
        "cat_id": str(row.get("catId") or "") or None,
        "title": title,
        "current_price": price,
        "stock": decimal_of(qty),
        "sold_quantity": decimal_of(row.get("soldQuantity_m")),
        "monthly_sold_quantity": monthly,
        "shelf_status": up_status,
        "item_status": item_status,
        "up_shelf_date": up_value,
        "raw_json": json.dumps(row, ensure_ascii=False),
    }


def latest_snapshot(data_dir: pathlib.Path, store_key: str, job: str, days: int = 7) -> dict | None:
    base = data_dir / "received"
    roots = (base / job, base / store_key / job) if store_key == "default" else (base / store_key / job,)
    today = dt.datetime.now(TZ).date()
    wanted = {today - dt.timedelta(days=i) for i in range(max(1, days))}
    best, best_rank = None, None
    for root in roots:
        if not root.is_dir():
            continue
        paths = []
        for day in wanted:
            folder = root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
            if folder.is_dir():
                paths.extend(folder.glob("*.json"))
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("state") not in USABLE_STATES:
                continue
            if not data.get("slot_time"):
                continue
            on_sale = (data.get("data") or {}).get("on_sale") or {}
            rows = on_sale.get("rows") or []
            complete = bool(on_sale.get("pagination_complete"))
            collected = str(data.get("collected_at") or "")
            # Prefer the most complete snapshot (all pages captured), then more
            # rows, then the freshest collection time. slot_time is identical
            # for every run of a day, so it cannot be used as the recency key.
            rank = (1 if complete else 0, len(rows), collected)
            if best_rank is None or rank > best_rank:
                best_rank, best = rank, data
    return best


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {q(TABLE)} ("
            f"`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, "
            f"`记录日期` DATE NOT NULL, `店铺` VARCHAR(50) NOT NULL, "
            f"`item_id` VARCHAR(40) NOT NULL, `cat_id` VARCHAR(40) NULL, "
            f"`title` VARCHAR(1000) NULL, `current_price` DECIMAL(12,2) NULL, "
            f"`stock` DECIMAL(14,2) NULL, `sold_quantity` DECIMAL(14,2) NULL, "
            f"`monthly_sold_quantity` DECIMAL(14,2) NULL, "
            f"`shelf_status` VARCHAR(50) NULL, `item_status` VARCHAR(50) NULL, "
            f"`up_shelf_date` VARCHAR(40) NULL, "
            f"`raw_json` LONGTEXT NULL, `collection_id` VARCHAR(100) NULL, "
            f"PRIMARY KEY (`id`), UNIQUE KEY `uk_store_day_item` (`店铺`,`记录日期`,`item_id`), "
            f"KEY `idx_cat` (`cat_id`), KEY `idx_item_status` (`item_status`)"
            f") DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )
        # 兼容已有表
        cur.execute(f"SELECT COUNT(*) FROM information_schema.columns "
                    f"WHERE table_schema=DATABASE() AND table_name=%s AND column_name='item_status'",
                    (TABLE,))
        if cur.fetchone()[0] == 0:
            cur.execute(f"ALTER TABLE {q(TABLE)} ADD COLUMN `item_status` VARCHAR(50) NULL "
                        f"AFTER `shelf_status`, ADD KEY `idx_item_status` (`item_status`)")


def persist(conn, record_date, client, rows, collection_id):
    cols = ["记录日期", "店铺", "item_id", "cat_id", "title", "current_price",
            "stock", "sold_quantity", "monthly_sold_quantity", "shelf_status",
            "item_status", "up_shelf_date", "raw_json", "collection_id"]
    sql = (f"INSERT INTO {q(TABLE)} (" + ", ".join(q(c) for c in cols) + ") VALUES ("
           + ", ".join(["%s"] * len(cols)) + ") ON DUPLICATE KEY UPDATE "
           + ", ".join(f"{q(c)}=VALUES({q(c)})" for c in cols if c not in ("记录日期", "店铺", "item_id")))
    values = []
    for row in rows:
        rec = unwrap(row)
        if not rec["item_id"]:
            continue
        values.append([record_date, client, rec["item_id"], rec["cat_id"], rec["title"],
                       rec["current_price"], rec["stock"], rec["sold_quantity"],
                       rec["monthly_sold_quantity"], rec["shelf_status"],
                       rec["item_status"], rec["up_shelf_date"], rec["raw_json"], collection_id])
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {q(TABLE)} WHERE `记录日期`=%s AND `店铺`=%s", (record_date, client))
        if values:
            cur.executemany(sql, values)
    return len(values)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/opt/subsidy/config.json")
    ap.add_argument("--data-dir", type=pathlib.Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--store", action="append", help="只处理指定 store key（可重复）")
    ap.add_argument("--scan-days", type=int, default=7, help="只扫描最近 N 天的快照目录（默认 7）")
    args = ap.parse_args()

    cfg = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8"))
    selected = set(args.store or []) or set(STORE_KEYS)
    conn = pymysql.connect(**cfg["mysql"], autocommit=False)
    try:
        ensure_table(conn)
        for key in sorted(selected):
            if key not in STORE_KEYS:
                raise ValueError(f"unknown store: {key}")
            snap = latest_snapshot(args.data_dir, key, "daily", args.scan_days)
            if not snap:
                print(json.dumps({"store": key, "status": "no_snapshot"}, ensure_ascii=False))
                continue
            rows = ((snap.get("data") or {}).get("on_sale") or {}).get("rows") or []
            on_sale = (snap.get("data") or {}).get("on_sale") or {}
            record_date = parse_slot_date(snap.get("slot_time"))
            if record_date is None:
                print(json.dumps({"store": key, "status": "no_slot_date"}, ensure_ascii=False))
                continue
            n = persist(conn, record_date, STORE_KEYS[key], rows, snap.get("collection_id"))
            conn.commit()
            print(json.dumps({
                "store": key, "client": STORE_KEYS[key], "status": "ok",
                "record_date": record_date.isoformat(), "collection_id": snap.get("collection_id"),
                "on_sale_total": on_sale.get("total"), "rows_in_snapshot": len(rows),
                "rows_persisted": n,
            }, ensure_ascii=False))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
