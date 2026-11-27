"""Consume Windows ecommerce snapshots directly into ec_dw; no browser changes."""
import argparse
import datetime as dt
import json
import sys
import uuid
from pathlib import Path
import pymysql
import direct_pipeline as p
import tmall_ecommerce_to_mysql as source
import tmall_onsale_to_mysql as listings

def stamp(value):return dt.datetime.fromisoformat(value).astimezone(p.TZ).replace(tzinfo=None)
def pick_snapshots(candidates):
    usable=[s for s in candidates if s.get('state') in source.USABLE_STATES]
    rank=lambda s:(stamp(s['slot_time']),stamp(s['collected_at']))
    return (max(candidates,key=rank) if candidates else None,max(usable,key=rank) if usable else None)

def latest_snapshots(key,job):
    candidates=[]
    for path in source._iter_json(source.DEFAULT_DATA_DIR,job,key,7):
        try:
            snapshot=json.loads(path.read_text())
            if snapshot.get('slot_time') and snapshot.get('collected_at'):stamp(snapshot['slot_time']);stamp(snapshot['collected_at']);candidates.append(snapshot)
        except (OSError,ValueError,TypeError):continue
    return pick_snapshots(candidates)

def record_source_failure(conn,client,key,job,snapshot):
    identity=str(uuid.uuid5(uuid.NAMESPACE_URL,'tmall.failed:'+key+':'+job+':'+p.sha(snapshot)))
    reasons=','.join(str(e.get('dataset',''))+'/'+str(e.get('error_code','')) for e in snapshot.get('errors',[]) if isinstance(e,dict))
    with conn.cursor() as c:
        c.execute("INSERT INTO ops_collect_run(客户,平台,阶段,开始时间,结束时间,状态,行数,采集UUID,说明) VALUES (%s,'天猫',%s,%s,%s,'failed',0,%s,%s) ON DUPLICATE KEY UPDATE 批次ID=批次ID",(client,'tmall.'+job+':'+key,p.now(),p.now(),identity,('Windows 采集端失败（'+str(snapshot.get('collected_at'))+'）：'+reasons)[:950]))
    conn.commit()
def ingest(conn,client,key,job,snapshot):
    captured=stamp(snapshot['collected_at']);day=source.parse_slot_date(snapshot['slot_time'])
    if day is None or captured>p.now()+dt.timedelta(minutes=5):raise ValueError('Invalid snapshot time')
    age=p.now()-captured
    if age>dt.timedelta(hours=26 if job=='daily' else 2):raise ValueError('Windows '+key+'/'+job+' snapshot is stale')
    identity=str(uuid.uuid5(uuid.NAMESPACE_URL,'tmall:'+key+':'+job+':'+p.sha(snapshot)+':'+p.sha(Path(__file__).read_text())))
    with conn.cursor() as c:
        c.execute("SELECT 状态 FROM ops_collect_run WHERE 采集UUID=%s",(identity,));old=c.fetchone()
        if old and old[0] in ('success','partial'):return {'state':'skipped','client':client,'job':job,'snapshot_state':old[0],'observed_at':captured.isoformat(),'reason':'available snapshot already ingested'}
    w=p.Writer(conn,client,'天猫');w.run_uuid=identity;run_id=None
    payload=snapshot.get('data') or {};partial=snapshot.get('state')=='partial'
    try:
        with conn.cursor() as c:
            c.execute("INSERT INTO ops_collect_run(客户,平台,阶段,开始时间,状态,行数,采集UUID,写入版本) VALUES (%s,'天猫',%s,%s,'running',0,%s,%s) ON DUPLICATE KEY UPDATE 批次ID=LAST_INSERT_ID(批次ID),状态='running',开始时间=VALUES(开始时间)",(client,'tmall.'+job+':'+key,p.now(),identity,p.sha(snapshot)));run_id=c.lastrowid
        conn.commit()
        if job=='daily':
            metrics=source.parse_daily(snapshot);available=source.usable_columns(snapshot,source.DAILY_COLUMN_DATASETS,source.DAILY_METRIC_COLUMNS)
            values={k:v for k,v in metrics.items() if v is not None and k in available and k not in ('退货退款初审时间','本月累计交易额')}
            if '本月累计交易额' in available and metrics['本月累计交易额'] is not None:values['近30天交易额']=metrics['本月累计交易额']
            rank=payload.get('item_rank') or {}
            if 'item_rank' in payload and (rank.get('metrics') or {}).get('替尔泊肽支付金额合计') is not None:values['近30天替尔泊肽销售额']=source.decimal_or_none(rank['metrics']['替尔泊肽支付金额合计'])
            if values:w.panel(dict(values,修改日期=captured),day=day)
            portal=(payload.get('portal') or {}).get('metrics') or {}
            duration=source.decimal_or_none(portal.get('退款处理时长_天'))
            if duration is not None:
                w.upsert('aftersale_status_snapshot',{'客户':client,'平台':'天猫','日期':day,'退款处理时长':duration*24,'时长单位':'小时'},['客户','平台','日期'])
                with conn.cursor() as c:c.execute("UPDATE aftersale_status_snapshot SET 退货退款初审时间=NULL WHERE 客户=%s AND 平台='天猫' AND 日期=%s",(client,day))
            on_sale=payload.get('on_sale') or {};raw_rows=on_sale.get('rows') or []
            expected=on_sale.get('total')
            if raw_rows:
                records=[]
                for item in raw_rows:
                    r=listings.unwrap(item)
                    if not r['item_id'] or not r['title']:raise ValueError('Incomplete native Tmall product identity')
                    records.append(dict(code=r['item_id'],native=r['item_id'],name=r['title'],price=r['current_price'],stock=r['stock'],extra={'原月销量':r['monthly_sold_quantity'],'上架日期原值':r['up_shelf_date'],'售卖状态':r['shelf_status'] or r['item_status']}))
                w.product(records)
                # Incoming observation time belongs to the file, not this ingest attempt.
                with conn.cursor() as c:
                    for r in records:c.execute("UPDATE esale_product SET 更新日期=%s,商品信息修改日期=%s WHERE 客户=%s AND 平台='天猫' AND 平台商品ID=%s",(captured.date(),captured.date(),client,r['native']))
                if not on_sale.get('pagination_complete') or expected is None or len(raw_rows)!=int(expected):partial=True
        else:
            values=source.parse_every10(snapshot);available=source.usable_columns(snapshot,source.EVERY10_COLUMN_DATASETS,source.EVERY10_METRIC_COLUMNS)
            if any(values[k] is not None for k in available):w.upsert('aftersale_status_snapshot',{'客户':client,'平台':'天猫','日期':day,**{k:values[k] for k in available}},['客户','平台','日期'])
        if w.rows==0:raise ValueError('No valid datasets in Windows snapshot')
        with conn.cursor() as c:c.execute('UPDATE ops_collect_run SET 状态=%s,结束时间=%s,行数=%s,说明=%s WHERE 批次ID=%s',('partial' if partial else 'success',p.now(),w.rows,'Windows 快照观察时间：'+captured.isoformat(),run_id))
        conn.commit();return {'state':'partial' if partial else 'success','client':client,'job':job,'rows':w.rows,'run_id':run_id}
    except Exception as error:
        conn.rollback()
        if run_id:
            with conn.cursor() as c:c.execute("UPDATE ops_collect_run SET 状态='failed',结束时间=%s,说明=%s WHERE 批次ID=%s",(p.now(),('采集失败（'+type(error).__name__+'）：'+str(error))[:900],run_id))
            conn.commit()
        raise

def activities(conn,path):
    body=json.loads(path.read_text());records=body.get('rows')
    if not isinstance(records,list) or not records:raise ValueError('Activity file has no rows')
    if p.now()-dt.datetime.fromtimestamp(path.stat().st_mtime)>dt.timedelta(days=2):raise ValueError('Activity file stale; waiting for Windows upload')
    w=p.Writer(conn,None,'天猫');observed=dt.datetime.fromtimestamp(path.stat().st_mtime)
    try:
        for r in records:
            activity=r.get('activityId');sku=r.get('skuId') or r.get('skuItemId') or r.get('platformItemId')
            if not activity or not sku:raise ValueError('Activity row has no stable activity/item identity')
            w.upsert('subsidy',{'平台':'天猫','记录类型':'天猫活动池','来源表':'direct.tmall.activity','源记录键':p.sha([activity,sku,r.get('skuViewName')]),'活动ID':str(activity),'商品名称':r.get('itemTitle') or r.get('name'),'规格':r.get('spec'),'平台商品ID':r.get('platformItemId'),'平台SKU':r.get('skuId'),'包装档位':r.get('skuViewName'),'招商价':r.get('skuPrice'),'服务费原值':r.get('commissionInfo'),'报名状态':str(r['alreadyApply']) if r.get('alreadyApply') is not None else None,'活动状态':str(r['status']) if r.get('status') is not None else None,'补贴方式':r.get('subsidyStyle'),'观察时间':observed,'修改日期':observed.date(),'来源原始字段':p.canonical(r)},['来源表','源记录键'])
        conn.commit();return {'state':'success','feed':'tmall.activity','rows':w.rows}
    except Exception:conn.rollback();raise

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',default='/opt/subsidy/config.json');parser.add_argument('--dw-config',default='/etc/subsidy/ec-dw-collector.json');parser.add_argument('--activities',type=Path);args=parser.parse_args()
    cfg=json.loads(Path(args.config).read_text());db=json.loads(Path(args.dw_config).read_text());assert db['database']=='ec_dw'
    conn=pymysql.connect(**db,autocommit=False,connect_timeout=10);failed=0
    try:
        if args.activities:print(p.canonical(activities(conn,args.activities)));return 0
        for key,store in source.configured_stores(cfg):
            for job in ['daily','every10']:
                try:
                    latest,snapshot=latest_snapshots(key,job)
                    if latest and latest.get('state')=='failed':
                        record_source_failure(conn,store['client'],key,job,latest);failed+=1
                        print(p.canonical({'state':'source_failed','store':key,'job':job,'observed_at':latest['collected_at'],'reason':'preserving previous usable data'}),file=sys.stderr)
                    if snapshot is None:raise ValueError('No usable Windows snapshot for '+key+'/'+job)
                    print(p.canonical(ingest(conn,store['client'],key,job,snapshot)))
                except Exception as error:failed+=1;print(p.canonical({'state':'failed','store':key,'job':job,'message':str(error)[:500]}),file=sys.stderr)
        return 1 if failed else 0
    finally:conn.close()
if __name__=='__main__':raise SystemExit(main())
