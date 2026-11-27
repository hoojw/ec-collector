#!/usr/bin/env python3
"""Persist Tmall after-sales task detail rows from every10 snapshots into MySQL.

Reads the latest usable every10 snapshot per store, extracts
``data.after_sales.rows`` (captured by the collector from the PBC workbench
``NEW_PBC_LIST_CASE`` gateway component) and upserts them into a dedicated
table.  Idempotent: re-running replaces that store/date's rows.

The summary counts (待商家审核数量 / 即将自动退售后单) continue to be handled
by tmall_ecommerce_to_mysql.py into esale_system; this script only stores detail.
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib
from zoneinfo import ZoneInfo

import pymysql

TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DATA_DIR = pathlib.Path("/opt/subsidy/tmall-ecommerce-data")
TABLE = "tmall_after_sale_task_daily"
STORE_KEYS = {"default": "保民", "jinghetang": "璟和堂"}
USABLE_STATES = ("success", "partial")


def q(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {q(TABLE)} ("
            f"`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, "
            f"`记录日期` DATE NOT NULL, `店铺` VARCHAR(50) NOT NULL, "
            f"`case_id` VARCHAR(40) NOT NULL, "
            f"`case_type` VARCHAR(80) NULL, `case_status` VARCHAR(80) NULL, "
            f"`case_type_symbol` VARCHAR(80) NULL, "
            f"`order_id` VARCHAR(60) NULL, `waybill_no` VARCHAR(60) NULL, "
            f"`buyer_nick` VARCHAR(80) NULL, `sla_time` VARCHAR(80) NULL, "
            f"`receiver_address` VARCHAR(300) NULL, `goods_title` VARCHAR(500) NULL, "
            f"`goods_sku` VARCHAR(200) NULL, `reason` VARCHAR(200) NULL, "
            f"`create_time` VARCHAR(40) NULL, `dealer` VARCHAR(80) NULL, "
            f"`proposer` VARCHAR(80) NULL, "
            f"`collection_id` VARCHAR(100) NULL, `raw_json` LONGTEXT NULL, "
            f"`updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, "
            f"PRIMARY KEY (`id`), "
            f"UNIQUE KEY `uk_store_day_case` (`记录日期`,`店铺`,`case_id`), "
            f"KEY `idx_case_order` (`order_id`)"
            f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )


def latest_snapshot(data_dir, store_key, days=7):
    base = data_dir / "received"
    roots = (base / "every10", base / store_key / "every10") if store_key == "default" else (base / store_key / "every10",)
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
            if data.get("state") not in USABLE_STATES or not data.get("slot_time"):
                continue
            af = (data.get("data") or {}).get("after_sales") or {}
            rows = af.get("rows") or []
            rank = (len(rows), str(data.get("collected_at") or ""))
            if best_rank is None or rank > best_rank:
                best_rank, best = rank, data
    return best


def persist(conn, record_date, client, rows, collection_id):
    cols = ["记录日期", "店铺", "case_id", "case_type", "case_status", "case_type_symbol",
            "order_id", "waybill_no", "buyer_nick", "sla_time", "receiver_address",
            "goods_title", "goods_sku", "reason", "create_time", "dealer", "proposer",
            "collection_id", "raw_json"]
    values = []
    seen = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        case_id = str(r.get("case_id") or "").strip()
        if not case_id or case_id in seen:
            continue
        seen.add(case_id)
        raw = json.dumps(r, ensure_ascii=False)
        values.append([
            record_date, client, case_id, r.get("case_type"), r.get("case_status"),
            r.get("case_type_symbol"), r.get("order_id"), r.get("waybill_no"),
            r.get("buyer_nick"), r.get("sla_time"), r.get("receiver_address"),
            r.get("goods_title"), r.get("goods_sku"), r.get("reason"),
            r.get("create_time"), r.get("dealer"), r.get("proposer"),
            collection_id, raw,
        ])
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {q(TABLE)} WHERE `记录日期`=%s AND `店铺`=%s", (record_date, client))
        if values:
            sql = (f"INSERT INTO {q(TABLE)} (" + ", ".join(q(c) for c in cols) + ") VALUES ("
                   + ", ".join(["%s"] * len(cols)) + ")")
            cur.executemany(sql, values)
    return len(values)


def main():
    ap = argparse.ArgumentParser(description="落库天猫售后任务明细")
    ap.add_argument("--config", required=True, type=pathlib.Path)
    ap.add_argument("--data-dir", type=pathlib.Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--store", action="append")
    ap.add_argument("--scan-days", type=int, default=7)
    args = ap.parse_args()
    cfg = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8"))
    selected = set(args.store or []) or set(STORE_KEYS)
    conn = pymysql.connect(**cfg["mysql"], autocommit=False)
    try:
        ensure_table(conn)
        for key in sorted(selected):
            if key not in STORE_KEYS:
                raise ValueError(f"unknown store: {key}")
            snap = latest_snapshot(args.data_dir, key, args.scan_days)
            if not snap:
                print(json.dumps({"store": key, "status": "no_snapshot"}, ensure_ascii=False))
                continue
            af = (snap.get("data") or {}).get("after_sales") or {}
            rows = af.get("rows") or []
            record_date = parse_date(snap.get("slot_time"))
            if record_date is None:
                print(json.dumps({"store": key, "status": "no_slot_date"}, ensure_ascii=False))
                continue
            n = persist(conn, record_date, STORE_KEYS[key], rows, snap.get("collection_id"))
            conn.commit()
            print(json.dumps({
                "store": key, "client": STORE_KEYS[key], "status": "ok",
                "record_date": record_date.isoformat(),
                "collection_id": snap.get("collection_id"),
                "detail_status": af.get("detail_status"),
                "rows_in_snapshot": len(rows), "rows_persisted": n,
            }, ensure_ascii=False))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
