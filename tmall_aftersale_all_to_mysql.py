#!/usr/bin/env python3
"""把 147 上传的天猫售后全量明细增量落库（首次全量，后续只新增）。

数据源：/home/dataease/incoming/tmall-aftersale-all.json（147 采集端上传，按售后单号去重后的全量）。
策略：按售后单号 INSERT ... ON DUPLICATE KEY UPDATE，不删除历史行；每次跑只会新增未见过的单、
并刷新已知单的状态（退款状态可能从待处理变成功）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re

import pymysql
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_SRC = pathlib.Path("/home/dataease/incoming/tmall-aftersale-all.json")
EXTRA_SRC = pathlib.Path("/home/dataease/incoming/tmall-aftersale-daily.json")
TABLE = "tmall_after_sale_all"


def q(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def money(v):
    m = re.search(r"[\d.]+", str(v or ""))
    return float(m.group()) if m else None


def dt_norm(v):
    if not v:
        return None
    s = str(v).strip()
    if len(s) == 19:
        return s
    if len(s) == 16:
        return s + ":00"
    return s[:19] if len(s) > 19 else s


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {q(TABLE)} (
          `售后单号` VARCHAR(32) NOT NULL,
          `订单编号` VARCHAR(32) NULL,
          `申请时间` DATETIME NULL,
          `售后类型` VARCHAR(64) NULL,
          `纠纷类型` VARCHAR(64) NULL,
          `退款状态` VARCHAR(128) NULL,
          `客服状态` VARCHAR(64) NULL,
          `协商状态` VARCHAR(64) NULL,
          `商品标题` VARCHAR(512) NULL,
          `SKU` VARCHAR(128) NULL,
          `条码` VARCHAR(64) NULL,
          `退款金额` DECIMAL(12,2) NULL,
          `买家实付` DECIMAL(12,2) NULL,
          `退款原因` VARCHAR(255) NULL,
          `买家` VARCHAR(64) NULL,
          `审核状态` VARCHAR(64) NULL,
          `店铺` VARCHAR(64) NOT NULL DEFAULT '保民',
          `首次入库时间` DATETIME NOT NULL,
          `最近采集时间` DATETIME NOT NULL,
          PRIMARY KEY (`售后单号`),
          KEY `idx_apply` (`申请时间`),
          KEY `idx_order` (`订单编号`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='天猫售后全量明细（含历史退款，按售后单号增量 upsert）'
        """)


def main():
    ap = argparse.ArgumentParser(description="天猫售后全量明细增量落库")
    ap.add_argument("--config", required=True, type=pathlib.Path)
    ap.add_argument("--src", type=pathlib.Path, default=DEFAULT_SRC)
    args = ap.parse_args()

    sources = [args.src]
    if EXTRA_SRC.exists() and EXTRA_SRC != args.src:
        sources.append(EXTRA_SRC)
    rows = []
    collected = ""
    src_info = []
    for src in sources:
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        part = data.get("rows") or []
        rows.extend(part)
        collected = max(collected, data.get("collected_at") or "")
        src_info.append(f"{src.name}:{len(part)}")
    collected = collected or dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    conn = pymysql.connect(**cfg["mysql"], autocommit=False)
    try:
        ensure_table(conn)
        cols = ["售后单号", "订单编号", "申请时间", "售后类型", "纠纷类型", "退款状态",
                "客服状态", "协商状态", "商品标题", "SKU", "条码", "退款金额", "买家实付",
                "退款原因", "买家", "审核状态", "店铺", "首次入库时间", "最近采集时间"]
        now = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        values, seen = [], set()
        for r in rows:
            if not isinstance(r, dict):
                continue
            sid = str(r.get("售后单号") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            values.append([
                sid, r.get("订单编号"), dt_norm(r.get("申请时间")), r.get("售后类型"),
                r.get("纠纷类型"), r.get("退款状态"), r.get("客服状态"), r.get("协商状态"),
                r.get("商品标题"), r.get("SKU"), r.get("条码"), money(r.get("退款金额")),
                money(r.get("买家实付")), r.get("退款原因"), r.get("买家"), r.get("审核状态"),
                "保民", now, collected,
            ])
        with conn.cursor() as cur:
            if values:
                sql = (f"INSERT INTO {q(TABLE)} (" + ", ".join(q(c) for c in cols) + ") VALUES ("
                       + ", ".join(["%s"] * len(cols)) + ") "
                       + "ON DUPLICATE KEY UPDATE "
                       + ", ".join(f"{q(c)}=VALUES({q(c)})" for c in cols if c not in ("售后单号", "首次入库时间")))
                cur.executemany(sql, values)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {q(TABLE)}")
            total = cur.fetchone()[0]
        print(json.dumps({
            "status": "ok", "sources": src_info, "rows_in_file": len(rows),
            "rows_upserted": len(values), "total_in_table": total,
            "collected_at": collected,
        }, ensure_ascii=False))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
