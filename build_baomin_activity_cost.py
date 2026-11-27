#!/usr/bin/env python3
"""Build Baomin-only activity cost workbook using latest platform snapshots."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

import pymysql
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

BASE = pathlib.Path('/opt/subsidy')
sys.path.insert(0, str(BASE))
from build_jd_subsidy_sku import canonical_spec, normalize_brand, normalize_text, spec_score  # noqa: E402

DEFAULT_COST = BASE / 'inbox' / '保民医院_成本价对比与取低_20261113.xlsx'
DEFAULT_MT = BASE / 'exports' / '美团补贴_按店铺_最新_20261124.xlsx'
DEFAULT_CONFIG = BASE / 'config.json'
DEFAULT_OUTPUT = BASE / 'exports' / '保民_活动成本表_20261124.xlsx'

HEADER_FILL = PatternFill('solid', fgColor='305496')
NEW_FILL = PatternFill('solid', fgColor='FFF2CC')
HEADER_FONT = Font(bold=True, color='FFFFFF')
MONEY_FMT = '0.00'
JOIN_FILL = PatternFill('solid', fgColor='C6EFCE')   # 参与中 - 绿
JOIN_FONT = Font(bold=True, color='006100')
QUIT_FILL = PatternFill('solid', fgColor='FFC7CE')   # 未参与 - 红
QUIT_FONT = Font(color='9C0006')


def norm(v):
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def amount(v):
    if v in (None, ''):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def connect(config_path):
    cfg = json.loads(pathlib.Path(config_path).read_text(encoding='utf-8'))['mysql']
    return pymysql.connect(
        host=cfg['host'], port=int(cfg['port']), user=cfg['user'], password=cfg['password'],
        database=cfg['database'], charset=cfg.get('charset', 'utf8mb4'),
        init_command="SET time_zone='+08:00'",
    )


def style_header(ws, row=1, fill=HEADER_FILL, font=HEADER_FONT):
    for c in ws[row]:
        c.fill = fill
        c.font = font
        c.alignment = Alignment(horizontal='center', vertical='center')


def load_registration_cost(path):
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb['成本价对比表']
    headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    idx = {h: i for i, h in enumerate(headers)}
    rows = []
    by_title = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        title = norm(r[idx['商品标题']])
        if not title:
            continue
        rec = {h: r[idx[h]] for h in headers}
        rows.append(rec)
        by_title[normalize_text(title)] = amount(r[idx['新成本价·取低（元）']])
    index_ws = wb['Sheet2']
    idx_headers = [c.value for c in next(index_ws.iter_rows(min_row=1, max_row=1))]
    index_rows = [dict(zip(idx_headers, r)) for r in index_ws.iter_rows(min_row=2, values_only=True) if any(x not in (None, '') for x in r)]
    wb.close()
    return rows, by_title, index_rows


def load_stock_cost(conn, day):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 货品编码, 商家编码, 主条码, 成本价
            FROM ec_dw.bi_stock
            WHERE 记录日期=%s AND 仓库='保民医院' AND 成本价 IS NOT NULL AND 成本价>0
        """, (day,))
        by_key = {}
        for code, seller, barcode, cost in cur.fetchall():
            value = amount(cost)
            for key in (code, seller, barcode):
                if key:
                    by_key[norm(key)] = value
        return by_key


def load_mt_price_map(conn, day):
    by_upc = {}
    with conn.cursor() as cur:
        for table, col in (
            ('mt_subsidy_subsidized_daily', '当前售价'),
            ('mt_subsidy_unsubsidized_daily', '我的价格'),
            ('mt_subsidy_unlisted_daily', '建议定价'),
        ):
            cur.execute(f"SELECT upc, `{col}` FROM `{table}` WHERE 店铺='保民' AND 数据日期=%s", (day,))
            for upc, price in cur.fetchall():
                if upc and price not in (None, ''):
                    by_upc.setdefault(norm(upc), (amount(price), f'美团平台/{col}'))
    return by_upc


def find_registration_cost_by_parts(registration_rows, brand, generic, spec):
    b = normalize_brand(brand)
    g = normalize_text(generic)
    s = normalize_text(spec)
    for rec in registration_rows:
        title = normalize_text(rec.get('商品标题'))
        if not title:
            continue
        if b and b not in title:
            continue
        if g and g not in title:
            continue
        if s and canonical_spec(s) not in canonical_spec(title) and spec_score(spec, title) == 0:
            continue
        value = amount(rec.get('新成本价·取低（元）'))
        if value is not None:
            return value
    return None


def add_price_formulas(ws, row, price_col, cost_col, fixed_col, source_col, threshold_col=None, compare_col=None):
    p = get_column_letter(price_col)
    e = get_column_letter(fixed_col)
    ws.cell(row, cost_col, f'=IF({p}{row}="","",IF({p}{row}>29,{p}{row}-3,{p}{row}))')
    adj = get_column_letter(cost_col)
    ws.cell(row, adj + '0') if False else None
    return adj


def build_mt_sheet(wb, mt_path, registration_rows, by_title, stock_cost):
    wb2 = load_workbook(mt_path, read_only=True, data_only=True)
    src = wb2['保民']
    headers = [c.value for c in next(src.iter_rows(min_row=1, max_row=1))]
    idx = {h: i + 1 for i, h in enumerate(headers)}
    cur_pos = idx['当前售价'] - 1
    my_pos = idx['我的价格'] - 1 if '我的价格' in idx else None
    th_pos = idx['补贴门槛'] - 1 if '补贴门槛' in idx else None
    out_headers = list(headers)
    out_headers.insert(cur_pos + 1, '差额')
    new_headers = [
        '平台售价', '售价来源', '固定成本价', '成本来源', '美团调后售价', '美团倒推成本',
        '最低成本价', '美团最低售价', '美团活动门槛价', '美团活动参与判断',
    ]
    ws = wb.create_sheet('保民美团补贴活动')
    ws.append(out_headers + new_headers)
    start = len(out_headers) + 1
    cols = {name: start + i for i, name in enumerate(new_headers)}

    for r in src.iter_rows(min_row=2, values_only=True):
        if not any(x not in (None, '') for x in r):
            continue
        price = None
        source = ''
        for field in ('当前售价', '我的价格', '建议定价'):
            if field in idx and r[idx[field]-1] not in (None, ''):
                price = amount(r[idx[field]-1])
                source = f'美团平台/{field}'
                break
        row = list(r)
        cur = row[cur_pos]
        if cur in (None, '') and my_pos is not None and row[my_pos] not in (None, ''):
            cur = row[my_pos]
        row[cur_pos] = cur
        gap = None
        if th_pos is not None and row[th_pos] not in (None, ''):
            gap = amount(row[th_pos])
        row.insert(cur_pos + 1, gap)
        row += [''] * len(new_headers)
        row[cols['平台售价']-1] = price
        row[cols['售价来源']-1] = source
        title_key = normalize_text(r[idx['sku_name']-1])
        fixed = by_title.get(title_key)
        cost_source = '报名价格表' if fixed is not None else ''
        if fixed is None:
            upc = norm(r[idx['upc']-1])
            fixed = stock_cost.get(upc)
            cost_source = '旺店通库存成本' if fixed is not None else '未匹配有效成本'
        row[cols['固定成本价']-1] = fixed
        row[cols['成本来源']-1] = cost_source
        row[cols['美团活动门槛价']-1] = amount(r[idx['补贴门槛']-1]) if '补贴门槛' in idx else None
        ws.append(row)
        n = ws.max_row
        p = get_column_letter(cols['平台售价'])
        f = get_column_letter(cols['固定成本价'])
        a = get_column_letter(cols['美团调后售价'])
        i = get_column_letter(cols['美团倒推成本'])
        l = get_column_letter(cols['最低成本价'])
        t = get_column_letter(cols['美团活动门槛价'])
        m = get_column_letter(cols['美团最低售价'])
        ws.cell(n, cols['美团调后售价'], f'=IF({p}{n}="","",IF({p}{n}>29,{p}{n}-3,{p}{n}))')
        ws.cell(n, cols['美团倒推成本'], f'=IF({a}{n}="","",{a}{n}*0.92)')
        ws.cell(n, cols['最低成本价'], f'=IF(AND({i}{n}="",{f}{n}=""),"",IF({i}{n}="",{f}{n},IF({f}{n}="",{i}{n},MIN({i}{n},{f}{n}))))')
        ws.cell(n, cols['美团最低售价'], f'=IF({l}{n}="","",IF({l}{n}>27.5,({l}{n}+3)/0.92,{l}{n}/0.92))')
        ws.cell(n, cols['美团活动参与判断'], f'=IF(OR({t}{n}="",{m}{n}=""),"",IF({t}{n}>={m}{n},"可参加","不参加"))')
        # 差额 = 当前售价 - 补贴门槛；两者有值才算，补贴中(售价=门槛)显示 0.00 不留空
        mcol = get_column_letter(cur_pos + 1)
        ocol = get_column_letter(cur_pos + 3)
        ws.cell(n, cur_pos + 2, f'=IF(OR({mcol}{n}="",{ocol}{n}=""),"",{mcol}{n}-{ocol}{n})')
        ws.cell(n, cur_pos + 2).number_format = MONEY_FMT
        for name in ('平台售价', '固定成本价', '美团调后售价', '美团倒推成本', '最低成本价', '美团最低售价', '美团活动门槛价'):
            ws.cell(n, cols[name]).number_format = MONEY_FMT
    wb2.close()
    style_header(ws, 1, NEW_FILL, Font(bold=True))
    for c in range(start, ws.max_column + 1):
        ws.cell(1, c).fill = NEW_FILL
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
    return ws.max_row - 1


def build_jd_sheet(wb, conn, registration_rows, mt_price_map, stock_cost, day):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 记录日期,策略ID,商品upc,商家SKU,品牌,通用名称,单盒规格,活动盒数,京东价
            FROM jd_136186144.jd_subsidy_sku WHERE 记录日期=%s ORDER BY 策略ID
        """, (day,))
        matches = cur.fetchall()
        cur.execute("""
            SELECT 策略ID,价格下限,价格上限,实际入围最低价,实际入围最高价,同步日期
            FROM adjust_price.jd_subsidy_catalog WHERE 同步日期=(SELECT MAX(同步日期) FROM adjust_price.jd_subsidy_catalog)
        """)
        catalog = {norm(r[0]): r for r in cur.fetchall()}

    headers = [
        '记录日期', '店铺', '策略ID', '商品upc', '商家SKU', '品牌', '通用名称', '单盒规格', '活动盒数',
        '京东平台售价', '单盒售价', '美团平台售价', '售价来源', '固定成本价', '成本来源', '美团调后售价',
        '美团倒推成本', '最低成本价', '京东最低售价', '实际入围最高价', '实际入围最低价', '京东活动参与判断',
    ]
    ws = wb.create_sheet('保民京东小官补')
    ws.append(headers)
    cols = {h: i+1 for i, h in enumerate(headers)}
    for rec in matches:
        day_v, sid, upc, seller, brand, generic, spec, boxes, jd_price = rec
        jd_price = amount(jd_price)
        mt = mt_price_map.get(norm(upc))
        mt_price, mt_source = mt if mt else (None, '')
        platform_price = mt_price if mt_price is not None else jd_price
        source = mt_source if mt_price is not None else '京东平台/京东价'
        fixed = stock_cost.get(norm(upc))
        cost_source = '旺店通库存成本' if fixed is not None else ''
        if fixed is None:
            fixed = find_registration_cost_by_parts(registration_rows, brand, generic, spec)
            cost_source = '报名价格表' if fixed is not None else '未匹配有效成本'
        cat = catalog.get(norm(sid))
        actual_high = amount(cat[4]) if cat else None
        actual_low = amount(cat[3]) if cat else None
        unit_price = jd_price / int(boxes) if jd_price is not None and boxes and int(boxes) > 0 else None
        ws.append([
            day_v, '保民', norm(sid), norm(upc), norm(seller), norm(brand), norm(generic), norm(spec),
            boxes, jd_price, unit_price, mt_price, source, fixed, cost_source,
        ] + [''] * 7)
        n = ws.max_row
        p, f = get_column_letter(cols['美团平台售价']), get_column_letter(cols['固定成本价'])
        a, i, l = get_column_letter(cols['美团调后售价']), get_column_letter(cols['美团倒推成本']), get_column_letter(cols['最低成本价'])
        j, t = get_column_letter(cols['京东最低售价']), get_column_letter(cols['实际入围最高价'])
        ws.cell(n, cols['美团调后售价'], f'=IF({p}{n}="","",IF({p}{n}>29,{p}{n}-3,{p}{n}))')
        ws.cell(n, cols['美团倒推成本'], f'=IF({a}{n}="","",{a}{n}*0.92)')
        ws.cell(n, cols['最低成本价'], f'=IF(AND({i}{n}="",{f}{n}=""),"",IF({i}{n}="",{f}{n},IF({f}{n}="",{i}{n},MIN({i}{n},{f}{n}))))')
        ws.cell(n, cols['京东最低售价'], f'=IF({l}{n}="","",({l}{n}+3)/0.88)')
        ws.cell(n, cols['实际入围最高价'], actual_high)
        ws.cell(n, cols['实际入围最低价'], actual_low)
        ws.cell(n, cols['京东活动参与判断'], f'=IF(OR({t}{n}="",{j}{n}=""),"",IF({t}{n}>={j}{n},"可参加","不参加"))')
        for name in ('京东平台售价', '单盒售价', '美团平台售价', '固定成本价', '美团调后售价', '美团倒推成本', '最低成本价', '京东最低售价', '实际入围最高价', '实际入围最低价'):
            ws.cell(n, cols[name]).number_format = MONEY_FMT
    style_header(ws)
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
    return ws.max_row - 1


def build_index_sheet(wb, conn, index_rows, day):
    """以京东店铺在售商品(商品编码级)为出发点匹配可报名指数清单。

    一个商品一行，避免按 SKU(盒数) 展开导致匹配条数虚高；同商品多个指数条目
    在一行内聚合展示，并用「匹配指数条目数」标注真实命中数量。
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 商品编码,商品名称,销售属性,商家SKU原值,SKUID,京东价,商品状态,品牌
            FROM jd_136186144.jd_store_sku_normalized_daily
            WHERE 记录日期=%s AND 商品状态='上架'
        """, (day,))
        jd_rows = cur.fetchall()

    onsale = {}
    for code, name, prop, seller, sku, price, state, jd_brand in jd_rows:
        key = norm(code) or norm(name)
        rec = onsale.setdefault(key, {
            'code': code, 'name': name, 'brand': jd_brand,
            'props': [], 'skus': [], 'prices': [],
        })
        if prop and norm(prop) not in rec['props']:
            rec['props'].append(norm(prop))
        if seller and norm(seller) not in rec['skus']:
            rec['skus'].append(norm(seller))
        p = amount(price)
        if p is not None and p not in rec['prices']:
            rec['prices'].append(p)

    headers = [
        '商品编码', '商品名称', '本店品牌', '在售SKU数', '在售最低价', '商品状态',
        '是否匹配指数交易', '匹配指数条目数', '指数SKU名称', '指数标准品名', '指数通用名',
        '指数规格', '指数药品ID', '指数SKUID编号', '二级类目', '匹配方式', '在售销售属性',
    ]
    ws = wb.create_sheet('保民指数交易匹配')
    ws.append(headers)
    matched = 0
    for key, item in sorted(onsale.items(), key=lambda kv: norm(kv[1]['name'])):
        title = normalize_text(item['name'] or '')
        jb = normalize_brand(item['brand'] or '')
        hits = []
        for ir in index_rows:
            b = normalize_brand(ir.get('品牌'))
            generic = normalize_text(ir.get('通用名'))
            spec = norm(ir.get('规格'))
            std = normalize_text(ir.get('标准品名'))
            brand_ok = bool(b and (b == jb or b in normalize_brand(item['name'] or '')))
            name_ok = bool(generic and generic in title)
            spec_ok = any(spec_score(spec, p) > 0 or (spec and spec in p) for p in item['props'])
            spec_ok = spec_ok or bool(spec and spec in std)
            if brand_ok and name_ok and spec_ok:
                hits.append(ir)
        base = [
            item['code'], item['name'], item['brand'], len(item['skus']),
            min(item['prices']) if item['prices'] else None, '上架',
            '是' if hits else '否', len(hits),
        ]
        props_text = ' | '.join(item['props'][:5])
        if not hits:
            ws.append(base + [''] * (len(headers) - len(base) - 1) + [props_text])
            continue
        matched += 1

        def joined(field, limit=5):
            return ' | '.join(norm(h.get(field)) for h in hits[:limit] if h.get(field) not in (None, ''))

        ws.append(base + [
            joined('指数SKU名称'), joined('标准品名'), joined('通用名'), joined('规格'),
            joined('指数药品ID'), joined('指数SKUID编号'), joined('二级类目'),
            '品名+规格+品牌', props_text,
        ])
    price_col = headers.index('在售最低价') + 1
    flag_col = headers.index('是否匹配指数交易') + 1
    for n in range(2, ws.max_row + 1):
        ws.cell(n, price_col).number_format = MONEY_FMT
        flag = ws.cell(n, flag_col)
        yes = flag.value == '是'
        flag.fill = JOIN_FILL if yes else QUIT_FILL
        flag.font = JOIN_FONT if yes else QUIT_FONT
        flag.alignment = Alignment(horizontal='center', vertical='center')
    style_header(ws)
    ws.freeze_panes = 'C2'
    ws.auto_filter.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
    return len(onsale), matched


def build_index_joined_sheet(wb, conn, day):
    """本店参与活动的指数商品（京东指数交易-我的竞价），来自 CDP 采集落库。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT skuName, skuId, indexSkuName, indexSkuId, indexSpuId, quotation, `竞价最低价`,
                   activityId, status, statusReason, orderNum, indexSalesRatio,
                   topDurationMinutes, sellNum, remainStock, autoQuotation,
                   autoFollowPrice, createTime, stockUrl
            FROM jd_index_quotation_daily
            WHERE 记录日期=%s AND 店铺='保民'
            ORDER BY CASE WHEN `status`=31 THEN 0 ELSE 1 END, `statusReason`, `quotation`
        """, (day,))
        rows = cur.fetchall()
    headers = [
        '是否在参与', '本店商品名称', '本店skuId', '指数商品名称', '指数skuId', '指数spuId', '本店报价',
        '竞价详情(最低价)', '活动ID', '状态', '状态说明(排名)', '指数订单数', '指数销售占比', '霸榜时长(分钟)',
        '已售数量', '剩余库存', '自动报价', '自动跟价', '报价时间', '商品链接',
    ]
    ws = wb.create_sheet('保民指数交易本店参与')
    ws.append(headers)
    for r in rows:
        ws.append([None] + list(r))
    for name in ('本店报价', '竞价详情(最低价)'):
        col = headers.index(name) + 1
        for n in range(2, ws.max_row + 1):
            ws.cell(n, col).number_format = MONEY_FMT
    status_col = headers.index('状态') + 1
    for n in range(2, ws.max_row + 1):
        status = ws.cell(n, status_col).value
        joined = status == 31
        flag = ws.cell(n, 1)
        flag.value = '参与中' if joined else '未参与'
        flag.fill = JOIN_FILL if joined else QUIT_FILL
        flag.font = JOIN_FONT if joined else QUIT_FONT
        flag.alignment = Alignment(horizontal='center', vertical='center')
    style_header(ws)
    ws.freeze_panes = 'B2'
    ws.auto_filter.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
    return len(rows)


def build_tmall_activity_sheet(wb, conn, day):
    """天猫活动（已报名 或 有补贴 的并集），SKU 级，已报名排前。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT `报名状态`,`是否补贴`,`活动ID`,`活动类型`,`服务费`,`活动状态`,
                   `通用名`,`规格`,`活动商品名`,`天猫商品ID`,`品牌`,`skuId`,`skuItemId`,
                   `包装档位`,`招商价格`,`活动开始`,`活动结束`
            FROM tmall_activity_available_daily
            WHERE 记录日期=%s AND (`报名状态`='true' OR `是否补贴`='true')
            ORDER BY CASE WHEN `报名状态`='true' THEN 0 ELSE 1 END,
                     CASE WHEN `是否补贴`='true' THEN 0 ELSE 1 END, `活动ID`, `skuId`
        """, (day,))
        rows = cur.fetchall()
    headers = [
        '报名状态', '是否补贴', '活动ID', '活动类型', '服务费', '活动状态',
        '通用名', '规格', '活动商品名', '天猫商品ID', '品牌', 'skuId', 'skuItemId',
        '包装档位', '招商价格', '活动开始', '活动结束',
    ]
    ws = wb.create_sheet('保民天猫活动')
    ws.append(headers)
    for r in rows:
        ws.append(list(r))
    apply_col = headers.index('报名状态') + 1
    subsidy_col = headers.index('是否补贴') + 1
    price_col = headers.index('招商价格') + 1
    for n in range(2, ws.max_row + 1):
        ws.cell(n, price_col).number_format = MONEY_FMT
        applied = str(ws.cell(n, apply_col).value).lower() == 'true'
        ws.cell(n, apply_col).value = '已报名' if applied else '未报名'
        ws.cell(n, apply_col).fill = JOIN_FILL if applied else QUIT_FILL
        ws.cell(n, apply_col).font = JOIN_FONT if applied else QUIT_FONT
        ws.cell(n, apply_col).alignment = Alignment(horizontal='center', vertical='center')
        sub = str(ws.cell(n, subsidy_col).value).lower() == 'true'
        ws.cell(n, subsidy_col).value = '有补贴' if sub else '无补贴'
        if sub:
            ws.cell(n, subsidy_col).fill = JOIN_FILL
            ws.cell(n, subsidy_col).font = JOIN_FONT
        ws.cell(n, subsidy_col).alignment = Alignment(horizontal='center', vertical='center')
    style_header(ws)
    ws.freeze_panes = 'C2'
    ws.auto_filter.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
    return len(rows)


def build_tmall_match_sheet(wb, conn, day):
    """在售商品 × 可报名活动匹配（离线按通用名+品牌），供三平台比选。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT m.`在售商品标题`,m.`在售商品ID`,m.`在售售价`,m.`库存`,m.`本店品牌`,m.`在售规格`,m.`活动ID`,
                   m.`报名状态`,m.`是否补贴`,m.`服务费`,m.`规格`,m.`包装档位`,m.`招商价格`,m.`match_type`,
                   a.`活动类型`,a.`活动商品名`,a.`品牌`,a.`skuId`,a.`天猫商品ID`,a.`活动开始`,a.`活动结束`
            FROM tmall_onsale_activity_match_daily_v2 m
            LEFT JOIN tmall_activity_available_daily a
              ON a.`记录日期`=m.`记录日期` AND a.`活动ID`=m.`活动ID` AND a.`skuId`=m.`skuId`
            WHERE m.`记录日期`=%s
            ORDER BY m.`在售商品ID`,m.`活动ID`,m.`包装档位`
        """, (day,))
        rows = cur.fetchall()
    headers = [
        '在售商品标题', '在售商品ID', '在售售价(单盒)', '库存', '本店品牌', '在售规格', '活动ID',
        '活动类型', '活动商品名', '活动品牌', '活动规格', '报名状态', '是否补贴', '服务费',
        '包装档位', '档位盒数', '招商价格(档位)', '单盒招商价', '单盒价差(招商-在售)',
        '匹配方式', 'skuId', '天猫商品ID', '活动开始', '活动结束',
    ]
    ws = wb.create_sheet('天猫在售商品可报名匹配')
    ws.append(headers)
    for r in rows:
        (title, item_id, sale_price, stock, brand, onsale_spec, act_id, applied, subsidy, fee,
         spec, pack, thr, match_type, act_type, act_name, act_brand, sku_id, tmall_item_id,
         act_start, act_end) = r
        m = re.search(r'(\d+)\s*盒装', str(pack or ''))
        boxes = int(m.group(1)) if m else 1
        ws.append([
            title, item_id, sale_price, stock, brand, onsale_spec, act_id, act_type, act_name,
            act_brand, spec, applied, subsidy, fee, pack, boxes, thr, None, None,
            match_type, sku_id, tmall_item_id, act_start, act_end,
        ])
    for name in ('在售售价(单盒)', '招商价格(档位)', '单盒招商价', '单盒价差(招商-在售)'):
        col = headers.index(name) + 1
        for n in range(2, ws.max_row + 1):
            ws.cell(n, col).number_format = MONEY_FMT
    box_col = headers.index('档位盒数') + 1
    thr_col = headers.index('招商价格(档位)') + 1
    unit_col = headers.index('单盒招商价') + 1
    p_col = headers.index('在售售价(单盒)') + 1
    d_col = headers.index('单盒价差(招商-在售)') + 1
    from openpyxl.utils import get_column_letter as gl
    for n in range(2, ws.max_row + 1):
        ws.cell(n, unit_col, f'=IF(OR({gl(thr_col)}{n}="",{gl(box_col)}{n}=""),"",{gl(thr_col)}{n}/{gl(box_col)}{n})')
        ws.cell(n, d_col, f'=IF(OR({gl(unit_col)}{n}="",{gl(p_col)}{n}=""),"",{gl(unit_col)}{n}-{gl(p_col)}{n})')
    style_header(ws)
    ws.freeze_panes = 'B2'
    ws.auto_filter.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
    return len(rows)


def build_summary_sheet(wb, conn, mt_path, registration_rows, by_title, stock_cost, mt_price_map, day):
    """三平台汇总：商品维度，一个商品一行，看它在美团/京东/天猫各自有没有活动、单盒价格与最低参考售价。"""
    BRAND_RE = re.compile(r'^\[([^\]]+)\]')

    def strip_brand(t):
        s = str(t or '').strip()
        m = re.match(r'^(?:\[([^\]]+)\]|【([^】]+)】)', s)
        if m:
            return s[m.end():].strip(), (m.group(1) or m.group(2))
        return s, ''

    def generic_of(title):
        s, _ = strip_brand(title)
        s = s.lstrip('[【')
        # 通用名到第一个剂量数字或规格括号为止，避免把规格带进商品名
        m = re.search(r'\d|[\(（]', s)
        g = normalize_text(s[:m.start()]) if m else normalize_text(s)
        return re.sub(r'[【\[\(（]+$', '', g).strip()

    def boxes_of(pack):
        m = re.search(r'(\d+)\s*盒装', str(pack or ''))
        return int(m.group(1)) if m else 1

    # 报名价格表的通用名索引，用于品牌粘连时兜底
    reg_generic = {}
    for rec in registration_rows:
        g = generic_of(rec.get('商品标题'))
        cost = amount(rec.get('新成本价·取低（元）'))
        if g and cost is not None:
            reg_generic.setdefault(g, cost)

    def erp_cost(title, upc='', brand='', generic='', spec=''):
        value = by_title.get(normalize_text(title))
        if value is not None:
            return value, '报名价格表'
        if upc and upc in stock_cost:
            return stock_cost[upc], '旺店通库存成本'
        if generic:
            value = find_registration_cost_by_parts(registration_rows, brand, generic, spec)
            if value is not None:
                return value, '报名价格表(拆分匹配)'
        return None, ''

    products = {}

    def prod(generic):
        if generic not in products:
            products[generic] = {
                'generic': generic, 'brand': '', 'spec': '', 'erp': None, 'erp_src': '',
                'from_reg': False,
                'mt': {'sale': None, 'thr': None, 'act': ''},
                'jd': {'sale': None, 'thr': None, 'act': ''},
                'tm': {'sale': None, 'thr': None, 'act': ''},
            }
        return products[generic]

    with conn.cursor() as cur:
        cur.execute("""SELECT 策略ID,价格下限,实际入围最高价 FROM adjust_price.jd_subsidy_catalog
                       WHERE 同步日期=(SELECT MAX(同步日期) FROM adjust_price.jd_subsidy_catalog)""")
        catalog = {norm(r[0]): r for r in cur.fetchall()}
        cur.execute("""SELECT 策略ID,商品upc,商家SKU,品牌,通用名称,单盒规格,活动盒数,京东价
                       FROM jd_136186144.jd_subsidy_sku WHERE 记录日期=%s ORDER BY 策略ID""", (day,))
        jd_rows = cur.fetchall()
        cur.execute("""SELECT `在售商品标题`,`在售商品ID`,`在售售价`,`本店品牌`,`在售规格`,`活动ID`,
                              `规格`,`包装档位`,`招商价格`
                       FROM tmall_onsale_activity_match_daily_v2 WHERE 记录日期=%s""", (day,))
        tm_rows = cur.fetchall()

    # 成本表商品作为基础商品池（保民的全部商品）
    for rec in registration_rows:
        title = norm(rec.get('商品标题'))
        g = generic_of(title)
        if not g:
            continue
        p = prod(g)
        p['from_reg'] = True
        _, brand = strip_brand(title)
        if brand and not p['brand']:
            p['brand'] = brand
        p['erp'], p['erp_src'] = erp_cost(title)

    # 美团在售商品列表（商家后台 #/page/product/list/single，CDP 采集落 adjust_price.esale_product）
    # 是本店在售商品和售价的权威来源。先用它建商品池，再用补贴清单/最低售价表兜底。
    with conn.cursor() as cur:
        cur.execute("""SELECT 商品,唯一编码,价格 FROM esale_product
                       WHERE 客户='保民' AND 平台='美团'
                         AND 更新日期=(SELECT MAX(更新日期) FROM esale_product
                                       WHERE 客户='保民' AND 平台='美团')""")
        mt_listed = cur.fetchall()
    mt_listed_price = {}
    for title, upc, price in mt_listed:
        g = generic_of(title)
        if not g:
            continue
        p = prod(g)
        _, brand = strip_brand(title)
        if brand and not p['brand']:
            p['brand'] = brand
        if not p['spec']:
            p['spec'] = strip_brand(title)[0][:60]
        value = amount(price)
        if value is not None and (g not in mt_listed_price or value < mt_listed_price[g]):
            mt_listed_price[g] = value
        if p['erp'] is None and upc:
            key = norm(upc)
            if key in stock_cost:
                p['erp'], p['erp_src'] = stock_cost[key], '旺店通库存成本'

    # 最低售价表作为兜底（本店已建品）；商品列表里有价的以后者为准
    with conn.cursor() as cur:
        cur.execute("""SELECT sku_name,`最低售价` FROM mt_subsidy_lowest_price
                       WHERE 店铺='保民' AND `最低售价` IS NOT NULL""")
        mt_lowest = cur.fetchall()
    for title, price in mt_lowest:
        g = generic_of(title)
        if not g:
            continue
        p = prod(g)
        price = amount(price)
        if price is not None and (p['mt']['sale'] is None or price < p['mt']['sale']):
            p['mt']['sale'] = price

    # 美团补贴清单：只用于活动信息；在售售价仅取「当前售价/我的价格」，
    # 「建议定价」是未建品(本店没在售)的平台建议价，不作为售价
    wb2 = load_workbook(mt_path, read_only=True, data_only=True)
    src = wb2['保民']
    hdr = [c.value for c in next(src.iter_rows(min_row=1, max_row=1))]
    idx = {h: i for i, h in enumerate(hdr)}
    mt_excel = [r for r in src.iter_rows(min_row=2, values_only=True) if any(x not in (None, '') for x in r)]
    for r in mt_excel:
        title = norm(r[idx['sku_name']])
        if not title:
            continue
        g = generic_of(title)
        if not g:
            continue
        p = prod(g)
        _, brand = strip_brand(title)
        if not p['brand']:
            p['brand'] = brand
        if not p['spec']:
            p['spec'] = strip_brand(title)[0][:60]
        status = norm(r[idx['补贴状态']])
        if status != '未建品':
            price = None
            for field in ('当前售价', '我的价格'):
                if field in idx and r[idx[field]] not in (None, ''):
                    price = amount(r[idx[field]])
                    break
            if price is not None and (p['mt']['sale'] is None or price < p['mt']['sale']):
                p['mt']['sale'] = price
        # 活动门槛价：所有状态都记录（未建品是可报名但本店没在售的活动）
        if '补贴门槛' in idx:
            thr = amount(r[idx['补贴门槛']])
            boxes = int(r[idx['门槛盒数']]) if '门槛盒数' in idx and r[idx['门槛盒数']] else 1
            if thr and boxes > 1:
                thr = thr / boxes
            if thr and (p['mt']['thr'] is None or thr < p['mt']['thr']):
                p['mt']['thr'] = thr
                p['mt']['act'] = norm(r[idx['活动名称']]) if '活动名称' in idx else ''
        if p['erp'] is None:
            p['erp'], p['erp_src'] = erp_cost(title, norm(r[idx['upc']]))
    wb2.close()

    # 商品列表页抓到的在售价优先于补贴清单/最低售价表的兜底价
    for g, price in mt_listed_price.items():
        prod(g)['mt']['sale'] = price

    # 京东在售商品：单盒售价 = 京东价 / 组合数量（与活动无关，所有在售商品都取）
    with conn.cursor() as cur:
        cur.execute("""SELECT 商品名称,销售属性,组合数量,京东价,品牌,基础UPC
                       FROM jd_136186144.jd_store_sku_normalized_daily
                       WHERE 记录日期=%s AND 商品状态='上架'""", (day,))
        jd_onsale = cur.fetchall()
        cur.execute("""SELECT title,current_price FROM tmall_on_sale_daily
                       WHERE 记录日期=(SELECT MAX(记录日期) FROM tmall_on_sale_daily)""")
        tm_onsale = cur.fetchall()

    for name, prop, combo, jd_price, jd_brand, upc in jd_onsale:
        g = generic_of(name)
        if not g:
            continue
        p = prod(g)
        if not p['brand'] and jd_brand:
            p['brand'] = norm(jd_brand)
        if not p['spec'] and prop:
            p['spec'] = norm(prop)
        jd_price = amount(jd_price)
        boxes = int(combo) if combo and int(combo) > 0 else 1
        unit = jd_price / boxes if jd_price is not None else None
        if unit is not None and (p['jd']['sale'] is None or unit < p['jd']['sale']):
            p['jd']['sale'] = unit
        if p['erp'] is None and upc:
            key = norm(upc)
            if key in stock_cost:
                p['erp'], p['erp_src'] = stock_cost[key], '旺店通库存成本'

    for title, current_price in tm_onsale:
        g = generic_of(title)
        if not g:
            continue
        p = prod(g)
        _, brand = strip_brand(title)
        if not p['brand'] and brand:
            p['brand'] = brand
        if not p['spec']:
            p['spec'] = strip_brand(title)[0][:60]
        sale = amount(current_price)
        if sale is not None and (p['tm']['sale'] is None or sale < p['tm']['sale']):
            p['tm']['sale'] = sale

    # 京东小官补活动：门槛价来自 catalog
    for sid, upc, seller, brand, generic, spec, boxes, jd_price in jd_rows:
        g = normalize_text(generic)
        if not g:
            continue
        p = prod(g)
        if not p['brand']:
            p['brand'] = norm(brand)
        if not p['spec']:
            p['spec'] = norm(spec)
        cat = catalog.get(norm(sid))
        # 门槛价口径：用实际入围最高价（单盒到手价），不再用价格下限/最低价
        thr = (amount(cat[2]) or amount(cat[1])) if cat else None
        if thr and (p['jd']['thr'] is None or thr < p['jd']['thr']):
            p['jd']['thr'] = thr
            p['jd']['act'] = f'小官补/{norm(sid)}'
        if p['erp'] is None:
            p['erp'], p['erp_src'] = erp_cost(f'{norm(brand)}{g}', norm(upc), norm(brand), g, norm(spec))

    # 天猫：单盒招商价 = 招商价格 / 档位盒数；在售售价本身是单盒价
    for title, item_id, sale_price, brand, onsale_spec, act_id, act_spec, pack, thr in tm_rows:
        g = generic_of(title)
        if not g:
            continue
        p = prod(g)
        if not p['brand']:
            p['brand'] = norm(brand)
        if not p['spec']:
            p['spec'] = norm(onsale_spec)
        thr = amount(thr)
        if thr:
            thr = thr / boxes_of(pack)
        if thr and (p['tm']['thr'] is None or thr < p['tm']['thr']):
            p['tm']['thr'] = thr
            p['tm']['act'] = norm(act_id)
        if p['erp'] is None:
            p['erp'], p['erp_src'] = erp_cost(norm(title), '', norm(brand), '', norm(onsale_spec))

    # 统一兜底：汇总名=品牌+通用名粘连时，去掉前缀品牌后再精确匹配报名表通用名
    brand_prefixes = set()
    for rec in registration_rows:
        m = re.match(r'^\[([^\]]+)\]', str(rec.get('商品标题') or ''))
        if m:
            brand_prefixes.add(normalize_text(m.group(1)))
    brand_prefixes |= {normalize_text(norm(p.get('brand'))) for p in products.values() if norm(p.get('brand'))}
    for g, p in products.items():
        if p['erp'] is not None:
            continue
        ng = normalize_text(g)
        for brand in brand_prefixes:
            if brand and ng.startswith(brand) and len(ng) > len(brand):
                core = ng[len(brand):]
                if core in reg_generic:
                    p['erp'], p['erp_src'] = reg_generic[core], '报名价格表(品牌粘连兜底)'
                    break
        if p['erp'] is not None:
            continue
        # 英文/alias 品牌粘连（如 ursofold熊去氧胆酸胶囊）：报名表通用名做后缀，
        # 且前缀不能是「左/右/消旋」这类改变药品的修饰字；多个候选则不填避免歧义
        suffix_hits = []
        for rg, cost in reg_generic.items():
            if ng.endswith(rg) and len(ng) > len(rg):
                prefix = ng[:-len(rg)]
                if prefix and not any(x in prefix for x in ('左', '右', '消旋')):
                    suffix_hits.append((rg, cost))
        if len(suffix_hits) == 1:
            p['erp'], p['erp_src'] = suffix_hits[0][1], '报名价格表(品牌粘连兜底)'

    headers = [
        '通用名', '本店品牌', '规格', '固定成本价(ERP)', '成本来源', '美团售价(单盒)',
        '美团调后售价', '美团倒推成本', '最低成本价',
        '美团-有无活动', '美团活动价(单盒)', '美团最低参考售价', '美团-是否可参加',
        '京东-有无活动', '京东活动价(单盒)', '京东售价(单盒)', '京东最低参考售价', '京东-是否可参加',
        '天猫-有无活动', '天猫单盒招商价', '天猫在售售价(单盒)', '淘宝最低参考售价', '天猫-是否可参加',
    ]
    ws = wb.create_sheet('三平台活动汇总')
    ws.append(headers)
    kept_products = []
    for g, p in sorted(products.items()):
        # 只保留本店商品：在成本表，或任一平台有在售价。
        # 美团「未建品」是可报名但本店没在售，不算本店商品，其活动只留在美团分表。
        has_sale = any(p[x]['sale'] is not None for x in ('mt', 'jd', 'tm'))
        if not p['from_reg'] and not has_sale:
            continue
        kept_products.append((g, p))
        ws.append([
            g, p['brand'], p['spec'][:80], p['erp'], p['erp_src'], p['mt']['sale'],
            None, None, None,
            '有' if p['mt']['thr'] is not None else '无', p['mt']['thr'], None, None,
            '有' if p['jd']['thr'] is not None else '无', p['jd']['thr'], p['jd']['sale'], None, None,
            '有' if p['tm']['thr'] is not None else '无', p['tm']['thr'], p['tm']['sale'], None, None,
        ])
    col = {h: get_column_letter(i + 1) for i, h in enumerate(headers)}
    erp_c, mt_c = col['固定成本价(ERP)'], col['美团售价(单盒)']
    adj_c, back_c, base_c = col['美团调后售价'], col['美团倒推成本'], col['最低成本价']
    mt_ref, jd_ref, tb_ref = col['美团最低参考售价'], col['京东最低参考售价'], col['淘宝最低参考售价']
    for n in range(2, ws.max_row + 1):
        ws.cell(n, headers.index('美团调后售价') + 1, f'=IF({mt_c}{n}="","",IF({mt_c}{n}>29,{mt_c}{n}-3,{mt_c}{n}))')
        ws.cell(n, headers.index('美团倒推成本') + 1, f'=IF({adj_c}{n}="","",{adj_c}{n}*0.92)')
        ws.cell(n, headers.index('最低成本价') + 1,
                f'=IF(AND({back_c}{n}="",{erp_c}{n}=""),"",IF({back_c}{n}="",{erp_c}{n},IF({erp_c}{n}="",{back_c}{n},MIN({back_c}{n},{erp_c}{n}))))')
        ws.cell(n, headers.index('美团最低参考售价') + 1,
                f'=IF({base_c}{n}="","",IF({base_c}{n}>27.5,({base_c}{n}+3)/0.92,{base_c}{n}/0.92))')
        ws.cell(n, headers.index('京东最低参考售价') + 1, f'=IF({base_c}{n}="","",({base_c}{n}+3)/0.88)')
        ws.cell(n, headers.index('淘宝最低参考售价') + 1, f'=IF({base_c}{n}="","",({base_c}{n}+3)/0.95)')
        for plat, thr_h, ref_h, judge_h in (
            ('美团', '美团活动价(单盒)', '美团最低参考售价', '美团-是否可参加'),
            ('京东', '京东活动价(单盒)', '京东最低参考售价', '京东-是否可参加'),
            ('天猫', '天猫单盒招商价', '淘宝最低参考售价', '天猫-是否可参加'),
        ):
            ws.cell(n, headers.index(judge_h) + 1,
                    f'=IF(OR({col[thr_h]}{n}="",{col[ref_h]}{n}=""),"",IF({col[thr_h]}{n}>={col[ref_h]}{n},"可参加","不参加"))')
        for name in ('固定成本价(ERP)', '美团售价(单盒)', '美团调后售价', '美团倒推成本', '最低成本价',
                     '美团活动价(单盒)', '美团最低参考售价', '京东活动价(单盒)', '京东售价(单盒)',
                     '京东最低参考售价', '天猫单盒招商价', '天猫在售售价(单盒)', '淘宝最低参考售价'):
            ws.cell(n, headers.index(name) + 1).number_format = MONEY_FMT
        for name in ('美团-有无活动', '京东-有无活动', '天猫-有无活动'):
            cell = ws.cell(n, headers.index(name) + 1)
            cell.fill = JOIN_FILL if cell.value == '有' else QUIT_FILL
            cell.font = JOIN_FONT if cell.value == '有' else QUIT_FONT
            cell.alignment = Alignment(horizontal='center', vertical='center')
    style_header(ws)
    ws.freeze_panes = 'D2'
    ws.auto_filter.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
    return {'商品数': len(kept_products),
            '有美团活动': sum(1 for g, p in kept_products if p['mt']['thr'] is not None),
            '有京东活动': sum(1 for g, p in kept_products if p['jd']['thr'] is not None),
            '有天猫活动': sum(1 for g, p in kept_products if p['tm']['thr'] is not None)}


def build_registration_sheet(wb, registration_rows):
    ws = wb.create_sheet('报名价格表')
    headers = list(registration_rows[0].keys()) if registration_rows else []
    ws.append(headers)
    for rec in registration_rows:
        ws.append([rec.get(h) for h in headers])
    style_header(ws)
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'


def build_notes(wb, mt_rows, jd_rows, index_rows, index_matched, tmall_rows):
    ws = wb.create_sheet('口径说明')
    rows = [
        ('项目', '口径'),
        ('范围', '只做保民；数据日期 2026-11-24；先按平台分表，最后用「三平台活动汇总」合并对比'),
        ('【数据来源】', ''),
        ('天猫-活动', '拉取可报名活动接口的全量活动商品并落库（3457 行），再与保民在售商品按「通用名+规格」匹配；全量落库便于复用，匹配时只取规格一致的活动'),
        ('天猫-在售', '天猫商家后台「出售中的商品」列表采集，87 个在售商品'),
        ('京东-活动', '以活动为起点匹配商品：活动清单（小官补、指数交易可报名清单）驱动，再与本店在售商品匹配；指数交易按「品名+规格+品牌」匹配（对方已确认）'),
        ('京东-在售', '京东商家中心在售商品列表采集（377 个 SKU 行 / 92 个商品编码），作为匹配底表'),
        ('京东-竞价详情', '京东指数交易「我的竞价」逐条点开竞价详情采集最低价，回填 jd_index_quotation_daily'),
        ('美团-在售', '美团商家后台「商品列表」页（#/page/product/list/single）CDP 采集本店全部上架商品（当前 140 行，含售价/UPC/库存），落在 adjust_price.esale_product；这是本店在售商品和售价的权威来源'),
        ('美团-活动', '美团活动数量多，直接爬美团补贴清单（当前 705 行：补贴中、未补贴、未建品），按店铺=保民；只用于活动门槛信息，「未建品」是本店没在售但可报名的商品，不计入本店在售商品和售价'),
        ('【成本与售价】', ''),
        ('固定成本价(ERP)', '报名价格表「新成本价·取低」为主，取不到用旺店通库存查询的「成本价」兜底（对方确认：ERP cost 就是库存查询中的成本价）'),
        ('美团调后售价', '美团售价 >29 的先减 3 元运费，否则不减'),
        ('最低成本价', '美团调后售价 × 0.92 与固定成本价取最低值；该最低成本价同时用于京东/淘宝参考售价的计算（对方确认：京东和淘宝活动价也按生出来的最低成本价计算）'),
        ('京东最低参考售价', '(最低成本价+3)/0.88'),
        ('美团最低参考售价', '最低成本价 >27.5 时 (最低成本价+3)/0.92；否则 最低成本价/0.92'),
        ('淘宝最低参考售价', '(最低成本价+3)/0.95'),
        ('活动参与判断', '活动门槛价 >= 对应平台计算出的最低售价，则可参加；否则不参加（对方确认）'),
        ('【三平台汇总】', ''),
        ('汇总口径', '三平台活动堆叠到「三平台活动汇总」，每行一个平台活动；统一套用上面的成本/售价公式，并给出该平台是否可参加，便于跨平台比选'),
        ('汇总平台', '美团：补贴清单 705 行；京东：小官补 214 行；天猫：在售商品×活动匹配 82 行'),
        ('数量核对', f'美团 {mt_rows} 行；京东小官补 {jd_rows} 行；京东在售商品 {index_rows} 个，匹配指数交易 {index_matched} 个；天猫在售×活动 {tmall_rows} 行'),
        ('确认时间', '用户截图答复（对方口径确认），2026-11-24'),
    ]
    for r in rows:
        ws.append(r)
    style_header(ws)
    for n in range(2, ws.max_row + 1):
        label = ws.cell(n, 1).value
        if isinstance(label, str) and label.startswith('【'):
            ws.cell(n, 1).fill = NEW_FILL
            ws.cell(n, 1).font = Font(bold=True)
            ws.cell(n, 2).fill = NEW_FILL
    ws.column_dimensions['A'].width = 24
    ws.column_dimensions['B'].width = 120


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cost', default=str(DEFAULT_COST))
    parser.add_argument('--mt', default=str(DEFAULT_MT))
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    registration_rows, by_title, index_rows = load_registration_cost(args.cost)
    conn = connect(args.config)
    try:
        stock_cost = load_stock_cost(conn, '2026-11-24')
        mt_price_map = load_mt_price_map(conn, '2026-11-24')
        wb = Workbook()
        wb.remove(wb.active)
        mt_rows = build_mt_sheet(wb, args.mt, registration_rows, by_title, stock_cost)
        jd_rows = build_jd_sheet(wb, conn, registration_rows, mt_price_map, stock_cost, '2026-11-24')
        index_rows_n, index_matched = build_index_sheet(wb, conn, index_rows, '2026-11-24')
        index_joined = build_index_joined_sheet(wb, conn, '2026-11-24')
        tmall_match = build_tmall_match_sheet(wb, conn, '2026-11-24')
        summary_counts = build_summary_sheet(wb, conn, args.mt, registration_rows, by_title,
                                             stock_cost, mt_price_map, '2026-11-24')
        out = pathlib.Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        wb.save(out)
    finally:
        conn.close()
    print(f'mt_rows={mt_rows} jd_rows={jd_rows} index_rows={index_rows_n} index_matched={index_matched} index_joined={index_joined} tmall_match={tmall_match}')
    print(f'summary_counts={summary_counts}')
    print(f'output={out}')


if __name__ == '__main__':
    main()
