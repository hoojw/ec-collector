#!/usr/bin/env python3
"""把天猫 Patchright 电商经营快照落库到 esale_system，对齐美团/京东模式。

数据来源：/opt/subsidy/tmall-ecommerce-data/received/ 下的 daily / every10 快照。
- daily 快照（portal/macro/item_rank/on_sale）→ esale_system 前 11 列经营指标。
- every10 快照（after_sales）→ esale_system 售后两列。

「日期」取快照 slot_time 的日期（采集当天），与京东/美团的 record_date 语义一致；
「昨日交易额」取 portal 页面已算好的昨日成交金额；「昨日周环比」复用 workbench
模块的统一口径（当前昨日交易额 与 7 天前同客户同平台昨日交易额相比），保证跨平台一致。

采集中途失败但未整体失败（state=partial）的快照同样可用：只写入本次真的采到的
数据集所对应的列，缺失数据集负责的列保持原值，绝不用 NULL 覆盖既有数据。
仅 state=failed 的快照被跳过。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import pymysql

from workbench import qualified, refresh_yesterday_week_ratio, target as workbench_target


TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DATA_DIR = pathlib.Path("/opt/subsidy/tmall-ecommerce-data")

# A snapshot whose collection ended up partial still carries accurate values for
# the datasets that did arrive, so one flaky page should not cost the whole
# workbench row.  Only "failed" snapshots are unusable.
USABLE_STATES = ("success", "partial")

DAILY_METRIC_COLUMNS = (
    "昨日交易额",
    "商品动销率",
    "经营商品数量",
    "未动销商品数量",
    "本月累计交易额",
    "近30日环比",
    "非替尔泊肽销售金额占比",
    "日环比平台",
    "退货退款初审时间",
)
# Which collected datasets each workbench column is derived from.  A column is
# written only when every dataset it needs is present, so a missing dataset can
# never blank out a value that an earlier run already stored.
DAILY_COLUMN_DATASETS = {
    "昨日交易额": ("portal",),
    "经营商品数量": ("on_sale",),
    "本月累计交易额": ("macro",),
    "近30日环比": ("macro",),
    "商品动销率": ("macro", "on_sale"),
    "未动销商品数量": ("macro", "on_sale"),
    "非替尔泊肽销售金额占比": ("macro", "item_rank"),
    "日环比平台": ("portal",),
    "退货退款初审时间": ("portal",),
}
EVERY10_METRIC_COLUMNS = ("待商家审核数量", "即将自动退售后单")
EVERY10_COLUMN_DATASETS = {
    "待商家审核数量": ("after_sales",),
    "即将自动退售后单": ("after_sales",),
}


def usable_columns(
    data: dict,
    column_datasets: dict[str, tuple[str, ...]],
    order: tuple[str, ...],
) -> tuple[str, ...]:
    """Return the workbench columns this snapshot's datasets can populate."""
    payload = data.get("data") or {}
    return tuple(
        column
        for column in order
        if all(name in payload for name in column_datasets[column])
    )


def decimal_or_none(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def int_or_none(value):
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def parse_slot_date(value) -> dt.date | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.date()


def store_settings(config: dict) -> dict[str, str]:
    store = config.get("tmall_store") or {}
    result = {
        "client": str(store.get("client") or "").strip(),
        "owner": str(store.get("owner") or "待维护").strip(),
        "platform": str(store.get("platform") or "天猫").strip(),
    }
    if not result["client"]:
        raise ValueError("tmall store config requires tmall_store.client")
    return result


def configured_stores(config: dict) -> list[tuple[str, dict[str, str]]]:
    """Return the ``(store_key, store settings)`` pairs this run should load.

    Multi-store shape (one config drives every Tmall store)::

        "tmall_stores": {
          "baomin":     {"client": "保民",   "owner": "..."},
          "jinghetang": {"client": "璟和堂", "owner": "待维护"}
        }

    Legacy shape (a dedicated config per store, as 保民 has today) yields one
    pair whose store key is read from ``tmall_store.store_key`` or defaults to
    ``default`` — the historical single-directory layout.
    """
    stores = config.get("tmall_stores")
    if stores is not None:
        if not isinstance(stores, dict) or not stores:
            raise ValueError("tmall_stores must be a non-empty object")
        pairs: list[tuple[str, dict[str, str]]] = []
        for store_key in sorted(stores):
            entry = stores[store_key] or {}
            if not isinstance(entry, dict):
                raise ValueError(f"tmall_stores.{store_key} must be an object")
            client = str(entry.get("client") or "").strip()
            if not client:
                raise ValueError(f"tmall_stores.{store_key}.client is required")
            pairs.append((
                str(store_key),
                {
                    "client": client,
                    "owner": str(entry.get("owner") or "待维护").strip(),
                    "platform": str(entry.get("platform") or "天猫").strip(),
                },
            ))
        return pairs
    store = store_settings(config)
    store_key = str((config.get("tmall_store") or {}).get("store_key") or "default").strip()
    return [(store_key, store)]


def snapshot_roots(data_dir: pathlib.Path, store_key: str, job: str) -> tuple[pathlib.Path, ...]:
    """Every directory a store's snapshots for one job can live under.

    The multi-store receiver namespaces snapshots as
    ``received/<store_key>/<job>/<yyyy>/<mm>/<dd>/``, but the historical
    single-store layout kept them directly under ``received/<job>/...``.

    ``default`` is the store that already existed when namespacing arrived, so
    its files are split across *both* locations: everything written before the
    receiver learned about store keys sits in the legacy path, everything after
    sits in the namespaced one.  Reading only one of them silently freezes that
    store's data at the migration boundary — which is exactly what happened to
    保民 between 2026-09-23 and 2026-09-24.  Always scan both.
    """
    base = data_dir / "received"
    if store_key == "default":
        return (base / job, base / store_key / job)
    return (base / store_key / job,)


def _iter_json(
    data_dir: pathlib.Path,
    job: str,
    store_key: str = "default",
    days: int = 7,
    extra_date: dt.date | None = None,
):
    """List snapshot JSONs for recent dates only (plus an optional backfill date).

    Snapshots live under ``<root>/<yyyy>/<mm>/<dd>/``.  Glob-ing every date
    directory with ``*/*/*/*.json`` walks the entire archive (16k+ files today),
    which makes each 10-minute sync take minutes.  We only need the newest
    usable snapshot, so scanning the last ``days`` dates is sufficient; an
    explicit ``--record-date`` is added so backfills still find their snapshot.
    """
    wanted: set[dt.date] = set()
    today = dt.datetime.now(TZ).date()
    for offset in range(max(1, days)):
        wanted.add(today - dt.timedelta(days=offset))
    if extra_date is not None:
        wanted.add(extra_date)
    paths: set[pathlib.Path] = set()
    for root in snapshot_roots(data_dir, store_key, job):
        for day in wanted:
            folder = root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
            if folder.is_dir():
                paths.update(folder.glob("*.json"))
    return sorted(paths)


def _latest_usable_snapshot(
    data_dir: pathlib.Path,
    job: str,
    store_key: str = "default",
    days: int = 7,
    extra_date: dt.date | None = None,
):
    """返回 job 下 state 可用且 slot_time 最新的快照 dict，无则 None。

    "可用"指 ``success`` 或 ``partial``；``failed`` 没有任何可信数据集，跳过。
    """
    best = None
    best_rank = None
    for path in _iter_json(data_dir, job, store_key, days, extra_date):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("state") not in USABLE_STATES:
            continue
        if not data.get("slot_time"):
            continue
        payload = data.get("data") or {}
        on_sale = payload.get("on_sale") or {}
        rows = on_sale.get("rows") or []
        complete = bool(on_sale.get("pagination_complete"))
        # 先按数据日期取最新，再在同一天内优先分页完整、行数更多的快照。
        # 不能让历史某天更完整的 on_sale 抢掉当天数据。
        rank = (
            str(data.get("slot_time") or ""),
            1 if complete else 0,
            len(rows),
            str(data.get("collected_at") or ""),
        )
        if best_rank is None or rank > best_rank:
            best_rank, best = rank, data
    return best


def parse_daily(data: dict) -> dict:
    payload = data.get("data") or {}
    portal = payload.get("portal") or {}
    macro = payload.get("macro") or {}
    item_rank = payload.get("item_rank") or {}
    on_sale = payload.get("on_sale") or {}

    pm = portal.get("metrics") or {}
    mm = macro.get("metrics") or {}

    on_sale_total = int_or_none(on_sale.get("total"))
    moving = int_or_none(mm.get("动销商品数"))
    near30_amount = decimal_or_none(mm.get("近30日支付金额"))
    tirzepatide_amount = decimal_or_none(item_rank.get("替尔泊肽支付金额合计"))

    turnover_rate = None
    if moving is not None and on_sale_total:
        turnover_rate = Decimal(moving) / Decimal(on_sale_total)

    nonmoving = None
    if on_sale_total is not None and moving is not None and on_sale_total >= moving:
        nonmoving = Decimal(on_sale_total - moving)

    non_tirzepatide_ratio = None
    if near30_amount is not None and near30_amount != 0 and tirzepatide_amount is not None:
        non_tirzepatide_ratio = (near30_amount - tirzepatide_amount) / near30_amount

    return {
        "昨日交易额": decimal_or_none(pm.get("昨日交易额")),
        "商品动销率": turnover_rate,
        "经营商品数量": Decimal(on_sale_total) if on_sale_total is not None else None,
        "未动销商品数量": nonmoving,
        "本月累计交易额": near30_amount,
        "近30日环比": decimal_or_none(mm.get("近30日环比")),
        "非替尔泊肽销售金额占比": non_tirzepatide_ratio,
        "日环比平台": decimal_or_none(pm.get("昨日环比")),
        "退货退款初审时间": decimal_or_none(pm.get("退款处理时长_天")),
    }


def parse_every10(data: dict) -> dict:
    payload = data.get("data") or {}
    after_sales = payload.get("after_sales") or {}
    m = after_sales.get("metrics") or {}
    return {
        "待商家审核数量": int_or_none(m.get("待审核或待商家处理数量")),
        "即将自动退售后单": int_or_none(m.get("即将超时售后单数量")),
    }


def load_mysql_config(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def persist_daily(
    conn,
    schema: str,
    table: str,
    record_date: dt.date,
    metrics: dict,
    store: dict,
    columns: tuple[str, ...],
) -> tuple[str, ...]:
    """只写 ``columns`` 覆盖到的列，其余列保持原值不动。"""
    target = qualified(schema, table)
    selected = tuple(column for column in columns if column in metrics)
    if not selected:
        raise ValueError("daily snapshot provides no writable metric column")
    names = ["日期", "客户", "商家负责人", "平台", *selected]
    assignments = ["`商家负责人`=VALUES(`商家负责人`)"] + [
        f"`{column}`=VALUES(`{column}`)" for column in selected
    ]
    sql = (
        f"INSERT INTO {target} (`" + "`,`".join(names) + "`) "
        f"VALUES ({', '.join(['%s'] * len(names))}) "
        f"ON DUPLICATE KEY UPDATE " + ", ".join(assignments)
    )
    values = [record_date, store["client"], store["owner"], store["platform"]]
    values.extend(metrics[column] for column in selected)
    with conn.cursor() as cursor:
        cursor.execute(sql, values)
    # 昨日周环比 由 昨日交易额 推导，只有本次真的刷新了该输入才重算。
    if "昨日交易额" in selected:
        refresh_yesterday_week_ratio(
            conn,
            schema=schema,
            table=table,
            record_date=record_date,
            client=store["client"],
            platform=store["platform"],
        )
    return selected


def persist_every10(
    conn,
    schema: str,
    table: str,
    record_date: dt.date,
    metrics: dict,
    store: dict,
    columns: tuple[str, ...],
) -> tuple[str, ...]:
    """只写 ``columns`` 覆盖到的售后列，其余列保持原值不动。"""
    target = qualified(schema, table)
    selected = tuple(column for column in columns if column in metrics)
    if not selected:
        raise ValueError("every10 snapshot provides no writable after-sales column")
    names = ["日期", "客户", "平台", *selected]
    assignments = [f"`{column}`=VALUES(`{column}`)" for column in selected]
    sql = (
        f"INSERT INTO {target} (`" + "`,`".join(names) + "`) "
        f"VALUES ({', '.join(['%s'] * len(names))}) "
        f"ON DUPLICATE KEY UPDATE " + ", ".join(assignments)
    )
    values = [record_date, store["client"], store["platform"]]
    values.extend(metrics[column] for column in selected)
    with conn.cursor() as cursor:
        cursor.execute(sql, values)
    return selected


def run_store(
    conn,
    schema: str,
    table: str,
    data_dir: pathlib.Path,
    store_key: str,
    store: dict,
    jobs: tuple[str, ...],
    record_date: dt.date | None,
    scan_days: int = 7,
) -> None:
    """Load the newest usable snapshot for one store into the workbench."""
    for job in jobs:
        snapshot = _latest_usable_snapshot(data_dir, job, store_key, scan_days, record_date)
        if snapshot is None:
            print(json.dumps({
                "store_key": store_key, "client": store["client"],
                "job": job, "status": "no_snapshot",
            }, ensure_ascii=False))
            continue
        if job == "daily":
            metrics = parse_daily(snapshot)
            columns = usable_columns(snapshot, DAILY_COLUMN_DATASETS, DAILY_METRIC_COLUMNS)
            persist = persist_daily
        else:
            metrics = parse_every10(snapshot)
            columns = usable_columns(
                snapshot, EVERY10_COLUMN_DATASETS, EVERY10_METRIC_COLUMNS
            )
            persist = persist_every10
        if not columns:
            print(json.dumps({
                "store_key": store_key, "client": store["client"], "job": job,
                "status": "no_dataset",
                "state": snapshot.get("state"),
                "collection_id": snapshot.get("collection_id"),
            }, ensure_ascii=False))
            continue
        # A snapshot's record date follows its own slot; --record-date only
        # overrides it for deliberate backfills.
        target_date = record_date or parse_slot_date(snapshot.get("slot_time"))
        if target_date is None:
            raise ValueError(f"{job} snapshot missing slot_time")
        written = persist(conn, schema, table, target_date, metrics, store, columns)
        print(json.dumps({
            "store_key": store_key,
            "client": store["client"],
            "job": job,
            "status": "ok",
            "record_date": target_date.isoformat(),
            "state": snapshot.get("state"),
            "columns": list(written),
            "collection_id": snapshot.get("collection_id"),
        }, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="落库天猫电商经营快照到 esale_system")
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--data-dir", type=pathlib.Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--record-date", type=dt.date.fromisoformat, help="指定日期（默认取最新快照的 slot_time 日期）")
    parser.add_argument("--job", choices=["daily", "every10", "both"], default="both")
    parser.add_argument(
        "--scan-days", type=int, default=7,
        help="只扫描最近 N 天的快照目录（默认 7；--record-date 指定的日期会自动加入）",
    )
    parser.add_argument(
        "--store",
        action="append",
        default=None,
        help="只处理指定 store key（可重复）；缺省处理配置里的全部店铺",
    )
    args = parser.parse_args()

    config = load_mysql_config(args.config)
    stores = configured_stores(config)
    selected = set(args.store or [])
    if selected:
        known = {key for key, _ in stores}
        unknown = selected - known
        if unknown:
            raise ValueError(f"unknown store key(s): {', '.join(sorted(unknown))}")
        stores = [(key, store) for key, store in stores if key in selected]
    schema, table = workbench_target(config)
    jobs = ("daily", "every10") if args.job == "both" else (args.job,)

    conn = pymysql.connect(
        **config["mysql"],
        autocommit=False,
    )
    try:
        for store_key, store in stores:
            run_store(
                conn,
                schema,
                table,
                args.data_dir,
                store_key,
                store,
                jobs,
                args.record_date,
                args.scan_days,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
