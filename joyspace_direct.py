"""Publish account-independent JoySpace catalog directly to subsidy."""
import json
import uuid
from pathlib import Path
import pymysql
import direct_pipeline as p

def publish(headers,records,record_date):
    import sync_joyspace_subsidy as source
    if not records:raise ValueError('Empty JoySpace catalog')
    box=headers.index('盒数')
    if sum(source.int_or_none(r[box]) is not None for r in records)<len(records)//2:raise ValueError('Catalog box column incomplete')
    db=json.loads(Path('/etc/subsidy/ec-dw-collector.json').read_text());assert db['database']=='ec_dw'
    conn=pymysql.connect(**db,autocommit=False);w=p.Writer(conn,None,'京东');run_id=None
    try:
        with conn.cursor() as c:
            c.execute("INSERT INTO ops_collect_run(平台,阶段,开始时间,状态,行数,采集UUID,写入版本) VALUES ('京东','joyspace.catalog',%s,'running',0,%s,%s)",(p.now(),w.run_uuid,p.sha(headers)));run_id=c.lastrowid
        conn.commit()
        seen=set()
        for values in records:
            r=dict(zip(headers,values));strategy=r.get('策略ID');drug=r.get('药品ID')
            if not strategy or not drug:raise ValueError('Catalog row missing strategy/drug ID')
            key=p.sha([strategy,drug,r.get('盒数')])
            if key in seen:raise ValueError('Duplicate catalog business identity')
            seen.add(key)
            d={'平台':'京东','记录类型':'京东补贴目录','来源表':'direct.joyspace.catalog','源记录键':key,'活动ID':strategy,'商品编码':drug,'编码类型':'平台药品ID','商品名称':r.get('通用名称'),'规格':r.get('规格'),'活动盒数':source.int_or_none(r.get('盒数')),'门槛价格':source.decimal_or_none(r.get('价格下限')),'建议售价':source.decimal_or_none(r.get('价格上限')),'观察时间':p.now(),'修改日期':record_date,'采集批次ID':w.run_uuid,'来源原始字段':p.canonical(r)}
            w.upsert('subsidy',d,['来源表','源记录键'])
        with conn.cursor() as c:c.execute("UPDATE ops_collect_run SET 状态='success',结束时间=%s,行数=%s,说明='共享商品目录已写入 ec_dw' WHERE 批次ID=%s",(p.now(),w.rows,run_id))
        conn.commit();return {'state':'success','rows':w.rows,'run_id':run_id,'target':'ec_dw.subsidy'}
    except Exception as error:
        conn.rollback()
        if run_id:
            with conn.cursor() as c:c.execute("UPDATE ops_collect_run SET 状态='failed',结束时间=%s,说明=%s WHERE 批次ID=%s",(p.now(),('采集失败（'+type(error).__name__+'）：'+str(error))[:950],run_id))
            conn.commit()
        raise
    finally:conn.close()
