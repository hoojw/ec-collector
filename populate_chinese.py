"""One-time rebuild population from existing source data; ec_dw mutations only."""
import datetime as dt
import hashlib
import json
import sys
import uuid
from pathlib import Path
import pymysql
sys.path.insert(0,'/opt/subsidy/.ec-phase1-stage')
from migrate import admin

NAMES={'dim_client':'客户','dim_store':'店铺','product':'商品','subsidy':'补贴','input_cost':'input_成本','input_freight':'input_运费','merchant_profit_setting':'商家利润设置','aftersale_case':'售后单','aftersale_node':'售后节点','aftersale_status_snapshot':'售后状态监控','store_metric_window':'店铺经营统计','store_status_snapshot':'店铺状态快照','ops_collect_run':'采集批次','ops_import_run':'导入批次'}
NOW=dt.datetime.now();conn=admin('ec_dw')
def canonical(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':'))
def digest(v):return hashlib.sha256(canonical(v).encode()).hexdigest()
def rows(table):
    with conn.cursor(pymysql.cursors.DictCursor) as c:c.execute('SELECT * FROM '+table);return c.fetchall()
def insert(table,values):
    with conn.cursor() as c:
        cols=list(values);c.execute('INSERT INTO `'+table+'` ('+','.join('`'+k+'`' for k in cols)+') VALUES ('+','.join(['%s']*len(cols))+')',list(values.values()));return c.lastrowid
def run(client,platform,code,source,observed):
    rid=str(uuid.uuid4());insert('采集批次',dict(run_id=rid,client_name=client,platform=platform,store_code=code,collector_code='existing_source.import',started_at=NOW,finished_at=NOW,status='success',message='沿用来源快照 '+source+'；已知的源观察时间保持不变'))
    return rid
def batch(table,client,data):
    bid=str(uuid.uuid4());insert('导入批次',dict(import_id=bid,import_type='existing_source',client_name=client,file_name='mysql:'+table,file_sha256=digest(data),status='success',row_count=len(data),started_at=NOW,finished_at=NOW,message='Existing source rows; original fields and source date retained'))
    return bid
def source_columns(table):
    with conn.cursor() as c:
        c.execute('ALTER TABLE `'+table+'` ADD source_table VARCHAR(128) NULL, ADD source_row_key VARCHAR(191) NULL, ADD source_fields JSON NULL, ADD UNIQUE KEY uq_source(source_table,source_row_key)')

with conn.cursor() as c:
    c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='ec_dw'");existing={r[0] for r in c.fetchall()}
    assert existing in (set(NAMES),set(NAMES.values())), 'Unexpected schema'
    if existing==set(NAMES):
        c.execute('RENAME TABLE '+','.join('`'+old+'` TO `'+new+'`' for old,new in NAMES.items()))
        c.execute('ALTER TABLE `input_成本` MODIFY unit_cost DECIMAL(18,4) NULL, MODIFY cost_unit ENUM(\'box\',\'sale_unit\',\'unknown\') NOT NULL, MODIFY valid_from DATE NULL, ADD data_quality VARCHAR(32) NOT NULL DEFAULT \'unverified\'')
        c.execute('ALTER TABLE `input_运费` MODIFY freight_amount DECIMAL(36,6) NOT NULL, MODIFY valid_from DATE NULL, ADD product_name VARCHAR(255) NULL')
        c.execute('ALTER TABLE `商品` ADD source_fields JSON NULL')
        source_columns('input_成本');source_columns('input_运费')
catalog=rows('adjust_price.bi_sku_platform');erp_cost=rows('adjust_price.bi_sku_cost');cost=rows('upload.sku_cost');freight=rows('upload.delivery_freight')
clients={r['客户'] for r in catalog+erp_cost+cost+freight if r.get('客户')}
with conn.cursor() as c:
    for client in clients:c.execute('INSERT IGNORE INTO `客户`(client_name) VALUES (%s)',(client,))
stores={}
for r in catalog+erp_cost:
    key=(r.get('客户'),r.get('平台'),r.get('店铺'))
    if not all(key) or key in stores:continue
    code='store_'+digest(key)[:24];stores[key]=code
    native=None
    if key==('保民','美团','北辰保民美团普药店'):code='mt-baomin';stores[key]=code;native='32581900'
    with conn.cursor() as c:c.execute('INSERT IGNORE INTO `店铺` (store_code,client_name,platform,platform_store_id,store_name) VALUES (%s,%s,%s,%s,%s)',(code,key[0],key[1],native,key[2]))

conn.autocommit(False)
try:
    for client in sorted(clients):
        for source,data in [('upload.sku_cost',cost),('adjust_price.bi_sku_cost',erp_cost)]:
            subset=[r for r in data if r['客户']==client]
            if not subset:continue
            bid=batch(source,client,subset)
            for i,r in enumerate(subset):
                is_upload=source.startswith('upload');code=r.get('商品upc') if is_upload else r.get('69码')
                code_type='barcode'
                if not code:
                    code=next((str(r[k]) for k in ['平台规格ID','平台商家编码','平台商品ID'] if r.get(k)),None);code_type='source_code'
                if not code:code='source_row_'+digest(r)[:24];code_type='source_row'
                amount=r.get('成本价') if is_upload else None
                quality='unverified_unit' if amount and amount>0 else 'unverified_zero' if amount==0 else 'unverified_cost'
                insert('input_成本',dict(client_name=client,platform=None if is_upload else r.get('平台'),store_code=None if is_upload else stores.get((client,r.get('平台'),r.get('店铺'))),product_code=code,code_type=code_type,product_name=r.get('货品名称'),specification=r.get('规格名称'),unit_cost=amount,cost_unit='unknown',valid_from=None,import_id=bid,data_quality=quality,source_table=source,source_row_key=str(r['id']) if is_upload else client+':'+str(i),source_fields=canonical(r)))
        subset=[r for r in freight if r['客户']==client]
        if subset:
            bid=batch('upload.delivery_freight',client,subset)
            for r in subset:
                insert('input_运费',dict(client_name=client,product_code=r['货品编号'],code_type='erp_goods_code',product_name=r['货品名称'],quantity_min=r['订购盒数'],quantity_max=r['订购盒数'],freight_amount=r['订单平均运费'],charge_basis='per_order',freight_type='statistical_average',valid_from=None,import_id=bid,source_table='upload.delivery_freight',source_row_key=str(r['id']),source_fields=canonical(r)))

    runs={}
    for i,r in enumerate(catalog):
        key=(r['客户'],r['平台'],r['店铺']);store=stores[key]
        if key not in runs:runs[key]=run(key[0],key[1],store,'adjust_price.bi_sku_platform',r['写入时间'])
        rid=runs[key];code=r.get('69码');typ='barcode'
        if not code:
            code=next((str(r[k]) for k in ['平台规格ID','平台商家编码','平台商品ID'] if r.get(k)),None);typ='source_code'
        if not code:code='source_row_'+str(i);typ='source_row'
        # Preserve each source specification; IDs equal to barcode are not certified native IDs.
        native=lambda k:r.get(k) if r.get(k) and str(r[k])!=str(r.get('69码')) else None
        source_key=digest([r.get(k) for k in ['平台商品ID','平台规格ID','平台商家编码','69码','平台商品名称','平台规格名称']])
        vals=dict(client_name=key[0],platform=key[1],store_code=store,product_name=r.get('平台商品名称') or r.get('货品名称') or code,specification=r.get('平台规格名称'),product_code=code,code_type=typ,platform_product_id=native('平台商品ID'),platform_sku_id=native('平台规格ID'),merchant_code=r.get('平台商家编码'),barcode=r.get('69码'),source_item_key=source_key,package_quantity=r.get('盒数') if r.get('盒数') and r['盒数']>0 else None,price=r.get('平台售价'),stock=r.get('平台库存'),listing_status=r.get('售卖状态'),observed_at=r['写入时间'],run_id=rid,source_fields=canonical(r))
        # Exact duplicate source identities are represented once, without inventing SKU IDs.
        with conn.cursor() as c:c.execute('SELECT product_id FROM `商品` WHERE client_name=%s AND platform=%s AND store_code=%s AND source_item_key=%s',(*key[:2],store,source_key));found=c.fetchone()
        if not found:insert('商品',vals)
    for rid in runs.values():
        with conn.cursor() as c:c.execute('UPDATE `采集批次` SET row_count=(SELECT COUNT(*) FROM `商品` WHERE run_id=%s) WHERE run_id=%s',(rid,rid))

    # Reuse the verified business evidence, preserving the original observation times.
    for path in sorted(Path('/var/lib/ec-collectors/raw/meituan/mt-baomin').glob('*/*.json')):
        d=json.loads(path.read_text());windows=d.get('windows',{})
        if set(windows)!={'daily','near_30'} or 'collection_error' in d:continue
        rid=d['run_uuid']
        insert('采集批次',dict(run_id=rid,client_name='保民',platform='美团',store_code='mt-baomin',collector_code=d['collector'],source_version=d['source_version'],started_at=dt.datetime.fromisoformat(d['started_at']).replace(tzinfo=None),finished_at=dt.datetime.fromisoformat(d['finished_at']).replace(tzinfo=None),status='success',artifact_path=str(path),artifact_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),row_count=sum(len(w['metrics']) for w in windows.values()),message='已按已核验证据重新解析；历史观察时间保持不变'))
        for label,w in windows.items():
            p=w['params'];start=dt.datetime.strptime(p['startDate'],'%Y%m%d');end=dt.datetime.strptime(p['endDate'],'%Y%m%d')
            for metric,value in w['metrics'].items():
                insert('店铺经营统计',dict(client_name='保民',platform='美团',store_code='mt-baomin',business_date=end.date(),window_type='day' if label=='daily' else 'rolling_30d',window_start_at=start,window_end_at=end+dt.timedelta(days=1),metric_code=metric,metric_value=value,unit='CNY' if metric in ['amount','tirzepatide_amount'] else 'ratio' if metric in ['ring_ratio','product_turnover_rate','non_tirzepatide_ratio'] else 'sku',observed_at=dt.datetime.fromisoformat(w['observed_at']).replace(tzinfo=None),run_id=rid))
    conn.commit()
except Exception:conn.rollback();raise

with conn.cursor() as c:
    c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='ec_dw'")
    for (name,) in c.fetchall():c.execute('SELECT COUNT(*) FROM `'+name+'`');print(name,c.fetchone()[0])
    c.execute('SELECT source_table,COUNT(*),SUM(unit_cost>0),SUM(unit_cost IS NULL) FROM `input_成本` GROUP BY source_table');print('COST_AUDIT',c.fetchall())
print('Done; all source databases read-only. Legacy writers remain paused.')
