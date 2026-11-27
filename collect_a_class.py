#!/usr/bin/env python3
"""A 类字段采集（零侵入框架，2026-11-04）。

=====================================================================
红线（绝不能违反）
=====================================================================
- 不修改任何现有采集脚本、不写任何现有表、不重新登录、不触发任何写操作。
- 复用常驻 Chrome 的 CDP 连接时，只做只读 fetch / 读 DOM。
- 美团当日实时走「已落库的 ops_business_report 同步」，连 CDP 都不需要。
- 所有新增数据落独立新表（realtime_amount_snapshot / a_class_compliance_fields），
  绝不碰 store_daily / esale_system / 各平台原采集表。

=====================================================================
现状（代码探查结论，详见对话）
=====================================================================
- 美团当日实时交易额：已由 meituan_business_report 落入 ops_business_report
  （时间='实时' 那行的 成交金额 列）。本脚本仅做 SQL 同步 —— 零侵入、零 CDP。
- 京东/美团 商品文案/适应症/说明书、下架原因/驳回、可售卖时间/禁售区域：
  页面 URL 已知，但对应 fetch API 尚未在浏览器会话实测，故为占位 TODO，
  待实测后填充实现（见 PLATFORM_PAGES + 各 collect_* 函数顶部注释）。
- 商品状态（下架/上架）：已由 jd_store_sku_normalized_daily / 美团脚本覆盖，
  面板直接读原表即可，本框架不重爬（见 get_product_status_helper 说明）。
- 京东/天猫当日实时入口：未定位（京东 realtime 被网关拒、天猫走快照 receiver），
  占位 TODO。

运行：默认只跑已实现的「美团当日实时同步」。其余字段需 --enable-todo 才尝试，
但当前会显式 NotImplementedError 并说明待实测项，绝不臆造数据。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
from decimal import Decimal
from zoneinfo import ZoneInfo

import pymysql

TZ = ZoneInfo("Asia/Shanghai")
LOG = logging.getLogger("collect_a_class")

# 独立新表，不影响任何现有表
REALTIME_TABLE = "realtime_amount_snapshot"
COMPLIANCE_TABLE = "a_class_compliance_fields"


def load_config(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 已实现：美团当日实时交易额 同步（零侵入，纯 SQL，复用已落库数据）
# ---------------------------------------------------------------------------
def ensure_realtime_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS `{REALTIME_TABLE}` ("
            "  `capture_date` DATE NOT NULL,"
            "  `platform` VARCHAR(16) NOT NULL,"
            "  `client` VARCHAR(64) NOT NULL,"
            "  `poi_id` VARCHAR(32),"
            "  `amount` DECIMAL(18,2),"
            "  `captured_at` DATETIME NOT NULL,"
            "  PRIMARY KEY (`capture_date`, `platform`, `client`)"
            ") DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )


def sync_meituan_realtime(config: dict) -> dict:
    """美团当日实时成交金额 = ops_business_report 中 时间='实时' 的 成交金额。

    该数据已由 meituan_business_report.py 采集落入店铺库，本函数只做同步，
    不触达任何页面、不重新登录。
    """
    store = config.get("meituan_store") or {}
    client = str(store.get("client") or "美团")
    poi_id = str(store.get("poi_id") or "")
    platform = str(store.get("platform") or "美团")
    mysql = config["mysql"]
    now = dt.datetime.now(TZ).replace(microsecond=0)
    conn = pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
        password=mysql["password"], database=mysql["database"],
        charset=mysql.get("charset", "utf8mb4"), autocommit=False,
    )
    try:
        ensure_realtime_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT `成交金额` FROM `ops_business_report` WHERE `时间` = '实时'"
            )
            row = cur.fetchone()
        if not row or row[0] is None:
            raise RuntimeError(
                "ops_business_report 无 '实时' 行（meituan_business_report 可能尚未运行）"
            )
        amount = Decimal(str(row[0]))
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO `{REALTIME_TABLE}` "
                "(`capture_date`,`platform`,`client`,`poi_id`,`amount`,`captured_at`) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE `amount`=VALUES(`amount`), "
                "`captured_at`=VALUES(`captured_at`)",
                (now.date(), platform, client, poi_id, amount, now),
            )
        conn.commit()
        LOG.info("美团当日实时成交金额 同步成功: %s = %.2f @ %s",
                 client, amount, now.isoformat())
        return {"platform": platform, "client": client,
                "amount": float(amount), "captured_at": now.isoformat()}
    finally:
        conn.close()


def sync_realtime(config: dict, platform: str) -> dict:
    if platform == "美团":
        return sync_meituan_realtime(config)
    # 京东/天猫当日实时入口未定位（京东 realtime 被网关拒、天猫走快照 receiver）。
    raise NotImplementedError(
        f"{platform} 当日实时入口未定位：需在浏览器会话实测后实现"
    )


# ---------------------------------------------------------------------------
# 占位 TODO：页面 URL 已知，但 fetch API 尚未在浏览器会话实测。
# 切勿臆造 endpoint；待实测后填充实现。需 --enable-todo 才调用。
# ---------------------------------------------------------------------------
PLATFORM_PAGES = {
    "jd_product_detail": "https://jdsz.jd.com/szweb/view/product/productDetail.html",
    "jd_product_list": "https://wares-jdm.jd.com/ware/wareList?activeTab=OnsaleWare",
    "mt_product_detail": "https://yiyao.meituan.com/main/frame#/page/analysis/product/b2c",
    "mt_product_list": "https://yiyao.meituan.com/main/frame#/page/product/list/single",
}


def _todo(field: str, note: str):
    LOG.warning("TODO 未实现: %s — %s", field, note)
    raise NotImplementedError(f"{field} 待浏览器实测 API 后实现: {note}")


def collect_product_copywriting(page, platform: str, client: str) -> None:
    """商品文案 / 适应症 / 说明书（处方药合规）。

    页面: 各平台「商品详情页」（见 PLATFORM_PAGES 的 *_product_detail）。
    TODO: 实测商品详情 API，提取文案文本 / 说明书 tab 内容。
    实现时复用 meituan_business_report.CdpPage.fetch_json（美团）或
    jd_store_sku_web_collect.Page（京东），只读 fetch，落 COMPLIANCE_TABLE。
    """
    _todo("product_copywriting",
          f"页面 {PLATFORM_PAGES.get(platform + '_product_detail', '?')} 的详情 API 待实测")


def collect_offline_reasons(page, platform: str, client: str) -> None:
    """商品下架原因 / 驳回原因 / 审核。

    页面: 各平台「消息中心 / 商品审核 / 下架列表」页。
    TODO: 实测消息列表 API。注意平台通常只给违规通知自由文本，
    无结构化「原因」字段，需规则/NLP 提取或人工补录（与 B7 方向一致）。
    """
    _todo("offline_reasons",
          "消息列表/商品审核页 API 待实测；平台多无结构化原因字段，需人工补录")


def collect_sell_time_ban_region(page, platform: str, client: str) -> None:
    """可售卖时间 / 禁售区域（B 端商品配置）。

    页面: 各平台「商品编辑 / 配送设置」页。
    TODO: 实测配置接口。商家每日/一次性提供禁售表格，平台配置爬回后
    与表格比对，差异触发预警（见 md 合规·商品合规 备注）。
    """
    _todo("sell_time_ban_region",
          "商品编辑/配送配置 API 待实测；需与商家给的禁售表格比对")


def get_product_status_helper(config: dict) -> list:
    """已覆盖字段：商品状态（下架/上架）。

    京东: jd_store_sku_normalized_daily.商品状态
    美团: meituan 商品采集脚本的 商品状态 列
    天猫: 天猫脚本商品状态列
    该数据已由现有采集覆盖，面板直接读原表即可，本框架不重爬。
    此 helper 仅作说明入口，默认不调用。
    """
    raise NotImplementedError("商品状态已覆盖，请直读原采集表，勿经此重爬")


# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="A-class field collection (zero-touch)")
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--port", type=int, default=9333,
                        help="常驻 Chrome CDP 端口（仅 --enable-todo 时用）")
    parser.add_argument("--enable-todo", action="store_true",
                        help="尝试运行占位 TODO 字段（当前会显式 NotImplementedError）")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    config = load_config(args.config)
    results: dict = {}

    # 已实现：美团当日实时（零侵入同步）
    try:
        results["meituan_realtime"] = sync_realtime(config, "美团")
    except Exception as exc:  # noqa: BLE001
        LOG.error("美团当日实时同步失败: %s", exc)
        results["meituan_realtime"] = {"error": str(exc)}

    # TODO 字段：默认跳过；--enable-todo 时尝试（会显式提示待实测项）
    if args.enable_todo:
        from meituan_business_report import CdpPage
        page = CdpPage(args.port)
        try:
            store = config.get("meituan_store") or {}
            client = str(store.get("client") or "美团")
            for fn in (collect_product_copywriting, collect_offline_reasons,
                       collect_sell_time_ban_region):
                try:
                    fn(page, "mt", client)
                except NotImplementedError as exc:
                    LOG.warning("跳过: %s", exc)
        finally:
            page.close()

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
