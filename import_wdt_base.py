#!/usr/bin/env python3
"""旺店通导出（商品档案 / 平台商品 / 按平台商品匹配 / 库存查询）→ adjust_price 基础表。

只做"原样落库"，不做任何指标计算：
- bi_sku_platform  平台商品映射（平台 SPU/SKU → 69码），解决京东无 UPC 的对齐问题
- bi_sku_cost      平台商品成本（默认采购价 / 零售价 / 平台售价）
- bi_stock         库存时点快照（按 记录日期+仓库+货品编码 保留历史）
- bi_region_ban    禁售区域（按 客户+商品名称+省份 一行一省，便于视图查询）
- bi_night_listing 晚上上架药品清单

设计约束：
- 全部幂等 upsert，可重复执行；不删除任何既有行。
- 采集/计算逻辑不在此处，指标口径统一放到视图层。
- 客户名按「店铺 → 客户」显式映射；未识别的店铺只告警、仍按原样入库（客户留空）。
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import pathlib
import re
import sys
from decimal import Decimal, InvalidOperation

import pymysql
from openpyxl import load_workbook

ROOT = pathlib.Path(__file__).resolve().parent
INBOX = ROOT / "inbox"
CONFIG = ROOT / "config.json"
MAX_COLS = 80

# 旺店通「店铺」→ 库内「客户」。未列出的店铺会在导入时告警。
STORE_TO_CLIENT = {
    "保民医院京东": "保民",
    "北辰保民天猫店": "保民",
    "北辰保民美团普药店": "保民",
    "北辰保民美团特药店": "保民",
    "南无门诊京东店": "津南南无门诊",
    "南无门诊美团店": "津南南无门诊",
    "百恩堂京东": "百恩堂",
    "百恩堂天猫": "百恩堂",
    "百恩堂美团普药店": "百恩堂",
    "天津璟和堂京东店": "璟和堂",
    "天津璟和堂天猫店": "璟和堂",
    "璟和堂美团普药店": "璟和堂",
}

# 禁售区域 / 晚上上架表里的 sheet 名 → 客户
SHEET_TO_CLIENT = {
    "保民禁售区域": "保民",
    "保民医院": "保民",
    "璟和堂禁售区域": "璟和堂",
    "璟和堂": "璟和堂",
}

PROVINCES = {
    "北京市", "天津市", "上海市", "重庆市", "河北省", "山西省", "辽宁省", "吉林省",
    "黑龙江省", "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省", "河南省",
    "湖北省", "湖南省", "广东省", "海南省", "四川省", "贵州省", "云南省", "陕西省",
    "甘肃省", "青海省", "台湾省", "内蒙古自治区", "广西壮族自治区", "西藏自治区",
    "宁夏回族自治区", "新疆维吾尔自治区", "香港特别行政区", "澳门特别行政区",
}

CODE_RE = re.compile(r"^(\d{8,14})(?:\*(\d+))?$")

# 文件名 → 用途。文件名可能带客户前缀（如「璟和堂库存查询…」），
# 所以用「*关键词*」匹配；同一类导出有多家客户的文件时全部导入。
FILE_PATTERNS = {
    "goods": "*平台【商品】*.xlsx",
    "match": "*按平台商品匹配*.xlsx",
    "stock": "*库存查询*.xlsx",
    "ban": "*禁售区域汇总*.xlsx",
    "night": "*晚上上架药品*.xlsx",
    "batch": "*货品批次管理*.xlsx",
    "expiry": "*有效期管理*.xlsx",
}

# 导出文件名里的时间戳：库存查询20261117163241535.xlsx
STAMP_RE = re.compile(r"(\d{8})\d{6,9}")


# ---------------------------------------------------------------- 基础工具


def platform_norm(value) -> str:
    text = str(value or "").strip()
    for name in ("京东", "美团", "天猫"):
        if text.startswith(name):
            return name
    return text


def split_code(value) -> tuple[str | None, int]:
    """6924147651014*3 -> ('6924147651014', 3)；无法解析返回 (None, 1)。"""
    match = CODE_RE.match(str(value or "").strip())
    if not match:
        return None, 1
    return match.group(1), int(match.group(2) or 1)


def dec(value) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def integer(value) -> int | None:
    number = dec(value)
    if number is None:
        return None
    try:
        return int(number)
    except (ValueError, OverflowError):
        return None


def text(value, limit: int | None = None) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if not result:
        return None
    return result[:limit] if limit else result


def find_files(key: str) -> list[pathlib.Path]:
    """同一类导出可能有多个客户的文件（保民、璟和堂…），全部返回，按导出时间排序。"""
    matches = sorted(INBOX.glob(FILE_PATTERNS[key]))
    if not matches:
        raise FileNotFoundError(f"inbox 中找不到 {key} 对应文件：{FILE_PATTERNS[key]}")
    return matches


def find_file(key: str) -> pathlib.Path:
    return find_files(key)[-1]


def file_date(path: pathlib.Path, fallback: dt.date) -> dt.date:
    """导出日期：优先用文件名里的时间戳，免得 11-17 的表格被记成今天。

    文件名形如「库存查询20261117163241535.xlsx」，取前 8 位当日期。
    """
    match = STAMP_RE.search(path.stem)
    if not match:
        return fallback
    try:
        parsed = dt.datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return fallback
    return parsed


def read_sheet(path: pathlib.Path, sheet: str | None = None) -> tuple[list[str], list[dict]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        name = sheet or workbook.sheetnames[0]
        worksheet = workbook[name]
        # Some WDT exports declare a wrong A1:A<n> dimension while the worksheet
        # XML actually contains all columns. Reset the cached dimension before
        # iterating, otherwise openpyxl silently exposes only column A.
        worksheet.reset_dimensions()
        iterator = worksheet.iter_rows(max_col=MAX_COLS, values_only=True)
        header = [("" if cell is None else str(cell).strip()) for cell in next(iterator)]
        while header and header[-1] == "":
            header.pop()
        rows = []
        for raw in iterator:
            values = list(raw[: len(header)])
            if all(cell is None or str(cell).strip() == "" for cell in values):
                continue
            rows.append(dict(zip(header, values)))
        return header, rows
    finally:
        workbook.close()


def connect():
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    mysql = config["mysql"]
    return pymysql.connect(
        host=mysql["host"],
        port=int(mysql["port"]),
        user=mysql["user"],
        password=mysql["password"],
        database=mysql.get("dw_database") or mysql["database"],
        charset=mysql.get("charset", "utf8mb4"),
        autocommit=False,
        init_command="SET time_zone='+08:00'",
    )


def upsert(conn, table: str, columns: list[str], conflict: list[str], rows: list[tuple]) -> int:
    if not rows:
        return 0
    quoted = ",".join("`" + column + "`" for column in columns)
    placeholders = ",".join(["%s"] * len(columns))
    updates = ",".join(
        "`{0}`=VALUES(`{0}`)".format(column) for column in columns if column not in conflict
    )
    sql = (
        f"INSERT INTO `{table}` ({quoted}) VALUES ({placeholders})"
        + (f" ON DUPLICATE KEY UPDATE {updates}" if updates else "")
    )
    with conn.cursor() as cursor:
        cursor.executemany(sql, rows)
        return cursor.rowcount


# ---------------------------------------------------------------- 建表


DDL = {
    "bi_sku_platform": """
        CREATE TABLE IF NOT EXISTS `bi_sku_platform` (
          `平台` VARCHAR(32) NOT NULL COMMENT '京东/美团/天猫',
          `店铺` VARCHAR(64) NOT NULL,
          `客户` VARCHAR(64) NULL COMMENT '店铺映射后的客户名，未识别为空',
          `平台商品ID` VARCHAR(64) NOT NULL COMMENT 'SPU',
          `平台规格ID` VARCHAR(64) NOT NULL COMMENT 'SKU',
          `平台商品名称` VARCHAR(255) NULL,
          `平台规格名称` VARCHAR(255) NULL,
          `平台商家编码` VARCHAR(64) NULL COMMENT '原始编码，组合装形如 69码*N',
          `69码` VARCHAR(32) NULL COMMENT '解析出的商品条码',
          `盒数` INT NOT NULL DEFAULT 1 COMMENT '组合装盒数',
          `平台售价` DECIMAL(18,2) NULL,
          `平台库存` INT NULL,
          `售卖状态` VARCHAR(16) NULL,
          `规格类型` VARCHAR(16) NULL,
          `货品名称` VARCHAR(255) NULL,
          `数据日期` DATE NOT NULL,
          `写入时间` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (`平台`,`店铺`,`平台规格ID`),
          KEY `idx_69` (`69码`),
          KEY `idx_spu` (`平台`,`平台商品ID`),
          KEY `idx_client` (`客户`,`平台`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='平台商品映射（旺店通导出），69码跨平台对齐的唯一来源；美团规格ID跨店铺重复，主键必须带店铺'
    """,
    "bi_sku_cost": """
        CREATE TABLE IF NOT EXISTS `bi_sku_cost` (
          `平台` VARCHAR(32) NOT NULL,
          `店铺` VARCHAR(64) NOT NULL,
          `客户` VARCHAR(64) NULL,
          `平台商品ID` VARCHAR(64) NULL,
          `平台规格ID` VARCHAR(64) NOT NULL,
          `平台商家编码` VARCHAR(64) NULL,
          `69码` VARCHAR(32) NULL,
          `盒数` INT NOT NULL DEFAULT 1,
          `匹配状态` VARCHAR(16) NULL COMMENT '自动匹配/人工匹配',
          `默认采购价` DECIMAL(18,4) NULL,
          `成本价` DECIMAL(18,4) NULL,
          `平台售价` DECIMAL(18,4) NULL,
          `零售价` DECIMAL(18,4) NULL,
          `重量` DECIMAL(18,4) NULL,
          `数据日期` DATE NOT NULL,
          `写入时间` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (`平台`,`店铺`,`平台规格ID`),
          KEY `idx_69` (`69码`),
          KEY `idx_client` (`客户`,`平台`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='平台商品成本（旺店通「按平台商品匹配」导出）；美团规格ID跨店铺重复，主键必须带店铺'
    """,
    "bi_stock": """
        CREATE TABLE IF NOT EXISTS `bi_stock` (
          `记录日期` DATE NOT NULL,
          `仓库` VARCHAR(64) NOT NULL,
          `货品编码` VARCHAR(64) NOT NULL,
          `货品名称` VARCHAR(255) NULL,
          `规格名称` VARCHAR(128) NULL,
          `商家编码` VARCHAR(64) NULL,
          `主条码` VARCHAR(64) NULL,
          `单位` VARCHAR(16) NULL,
          `零售价` DECIMAL(18,4) NULL,
          `可用库存` DECIMAL(18,2) NULL,
          `锁定量` DECIMAL(18,2) NULL,
          `正品库存` DECIMAL(18,2) NULL,
          `次品库存` DECIMAL(18,2) NULL,
          `待发货` DECIMAL(18,2) NULL,
          `待审核` DECIMAL(18,2) NULL,
          `待采购量` DECIMAL(18,2) NULL,
          `采购在途` DECIMAL(18,2) NULL,
          `可分配库存` DECIMAL(18,2) NULL,
          `成本价` DECIMAL(18,4) NULL,
          `总成本` DECIMAL(18,2) NULL,
          `近7天净销量` DECIMAL(18,2) NULL,
          `近30天净销量` DECIMAL(18,2) NULL,
          `近60天净销量` DECIMAL(18,2) NULL,
          `写入时间` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (`记录日期`,`仓库`,`货品编码`),
          KEY `idx_goods` (`货品编码`),
          KEY `idx_warehouse` (`仓库`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='库存时点快照（旺店通「库存查询」导出，无批号/效期）'
    """,
    "bi_region_ban": """
        CREATE TABLE IF NOT EXISTS `bi_region_ban` (
          `客户` VARCHAR(64) NOT NULL,
          `商品名称` VARCHAR(255) NOT NULL,
          `69码` VARCHAR(32) NULL COMMENT '按商品名称回填，可能为空',
          `省份` VARCHAR(32) NOT NULL,
          `原始文本` VARCHAR(512) NULL,
          `数据日期` DATE NOT NULL,
          `写入时间` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (`客户`,`商品名称`,`省份`),
          KEY `idx_69` (`69码`),
          KEY `idx_province` (`省份`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='禁售区域（商家提供，一行一省）'
    """,
    "bi_night_listing": """
        CREATE TABLE IF NOT EXISTS `bi_night_listing` (
          `客户` VARCHAR(64) NOT NULL,
          `平台` VARCHAR(32) NOT NULL,
          `商品名称` VARCHAR(255) NOT NULL,
          `69码` VARCHAR(32) NULL,
          `数据日期` DATE NOT NULL,
          `写入时间` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (`客户`,`平台`,`商品名称`),
          KEY `idx_69` (`69码`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='晚上上架药品清单（商家提供）'
    """,
    "bi_stock_expiry": """
        CREATE TABLE IF NOT EXISTS `bi_stock_expiry` (
          `记录日期` DATE NOT NULL,
          `仓库` VARCHAR(64) NOT NULL,
          `货品编码` VARCHAR(64) NOT NULL,
          `货品名称` VARCHAR(255) NULL,
          `规格名称` VARCHAR(128) NULL,
          `商家编码` VARCHAR(64) NULL,
          `库存数量` DECIMAL(18,2) NULL,
          `库存属性` VARCHAR(16) NULL COMMENT '正品/次品',
          `生产日期` DATE NULL,
          `保质期` VARCHAR(32) NULL,
          `有效期` DATE NULL,
          `距离到期天数` INT NULL COMMENT '旺店通原样，负数=已过期',
          `写入时间` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (`记录日期`,`仓库`,`货品编码`),
          KEY `idx_goods` (`货品编码`),
          KEY `idx_expiry` (`有效期`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='货品有效期（旺店通「有效期管理」导出，货品级、无批号）'
    """,
    "bi_stock_batch": """
        CREATE TABLE IF NOT EXISTS `bi_stock_batch` (
          `记录日期` DATE NOT NULL,
          `仓库` VARCHAR(64) NOT NULL,
          `货品编码` VARCHAR(64) NOT NULL,
          `批次` VARCHAR(64) NOT NULL,
          `货品名称` VARCHAR(255) NULL,
          `规格名称` VARCHAR(128) NULL,
          `商家编码` VARCHAR(64) NULL,
          `主条码` VARCHAR(64) NULL,
          `货品类型` VARCHAR(16) NULL,
          `库存数量` DECIMAL(18,2) NULL,
          `批次成本价` DECIMAL(18,4) NULL,
          `零售价` DECIMAL(18,4) NULL,
          `批准文号` VARCHAR(128) NULL,
          `生产厂家` VARCHAR(128) NULL,
          `写入时间` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (`记录日期`,`仓库`,`货品编码`,`批次`),
          KEY `idx_goods` (`货品编码`),
          KEY `idx_batch` (`批次`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='货品批次（旺店通「货品批次管理」导出，有批号、无有效期）'
    """,
    "bi_store_config": """
        CREATE TABLE IF NOT EXISTS `bi_store_config` (
          `客户` VARCHAR(64) NOT NULL,
          `平台` VARCHAR(16) NOT NULL,
          `是否启用` TINYINT NOT NULL DEFAULT 1 COMMENT '0=已停用，视图与巡检都不再计入',
          `备注` VARCHAR(255) NULL,
          `更新日期` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (`客户`,`平台`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='客户/平台启用状态：停用后不再巡检、不进看板'
    """,
}

# 已停用客户：不再巡检、不进看板。历史数据保留，只是不再报 STALE。
INACTIVE_STORES = [
    ("津南南无门诊", "京东", "2026-11-17 起南无不合作，停止采集"),
    ("津南南无门诊", "美团", "2026-11-17 起南无不合作，停止采集"),
    ("津南南无门诊", "天猫", "2026-11-17 起南无不合作，停止采集"),
    ("百恩堂", "京东", "2026-11-24 起百恩堂不合作，停止采集"),
    ("百恩堂", "美团", "2026-11-24 起百恩堂不合作，停止采集"),
    ("百恩堂", "天猫", "2026-11-24 起百恩堂不合作，停止采集"),
]
INACTIVE_WAREHOUSE_MARKERS = ("南无", "百恩堂")


def ensure_tables(conn) -> None:
    with conn.cursor() as cursor:
        for name, ddl in DDL.items():
            cursor.execute(ddl)
            print(f"  [表] {name} ready")
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema=DATABASE() AND table_name='bi_stock' AND column_name='零售价'
        """)
        if not cursor.fetchone()[0]:
            cursor.execute("ALTER TABLE `bi_stock` ADD COLUMN `零售价` DECIMAL(18,4) NULL AFTER `单位`")
    conn.commit()


# ---------------------------------------------------------------- 各数据集


def import_goods(conn, path: pathlib.Path, day: dt.date, client_of_store) -> None:
    _, rows = read_sheet(path)
    seen: dict[tuple[str, str], dict] = {}
    conflicts = 0
    bad_code = 0
    for row in rows:
        platform = platform_norm(row.get("平台"))
        spec = text(row.get("平台规格ID"), 64)
        spu = text(row.get("平台商品ID"), 64)
        if not platform or not spec:
            continue
        code69, boxes = split_code(row.get("平台商家编码"))
        if not code69:
            bad_code += 1
        # 美团等平台的「平台规格ID」在不同店铺间会重复，唯一键必须带店铺
        key = (platform, text(row.get("店铺"), 64) or "", spec)
        if key in seen:
            conflicts += 1
        seen[key] = {
            "平台": platform,
            "店铺": text(row.get("店铺"), 64) or "",
            "客户": client_of_store(text(row.get("店铺"), 64) or ""),
            "平台商品ID": spu or "",
            "平台规格ID": spec,
            "平台商品名称": text(row.get("平台商品名称"), 255),
            "平台规格名称": text(row.get("平台规格名称"), 255),
            "平台商家编码": text(row.get("平台商家编码"), 64),
            "69码": code69,
            "盒数": boxes,
            "平台售价": dec(row.get("平台售价")),
            "平台库存": integer(row.get("平台库存")),
            "售卖状态": text(row.get("售卖状态"), 16),
            "规格类型": text(row.get("规格类型"), 16),
            "货品名称": text(row.get("货品名称"), 255),
            "数据日期": day,
        }
    columns = list(next(iter(seen.values())).keys())
    payload = [tuple(item[column] for column in columns) for item in seen.values()]
    upsert(conn, "bi_sku_platform", columns, ["平台", "店铺", "平台规格ID"], payload)
    conn.commit()
    with_client = sum(1 for item in seen.values() if item["客户"])
    print(
        f"  [平台商品] {len(rows)} 行 -> {len(seen)} 个SKU，"
        f"已映射客户 {with_client}，编码无法解析 {bad_code}，重复SKU覆盖 {conflicts}"
    )


def import_match(conn, path: pathlib.Path, day: dt.date, client_of_store) -> None:
    _, rows = read_sheet(path)
    seen: dict[tuple[str, str], dict] = {}
    for row in rows:
        platform = platform_norm(row.get("平台类型"))
        spec = text(row.get("平台规格ID"), 64)
        if not platform or not spec:
            continue
        code69, boxes = split_code(row.get("平台商家编码"))
        seen[(platform, text(row.get("店铺名称"), 64) or "", spec)] = {
            "平台": platform,
            "店铺": text(row.get("店铺名称"), 64) or "",
            "客户": client_of_store(text(row.get("店铺名称"), 64) or ""),
            "平台商品ID": text(row.get("平台商品ID"), 64),
            "平台规格ID": spec,
            "平台商家编码": text(row.get("平台商家编码"), 64),
            "69码": code69,
            "盒数": boxes,
            "匹配状态": text(row.get("匹配状态"), 16),
            "默认采购价": dec(row.get("默认采购价")),
            "成本价": dec(row.get("成本价")),
            "平台售价": dec(row.get("平台售价")),
            "零售价": dec(row.get("零售价")),
            "重量": dec(row.get("重量")),
            "数据日期": day,
        }
    columns = list(next(iter(seen.values())).keys())
    payload = [tuple(item[column] for column in columns) for item in seen.values()]
    upsert(conn, "bi_sku_cost", columns, ["平台", "店铺", "平台规格ID"], payload)
    conn.commit()
    priced = sum(
        1 for item in seen.values()
        if item["默认采购价"] is not None and item["默认采购价"] != 0
    )
    print(f"  [平台成本] {len(rows)} 行 -> {len(seen)} 个SKU，采购价非零 {priced}")


def import_stock(conn, path: pathlib.Path, day: dt.date) -> None:
    _, rows = read_sheet(path)
    seen: dict[tuple[str, str], dict] = {}
    for row in rows:
        warehouse = text(row.get("仓库"), 64)
        code = text(row.get("货品编码"), 64)
        if not warehouse or not code:
            continue
        if any(marker in warehouse for marker in INACTIVE_WAREHOUSE_MARKERS):
            continue
        seen[(warehouse, code)] = {
            "记录日期": day,
            "仓库": warehouse,
            "货品编码": code,
            "货品名称": text(row.get("货品名称"), 255),
            "规格名称": text(row.get("规格名称"), 128),
            "商家编码": text(row.get("商家编码"), 64),
            "主条码": text(row.get("主条码"), 64),
            "单位": text(row.get("单位"), 16),
            "零售价": dec(row.get("零售价")),
            "可用库存": dec(row.get("可用库存")),
            "锁定量": dec(row.get("锁定量")),
            "正品库存": dec(row.get("正品库存")),
            "次品库存": dec(row.get("次品库存")),
            # 这四列原来没落库，导致「待确认补货」无数据
            "待发货": dec(row.get("待发货")),
            "待审核": dec(row.get("待审核")),
            "待采购量": dec(row.get("待采购量")),
            "采购在途": dec(row.get("采购在途")),
            "可分配库存": dec(row.get("可分配库存")),
            "成本价": dec(row.get("成本价")),
            "总成本": dec(row.get("总成本")),
            "近7天净销量": dec(row.get("近7天净销量")),
            "近30天净销量": dec(row.get("近30天净销量")),
            "近60天净销量": dec(row.get("近60天净销量")),
        }
    columns = list(next(iter(seen.values())).keys())
    payload = [tuple(item[column] for column in columns) for item in seen.values()]
    upsert(conn, "bi_stock", columns, ["记录日期", "仓库", "货品编码"], payload)
    conn.commit()
    warehouses = collections.Counter(item["仓库"] for item in seen.values())
    print(f"  [库存] {len(rows)} 行 -> {len(seen)} (仓库,编码)，仓库分布 {dict(warehouses)}")


def import_ban(conn, path: pathlib.Path, day: dt.date) -> None:
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheets = list(workbook.sheetnames)
    workbook.close()
    total = 0
    unknown_provinces = collections.Counter()
    for sheet in sheets:
        client = SHEET_TO_CLIENT.get(sheet)
        _, rows = read_sheet(path, sheet)
        if not client:
            print(f"  [禁售] 警告：sheet「{sheet}」无客户映射，跳过")
            continue
        seen: dict[tuple[str, str], dict] = {}
        for row in rows:
            name = text(row.get("商品名称") or row.get("药品名"), 255)
            raw = text(row.get("禁售区域"), 512)
            if not name or not raw:
                continue
            for province in re.split(r"[、,，;；/\s]+", raw):
                province = province.strip()
                if not province:
                    continue
                if province not in PROVINCES:
                    unknown_provinces[province] += 1
                seen[(name, province)] = {
                    "客户": client,
                    "商品名称": name,
                    "69码": None,
                    "省份": province,
                    "原始文本": raw,
                    "数据日期": day,
                }
        columns = list(next(iter(seen.values())).keys())
        payload = [tuple(item[column] for column in columns) for item in seen.values()]
        upsert(conn, "bi_region_ban", columns, ["客户", "商品名称", "省份"], payload)
        total += len(payload)
        print(f"  [禁售] {sheet} -> 客户 {client}，{len(rows)} 行商品 / {len(payload)} 条省份")
    conn.commit()
    if unknown_provinces:
        print(f"  [禁售] 警告：未识别的省份 {dict(unknown_provinces)}")
    print(f"  [禁售] 合计写入 {total} 条")


def parse_date(value) -> dt.date | None:
    result = text(value, 32)
    if not result:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(result, fmt).date()
        except ValueError:
            continue
    return None


def import_expiry(conn, path: pathlib.Path, day: dt.date) -> None:
    """有效期管理：货品级（无批号）。同一 (仓库,货品编码) 若有多行，保留到期日最早的。"""
    _, rows = read_sheet(path)
    seen: dict[tuple[str, str], dict] = {}
    expired_zero = 0
    for row in rows:
        warehouse = text(row.get("仓库"), 64)
        code = text(row.get("货品编码"), 64)
        if not warehouse or not code:
            continue
        if any(marker in warehouse for marker in INACTIVE_WAREHOUSE_MARKERS):
            continue
        expiry = parse_date(row.get("有效期"))
        days = integer(row.get("距离到期天数"))
        item = {
            "记录日期": day,
            "仓库": warehouse,
            "货品编码": code,
            "货品名称": text(row.get("货品名称"), 255),
            "规格名称": text(row.get("规格名称"), 128),
            "商家编码": text(row.get("商家编码"), 64),
            "库存数量": dec(row.get("库存数量")),
            "库存属性": text(row.get("库存属性"), 16),
            "生产日期": parse_date(row.get("生产日期")),
            "保质期": text(row.get("保质期"), 32),
            "有效期": expiry,
            "距离到期天数": days,
        }
        key = (warehouse, code)
        if key in seen and seen[key]["有效期"] and expiry and seen[key]["有效期"] <= expiry:
            continue
        seen[key] = item
        if expiry is None:
            expired_zero += 1
    columns = list(next(iter(seen.values())).keys())
    payload = [tuple(item[column] for column in columns) for item in seen.values()]
    upsert(conn, "bi_stock_expiry", columns, ["记录日期", "仓库", "货品编码"], payload)
    conn.commit()
    real = sum(1 for item in seen.values() if item["有效期"] is not None)
    print(f"  [有效期] {len(rows)} 行 -> {len(seen)} (仓库,编码)，有效期非空 {real}，为空 {expired_zero}")
    if expired_zero:
        print("  [有效期] 警告：部分行有效期为 0/空（旺店通保质期未维护，有效期可能等于生产日期）")


def import_batch(conn, path: pathlib.Path, day: dt.date) -> None:
    """货品批次管理：有批号、无有效期。"""
    _, rows = read_sheet(path)
    seen: dict[tuple[str, str, str], dict] = {}
    no_batch = 0
    for row in rows:
        warehouse = text(row.get("仓库"), 64)
        code = text(row.get("货品编码"), 64)
        batch = text(row.get("批次"), 64)
        if not warehouse or not code:
            continue
        if not batch:
            no_batch += 1
            batch = "无批次"
        seen[(warehouse, code, batch)] = {
            "记录日期": day,
            "仓库": warehouse,
            "货品编码": code,
            "批次": batch,
            "货品名称": text(row.get("货品名称"), 255),
            "规格名称": text(row.get("规格名称"), 128),
            "商家编码": text(row.get("商家编码"), 64),
            "主条码": text(row.get("主条码"), 64),
            "货品类型": text(row.get("货品类型"), 16),
            "库存数量": dec(row.get("库存数量")),
            "批次成本价": dec(row.get("批次成本价")),
            "零售价": dec(row.get("零售价")),
            "批准文号": text(row.get("批准文号"), 128),
            "生产厂家": text(row.get("生产厂家"), 128),
        }
    columns = list(next(iter(seen.values())).keys())
    payload = [tuple(item[column] for column in columns) for item in seen.values()]
    upsert(conn, "bi_stock_batch", columns, ["记录日期", "仓库", "货品编码", "批次"], payload)
    conn.commit()
    print(f"  [批次] {len(rows)} 行 -> {len(seen)} (仓库,编码,批次)，无批次号 {no_batch}")


def seed_store_config(conn) -> None:
    """写入停用客户标记（幂等，只动 INACTIVE_STORES 列出的组合）。"""
    if not INACTIVE_STORES:
        return
    sql = (
        "INSERT INTO `bi_store_config` (`客户`,`平台`,`是否启用`,`备注`) "
        "VALUES (%s,%s,0,%s) ON DUPLICATE KEY UPDATE `是否启用`=0,`备注`=VALUES(`备注`)"
    )
    with conn.cursor() as cursor:
        cursor.executemany(sql, INACTIVE_STORES)
    conn.commit()
    print(f"  [门店配置] 标记停用 {len(INACTIVE_STORES)} 个 (客户,平台)")


def import_night(conn, path: pathlib.Path, day: dt.date) -> None:
    workbook = load_workbook(path, read_only=True, data_only=True)
    sheets = list(workbook.sheetnames)
    workbook.close()
    total = 0
    for sheet in sheets:
        client = SHEET_TO_CLIENT.get(sheet)
        _, rows = read_sheet(path, sheet)
        if not client:
            print(f"  [晚上上架] 警告：sheet「{sheet}」无客户映射，跳过")
            continue
        seen: dict[tuple[str, str], dict] = {}
        for row in rows:
            for column, value in row.items():
                if column not in ("京东", "天猫", "美团"):
                    continue
                name = text(value, 255)
                if not name:
                    continue
                seen[(column, name)] = {
                    "客户": client,
                    "平台": column,
                    "商品名称": name,
                    "69码": None,
                    "数据日期": day,
                }
        columns = list(next(iter(seen.values())).keys())
        payload = [tuple(item[column] for column in columns) for item in seen.values()]
        upsert(conn, "bi_night_listing", columns, ["客户", "平台", "商品名称"], payload)
        total += len(payload)
        print(f"  [晚上上架] {sheet} -> 客户 {client}，{len(payload)} 条")
    conn.commit()
    print(f"  [晚上上架] 合计写入 {total} 条")


# ---------------------------------------------------------------- 入口


def fill_ban_codes(conn) -> None:
    """禁售区域按「客户 + 商品名称」回填 69 码。

    只接受唯一匹配：同一客户下同名商品若对应多个 69 码（组合装/多规格），
    一律不填，避免把禁售规则挂到错误的商品上。
    """
    sql = """
        UPDATE `bi_region_ban` b
        JOIN (
          SELECT `客户`, `平台商品名称`, MIN(`69码`) AS `code`
          FROM `bi_sku_platform`
          WHERE `69码` IS NOT NULL
          GROUP BY `客户`, `平台商品名称`
          HAVING COUNT(DISTINCT `69码`) = 1
        ) m ON m.`客户` = b.`客户` AND m.`平台商品名称` = b.`商品名称`
        SET b.`69码` = m.`code`
        WHERE b.`69码` IS NULL
    """
    with conn.cursor() as cursor:
        cursor.execute(sql)
        filled = cursor.rowcount
        cursor.execute("SELECT COUNT(*) FROM `bi_region_ban` WHERE `69码` IS NULL")
        remain = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(DISTINCT `客户`) FROM `bi_region_ban` WHERE `69码` IS NULL")
        clients = cursor.fetchone()[0]
    conn.commit()
    print(f"  [禁售] 回填 69 码 {filled} 条；仍为空 {remain} 条（涉及 {clients} 个客户）")


def client_of_store(store: str) -> str | None:
    return STORE_TO_CLIENT.get(store)


def report_unmapped_stores() -> None:
    mapped = set(STORE_TO_CLIENT)
    unknown: set[str] = set()
    for path in find_files("goods"):
        try:
            _, rows = read_sheet(path)
        except Exception:  # noqa: BLE001 - 单份文件坏了不影响其它客户
            continue
        unknown |= {text(row.get("店铺"), 64) for row in rows}
    unknown = {name for name in unknown if name} - mapped
    if unknown:
        print(f"  [警告] 未配置「店铺→客户」映射：{sorted(unknown)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="旺店通导出 → adjust_price 基础表")
    parser.add_argument("--only", choices=sorted(FILE_PATTERNS), nargs="*", help="只导入指定数据集")
    parser.add_argument("--date", help="数据日期 YYYY-MM-DD，默认今天")
    args = parser.parse_args()

    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    keys = args.only or ["goods", "match", "stock", "batch", "expiry", "ban", "night"]

    conn = connect()
    try:
        print(f"数据日期 {day}")
        ensure_tables(conn)
        seed_store_config(conn)
        if "goods" in keys:
            report_unmapped_stores()
        for key in keys:
            for path in find_files(key):
                # 每份导出按自己文件名里的日期落库，避免把上周的表格记成今天
                file_day = file_date(path, day)
                print(f"读取 {path.name}（数据日期 {file_day}）")
                if key == "goods":
                    import_goods(conn, path, file_day, client_of_store)
                elif key == "match":
                    import_match(conn, path, file_day, client_of_store)
                elif key == "stock":
                    import_stock(conn, path, file_day)
                elif key == "batch":
                    import_batch(conn, path, file_day)
                elif key == "expiry":
                    import_expiry(conn, path, file_day)
                elif key == "ban":
                    import_ban(conn, path, file_day)
                elif key == "night":
                    import_night(conn, path, file_day)
        if "ban" in keys and "goods" in keys:
            fill_ban_codes(conn)
    finally:
        conn.close()
    return 0


# EC_DW_RESET_PAUSED_20261126
if __name__ == '__main__':
    raise SystemExit('ec_dw has been cleared; writer paused pending new schema')

if __name__ == "__main__":
    sys.exit(main())
