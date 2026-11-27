import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys

import pymysql
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

BASE = pathlib.Path('/opt/subsidy')
DEFAULT_COST = BASE / 'inbox' / '保民医院_成本价对比与取低_20261113.xlsx'
DEFAULT_OUTPUT = BASE / 'exports' / ('美团补贴成本匹配与京东活动_' + dt.date.today().strftime('%Y%m%d') + '.xlsx')
DEFAULT_CONFIG = BASE / 'config.json'
EXPORTER = BASE / 'export_meituan_subsidy_by_store.py'

STORE_SCHEMAS = {
    '保民': 'jd_136186144',
    '福康综合门诊': 'jd_135003681',
    '璟和堂': 'jd_203228988',
}
DB_MT_STORES = {
    '保民': ('北辰保民美团普药店', '北辰保民美团特药店'),
    '璟和堂': ('璟和堂美团普药店', '璟和堂'),
    '福康综合门诊': ('福康综合门诊美团普药店', '福康综合门诊', '福康'),
}

HEADER_FILL = PatternFill('solid', fgColor='305496')
NEW_FILL = PatternFill('solid', fgColor='FFF2CC')
HEADER_FONT = Font(bold=True, color='FFFFFF')
MONEY_FMT = '0.00'


def norm(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def norm_title(value):
    text = norm(value).lower()
    for src, dst in (('（', '('), ('）', ')'), ('＊', '*'), ('／', '/'), ('：', ':'), ('，', ',')):
        text = text.replace(src, dst)
    return re.sub(r'[\s\u3000]+', '', text)


def amount(value):
    if value is None or value == '':
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def connect(config_path):
    cfg = json.loads(pathlib.Path(config_path).read_text(encoding='utf-8'))['mysql']
    return pymysql.connect(
        host=cfg['host'], port=int(cfg['port']), user=cfg['user'], password=cfg['password'],
        database=cfg['database'], charset=cfg.get('charset', 'utf8mb4'),
        init_command="SET time_zone='+08:00'",
    )


def export_latest_subsidy():
    out = BASE / 'exports' / ('美团补贴_按店铺_最新_' + dt.date.today().strftime('%Y%m%d') + '.xlsx')
    subprocess.run([sys.executable, str(EXPORTER), '--out', str(out)], check=True, capture_output=True, text=True)
    return None, out


def load_cost_workbook(path):
    wb = load_workbook(path, read_only=True, data_only=True)
    cost_rows = []
    ws = wb['成本价对比表']
    headers = [norm(cell.value) for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    idx = {name: i for i, name in enumerate(headers)}
    for row in ws.iter_rows(min_row=2, values_only=True):
        title = norm(row[idx['商品标题']]) if '商品标题' in idx else ''
        if not title:
            continue
        rec = {name: row[idx[name]] if name in idx and idx[name] < len(row) else None for name in headers}
        rec['_key'] = norm_title(title)
        cost_rows.append(rec)

    index_rows = []
    ws2 = wb['Sheet2']
    headers2 = [norm(cell.value) for cell in next(ws2.iter_rows(min_row=1, max_row=1))]
    idx2 = {name: i for i, name in enumerate(headers2)}
    for row in ws2.iter_rows(min_row=2, values_only=True):
        sku = norm(row[idx2['指数SKUID编号']]) if '指数SKUID编号' in idx2 else ''
        if not sku:
            continue
        rec = {name: row[idx2[name]] if name in idx2 and idx2[name] < len(row) else None for name in headers2}
        rec['_sku'] = sku
        index_rows.append(rec)
    wb.close()
    return cost_rows, index_rows, headers, headers2


def load_db_costs(config_path):
    by_upc_store, by_sku_store, by_upc_global = {}, {}, {}
    conn = connect(config_path)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 店铺, `69码`, 平台规格ID, 成本价, 默认采购价, 数据日期
                FROM ec_dw.bi_sku_cost
                WHERE 平台='美团' AND 店铺 NOT LIKE '%南无%'
            """)
            rows = cur.fetchall()
    finally:
        conn.close()
    for store, upc, sku, cost_price, default_price, data_date in rows:
        candidates = [(x, label) for x, label in ((amount(cost_price), 'ERP成本价'), (amount(default_price), 'ERP默认采购价')) if x is not None]
        if not candidates:
            continue
        value, label = min(candidates, key=lambda item: item[0])
        rec = (value, label, norm(store), data_date)
        by_upc_store.setdefault((norm(store), norm(upc)), []).append(rec)
        by_sku_store.setdefault((norm(store), norm(sku)), []).append(rec)
        if norm(upc) not in by_upc_global or value < by_upc_global[norm(upc)][0]:
            by_upc_global[norm(upc)] = rec
    return by_upc_store, by_sku_store, by_upc_global


def db_cost(sheet_title, upc, sku, by_upc_store, by_sku_store, by_upc_global=None):
    candidates = []
    for store in DB_MT_STORES.get(sheet_title, ()):
        candidates.extend(by_upc_store.get((store, upc), []))
        candidates.extend(by_sku_store.get((store, sku), []))
    if candidates:
        value, label, store, day = min(candidates, key=lambda item: item[0])
        return value, f'{label}/{store}', f'ERP兜底/{day}'
    return None, '', '未匹配'


def load_jd_skus(config_path):
    rows = []
    conn = connect(config_path)
    try:
        with conn.cursor() as cur:
            for store, schema in STORE_SCHEMAS.items():
                cur.execute(f"""
                    SELECT 记录日期,商品编码,商品名称,销售属性,SKUID,京东价,商品状态
                    FROM `{schema}`.jd_store_sku_normalized_daily
                    WHERE 记录日期=(SELECT MAX(记录日期) FROM `{schema}`.jd_store_sku_normalized_daily)
                """)
                for day, code, name, prop, sku, price, state in cur.fetchall():
                    rows.append({'店铺': store, '记录日期': day, '商品编码': norm(code), '商品名称': norm(name),
                                 '规格': norm(prop), 'SKUID': norm(sku), '京东价': price, '商品状态': norm(state)})
    finally:
        conn.close()
    return rows


def load_xiaoguanbu(config_path):
    conn = connect(config_path)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 同步日期,策略ID,药品ID,品牌名,通用名称,规格,盒数,价格下限,价格上限,
                       实际入围最高价,实际入围最低价,券信息
                FROM adjust_price.jd_subsidy_catalog
                WHERE 同步日期=(SELECT MAX(同步日期) FROM adjust_price.jd_subsidy_catalog)
            """)
            rows = cur.fetchall()
    finally:
        conn.close()
    return rows


def style_header(ws, row=1, start=1, end=None, fill=HEADER_FILL, font=HEADER_FONT):
    end = end or ws.max_column
    for col in range(start, end + 1):
        cell = ws.cell(row=row, column=col)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal='center', vertical='center')


def add_store_columns(ws, cost_rows, db_costs, cost_headers):
    headers = [cell.value for cell in ws[1]]
    if headers and norm(headers[0]) == '状态':
        ws.delete_cols(1)
        headers = [cell.value for cell in ws[1]]
    hmap = {norm(v): i for i, v in enumerate(headers, 1) if v is not None and str(v).strip()}
    last_col = max(i for i, v in enumerate(headers, 1) if v is not None and str(v).strip())
    price_col = hmap['当前售价']
    upc_col = hmap['upc']
    sku_col = hmap['sku_id']
    name_col = hmap['sku_name']
    threshold_col = hmap.get('补贴门槛') or hmap.get('补贴后价')

    cols = {
        'erp': last_col + 1, 'source': last_col + 2, 'adjusted': last_col + 3,
        'inferred': last_col + 4, 'jd': last_col + 5, 'mt': last_col + 6,
        'tb': last_col + 7, 'decision': last_col + 8,
    }
    labels = {
        cols['erp']: '固定成本价', cols['source']: '成本来源', cols['adjusted']: '美团调后售价',
        cols['inferred']: '售价倒推可承受成本', cols['jd']: '京东最低售价',
        cols['mt']: '美团最低售价', cols['tb']: '淘宝最低售价', cols['decision']: '活动参与判断',
    }
    for col, label in labels.items():
        ws.cell(1, col, label)
    style_header(ws, 1, last_col + 1, cols['decision'], NEW_FILL, Font(bold=True))

    cost_map = {rec['_key']: rec for rec in cost_rows} if ws.title == '保民' else {}
    by_upc_store, by_sku_store, by_upc_global = db_costs
    total = cost_hits = db_hits = 0
    for row in range(2, ws.max_row + 1):
        upc = norm(ws.cell(row, upc_col).value)
        sku = norm(ws.cell(row, sku_col).value)
        sku_name = norm(ws.cell(row, name_col).value)
        if not any((upc, sku, sku_name)):
            continue
        total += 1
        rec = cost_map.get(norm_title(sku_name))
        if rec:
            value = amount(rec.get('新成本价·取低（元）'))
            source = '保民成本表/商品标题'
            status = '保民成本表'
            cost_hits += 1
        else:
            value, source, status = db_cost(ws.title, upc, sku, by_upc_store, by_sku_store, by_upc_global)
            if value is not None:
                db_hits += 1
        ws.cell(row, cols['erp'], value).number_format = MONEY_FMT
        ws.cell(row, cols['source'], source)
        p = get_column_letter(price_col)
        a = get_column_letter(cols['adjusted'])
        e = get_column_letter(cols['erp'])
        ws.cell(row, cols['adjusted'], f'=IF({p}{row}="","",IF({p}{row}>29,{p}{row}-3,{p}{row}))')
        ws.cell(row, cols['inferred'], f'=IF({a}{row}="","",{a}{row}*0.92)')
        ws.cell(row, cols['jd'], f'=IF({e}{row}="","",({e}{row}+3)/0.88)')
        ws.cell(row, cols['mt'], f'=IF({e}{row}="","",IF({e}{row}>27.5,({e}{row}+3)/0.92,{e}{row}/0.92))')
        ws.cell(row, cols['tb'], f'=IF({e}{row}="","",({e}{row}+3)/0.95)')
        for col in (cols['inferred'], cols['jd'], cols['mt'], cols['tb']):
            ws.cell(row, col).number_format = MONEY_FMT
        if threshold_col:
            t = get_column_letter(threshold_col)
            ws.cell(row, cols['decision'], f'=IF(OR({t}{row}="",{e}{row}=""),"",IF({t}{row}<{e}{row},"不参加","可参加"))')
        else:
            ws.cell(row, cols['decision'], '')
    return total, cost_hits, db_hits


def add_index_sheet(wb, index_rows, headers2):
    ws = wb.create_sheet('指数活动ID清单')
    base = [h for h in headers2 if h]
    ws.append(base)
    style_header(ws)
    for rec in index_rows:
        ws.append([rec.get(h) for h in base])
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
    return len(index_rows)


def load_xgb_store_matches(config_path):
    rows = []
    conn = connect(config_path)
    try:
        with conn.cursor() as cur:
            for store, schema in STORE_SCHEMAS.items():
                cur.execute(f"""
                    SELECT 记录日期,策略ID,商品upc,商家SKU,品牌,通用名称,单盒规格,活动盒数,京东价
                    FROM `{schema}`.jd_subsidy_sku
                    WHERE 记录日期=(SELECT MAX(记录日期) FROM `{schema}`.jd_subsidy_sku)
                    ORDER BY 策略ID
                """)
                for day, sid, upc, seller_sku, brand, generic, spec, boxes, price in cur.fetchall():
                    rows.append({
                        '店铺': store, '记录日期': day, '策略ID': norm(sid), '商品upc': norm(upc),
                        '商家SKU': norm(seller_sku), '品牌': norm(brand), '通用名称': norm(generic),
                        '单盒规格': norm(spec), '活动盒数': boxes, '京东价': price,
                    })
    finally:
        conn.close()
    return rows


def add_xgb_sheet(wb, sku_matches, catalog_rows):
    ws = wb.create_sheet('京东小官补店铺匹配')
    headers = [
        '店铺', '记录日期', '策略ID', '商品upc', '商家SKU', '品牌', '通用名称', '单盒规格',
        '活动盒数', '京东价', '活动价格下限', '活动价格上限', '实际入围最低价', '实际入围最高价',
        '券信息', '活动同步日期',
    ]
    ws.append(headers)
    style_header(ws)
    catalog_by_sid = {norm(row[1]): row for row in catalog_rows}
    for rec in sku_matches:
        row = catalog_by_sid.get(rec['策略ID'])
        if row:
            day, sid, drug_id, brand, generic, spec, boxes, low, high, actual_high, actual_low, coupon = row
        else:
            day = sid = drug_id = brand = generic = spec = boxes = low = high = actual_high = actual_low = coupon = None
        ws.append([
            rec['店铺'], rec['记录日期'], rec['策略ID'], rec['商品upc'], rec['商家SKU'], rec['品牌'],
            rec['通用名称'], rec['单盒规格'], rec['活动盒数'], rec['京东价'], amount(low), amount(high),
            norm(actual_low), norm(actual_high), norm(coupon), day,
        ])
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
    return len(sku_matches)


def add_note_sheet(wb, subsidy_path, cost_path):
    if '口径说明' in wb.sheetnames:
        del wb['口径说明']
    ws = wb.create_sheet('口径说明')
    rows = [
        ('项目', '口径'),
        ('美团补贴数据', f'最新落库导出：{subsidy_path}；原有定时任务每 10 分钟写入 mt_subsidy_*_daily'),
        ('成本表', f'{cost_path}；仅用于保民，按「成本价对比表.新成本价·取低（元）」与商品标题匹配'),
        ('ERP兜底', '其他店铺只用本店铺 adjust_price.bi_sku_cost 的成本价/默认采购价；不做跨店铺兜底；排除南无、百恩堂'),
        ('固定成本价', '成本表/本店ERP给出的固定成本，是参加活动判断和最低售价计算的唯一成本基准'),
        ('美团调后售价', '当前售价>29时减3元运费，否则不减；用于观察当前售价'),
        ('售价倒推可承受成本', '美团调后售价*0.92；仅作动态参考，不参与最低售价和参加活动判断'),
        ('京东最低售价', '(固定成本价+3)/0.88'),
        ('美团最低售价', '固定成本价>27.5时=(固定成本价+3)/0.92，否则=固定成本价/0.92'),
        ('淘宝最低售价', '(固定成本价+3)/0.95'),
        ('活动参与判断', '门槛价低于固定成本价则不参加；否则可参加。优先取补贴门槛，缺失时取补贴后价'),
        ('京东指数交易', '当前仅整理成本表 Sheet2 的活动ID清单；能否参加需要活动后台报名/查询口径或商品ID映射，暂不判定'),
        ('京东小官补', '读取各店铺库 jd_subsidy_sku，即店铺在售商品列表与活动清单的既有匹配结果，再关联 jd_subsidy_catalog 展示活动价'),
    ]
    for row in rows:
        ws.append(row)
    style_header(ws)
    ws.column_dimensions['A'].width = 20
    ws.column_dimensions['B'].width = 120


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--subsidy')
    parser.add_argument('--cost', default=str(DEFAULT_COST))
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--output', default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    tmp = None
    if args.subsidy:
        subsidy_path = pathlib.Path(args.subsidy)
    else:
        tmp, subsidy_path = export_latest_subsidy()

    try:
        cost_rows, index_rows, cost_headers, index_headers = load_cost_workbook(args.cost)
        db_costs = load_db_costs(args.config)
        catalog_rows = load_xiaoguanbu(args.config)
        xgb_matches = load_xgb_store_matches(args.config)

        wb = load_workbook(subsidy_path)
        summary = []
        for ws in wb.worksheets:
            if '南无' in ws.title:
                continue
            summary.append((ws.title,) + add_store_columns(ws, cost_rows, db_costs, cost_headers))

        index_total = add_index_sheet(wb, index_rows, index_headers)
        xgb_total = add_xgb_sheet(wb, xgb_matches, catalog_rows)
        add_note_sheet(wb, subsidy_path, args.cost)

        out = pathlib.Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        wb.save(out)
    finally:
        if tmp:
            tmp.cleanup()

    for sheet, total, cost_hits, db_hits in summary:
        print(f'{sheet}: rows={total} 成本表匹配={cost_hits} ERP兜底={db_hits}')
    print(f'指数活动ID清单: rows={index_total}')
    print(f'小官补店铺匹配: rows={xgb_total}')
    print(f'subsidy_source={subsidy_path}')
    print(f'output={out}')


if __name__ == '__main__':
    main()
