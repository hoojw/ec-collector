"""Platform collection only. ERP/import/manual input domains are excluded."""
import datetime as dt
import hashlib
import json
import re
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
COLLECTOR = 'meituan.product_analysis.v2'
METRICS = ('amount','ring_ratio','average_sold_skus','average_on_sale_skus',
           'average_available_skus','product_turnover_rate','tirzepatide_amount','non_tirzepatide_ratio')
SKU_KEYS = {'productId','skuId','skuCode','upcCode','productName','skuName','gtvAmt',
            'gtvProdCnt','skuCnt','productStatus','productUnit','wmProductId'}

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)

def checksum(raw):
    return hashlib.sha256(raw).hexdigest()

def timestamp(value):
    result=dt.datetime.fromisoformat(value)
    if result.tzinfo is None:raise ValueError('Timezone is required')
    return result.astimezone(TZ)

def business_evidence(evidence):
    """Allowlist product/aggregate data; never store authentication or customer data."""
    out={k:evidence[k] for k in ('params','metrics','pagination','observed_at')}
    for key in ('sale_chart','on_sale_chart'):
        out[key]=[{k:row[k] for k in ('indexCode','indexName','value','compareValue','unit','valueUnit') if k in row}
                  for row in evidence[key] if isinstance(row,dict)]
    out['sku_rows']=[{k:v for k,v in row.items() if k in SKU_KEYS} for row in evidence['sku_rows']]
    return out

def prepare(document):
    if document.get('schema_version')!=1 or document.get('collector')!=COLLECTOR:raise ValueError('Unknown collection contract')
    uuid.UUID(document['run_uuid'])
    if document.get('store_code')!='mt-baomin' or str(document.get('platform_store_id'))!='32581900':
        raise ValueError('Phase 1 only accepts the verified Baomin Meituan store')
    started=timestamp(document['started_at'])
    finished=timestamp(document['finished_at'])
    if finished<started:raise ValueError('Finish precedes start')
    end=started.date()-dt.timedelta(days=1)
    result=[]
    for label,days,kind in [('daily',1,'day'),('near_30',30,'rolling_30d')]:
        evidence=document.get('windows',{}).get(label)
        if not evidence:raise ValueError('Missing required window: '+label)
        params=evidence['params'];start=end-dt.timedelta(days=days-1)
        previous_end=start-dt.timedelta(days=1);previous_start=previous_end-dt.timedelta(days=days-1)
        expected={k:v.strftime('%Y%m%d') for k,v in [('startDate',start),('endDate',end),('compareStartDate',previous_start),('compareEndDate',previous_end)]}
        if any(params.get(k)!=v for k,v in expected.items()):raise ValueError('Window boundaries disagree with source request')
        if params.get('dateType')!=('dt' if label=='daily' else 'last_30_days'):raise ValueError('Wrong window type')
        audit=evidence['pagination'];pages=audit.get('pages') or []
        count=audit.get('expected_rows')
        if type(count) is not int or count<0 or audit.get('complete') is not True or audit.get('actual_rows')!=count:
            raise ValueError('Incomplete product pagination')
        if [p.get('page') for p in pages]!=list(range(1,len(pages)+1)) or not pages:
            raise ValueError('Invalid page sequence')
        if any(p.get('expected_total')!=count or type(p.get('rows')) is not int or p['rows']<0 for p in pages):raise ValueError('Pagination total changed')
        if sum(p['rows'] for p in pages)!=count or len(evidence['sku_rows'])!=count:raise ValueError('Pagination evidence does not reconcile')
        metrics={}
        for field in METRICS:
            value=evidence['metrics'].get(field)
            value=None if value is None else Decimal(str(value))
            limit=Decimal('1000000000000') if field=='ring_ratio' else Decimal('100000000000000')
            if value is not None and (not value.is_finite() or abs(value)>=limit):raise ValueError('Invalid metric '+field)
            if value is not None and field!='ring_ratio' and value<0:raise ValueError('Negative metric '+field)
            if value is not None and field in ('product_turnover_rate','non_tirzepatide_ratio') and value>1:raise ValueError('Invalid ratio '+field)
            metrics[field]=value
        if metrics['average_sold_skus'] is not None and metrics['average_on_sale_skus'] is not None and metrics['average_sold_skus']>metrics['average_on_sale_skus']:raise ValueError('Sold count exceeds on-sale count')
        if metrics['amount'] is not None and metrics['tirzepatide_amount'] is not None and metrics['tirzepatide_amount']>metrics['amount']:raise ValueError('Product amount exceeds total')
        observed=timestamp(evidence['observed_at'])
        if not started<=observed<=finished:raise ValueError('Observation outside collection interval')
        status='success' if all(metrics[k] is not None for k in ('amount','average_sold_skus','average_on_sale_skus')) else 'partial'
        result.append(dict(label=label,window_type=kind,start=start,end=end,compare_start=previous_start,
                           compare_end=previous_end,observed_at=observed,metrics=metrics,audit=audit,status=status))
    return result
