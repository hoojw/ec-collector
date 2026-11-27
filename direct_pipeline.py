#!/usr/bin/env python3
"""Browser readers -> validated current ec_dw models. No legacy DB writes."""
import argparse
import datetime as dt
import hashlib
import importlib
import json
import re
import sys
import subprocess
import time
import uuid
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import pymysql

TZ=ZoneInfo('Asia/Shanghai')
ERROR_ZH={
    'JD store home did not finish loading':'京东商家后台首页未加载完成',
    'JD store home failed after 3 attempts':'京东商家后台首页 3 次尝试后仍失败',
    'Meituan after-sales page is not logged in':'美团售后页未登录',
    'Meituan after-sales template capture failed after 2 targets':'美团售后模板捕获在两个目标页均失败',
    'Incomplete JD index activity/quotation pages':'京东指数活动/报价分页不完整',
    'Incomplete JD index pages':'京东指数分页不完整',
    '; previous data preserved':'，已保留原有数据',
    'Core business window unavailable':'核心经营窗口数据不可用',
    'JD after-sales page 2 returned no response':'京东售后第 2 页无响应',
    'JD after-sales list request was not observed on first load':'首次加载未捕获京东售后列表请求',
    'JD product-detail response body never became available':'京东商品详情响应体未返回',
    'Incomplete order status tabs':'订单状态页签计数不完整',
    "KeyError: 'poi_id'":'店铺配置缺少 poi_id',
    'Empty product result not published; retry later':'商品结果为空未发布，需稍后重试',
    'Validation interrupted before publishing; restarted using verified existing SKU cache':'发布前校验中断，已改用已核验的 SKU 缓存重跑',
}
def describe(error):
    text=str(error)
    if type(error).__name__=='KeyError' and 'poi_id' in text:text='店铺配置缺少 poi_id'
    for en,zh in ERROR_ZH.items():text=text.replace(en,zh)
    if str(type(error).__name__)=='IntegrityError' and 'Duplicate entry' in text:text='售后节点重复写入触发主键冲突，整批未写入，请重跑'
    return (type(error).__name__+'：'+text)[:950]
TABLES={'esale_system','esale_product','aftersale_case','aftersale_node','aftersale_status_snapshot','order_status','subsidy','top3_and_lowestprice'}
def now():return dt.datetime.now(TZ).replace(tzinfo=None,microsecond=0)
def canonical(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':'))
def sha(v):return hashlib.sha256(canonical(v).encode()).hexdigest()
def subsidy_identity(row):
    fields=['poi_id','数据日期','sku_id','upc','活动名称','活动类型','门槛盒数','活动开始','活动结束','活动时间段','活动周期']
    return sha([row.get(k) for k in fields])

def validate_amounts(row):
    for value in row.values():
        if isinstance(value,Decimal) and not value.is_finite():raise ValueError('Non-finite metric')

def scope_settings(config):
    base=dict(config.get('store') or {})
    platform=base.get('platform') or (config.get('meituan_store') or config.get('jd_store') or {}).get('platform')
    specific=config.get('meituan_store' if platform=='美团' else 'jd_store') or {}
    for field in ['client','platform']:
        if base.get(field) and specific.get(field) and base[field]!=specific[field]:raise ValueError('Conflicting explicit '+field+' identity')
    base.update({k:v for k,v in specific.items() if v is not None and v!=''})
    if base.get('wm_poi_id'):base['poi_id']=str(base['wm_poi_id'])
    return base

class Writer:
    def __init__(self,conn,client,platform):self.conn=conn;self.client=client;self.platform=platform;self.rows=0;self.run_uuid=str(uuid.uuid4());self.after_commit=[]
    def upsert(self,table,row,keys):
        if table not in TABLES:raise ValueError('Unsupported table')
        row={k:v for k,v in row.items() if v is not None};validate_amounts(row)
        if not all(k in row for k in keys):raise ValueError('Missing business identity')
        columns=list(row);updates=[k for k in columns if k not in keys]
        sql='INSERT INTO `'+table+'` ('+','.join('`'+k+'`' for k in columns)+') VALUES ('+','.join(['%s']*len(columns))+')'
        if updates:sql+=' ON DUPLICATE KEY UPDATE '+','.join('`'+k+'`=VALUES(`'+k+'`)' for k in updates)
        else:sql+=' ON DUPLICATE KEY UPDATE `'+keys[0]+'`=VALUES(`'+keys[0]+'`)'
        with self.conn.cursor() as c:c.execute(sql,list(row.values()))
        self.rows+=1
    def panel(self,values,window='原面板',day=None):
        row=dict(客户=self.client,平台=self.platform,日期=day or now().date(),时间窗口=window,修改日期=now());row.update(values)
        self.upsert('esale_system',row,['客户','平台','日期','时间窗口'])
    def product(self,records):
        # Identity includes original native product/SKU identifiers. A repeated
        # barcode is not sufficient to collapse different sale specifications.
        counts=Counter(r.get('barcode') for r in records if r.get('barcode'))
        with self.conn.cursor(pymysql.cursors.DictCursor) as c:
            c.execute('SELECT 唯一编码,平台商品ID,平台规格ID,商品粒度 FROM esale_product WHERE 客户=%s AND 平台=%s',(self.client,self.platform));existing=c.fetchall()
        native_candidates={}
        for r in existing:
            kind=r['商品粒度'] or ('sku' if r['平台规格ID'] else 'spu')
            value=r['平台规格ID'] if kind=='sku' else r['平台商品ID']
            if value:native_candidates.setdefault((kind,str(value)),[]).append(r['唯一编码'])
        bynative={key:values[0] for key,values in native_candidates.items() if len(values)==1}
        bycode={r['唯一编码']:r for r in existing}
        seen=set()
        for row in records:
            native=str(row.get('native') or row['code']);barcode=row.get('barcode');kind=row.get('native_kind','spu');code=bynative.get((kind,native))
            if code is None:
                code=barcode if barcode and counts[barcode]==1 else native
                old=bycode.get(code)
                if old and any(str(old[k])!=native for k in ['平台商品ID','平台规格ID'] if old[k]):code=native
            if code in seen:raise ValueError('Ambiguous product identity; refusing overwrite')
            seen.add(code)
            values=dict(客户=self.client,平台=self.platform,唯一编码=code,商品=str(row['name'])[:255],更新日期=now().date(),商品信息修改日期=now().date(),**row.get('extra',{}))
            for source,column in [('price','价格'),('stock','库存'),('quantity','销量'),('quantity_30','30天销量'),('amount','销售额')]:
                if source in row:values[column]=row[source]
            if barcode:values['条码']=barcode
            values['编码类型']='条码' if code==barcode else '平台原生标识'
            values['商品粒度']=kind
            values.setdefault('平台商品ID',native)
            self.upsert('esale_product',values,['客户','平台','唯一编码'])
    def cases(self,records,nodes,shop):
        for r in records:
            d=dict(客户=self.client,平台=self.platform,店铺=shop,日期=r['apply_date'],售后单号=r['after_sale_id'],售后状态=r['after_sale_status'],修改日期=now())
            for a,b in {'product_info':'商品信息','refund_amount':'退款金额','after_sale_type':'售后类型','shipment_status':'发货状态','refund_type':'退款类型','apply_reason':'申请原因','apply_time':'申请时间','audit_result':'售后审核结果','merchant_audit_time':'商家审核时间','system_audit_time':'系统审核时间','order_id':'订单号','auto_refund_deadline':'自动退款截止时间','auto_refund_text':'自动退款说明原值'}.items():
                if a in r:d[b]=r[a]
            self.upsert('aftersale_case',d,['客户','平台','店铺','日期','售后单号'])
        for identity,actor,action,at in nodes:
            self.upsert('aftersale_node',dict(客户=self.client,平台=self.platform,店铺=shop,售后单号=identity,节点主体=actor,节点操作=action,节点时间=at),['客户','平台','店铺','售后单号','节点主体','节点操作','节点时间'])

def analysis(w,config,port):
    if w.platform=='美团':
        import meituan_product_analysis as source
        metrics=source.collect(dt.datetime.now(TZ),port,str((config.get('store') or config.get('meituan_store'))['poi_id']))
        daily=metrics['daily'];near=metrics['near_30']
        d={'昨日交易额':daily['amount'],'昨日替尔泊肽销售额':daily['tirzepatide_amount'],'近30天交易额':near['amount'],'近30天替尔泊肽销售额':near['tirzepatide_amount'],'商品动销率':near['product_turnover_rate'],'经营商品数量':near['average_on_sale_skus'],'近30日环比':near['ring_ratio'],'日环比平台':daily['ring_ratio'],'非替尔泊肽销售金额占比':near['non_tirzepatide_ratio']}
        if near['average_on_sale_skus'] is not None and near['average_sold_skus'] is not None:d['未动销商品数量']=near['average_on_sale_skus']-near['average_sold_skus']
    else:
        import jd_product360_analysis as source
        try:metrics=source.collect(port)
        except source.NoPermissionError:return 'no_permission'
        daily=metrics['daily'];near=metrics['near_30']
        d={'昨日交易额':daily['transaction_amount'],'近30天交易额':near['transaction_amount'],'近30天替尔泊肽销售额':near.get('tirzepatide_amount'),'近30日环比':near.get('transaction_amount_ring_ratio'),'商品动销率':near.get('turnover_rate'),'经营商品数量':near.get('listed_spu_count')}
    if d.get('昨日交易额') is None or d.get('近30天交易额') is None:
        if any(value is not None for value in d.values()):w.panel(d)
        return 'partial:源未同时返回昨日与近30天交易额；缺失值保留为未知，未以零填充'
    w.panel(d)

def report(w,config,port):
    import meituan_business_report as m
    t=dt.datetime.now(TZ);page=m.CdpPage(port);results=[]
    try:
        page.open_report()
        for period in m.report_periods(t):
            params={'businessType':2,'storeType':1,**{k:v for k,v in period.items() if k!='label'}}
            results.append(m.normalize_report(period['label'],page.fetch_json(m.CORE_PATH,params)))
    finally:page.close()
    for label,amount,count,average,refund,all_refund,full_refund in results:
        end=now();start=end.replace(hour=0,minute=0,second=0);day=end.date()
        if label in ['按日','近7天']:end=start;start-=dt.timedelta(days=1 if label=='按日' else 7);day=end.date()-dt.timedelta(days=1)
        w.panel({'成交金额':amount,'成交订单量':count,'单均价':average,'退款金额':refund,'退款订单量(全部退款+部分退款)':all_refund,'退款订单量(全部退款)':full_refund,'统计开始时间':start,'统计结束时间':end},label,day)

def sales(w,config,port):
    if w.platform=='美团':
        import meituan_product_sales_collect as m
        collected=m.collect(port,'d7');records=[]
        for raw,shaped in zip(collected['rows'],m.sales_rows(collected)):
            shaped.update(native=raw['sku_code'],barcode=raw['upc_code']);records.append(shaped)
    else:
        import jd_product_sales_collect as m
        try:dimensions={key:m.collect(port,key) for key in ['d7','d30']}
        except m.NoPermissionError:return 'no_permission'
        except RuntimeError as error:
            if not any(s in str(error) for s in ['response body never became available','never issued its own','No resource with given identifier']):raise
            time.sleep(2)
            try:dimensions={key:m.collect(port,key) for key in ['d7','d30']}
            except m.NoPermissionError:return 'no_permission'
        records=m.merge_dimensions({key:m.sales_rows(value) for key,value in dimensions.items()})
        for row in records:row['native']=row['code']
    if not records:
        # Native readers validate pagination totals. Confirm a cold-start zero
        # twice before recording a legitimate no-sales window; never clear the
        # entire client/platform merely because one account has no sales.
        time.sleep(2)
        if w.platform=='美团':
            second=m.collect(port,'d7')
            if second['window']!=collected['window']:raise ValueError('Sales window changed during confirmation')
            records=m.sales_rows(second)
            for raw,shaped in zip(second['rows'],records):shaped.update(native=raw['sku_code'],barcode=raw['upc_code'])
        else:
            repeated={key:m.collect(port,key) for key in ['d7','d30']}
            records=m.merge_dimensions({key:m.sales_rows(value) for key,value in repeated.items()})
            for row in records:row['native']=row['code']
        if not records:return 'empty_confirmed'
    w.product(records)

def listed(w,config,port):
    import meituan_listed_products_collect as m
    collected=m.collect(port,config);records=[]
    for raw,shaped in zip(collected['rows'],m.sales_rows(collected)):
        shaped.update(native=raw['spu_id'],barcode=raw['upc_code'],extra={'售卖状态':str(raw['sell_status']) if raw['sell_status'] is not None else None})
        records.append(shaped)
    if not records:raise ValueError('Empty listing result not published')
    w.product(records)

def top3(w,config,port):
    import meituan_top3_lowest_discount_price as m
    page=m.CdpPage(port,m.TARGET_URL)
    try:records=m.collect_rows(page,m.compare_date_for(dt.datetime.now(TZ)),20)
    finally:page.close()
    for code,price in records:
        w.upsert('top3_and_lowestprice',{'客户':w.client,'平台':w.platform,'商品编码':code,'Top3参考价':price,'价格来源':'同品Top3观测','观察范围':'本客户已登录电商账号','时间精度':'时分秒','修改日期':now(),'来源表':'direct.top3','源记录键':sha([w.client,(config.get('store') or {}).get('poi_id'),code])},['来源表','源记录键'])

def subsidy(w,config,port):
    import meituan_subsidy_collect as m
    page=m.CdpPage(port);datasets={};day=now().date().isoformat();scope=config.get('store') or config.get('meituan_store')
    try:
        for name in m.DATASETS:datasets[name]=m.collect_dataset(page,name,w.client,day,100,100)
    finally:page.close()
    status={'subsidized':'已补贴','unsubsidized':'未补贴','unlisted':'未建品'}
    for kind,records in datasets.items():
        for r in records:
            if r.get('poi_id') and str(r['poi_id'])!=str(scope['poi_id']):raise ValueError('Subsidy source store mismatch')
            d={'客户':w.client,'平台':w.platform,'记录类型':'美团补贴','来源表':'direct.mt.'+kind,'源记录键':subsidy_identity(r),'商品编码':r.get('upc') or r.get('sku_id'),'编码类型':'条码' if r.get('upc') else '平台SKU','商品名称':r.get('sku_name'),'补贴状态':r.get('补贴状态') or status[kind],'活动名称':r.get('活动名称'),'原因':r.get('未补贴原因'),'修改日期':day,'观察时间':now(),'采集批次ID':w.run_uuid,'来源原始字段':canonical(r)}
            for a,b in {'原价':'原价','当前售价':'当前售价','我的价格':'当前售价','补贴门槛':'门槛价格','建议定价':'建议售价','补贴后价':'补贴后价','单件补贴':'补贴金额','商家承担':'商家承担','门槛盒数':'门槛数量','活动开始':'活动开始时间','活动结束':'活动结束时间'}.items():
                if a in r:d[b]=r[a]
            w.upsert('subsidy',d,['来源表','源记录键'])
    # Current observed minimum is scoped to this client and native account.
    for observation in m.price_observations(datasets,day,w.client):
        code=observation.get('upc');price=observation['最低售价']
        if not code:continue
        if price<0:raise ValueError('Negative observed selling price')
        key=sha([w.client,scope['poi_id'],code]);source='direct.lowest'
        with w.conn.cursor() as c:
            c.execute('SELECT 最低售价,价格来源,首次观测日期 FROM top3_and_lowestprice WHERE 来源表=%s AND 源记录键=%s',(source,key));old=c.fetchone()
        lower=not old or old[0] is None or price<old[0]
        w.upsert('top3_and_lowestprice',{'客户':w.client,'平台':w.platform,'商品编码':code,'商品名称':observation.get('sku_name'),'最低售价':price if lower else old[0],'首次观测日期':old[2] if old and old[2] else now().date(),'修改日期':now(),'时间精度':'时分秒','价格来源':observation['价格来源'] if lower else old[1],'观察范围':'本客户已登录电商账号，沿用原售价观测口径','来源表':source,'源记录键':key},['来源表','源记录键'])


def automatic_refund_fields(item,detail_payload):
    order=item.get('refundOrder') or {}
    detail=((detail_payload or {}).get('data') or {}).get('refundDetailView') or {}
    countdown=detail.get('countDown') or order.get('countDown') or {}
    text=str(countdown.get('countDownText') or '')
    deadline=detail.get('processDeadline') or order.get('processDeadline')
    result={'auto_refund_deadline':None,'auto_refund_text':text[:128] or None}
    # Only accept a platform deadline explicitly labelled as automatic refund.
    if '自动退款' not in text:return result
    try:
        stamp=float(deadline)
        if stamp>1e12:stamp/=1000
        value=dt.datetime.fromtimestamp(stamp,TZ).replace(tzinfo=None,microsecond=0)
        if 2000<=value.year<=2100:result['auto_refund_deadline']=value
    except (ValueError,TypeError,OverflowError,OSError):pass
    return result

def aftersales(w,config,port):
    scope=config.get('store') or config.get('meituan_store') or config.get('jd_store');shop=config['_shop']
    records=[];nodes=[]
    if w.platform=='美团':
        import meituan_after_sales_pending as m
        page,listing,count=m.open_page_with_templates(port)
        try:
            overview=page.fetch_json(count['url'],count['method'],count['body']).get('data') or {}
            pending=int(overview['pendingReviewCount']);imminent=int(overview['autoRefundCount'])
            items,day=m.collect_window(page,listing,7)
            for item in items:
                order=item.get('refundOrder') or {};identity=str(order.get('refundViewId') or '');oid=str(order.get('orderViewId') or order.get('orderId') or '')
                if not identity or not oid:raise ValueError('Missing aftersale identity')
                detail=page.fetch_json(m.build_detail_url(listing['url'],oid,identity),'GET')
                record,events=m.normalize_order(item,detail);record['order_id']=oid;record.update(automatic_refund_fields(item,detail));records.append(record);nodes.extend(events);time.sleep(0.25)
        finally:page.close()
        w.upsert('aftersale_status_snapshot',{'客户':w.client,'平台':w.platform,'日期':day,'待商家审核数量':pending,'即将自动退售后单':imminent},['客户','平台','日期'])
    else:
        import jd_after_sales_pending as m
        page=m.CdpPage(port)
        try:items,total=m.collect_pages(page)
        finally:page.close()
        records=[m.normalize_row(item) for item in items]
    if len({(r['apply_date'],r['after_sale_id']) for r in records})!=len(records):raise ValueError('Duplicate aftersale response')
    w.cases(records,list(dict.fromkeys(nodes)),shop)

def dashboard(w,config,port):
    import jd_store_dashboard as m
    home=m.collect_home(port)
    values={}
    for source,target in {'transaction_amount':'成交金额','transaction_orders':'成交单量','transaction_customers':'成交客户数','store_visitors':'店铺访客数','store_views':'店铺浏览量','transaction_product_units':'成交商品件数'}.items():
        if source in home:values[target]=home[source]
    # Keep original structured home payload for diagnostics without guessing keys.
    allowed=set(['official_shop_name','store_star','price_risks'])
    allowed.update(k for k in home if k.startswith(('transaction_','store_visitors','store_views')))
    values['经营原始指标']=canonical({k:v for k,v in home.items() if k in allowed})
    values['店铺星级']=home.get('store_star');values['价格风险']=home.get('price_risks')
    w.panel(values,'实时')
    pending=home.get('pending_after_sales');receiving=home.get('receiving_after_sales')
    if pending is not None:w.upsert('aftersale_status_snapshot',{'客户':w.client,'平台':w.platform,'日期':now().date(),'待商家审核数量':pending},['客户','平台','日期'])

def orders(w,config,port):
    import jd_order_status_collect as m
    url=m.open_tab(port,m.ORDER_URL,ready_len=1500);counts=total=None
    for attempt in range(10):
        text=m.evaluate(url,"document.body?document.body.innerText:''") or ''
        counts,raw,total=parse_order_counts(text,m.TABS)
        if all(counts.get(k) is not None for k in m.TABS):break
        time.sleep(3)
    if not raw:raise ValueError('Order status tabs unavailable; preserving last snapshot')
    complete=all(counts.get(k) is not None for k in m.TABS)
    t=now();hour=t.replace(minute=0,second=0)
    for kind,at in [('日',t.replace(hour=0,minute=0,second=0)),('小时',hour)]:
        keys=(w.client,w.platform,t.date(),kind,at)
        with w.conn.cursor() as c:
            c.execute('SELECT 数据质量 FROM order_status WHERE 客户=%s AND 平台=%s AND 日期=%s AND 统计类型=%s AND 统计时点=%s',keys);old=c.fetchone()
        if not complete and old and old[0]=='完整':continue
        w.upsert('order_status',dict(客户=w.client,平台=w.platform,日期=t.date(),统计类型=kind,统计时点=at,修改日期=t,查询总数=total,数据质量='完整' if complete else '部分计数或平台上限',计数原值=canonical(raw),**counts),['客户','平台','日期','统计类型','统计时点'])
        # A capped display such as 999+ is not an exact 999. Unknowns stay NULL.
        missing=[k for k in m.TABS if counts[k] is None]
        if missing:
            with w.conn.cursor() as c:c.execute('UPDATE order_status SET '+','.join('`'+k+'`=NULL' for k in missing)+' WHERE 客户=%s AND 平台=%s AND 日期=%s AND 统计类型=%s AND 统计时点=%s',keys)
    return None if complete else 'partial'

def parse_order_counts(text,tabs):
    counts={};raw={}
    for name in tabs:
        match=re.search(re.escape(name)+r'\s*[（(]\s*([\d,，]+)\s*(\+?)\s*[）)]',text)
        counts[name]=None
        if match:
            number=match[1].replace(',','').replace('，','');raw[name]=number+match[2]
            if not match[2]:counts[name]=int(number)
    total_match=re.search(r'共\s*([\d,，]+)\s*条',text)
    total=int(total_match[1].replace(',','').replace('，','')) if total_match else None
    return counts,raw,total

def quality(w,config,port):
    import jd_store_quality_metrics as m
    w.panel({'低于90分商品数':m.collect_low_score_count(port)})
def deposit(w,config,port):
    import jd_store_deposit_metrics as m
    result=m.run(SimpleNamespace(config=Path(config['_path']),port=port,record_date=None,dry_run=True))
    w.panel({'补缴保证金（店铺违规）':Decimal(result['pending_deposit'])})

def sku(w,config,port):
    import jd_store_sku_web_collect as m
    index_path=Path('/var/lib/subsidy-sync/stores')/Path(config['_path']).parent.name/'jd-sku-index.json'
    index=m.load_index(index_path)
    expected,products=m.collect_page_rows(port,click_delay_ms=1800,index=index,refresh_policy='changed')
    if len(products)!=expected:raise ValueError('Incomplete JD product pagination')
    records,summary=m.build_rows(products,index)
    incomplete=set(summary['failed_products'])|set(summary['missing_products'])
    shaped=[]
    for native,product_id,name,merchant,parsed,spec,brand,price in records:
        if product_id in incomplete:continue
        shaped.append(dict(code=str(native),native=str(native),native_kind='sku',name=name,barcode=parsed.get('base_upc'),price=Decimal(str(price)),extra={'平台商品ID':str(product_id),'平台规格ID':str(native),'商家编码':merchant,'规格':spec,'销售属性':spec,'组合盒数':parsed.get('bundle_quantity'),'包装单位':parsed.get('package_unit'),'品牌':brand,'售卖状态':'上架'}))
    if not shaped:raise ValueError('Empty JD SKU snapshot')
    w.product(shaped)
    w.after_commit.append(lambda:m.save_index(index_path,{'version':m.INDEX_VERSION,'products':summary['entries']}))
    if incomplete:return 'partial:京东 '+str(len(incomplete))+' 个父商品未取到 SKU 明细，沿用原有元数据'

def index_activity(w,config,port):
    import jd_index_trade_collect as m
    page=m.CDP(m.attach(port)['webSocketDebuggerUrl'])
    try:
        page.call('Page.enable');page.call('Network.enable',{'maxTotalBufferSize':268435456,'maxResourceBufferSize':134217728});page.call('Runtime.enable');page.pump(1)
        ok_q,total_q,quotes=m.collect_dataset(page,'我的竞价',m.API_QUOTATION,10)
        ok_p,total_p,promos=m.collect_dataset(page,'竞价活动',m.API_PROMO,12)
        if not ok_q or not ok_p or total_q is None or total_p is None or len(quotes)!=int(total_q) or len(promos)!=int(total_p):
            text=page.ev("document.body?document.body.innerText:''") or ''
            if any(marker in text for marker in ['无权限','未开通','暂无权限','暂未开通']):return 'no_permission'
            raise ValueError('Incomplete JD index pages: quotes='+str((ok_q,total_q,len(quotes)))+' promos='+str((ok_p,total_p,len(promos)))+'; previous data preserved')
    finally:page.close()
    native=(config.get('store') or {}).get('poi_id') or Path(config['_path']).parent.name
    for kind,records in [('京东指数活动',promos),('京东指数报价',quotes)]:
        for r in records:
            identity=r.get('id') or r.get('bidSheetId') if kind=='京东指数报价' else r.get('activityId')
            if not identity:raise ValueError('Missing JD activity/quote identity')
            d={'客户':w.client,'平台':w.platform,'记录类型':kind,'来源表':'direct.jd.index','源记录键':sha([native,kind,identity]),'活动ID':r.get('activityId'),'活动名称':r.get('activityName'),'商品名称':r.get('skuName'),'平台SKU':r.get('skuId'),'报价单ID':r.get('bidSheetId'),'报价ID':r.get('id'),'报价':r.get('quotation'),'报价状态':str(r['status']) if r.get('status') is not None else None,'报价状态说明':r.get('statusReason'),'活动状态':str(r['activityStatus']) if r.get('activityStatus') is not None else None,'报名状态':str(r['signed']) if r.get('signed') is not None else None,'自动报价':r.get('autoQuotation'),'自动跟价':r.get('autoFollowPrice'),'观察时间':now(),'修改日期':now().date(),'采集批次ID':w.run_uuid,'来源原始字段':canonical(r)}
            w.upsert('subsidy',d,['来源表','源记录键'])

FEEDS={'analysis':analysis,'report':report,'sales':sales,'listed':listed,'top3':top3,'subsidy':subsidy,'aftersales':aftersales,'dashboard':dashboard,'orders':orders,'quality':quality,'deposit':deposit,'sku':sku,'index':index_activity}
DAILY={'sales','listed','quality','deposit','sku'}
HOURLY={'analysis'}
def settlement_ready(platform,feed,current):
    if platform=='美团' and feed=='sales':return current.hour>=6
    if platform=='京东' and feed=='analysis':return current.hour>0 or current.minute>=30
    return True
def gate(conn,client,platform,stage,version,since,allow_partial=False):
    with conn.cursor() as c:
        c.execute("SELECT 1 FROM ops_collect_run WHERE 客户=%s AND 平台=%s AND 阶段=%s AND 写入版本=%s AND (状态='success' OR (状态='skipped' AND 说明='无权限访问该报表') OR (状态='partial' AND %s)) AND 结束时间>=%s LIMIT 1",(client,platform,stage,version,allow_partial,since));return c.fetchone() is not None

def run(args):
    config=json.loads(Path(args.config).read_text());scope=scope_settings(config);config['store']=scope
    client=scope.get('client');platform=scope.get('platform');native=str(scope.get('poi_id') or scope.get('vender_id') or scope.get('vendor_id') or Path(args.config).parent.name)
    if not client or platform not in ['美团','京东']:raise ValueError('Explicit client/platform required')
    if not settlement_ready(platform,args.feed,now()):return {'state':'deferred','feed':args.feed,'reason':'source settlement window not ready; no completion marker written'}
    db=json.loads(Path(args.dw_config).read_text())
    if db.get('database')!='ec_dw':raise ValueError('Only ec_dw is allowed')
    config['_path']=str(Path(args.config).resolve())
    known_shops={('美团','32581900'):'北辰保民美团普药店',('美团','34461640'):'璟和堂美团普药店'}
    config['_shop']=scope.get('name') or scope.get('store_name') or known_shops.get((platform,native)) or platform+'店铺('+native+')'
    conn=pymysql.connect(**db,autocommit=False,connect_timeout=10,read_timeout=60,write_timeout=60)
    writer=Writer(conn,client,platform);stage=('mt' if platform=='美团' else 'jd')+'.'+args.feed+':'+native
    sources={
        'analysis':['meituan_product_analysis.py','jd_product360_analysis.py'],
        'report':['meituan_business_report.py'], 'sales':['meituan_product_sales_collect.py','jd_product_sales_collect.py'],
        'listed':['meituan_listed_products_collect.py'], 'top3':['meituan_top3_lowest_discount_price.py'],
        'subsidy':['meituan_subsidy_collect.py'], 'aftersales':['meituan_after_sales_pending.py','jd_after_sales_pending.py'],
        'dashboard':['jd_store_dashboard.py'], 'orders':['jd_order_status_collect.py'],
        'quality':['jd_store_quality_metrics.py'], 'deposit':['jd_store_deposit_metrics.py'],
        'sku':['jd_store_sku_web_collect.py','import_jd_store_skus.py'], 'index':['jd_index_trade_collect.py']}
    files=['direct_pipeline.py','esale_product.py']+sources[args.feed]
    version=hashlib.sha256(b''.join(Path(__file__).with_name(name).read_bytes() for name in files)).hexdigest()
    started=now();since=started.replace(hour=0,minute=0,second=0) if args.feed in DAILY else started.replace(minute=0,second=0)
    lock='ecdw:'+sha([client,platform,stage])[:48];run_id=None
    try:
        with conn.cursor() as c:
            c.execute('SELECT GET_LOCK(%s,0)',(lock,))
            if c.fetchone()[0]!=1:return {'state':'busy','feed':args.feed}
        if not args.force and platform=='京东' and args.feed in ['analysis','sales','index']:
            with conn.cursor() as c:
                c.execute("SELECT 1 FROM ops_collect_run WHERE 客户=%s AND 平台=%s AND 阶段=%s AND 写入版本=%s AND 状态='skipped' AND 说明='无权限访问该报表' AND 结束时间>=%s LIMIT 1",(client,platform,stage,version,started.replace(hour=0,minute=0,second=0)))
                if c.fetchone():return {'state':'skipped','feed':args.feed,'reason':'explicit permission denial already recorded today'}
        if not args.force and args.feed in DAILY|HOURLY and gate(conn,client,platform,stage,version,since,args.feed in ['orders','sku','analysis']):return {'state':'skipped','feed':args.feed,'target':'ec_dw','reason':'committed batch exists'}
        with conn.cursor() as c:
            c.execute("INSERT INTO ops_collect_run(客户,平台,阶段,开始时间,状态,行数,采集UUID,写入版本) VALUES (%s,%s,%s,%s,'running',0,%s,%s)",(client,platform,stage,started,writer.run_uuid,version));run_id=c.lastrowid
        conn.commit()
        outcome=FEEDS[args.feed](writer,config,args.port)
        status='skipped' if outcome=='no_permission' else 'partial' if outcome=='partial' or (isinstance(outcome,str) and outcome.startswith('partial:')) else 'success'
        message=outcome[8:] if isinstance(outcome,str) and outcome.startswith('partial:') else '无权限访问该报表' if status=='skipped' else '平台页签计数缺失或已封顶，精确数量未知' if status=='partial' else '源两次确认零销量，未清空客户/平台数据' if outcome=='empty_confirmed' else '已直接写入 ec_dw，事务提交成功'
        with conn.cursor() as c:c.execute("UPDATE ops_collect_run SET 状态=%s,结束时间=%s,行数=%s,说明=%s WHERE 批次ID=%s",(status,now(),writer.rows,message,run_id))
        conn.commit()
        # Cache publication can only follow a committed business batch. A cache
        # write failure cannot undo that committed data or fabricate a rollback.
        for callback in writer.after_commit:
            try:callback()
            except OSError as error:print(canonical({'state':'cache_warning','message':str(error)[:200]}),file=sys.stderr)
        return {'state':status,'client':client,'platform':platform,'feed':args.feed,'rows':writer.rows,'run_id':run_id,'target':'ec_dw'}
    except Exception as error:
        conn.rollback()
        if run_id:
            with conn.cursor() as c:c.execute("UPDATE ops_collect_run SET 状态='failed',结束时间=%s,说明=%s WHERE 批次ID=%s",(now(),describe(error),run_id))
            conn.commit()
        raise
    finally:
        try:
            with conn.cursor() as c:c.execute('SELECT RELEASE_LOCK(%s)',(lock,))
        finally:conn.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--dw-config',default='/etc/subsidy/ec-dw-collector.json');p.add_argument('--port',required=True,type=int);p.add_argument('--feed',required=True,choices=FEEDS);p.add_argument('--force',action='store_true');args=p.parse_args()
    for attempt in range(2):
        try:
            print(canonical(run(args)));break
        except Exception as error:
            recoverable=isinstance(error,RuntimeError) and any(marker in str(error) for marker in ['not logged in','requires login or verification','template capture failed'])
            if attempt==0 and recoverable:
                cfg=json.loads(Path(args.config).read_text());platform=scope_settings(cfg).get('platform')
                helper='login_recovery.py' if platform=='美团' else 'jd_login_page_recover.py'
                command=[sys.executable,str(Path(__file__).with_name(helper))]
                if platform=='美团':command+=['--platform','meituan']
                command+=['--port',str(args.port),'--settle-seconds','12']
                print(canonical({'state':'retrying','feed':args.feed,'reason':'existing ecommerce login recovery; one bounded retry'}),file=sys.stderr)
                try:subprocess.run(command,timeout=45,check=False)
                except subprocess.SubprocessError:pass
                continue
            print(canonical({'state':'failed','feed':args.feed,'type':type(error).__name__,'message':str(error)[:500]}),file=sys.stderr)
            raise SystemExit(1)
