#!/usr/bin/env python3
"""Import the captured 天猫 ActivityList (available.list) dataset into MySQL.

Source: /home/dataease/incoming/tmall-activity-full.json (uploaded by the
Windows-side tmall_activity_collect.py).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
from zoneinfo import ZoneInfo

import pymysql

TZ = ZoneInfo("Asia/Shanghai")
TABLE = "tmall_activity_available_daily"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/opt/subsidy/config.json")
    ap.add_argument("--json", default="/home/dataease/incoming/tmall-activity-full.json")
    ap.add_argument("--day", default="")
    args = ap.parse_args()

    day = args.day or dt.datetime.now(TZ).date().isoformat()
    payload = json.loads(pathlib.Path(args.json).read_text(encoding="utf-8"))
    rows = payload.get("rows") or []

    mysql = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8"))["mysql"]
    conn = pymysql.connect(host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
                           password=mysql["password"], database=mysql["database"],
                           charset=mysql.get("charset", "utf8mb4"), autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"""CREATE TABLE IF NOT EXISTS `{TABLE}` (
                `id` BIGINT NOT NULL AUTO_INCREMENT,
                `记录日期` DATE NOT NULL,
                `活动ID` VARCHAR(50) DEFAULT NULL,
                `活动类型` VARCHAR(50) DEFAULT NULL,
                `报名状态` VARCHAR(20) DEFAULT NULL,
                `是否补贴` VARCHAR(20) DEFAULT NULL,
                `补贴方式` VARCHAR(20) DEFAULT NULL,
                `服务费` VARCHAR(50) DEFAULT NULL,
                `活动状态` VARCHAR(20) DEFAULT NULL,
                `通用名` VARCHAR(255) DEFAULT NULL,
                `规格` VARCHAR(255) DEFAULT NULL,
                `活动商品名` VARCHAR(500) DEFAULT NULL,
                `商品标题` VARCHAR(500) DEFAULT NULL,
                `天猫商品ID` VARCHAR(50) DEFAULT NULL,
                `品牌` TEXT DEFAULT NULL,
                `skuId` VARCHAR(60) DEFAULT NULL,
                `skuItemId` VARCHAR(60) DEFAULT NULL,
                `包装档位` VARCHAR(50) DEFAULT NULL,
                `招商价格` DECIMAL(12,2) DEFAULT NULL,
                `活动开始` VARCHAR(40) DEFAULT NULL,
                `活动结束` VARCHAR(40) DEFAULT NULL,
                `raw_json` LONGTEXT DEFAULT NULL,
                `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (`id`),
                UNIQUE KEY `uk_day_act_sku` (`记录日期`,`活动ID`,`skuId`),
                KEY `idx_day_apply` (`记录日期`,`报名状态`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")

            def b(v):
                return "true" if v is True or str(v).lower() == "true" else ("false" if v is False or str(v).lower() == "false" else None)

            sql = f"""INSERT INTO `{TABLE}`
                (`记录日期`,`活动ID`,`活动类型`,`报名状态`,`是否补贴`,`补贴方式`,`服务费`,`活动状态`,
                 `通用名`,`规格`,`活动商品名`,`商品标题`,`天猫商品ID`,`品牌`,`skuId`,`skuItemId`,
                 `包装档位`,`招商价格`,`活动开始`,`活动结束`,`raw_json`)
                VALUES ({','.join(['%s']*21)})
                ON DUPLICATE KEY UPDATE `raw_json`=VALUES(`raw_json`),`报名状态`=VALUES(`报名状态`),
                `招商价格`=VALUES(`招商价格`)"""
            vals = []
            for r in rows:
                vals.append((
                    day, r.get("activityId"), r.get("activityTypeView"), b(r.get("alreadyApply")),
                    b(r.get("hasSubsidy")), r.get("subsidyStyle"), r.get("commissionInfo"),
                    str(r.get("status")) if r.get("status") is not None else None,
                    r.get("commonName"), r.get("spec"), r.get("name"), r.get("itemTitle"),
                    r.get("platformItemId"), r.get("selectedBrand"), r.get("skuId"), r.get("skuItemId"),
                    r.get("skuViewName"), r.get("skuPrice"), r.get("startTime"), r.get("endTime"),
                    json.dumps(r, ensure_ascii=False),
                ))
            # The JSON is a full daily snapshot.  Replace the partition so
            # rows with a NULL skuId cannot accumulate across repeated runs
            # (MySQL UNIQUE KEY does not dedupe NULLs).
            cur.execute(f"DELETE FROM `{TABLE}` WHERE `记录日期`=%s", (day,))
            for i in range(0, len(vals), 500):
                cur.executemany(sql, vals[i:i+500])
        conn.commit()
        print("imported", len(vals), "rows into", TABLE, "day", day)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
