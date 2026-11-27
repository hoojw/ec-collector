"""Read each logged-in JD store's pending deposit into the shared workbench."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import pymysql

from jd_esale_after_sales_metrics import visible_text
from workbench import qualified as qualified_workbench
from workbench import target as workbench_target


TZ = ZoneInfo("Asia/Shanghai")
DEPOSIT_URL = "https://fin-jdm.jd.com/finance/depositManage/shopDepositNew"
AMOUNT = re.compile(r"^[¥￥]\s*([0-9][0-9,]*(?:\.\d{1,2})?)$")


def parse_pending_deposit(text: str) -> Decimal:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    try:
        index = lines.index("待缴保证金")
    except ValueError as error:
        raise RuntimeError("JD deposit page did not expose 待缴保证金") from error
    for value in lines[index + 1:index + 4]:
        match = AMOUNT.fullmatch(value)
        if not match:
            continue
        try:
            return Decimal(match.group(1).replace(",", ""))
        except InvalidOperation as error:
            raise RuntimeError(f"invalid JD pending deposit amount: {value!r}") from error
    raise RuntimeError("JD deposit page did not expose a pending-deposit amount")


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


def persist(conn, config: dict, record_date: dt.date, amount: Decimal) -> None:
    store = store_settings(config)
    schema, table = workbench_target(config)
    target = qualified_workbench(schema, table)
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {target} (`日期`,`客户`,`商家负责人`,`平台`,`补缴保证金（店铺违规）`)
            VALUES (%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              `商家负责人`=VALUES(`商家负责人`),
              `补缴保证金（店铺违规）`=VALUES(`补缴保证金（店铺违规）`)""",
            (record_date, store["client"], store["owner"], store["platform"], amount),
        )


def run(args: argparse.Namespace) -> dict:
    config = json.loads(args.config.read_text(encoding="utf-8"))
    record_date = args.record_date or dt.datetime.now(TZ).date()
    amount = parse_pending_deposit(
        visible_text(DEPOSIT_URL, args.port, ready_pattern=re.compile(r"待缴保证金"))
    )
    if not args.dry_run:
        mysql = config["mysql"]
        conn = pymysql.connect(
            host=mysql["host"], port=int(mysql["port"]), user=mysql["user"], password=mysql["password"],
            database=mysql["database"], charset=mysql.get("charset", "utf8mb4"), autocommit=False,
        )
        try:
            persist(conn, config, record_date, amount)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {"state": "success", "record_date": record_date.isoformat(), "pending_deposit": str(amount)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect JD pending deposit")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--record-date", type=dt.date.fromisoformat)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2))
