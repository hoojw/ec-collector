#!/usr/bin/env python3
"""按指定日期回填天猫经营指标到 esale_system（一次性补救脚本）。

背景（2026-09-23）：2026-09-22 10:02:34 的一次 `chown -R dataease:dataease /opt/subsidy`
把接收端数据目录的属主改错，导致此后所有上传返回 HTTP 500；同时
`tmall-ecommerce-sync.timer` 自 9/21 起处于 disabled。两者叠加使
`esale_system` 的天猫行停更在 2026-09-20。

权限修好并把积压快照回放到接收端之后，09-21 / 09-22 的快照已经在
`received/daily/<年>/<月>/<日>/` 里了，但常规同步只写「最新那一份」，
按日回填需要本脚本。

用法（本脚本需部署到 `/opt/subsidy/`，与 `workbench.py` 同目录后执行）：
  /opt/subsidy/venv/bin/python /opt/subsidy/backfill_tmall_ecommerce_days.py \
      --config /opt/subsidy/config.json --date 2026-09-21 --date 2026-09-22
加 --apply 才真正写库，默认只做 dry-run。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

import pymysql

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from workbench import target as workbench_target  # noqa: E402
import tmall_ecommerce_to_mysql as sync  # noqa: E402


def pick_snapshot(data_dir: pathlib.Path, job: str, target_date: dt.date):
    """挑出 slot 日期等于 target_date 的、最新的一份可用快照。"""
    best = None
    best_slot = None
    for path in sync._iter_json(data_dir, job):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("state") not in sync.USABLE_STATES:
            continue
        slot = data.get("slot_time")
        if not slot or sync.parse_slot_date(slot) != target_date:
            continue
        if best_slot is None or slot > best_slot:
            best_slot = slot
            best = data
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description="按日回填天猫经营指标")
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--data-dir", type=pathlib.Path, default=sync.DEFAULT_DATA_DIR)
    parser.add_argument("--date", required=True, action="append", type=dt.date.fromisoformat)
    parser.add_argument("--apply", action="store_true", help="真正写库；缺省仅演练")
    parser.add_argument("--job", choices=["daily", "every10", "both"], default="daily")
    args = parser.parse_args()

    config = sync.load_mysql_config(args.config)
    store = sync.store_settings(config)
    schema, table = workbench_target(config)

    conn = pymysql.connect(**config["mysql"], autocommit=False)
    try:
        for target_date in sorted(set(args.date)):
            jobs = ("daily", "every10") if args.job == "both" else (args.job,)
            for job in jobs:
                snapshot = pick_snapshot(args.data_dir, job, target_date)
                if snapshot is None:
                    print(json.dumps({
                        "date": target_date.isoformat(), "job": job,
                        "status": "no_usable_snapshot",
                    }, ensure_ascii=False))
                    continue
                if job == "daily":
                    metrics = sync.parse_daily(snapshot)
                    columns = sync.usable_columns(
                        snapshot, sync.DAILY_COLUMN_DATASETS, sync.DAILY_METRIC_COLUMNS
                    )
                else:
                    metrics = sync.parse_every10(snapshot)
                    columns = sync.usable_columns(
                        snapshot, sync.EVERY10_COLUMN_DATASETS, sync.EVERY10_METRIC_COLUMNS
                    )
                if not columns:
                    print(json.dumps({
                        "date": target_date.isoformat(), "job": job,
                        "status": "no_usable_column", "state": snapshot.get("state"),
                    }, ensure_ascii=False))
                    continue
                if args.apply:
                    persist = sync.persist_daily if job == "daily" else sync.persist_every10
                    persist(conn, schema, table, target_date, metrics, store, columns)
                print(json.dumps({
                    "date": target_date.isoformat(),
                    "job": job,
                    "status": "applied" if args.apply else "dry_run",
                    "state": snapshot.get("state"),
                    "columns": list(columns),
                    "collection_id": snapshot.get("collection_id"),
                }, ensure_ascii=False))
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
