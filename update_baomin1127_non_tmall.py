#!/usr/bin/env python3
"""只刷新保民1127.xlsx 的非天猫明细：美团、京东小官补、指数交易与汇总。"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import defaultdict
from datetime import datetime

import pymysql
from openpyxl import load_workbook
from openpyxl.styles import Alignment

sys.path.insert(0, '/opt/subsidy')
from apply_baomin_formulas import (
    HEADER_FILL, HEADER_FONT, MONEY_FMT, PCT_FMT,
    as_float, build_source_map, choose_source_row, ensure_col,
    load_redline, load_wdt_costs, match, profit_formula,
    red_formula, resolve_item, style_profit,
)
from update_cost_from_wdt import pick

DEFAULT_TARGET = pathlib.Path('/tmp/codebuddy-chat-logs/保民1127.xlsx')
DEFAULT_RED = pathlib.Path('/tmp/codebuddy-chat-logs/成本&红线表.xlsx')
CONFIG = pathlib.Path('/opt/subsidy/config.json')
MT_DAY = '2026-11-27'
JD_DAY = '2026-11-27'
INDEX_DAY = '2026-11-27'


def parse_dt(v):
    if v is None or v == '':
        return None
    if isinstance(v, datetime):
        return v
    s = str(v)
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return s


def brand_of(name):
    m = re.match(r'^\[([^\]]+)\]', str(name or ''))
    return m.group(1) if m else ''


def reset_sheet(wb, name, headers, index):
    if name in wb.sheetnames:
        old_index = wb.sheetnames.index(name)
        del wb[name]
    else:
        old_index = index
    ws = wb.create_sheet(name, old_index)
    ws.append(headers)
    for c in ws[1]:
        c.fill, c.font, c.alignment = HEADER_FILL, HEADER_FONT, Alignment(horizontal='center')
    return ws


def connect(database):
    cfg = json.loads(CONFIG.read_text(encoding='utf-8'))['mysql']
    return pymysql.connect(
        host=cfg['host'], port=int(cfg['port']), user=cfg['user'],
        password=cfg['password'], database=database,
        charset=cfg.get('charset', 'utf8mb4'),
    )


def refresh_mt(wb, exact, by_core, by_core_all, by_code, by_name):
    headers = [
        '数据日期', '店铺', 'poi_id', 'poi_name', 'upc', 'sku_id', 'sku_name',
        '类别', '活动类型', '活动名称', '补贴状态', '原价', '当前售价', '差额',
        '补贴门槛', '补贴后价', '单件补贴', '商家承担', '门槛盒数', '补贴范围',
        '活动开始', '活动结束', '活动时间段', '活动周期', '未补贴原因',
        '我的价格', '至少再降', '预估补贴', '建议定价', 'reject_time',
        '平台售价', '售价来源', '固定成本价', '成本来源',
        '美团调后售价', '美团最低售价', '美团活动门槛价', '是否可尝试报名', '实际利润点',
    ]
    ws = reset_sheet(wb, '保民美团补贴活动', headers, 0)
    conn = connect('ec_dw')
    cur = conn.cursor()
    cur.execute(
        "SELECT 来源原始字段 FROM subsidy WHERE 客户='保民' AND 平台='美团' "
        "AND 记录类型='美团补贴' AND 修改日期=%s ORDER BY 商品名称", (MT_DAY,)
    )
    raw_rows = [json.loads(r[0]) for r in cur.fetchall()]
    conn.close()

    n = 0
    item_map = defaultdict(list)
    for raw in raw_rows:
        name = raw.get('sku_name')
        upc = raw.get('upc')
        sku_id = raw.get('sku_id')
        status = (raw.get('补贴状态') or
                  ('未补贴' if raw.get('未补贴原因') else
                   ('未建品' if raw.get('建议定价') else None)))
        cur_price = as_float(raw.get('当前售价'))
        sub_price = as_float(raw.get('补贴后价'))
        original = as_float(raw.get('原价'))
        diff = (cur_price - sub_price) if cur_price is not None and sub_price is not None else (
            (original - cur_price) if original is not None and cur_price is not None else None)
        platform_price = cur_price if cur_price is not None else as_float(raw.get('我的价格'))
        threshold = as_float(raw.get('补贴门槛'))
        if threshold is None:
            threshold = as_float(raw.get('建议定价'))
        adjusted = (platform_price - 3 if platform_price is not None and platform_price > 29
                    else platform_price)

        item, rec = resolve_item(
            [upc, sku_id], name, None, exact, by_core, by_core_all, by_code,
        )
        cost = as_float(rec['cost']) if rec and rec.get('cost') is not None else None
        source = None
        if cost is not None and cost > 0:
            source = '旺店通库存成本'
        elif item and item.get('cost') is not None:
            cost = as_float(item['cost'])
            if cost is not None and cost > 0:
                source = '成本红线表'
        if cost is None or cost <= 0:
            cost, source = None, None

        red_num = None
        if item and cost is not None and cost > 0:
            boxes = as_float(raw.get('门槛盒数')) or 1
            red_num = (cost * boxes + item['freight_mt']) / 0.91
        join_flag = None
        if red_num is not None and threshold is not None:
            join_flag = '可参加' if threshold >= red_num else '不参加'

        values = [
            parse_dt(raw.get('数据日期')), raw.get('店铺'), raw.get('poi_id'),
            raw.get('poi_name'), upc, sku_id, name, raw.get('类别'),
            raw.get('活动类型'), raw.get('活动名称'), status,
            original, cur_price, diff, raw.get('补贴门槛'), raw.get('补贴后价'),
            raw.get('单件补贴'), raw.get('商家承担'), raw.get('门槛盒数'),
            raw.get('补贴范围'), parse_dt(raw.get('活动开始')),
            parse_dt(raw.get('活动结束')), raw.get('活动时间段'),
            raw.get('活动周期'), raw.get('未补贴原因'), raw.get('我的价格'),
            raw.get('至少再降'), raw.get('预估补贴'), raw.get('建议定价'),
            parse_dt(raw.get('reject_time')),
            platform_price, '采集' if platform_price is not None else None,
            cost, source, adjusted,
        ]
        r = ws.max_row + 1
        ws.append(values + [None, threshold, join_flag, None])
        if item and cost is not None and cost > 0:
            n += 1
            item_map[item['row']].append(r)
            ws.cell(r, 33).number_format = MONEY_FMT
            ws.cell(r, 36).value = red_formula(
                f'AG{r}', f'S{r}', 0.91, item['freight_mt']
            )
            ws.cell(r, 39).value = profit_formula(
                f'AK{r}', f'S{r}', f'AJ{r}', 0.91, item['mt_fee']
            )
    style_profit(ws, 39)
    ws.freeze_panes = 'A2'
    return ws, item_map, len(raw_rows), n


def refresh_jd(wb, exact, by_core, by_core_all, by_code, by_name):
    headers = [
        '记录日期', '店铺', '策略ID', '商品upc', '商家SKU', '品牌', '通用名称',
        '单盒规格', '活动盒数', '京东平台售价', '单盒售价', '美团平台售价',
        '售价来源', '固定成本价', '成本来源', '实际入围最高价', '实际入围最低价',
        '京东红线价', '是否可尝试报名', '京东利润点',
    ]
    ws = reset_sheet(wb, '保民京东小官补', headers, 1)
    conn = connect('adjust_price')
    cur = conn.cursor()
    cur.execute("""
        SELECT s.记录日期, s.策略ID, s.商品upc, s.商家SKU, s.品牌, s.通用名称,
               s.单盒规格, s.活动盒数, s.京东价,
               c.实际入围最高价, c.实际入围最低价
        FROM adjust_price.jd_subsidy_sku s
        LEFT JOIN adjust_price.jd_subsidy_catalog c ON s.策略ID=c.策略ID
        WHERE s.记录日期=(SELECT MAX(记录日期) FROM adjust_price.jd_subsidy_sku)
        ORDER BY s.策略ID
    """)
    rows = cur.fetchall()
    conn.close()

    item_map = defaultdict(list)
    n = 0
    for row in rows:
        (day, strategy, upc, sku, brand, name, spec, boxes, jd_price, high, low) = row
        single = (as_float(jd_price) / boxes) if as_float(jd_price) is not None and boxes else None
        item, rec = resolve_item(
            [upc, sku], name, spec, exact, by_core, by_core_all, by_code,
        )
        cost = as_float(rec['cost']) if rec and rec.get('cost') is not None else None
        source = None
        if cost is not None and cost > 0:
            source = '旺店通库存成本'
        elif item and item.get('cost') is not None:
            cost = as_float(item['cost'])
            if cost is not None and cost > 0:
                source = '成本红线表'
        if cost is None or cost <= 0:
            cost, source = None, None

        red_num = None
        if item and cost is not None and cost > 0:
            red_num = (cost * boxes + item['freight_jd']) / 0.84
        price_num = as_float(jd_price)
        join_flag = None
        if red_num is not None and price_num is not None:
            join_flag = '可参加' if price_num >= red_num else '不参加'

        ws.append([
            day, '保民', strategy, upc, sku, brand, name, spec, boxes,
            jd_price, single, None, '京麦平台', cost, source, high, low,
            None, join_flag, None,
        ])
        r = ws.max_row
        if item and cost is not None and cost > 0:
            n += 1
            item_map[item['row']].append(r)
            ws.cell(r, 14).number_format = MONEY_FMT
            ws.cell(r, 18).value = red_formula(f'N{r}', f'I{r}', 0.84, item['freight_jd'])
            ws.cell(r, 20).value = profit_formula(
                f'K{r}', f'I{r}', f'R{r}', 0.84, item['jd_fee']
            )
    style_profit(ws, 20)
    ws.freeze_panes = 'A2'
    return ws, item_map, len(rows), n


def refresh_index(wb, exact, by_core, by_core_all, by_code, by_name):
    headers = [
        '商品编码', '商品名称', '本店品牌', '在售SKU数', '在售最低价', '商品状态',
        '是否可尝试报名', '匹配指数条目数', '指数SKU名称', '指数标准品名',
        '指数通用名', '指数规格', '指数药品ID', '指数SKUID编号', '二级类目',
        '匹配方式', '在售销售属性', '京东红线价', '固定成本价',
    ]
    ws = reset_sheet(wb, '保民指数交易匹配', headers, 2)
    conn = connect('adjust_price')
    cur = conn.cursor()
    cur.execute(
        "SELECT skuId, skuName, specAttrs, indexPrice, salePrice, raw_json "
        "FROM jd_index_joinable_gap_daily WHERE 记录日期=%s AND 店铺='保民' "
        "ORDER BY skuName, skuId", (INDEX_DAY,)
    )
    rows = cur.fetchall()
    conn.close()

    grouped = {}
    for sku_id, sku_name, spec, index_price, sale_price, raw in rows:
        meta = json.loads(raw or '{}')
        code = str(meta.get('商品编码') or sku_id)
        g = grouped.setdefault(code, {
            'name': sku_name, 'status': meta.get('商品状态'),
            'specs': [], 'prices': [],
        })
        if spec:
            g['specs'].append(str(spec))
        price = as_float(sale_price) or as_float(index_price)
        if price is not None:
            g['prices'].append(price)

    n = 0
    for code, g in grouped.items():
        name = g['name']
        spec = ' | '.join(g['specs'])
        low = min(g['prices']) if g['prices'] else None
        item, rec = resolve_item(
            [code], name, spec, exact, by_core, by_core_all, by_code,
        )
        cost = as_float(rec['cost']) if rec and rec.get('cost') is not None else None
        source = None
        if cost is not None and cost > 0:
            source = '旺店通库存成本'
        elif item and item.get('cost') is not None:
            cost = as_float(item['cost'])
            if cost is not None and cost > 0:
                source = '成本红线表'
        if cost is None or cost <= 0:
            cost, source = None, None
        red_num = None
        if item and cost is not None and cost > 0:
            red_num = (cost * 1 + item['freight_jd']) / 0.84
        join_flag = None
        if red_num is not None and low is not None:
            join_flag = '可参加' if low >= red_num else '不参加'
        ws.append([
            code, name, brand_of(name), len(g['specs']), low, g['status'],
            join_flag, 0, None, None, None, None, None, None, None,
            '差集-未参加', spec, None, cost,
        ])
        r = ws.max_row
        if item and cost is not None and cost > 0:
            n += 1
            ws.cell(r, 18).value = red_formula(f'S{r}', '1', 0.84, item['freight_jd'])
            ws.cell(r, 19).number_format = MONEY_FMT
    ws.freeze_panes = 'A2'
    return ws, len(grouped), n


def refresh_summary(wb, mt_ws, jd_ws, exact, by_core, by_core_all, by_code, by_name):
    ws = wb['三平台活动汇总']
    tm_ws = wb['天猫在售商品可报名匹配']
    mt_map = build_source_map(mt_ws, 7, None, exact, by_core, by_core_all, [5, 6], by_code)
    jd_map = build_source_map(jd_ws, 7, 8, exact, by_core, by_core_all, [4, 5], by_code)
    tm_map = build_source_map(tm_ws, 1, None, exact, by_core, by_core_all)

    mt_price = lambda r: as_float(mt_ws.cell(r, 37).value)
    jd_price = lambda r: as_float(jd_ws.cell(r, 11).value)
    tm_price = lambda r: (
        (lambda q, p: q / p if q is not None and p else None)(
            as_float(tm_ws.cell(r, 17).value), as_float(tm_ws.cell(r, 16).value)
        )
    )

    n = 0
    for r in range(2, ws.max_row + 1):
        item = match(ws.cell(r, 1).value, ws.cell(r, 3).value, exact, by_core, by_core_all)
        if not item:
            continue
        item_row = item.get('row')
        rec = pick([], ws.cell(r, 1).value, ws.cell(r, 3).value, by_code, by_name)
        cost = as_float(rec['cost']) if rec and rec.get('cost') is not None else None
        if cost is None or cost <= 0:
            cost = as_float(item['cost'])
        if cost is None or cost <= 0:
            ws.cell(r, 4).value = None
            ws.cell(r, 5).value = None
            continue
        ws.cell(r, 4).value = cost
        ws.cell(r, 4).number_format = MONEY_FMT
        ws.cell(r, 5).value = '旺店通库存成本' if rec else '成本红线表'
        n += 1

        for label, source_map, source_ws, price_fn, cfg in [
            ('mt', mt_map, mt_ws, mt_price, {'has': 7, 'price': 8, 'red': 9, 'profit': 21,
                                             'factor': 0.91, 'freight': 'freight_mt',
                                             'src_profit': 39}),
            ('jd', jd_map, jd_ws, jd_price, {'has': 11, 'price': 12, 'red': 14, 'profit': 22,
                                             'factor': 0.84, 'freight': 'freight_jd',
                                             'src_profit': 20}),
        ]:
            candidates = source_map.get(item_row, [])
            src_row = choose_source_row(
                candidates, as_float(ws.cell(r, cfg['price']).value), price_fn
            )
            if src_row:
                ws.cell(r, cfg['has']).value = '有'
                ws.cell(r, cfg['price']).value = price_fn(src_row)
                ws.cell(r, cfg['red']).value = red_formula(
                    f'D{r}', '1', cfg['factor'], item[cfg['freight']]
                )
                ws.cell(r, cfg['profit']).value = (
                    f"='{source_ws.title}'!{chr(64 + cfg['src_profit'])}{src_row}"
                )
            else:
                ws.cell(r, cfg['has']).value = '无'
                for c in (cfg['price'], cfg['red'], cfg['profit']):
                    ws.cell(r, c).value = None
    return n


def refresh_notes(wb, mt_count, mt_formula, jd_count, jd_formula, idx_count, idx_formula, summary_count):
    if '版本说明' in wb.sheetnames:
        del wb['版本说明']
    ws = wb.create_sheet('版本说明', len(wb.sheetnames))
    rows = [
        ['项目', '内容', '备注'],
        ['生成日期', '2026-11-27', '按本次要求生成'],
        ['美团数据', MT_DAY, f'{mt_count}行，其中{mt_formula}行已匹配红线并写公式'],
        ['京东小官补', JD_DAY, f'{jd_count}行，其中{jd_formula}行已匹配红线并写公式'],
        ['京东指数交易', INDEX_DAY, f'{idx_count}个商品，其中{idx_formula}个已匹配红线并写公式'],
        ['天猫', '2026-11-26', '本次保持不变'],
        ['旺店通成本', '库存查询20261126.xlsx', '旺店通自动抓取尚未配置，沿用昨天版本'],
        ['三平台汇总', '2026-11-27', f'{summary_count}行更新成本与美团/京东口径；天猫列保持不变'],
    ]
    for row in rows:
        ws.append(row)
    for c in ws[1]:
        c.fill, c.font, c.alignment = HEADER_FILL, HEADER_FONT, Alignment(horizontal='center')
    ws.column_dimensions['A'].width = 20
    ws.column_dimensions['B'].width = 24
    ws.column_dimensions['C'].width = 72


def apply(target: pathlib.Path, redline: pathlib.Path):
    exact, by_core, by_core_all = load_redline(redline)
    by_code, by_name = load_wdt_costs()
    wb = load_workbook(target)
    mt_ws, mt_map, mt_count, mt_formula = refresh_mt(
        wb, exact, by_core, by_core_all, by_code, by_name
    )
    jd_ws, jd_map, jd_count, jd_formula = refresh_jd(
        wb, exact, by_core, by_core_all, by_code, by_name
    )
    idx_ws, idx_count, idx_formula = refresh_index(
        wb, exact, by_core, by_core_all, by_code, by_name
    )
    summary_count = refresh_summary(
        wb, mt_ws, jd_ws, exact, by_core, by_core_all, by_code, by_name
    )
    refresh_notes(wb, mt_count, mt_formula, jd_count, jd_formula,
                  idx_count, idx_formula, summary_count)
    wb.save(target)
    return {
        '美团': {'rows': mt_count, 'formula': mt_formula},
        '京东小官补': {'rows': jd_count, 'formula': jd_formula},
        '京东指数交易': {'rows': idx_count, 'formula': idx_formula},
        '三平台汇总': summary_count,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', type=pathlib.Path, default=DEFAULT_TARGET)
    ap.add_argument('--redline', type=pathlib.Path, default=DEFAULT_RED)
    args = ap.parse_args()
    print(apply(args.target, args.redline))


if __name__ == '__main__':
    main()
