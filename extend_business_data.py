"""One-time extension requested by user; only ec_dw is mutated."""
import datetime as dt
import hashlib
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import pymysql
sys.path.insert(0,'/opt/subsidy/.ec-phase1-stage')
from migrate import admin
c=admin('ec_dw')
def data(table):
    with c.cursor(pymysql.cursors.DictCursor) as x:x.execute('SELECT * FROM '+table);return x.fetchall()
def execute(sql,args=None):
    with c.cursor() as x:x.execute(sql,args);return x.rowcount
def add(table,columns):
    with c.cursor() as x:
        x.execute('SHOW COLUMNS FROM `'+table+'`');names={r[0] for r in x.fetchall()}
    for name,typ in columns.items():
        if name not in names:execute('ALTER TABLE `'+table+'` ADD `'+name+'` '+typ)
def insert(table,d):
    return execute('INSERT INTO `'+table+'` ('+','.join('`'+k+'`' for k in d)+') VALUES ('+','.join(['%s']*len(d))+')',list(d.values()))
def update(table,d,where,params):
    return execute('UPDATE `'+table+'` SET '+','.join('`'+k+'`=%s' for k in d)+' WHERE '+where,list(d.values())+list(params))
def canon(v):return json.dumps(v,ensure_ascii=False,default=str,sort_keys=True,separators=(',',':'))
def source_key(*parts):return hashlib.sha256(canon(parts).encode()).hexdigest()
def epoch(value):
    if value in (None,'',0,'0'):return None
    try:
        n=Decimal(str(value));n=n/1000 if n>100000000000 else n
        return dt.datetime.fromtimestamp(float(n),dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)
    except (ValueError,OverflowError,ArithmeticError):return None
def clock(value):
    if not value:return None
    try:return dt.datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:return epoch(value)

# Remove store-code dimensions without touching human-readable shop fields.
execute('ALTER TABLE merchant_profit_setting DROP CHECK merchant_profit_setting_chk_1')
execute('ALTER TABLE merchant_profit_setting DROP INDEX uq_profit_version, DROP COLUMN 店铺作用域, DROP COLUMN 店铺编码, ADD UNIQUE KEY uq_profit_version(客户,平台,生效日期)')
for table in ['ops_collect_run','ops_import_run','subsidy']:execute('ALTER TABLE `'+table+'` DROP COLUMN 店铺编码')
add('ops_collect_run',{'客户':'VARCHAR(64) NULL'})
execute("UPDATE ops_collect_run SET 客户='保民' WHERE 平台='美团' AND (阶段='meituan.product_analysis.v2')")
execute('ALTER TABLE subsidy RENAME COLUMN 来源日期 TO 修改日期')

add('esale_product',{'规格':'VARCHAR(500) NULL','平台商品ID':'VARCHAR(128) NULL','平台规格ID':'VARCHAR(128) NULL','商家编码':'VARCHAR(128) NULL','条码':'VARCHAR(128) NULL','编码类型':'VARCHAR(32) NULL','组合盒数':'DECIMAL(18,6) NULL','包装单位':'VARCHAR(32) NULL','销售属性':'VARCHAR(500) NULL','品牌':'VARCHAR(255) NULL','售卖状态':'VARCHAR(64) NULL','商品总库存':'DECIMAL(18,4) NULL','商品可用库存':'DECIMAL(18,4) NULL','商品信息修改日期':'DATE NULL','信息说明':'VARCHAR(255) NULL','上架日期原值':'VARCHAR(64) NULL','原月销量':'DECIMAL(18,4) NULL'})
add('esale_system',{'时间窗口':'VARCHAR(32) NOT NULL DEFAULT \'原面板\'','统计开始时间':'DATETIME NULL','统计结束时间':'DATETIME NULL','成交金额':'DECIMAL(18,2) NULL','成交订单量':'INT NULL','单均价':'DECIMAL(18,2) NULL','退款金额':'DECIMAL(18,2) NULL','退款订单量(全部退款+部分退款)':'INT NULL','退款订单量(全部退款)':'INT NULL','修改日期':'DATETIME NULL'})
execute('ALTER TABLE esale_system DROP PRIMARY KEY,ADD PRIMARY KEY(客户,平台,日期,时间窗口)')
add('aftersale_case',{'订单号':'VARCHAR(128) NULL','平台SKU':'VARCHAR(128) NULL','条码':'VARCHAR(128) NULL','买家实付':'DECIMAL(18,2) NULL','纠纷类型':'VARCHAR(64) NULL','退款状态':'VARCHAR(128) NULL','客服状态':'VARCHAR(64) NULL','协商状态':'VARCHAR(64) NULL','审核状态':'VARCHAR(64) NULL','首次入库时间':'DATETIME NULL','修改日期':'DATETIME NULL'})
add('subsidy',{'记录类型':'VARCHAR(32) NOT NULL DEFAULT \'美团补贴\'','来源表':'VARCHAR(128) NULL','报名状态':'VARCHAR(64) NULL','活动状态':'VARCHAR(64) NULL','是否补贴':'VARCHAR(32) NULL','补贴方式':'VARCHAR(64) NULL','招商价':'DECIMAL(18,4) NULL','服务费原值':'VARCHAR(128) NULL','包装档位':'VARCHAR(128) NULL','活动盒数':'DECIMAL(18,6) NULL','平台商品ID':'VARCHAR(128) NULL','平台SKU':'VARCHAR(128) NULL','报价单ID':'VARCHAR(128) NULL','报价ID':'VARCHAR(128) NULL','报价':'DECIMAL(18,4) NULL','竞价最低价':'DECIMAL(18,4) NULL','报价状态':'VARCHAR(64) NULL','报价状态说明':'VARCHAR(255) NULL','自动报价':'BOOLEAN NULL','自动跟价':'BOOLEAN NULL','报名开始时间':'DATETIME NULL','报名截止时间':'DATETIME NULL','报价生效时间':'DATETIME NULL','报价结束时间':'DATETIME NULL'})
execute('ALTER TABLE subsidy MODIFY 客户 VARCHAR(64) NULL,MODIFY 补贴状态 VARCHAR(64) NULL,MODIFY 采集批次ID CHAR(36) NULL,MODIFY 商品名称 VARCHAR(1000) NULL,MODIFY 规格 VARCHAR(500) NULL')
execute('ALTER TABLE subsidy ADD UNIQUE KEY uq_subsidy_source(来源表,源记录键)')
execute('''CREATE TABLE order_status (
 客户 VARCHAR(64) NOT NULL,平台 VARCHAR(32) NOT NULL,日期 DATE NOT NULL,统计类型 VARCHAR(16) NOT NULL,
 统计时点 DATETIME NOT NULL,待付款 INT NULL,待出库 INT NULL,已出库 INT NULL,暂停订单 INT NULL,锁定订单 INT NULL,合计 INT NULL,
 修改日期 DATETIME NULL,PRIMARY KEY(客户,平台,日期,统计类型,统计时点)
 ) CHARACTER SET utf8mb4 COMMENT='按客户平台统计的订单状态，保留日和小时粒度' ''')
execute('''CREATE TABLE top3_and_lowestprice (
 记录ID BIGINT AUTO_INCREMENT PRIMARY KEY,客户 VARCHAR(64) NULL,平台 VARCHAR(32) NOT NULL,
 商品编码 VARCHAR(128) NOT NULL,商品名称 VARCHAR(500) NULL,Top3参考价 DECIMAL(18,4) NULL,最低售价 DECIMAL(18,4) NULL,
 价格来源 VARCHAR(128) NULL,观察范围 VARCHAR(255) NULL,首次观测日期 DATE NULL,修改日期 DATETIME NULL,
 时间精度 VARCHAR(16) NULL,来源表 VARCHAR(128) NOT NULL,源记录键 VARCHAR(191) NOT NULL,
 UNIQUE KEY uq_price_source(来源表,源记录键),KEY idx_price(客户,平台,商品编码)
 ) CHARACTER SET utf8mb4 COMMENT='Top3和最低价观测；不将未记录观察账号的价格认定为全平台通用' ''')

# Cache sources and then publish data changes together.
erp=data('adjust_price.bi_sku_platform');jd=data('adjust_price.jd_store_sku_normalized_daily');tmall=data('adjust_price.tmall_on_sale_daily')
bycode=defaultdict(list)
for r in erp:
    for code in {str(r.get(k)) for k in ['69码','平台商品ID','平台规格ID','平台商家编码'] if r.get(k)}:bycode[(r['客户'],r['平台'],code)].append(r)
jd_latest={}
for r in jd:
    k=str(r['SKUID'])
    if k not in jd_latest or r['记录日期']>jd_latest[k]['记录日期']:jd_latest[k]=r
products=data('esale_product');known={(r['客户'],r['平台'],r['唯一编码']):r for r in products}
counts={};c.autocommit(False)
try:
    enriched=0
    for p in products:
        key=(p['客户'],p['平台'],p['唯一编码']);candidates=bycode.get(key,[]);d={}
        fields={'规格':'平台规格名称','平台商品ID':'平台商品ID','平台规格ID':'平台规格ID','商家编码':'平台商家编码','条码':'69码','组合盒数':'盒数','售卖状态':'售卖状态'}
        for target,source in fields.items():
            vals={r.get(source) for r in candidates if r.get(source) is not None and r.get(source)!=''}
            if len(vals)==1:
                val=next(iter(vals))
                if target in ['平台商品ID','平台规格ID'] and all(str(val)==str(r.get('69码')) for r in candidates):continue
                if target=='组合盒数' and val<=0:continue
                d[target]=val
        if candidates:d['商品信息修改日期']=max(r['数据日期'] for r in candidates);d['信息说明']='ERP导出补充信息；跨店冲突字段保留空值'
        if d.get('条码')==p['唯一编码']:d['编码类型']='条码'
        elif d.get('平台规格ID')==p['唯一编码']:d['编码类型']='平台规格ID'
        elif d.get('平台商品ID')==p['唯一编码']:d['编码类型']='平台商品ID'
        j=jd_latest.get(p['唯一编码']) if p['客户']=='保民' and p['平台']=='京东' else None
        if j:
            d.update(平台规格ID=str(j['SKUID']),商家编码=j['商家SKU原值'],条码=j['基础UPC'],销售属性=j['销售属性'],包装单位=j['包装单位'],品牌=j['品牌'],售卖状态=j['商品状态'],商品总库存=j['商品总库存'],商品可用库存=j['商品可用库存'],编码类型='平台规格ID',商品信息修改日期=j['记录日期'])
            if j.get('组合数量') and j['组合数量']>0:d['组合盒数']=j['组合数量']
        if d:enriched+=update('esale_product',d,'客户=%s AND 平台=%s AND 唯一编码=%s',key)
    counts['商品补充']=enriched

    # Tmall listings have an explicit shop/client label. Resolve only unique labels.
    pairs={(r['客户'],r['平台'],r['店铺']) for r in erp}
    tm_latest={}
    for r in tmall:
        k=(r['店铺'],r['item_id'])
        if k not in tm_latest or r['记录日期']>tm_latest[k]['记录日期']:tm_latest[k]=r
    for r in tm_latest.values():
        clients={client for client,platform,shop in pairs if platform=='天猫' and r['店铺'] in (client,shop)}
        if len(clients)!=1:continue
        client=next(iter(clients));item=str(r['item_id'])
        with c.cursor(pymysql.cursors.DictCursor) as x:
            x.execute("SELECT 客户,平台,唯一编码 FROM esale_product WHERE 客户=%s AND 平台='天猫' AND (唯一编码=%s OR 平台商品ID=%s)",(client,item,item));matches=x.fetchall()
        d={'上架日期原值':r['up_shelf_date'],'原月销量':r['monthly_sold_quantity'],'平台商品ID':item,'售卖状态':r['shelf_status'] or r['item_status'],'商品信息修改日期':r['记录日期']}
        if len(matches)==1:update('esale_product',d,'客户=%s AND 平台=%s AND 唯一编码=%s',list(matches[0].values()))
        elif not matches:insert('esale_product',dict(d,客户=client,平台='天猫',商品=r['title'][:255],唯一编码=item,编码类型='平台商品ID',价格=r['current_price'],库存=r['stock'],更新日期=r['记录日期']))

    # Source report has a successful state file with a collection timestamp.
    statepath=Path('/var/lib/subsidy-sync/meituan-business-report-status.json');raw=statepath.read_bytes();state=json.loads(raw)
    report=data('adjust_price.ops_business_report')
    if state.get('state')=='success' and raw==statepath.read_bytes():
        captured=clock(state['collected_at']);day=captured.date()
        for r in report:
            window=r['时间'];start=dt.datetime.combine(day,dt.time());end=captured;business=day
            if window in ('按日','近7天'):
                end=start;start-=dt.timedelta(days=1 if window=='按日' else 7);business=day-dt.timedelta(days=1)
            insert('esale_system',dict(客户='保民',平台='美团',日期=business,时间窗口=window,统计开始时间=start,统计结束时间=end,修改日期=captured,**{k:v for k,v in r.items() if k!='时间'}))
        counts['经营报表']=len(report)

    for table,kind in [('jd_order_status_daily','日'),('jd_order_status_hourly','小时')]:
        records=data('adjust_price.'+table)
        for r in records:
            insert('order_status',dict(客户=r['店铺'],平台='京东',日期=r['记录日期'],统计类型=kind,统计时点=r.get('小时') or r['采集时间'] or dt.datetime.combine(r['记录日期'],dt.time()),修改日期=r['采集时间'],**{k:r[k] for k in ['待付款','待出库','已出库','暂停订单','锁定订单','合计']}))
        counts[table]=len(records)

    for r in data('adjust_price.mt_Top3LowestDiscountPrice'):
        insert('top3_and_lowestprice',{'客户':None,'平台':'美团','商品编码':r['upc'],'Top3参考价':r['Top3LowestDiscountPrice'],'修改日期':r['updated_at'],'时间精度':'时分秒','价格来源':'同品Top3观测','观察范围':'原表未保存观察账号，不认定全平台通用','来源表':'adjust_price.mt_Top3LowestDiscountPrice','源记录键':r['upc']})
    for r in data('adjust_price.mt_subsidy_lowest_price'):
        insert('top3_and_lowestprice',{'客户':r['店铺'],'平台':'美团','商品编码':r['upc'],'商品名称':r['sku_name'],'最低售价':r['最低售价'],'首次观测日期':r['首次观测日期'],'修改日期':r['最近观测日期'],'时间精度':'日','价格来源':r['价格来源'],'观察范围':'源店铺：'+r['店铺'],'来源表':'adjust_price.mt_subsidy_lowest_price','源记录键':source_key(r['店铺'],r['upc'])})

    for r in data('adjust_price.tmall_after_sale_all'):
        d={'订单号':r['订单编号'],'平台SKU':r['SKU'],'条码':r['条码'],'买家实付':r['买家实付'],'纠纷类型':r['纠纷类型'],'退款状态':r['退款状态'],'客服状态':r['客服状态'],'协商状态':r['协商状态'],'审核状态':r['审核状态'],'售后审核结果':r['审核状态'],'首次入库时间':r['首次入库时间'],'修改日期':r['最近采集时间']}
        update('aftersale_case',d,"客户=%s AND 平台='天猫' AND 售后单号=%s",(r['店铺'],r['售后单号']))

    # Activity directory rows have no reliable client identity: retain NULL.
    for r in data('adjust_price.tmall_activity_available_daily'):
        d={'客户':None,'平台':'天猫','记录类型':'天猫活动池','来源表':'adjust_price.tmall_activity_available_daily','源记录键':str(r['id']),'活动ID':r['活动ID'],'活动名称':r['活动商品名'] or r['活动类型'],'商品名称':r['商品标题'] or r['通用名'],'规格':r['规格'],'报名状态':r['报名状态'],'活动状态':r['活动状态'],'是否补贴':r['是否补贴'],'补贴方式':r['补贴方式'],'招商价':r['招商价格'],'服务费原值':r['服务费'],'包装档位':r['包装档位'],'平台商品ID':r['天猫商品ID'],'平台SKU':r['skuId'],'活动开始时间':clock(r['活动开始']),'活动结束时间':clock(r['活动结束']),'观察时间':r['updated_at'],'修改日期':r['记录日期'],'来源原始字段':canon(r)}
        insert('subsidy',d)
    for r in data('adjust_price.tmall_activity_sku_daily'):
        insert('subsidy',{'客户':None,'平台':'天猫','记录类型':'天猫活动规格','来源表':'adjust_price.tmall_activity_sku_daily','源记录键':source_key(r['记录日期'],r['活动ID'],r['包装档位'],r['天猫商品ID'],r['平台SKU ID']),'活动ID':r['活动ID'],'商品名称':r['通用名'],'规格':r['规格'],'平台商品ID':r['天猫商品ID'],'平台SKU':r['平台SKU ID'],'商品编码':r['商品upc'],'编码类型':'条码' if r['商品upc'] else None,'包装档位':r['包装档位'],'活动盒数':r['活动盒数'],'招商价':r['招商价格'],'修改日期':r['记录日期'],'来源原始字段':canon(r)})
    for r in data('adjust_price.jd_index_promo_activity_daily'):
        insert('subsidy',{'客户':r['店铺'],'平台':'京东','记录类型':'京东指数活动','来源表':'adjust_price.jd_index_promo_activity_daily','源记录键':str(r['id']),'活动ID':r['activityId'],'活动名称':r['activityName'],'报名状态':None if r['signed'] is None else str(r['signed']),'活动状态':None if r['activityStatus'] is None else str(r['activityStatus']),'报价状态':None if r['bidStatus'] is None else str(r['bidStatus']),'报名开始时间':epoch(r['submitStartTime']),'报名截止时间':epoch(r['submitEndTime']),'活动开始时间':epoch(r['activityStartTime']),'活动结束时间':epoch(r['activityEndTime']),'修改日期':r['记录日期'],'观察时间':r['updated_at'],'来源原始字段':canon(r)})
    for r in data('adjust_price.jd_index_quotation_daily'):
        insert('subsidy',{'客户':r['店铺'],'平台':'京东','记录类型':'京东指数报价','来源表':'adjust_price.jd_index_quotation_daily','源记录键':str(r['id']),'活动ID':r['activityId'],'商品名称':r['skuName'],'平台SKU':r['skuId'],'报价单ID':r['bidSheetId'],'报价ID':r['id_报价'],'报价':r['quotation'],'竞价最低价':r['竞价最低价'],'报价状态':None if r['status'] is None else str(r['status']),'报价状态说明':r['statusReason'],'自动报价':r['autoQuotation'],'自动跟价':r['autoFollowPrice'],'报价生效时间':epoch(r['validTime']),'报价结束时间':epoch(r['endTime']),'观察时间':r['updated_at'],'修改日期':r['记录日期'],'来源原始字段':canon(r)})
    c.commit()
except Exception:c.rollback();raise
print('COUNTS',counts)
with c.cursor() as x:
    x.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='ec_dw' ORDER BY table_name");names=[r[0] for r in x.fetchall()];ddl=[]
    for table in names:
        x.execute('SHOW CREATE TABLE `'+table+'`');ddl.append(x.fetchone()[1]+';')
        x.execute('SELECT COUNT(*) FROM `'+table+'`');print(table,x.fetchone()[0])
    Path('/opt/subsidy/schema-v2.sql').write_text('SET NAMES utf8mb4;\n\n'+'\n\n'.join(ddl))
print('Source databases and prescription project unchanged; automatic writer entrypoints remain paused')
