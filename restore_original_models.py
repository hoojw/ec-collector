"""Replace rejected ec_dw models with confirmed original source structures."""
import datetime as dt
import hashlib
import json
import sys
import uuid
from pathlib import Path
import pymysql
sys.path.insert(0,'/opt/subsidy/.ec-phase1-stage')
from migrate import admin
c=admin('ec_dw')
def rows(t):
    with c.cursor(pymysql.cursors.DictCursor) as x:x.execute('SELECT * FROM '+t);return x.fetchall()
def insert(t,d):
    with c.cursor() as x:x.execute('INSERT INTO `'+t+'` ('+','.join('`'+k+'`' for k in d)+') VALUES ('+','.join(['%s']*len(d))+')',list(d.values()))
def clone(t,source):
    with c.cursor() as x:x.execute('CREATE TABLE `'+t+'` LIKE '+source)
def copy(t,source):
    with c.cursor() as x:
        x.execute('SHOW COLUMNS FROM `'+t+'`');columns=','.join('`'+r[0]+'`' for r in x.fetchall())
        x.execute('INSERT INTO `'+t+'` ('+columns+') SELECT '+columns+' FROM '+source)
def canonical(v):return json.dumps(v,ensure_ascii=False,default=str,sort_keys=True,separators=(',',':'))
old_runs=rows('ops_collect_run');tmall=rows('aftersale_case');cost=rows('upload.sku_cost');freight=rows('upload.delivery_freight')
cfg=json.loads(Path('/opt/subsidy/config.json').read_text());scope=cfg.get('store') or cfg.get('meituan_store')
assert scope['client']=='保民' and scope['platform']=='美团' and str(scope['poi_id'])=='32581900' and cfg['mysql']['database']=='adjust_price'
with c.cursor() as x:
    x.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='ec_dw'");existing={r[0] for r in x.fetchall()}
    assert 'dim_client' in existing and 'store_metric_window' in existing,'Already migrated or unexpected schema'

clone('_new_input_cost','upload.sku_cost');copy('_new_input_cost','upload.sku_cost')
clone('_new_input_freight','upload.delivery_freight');copy('_new_input_freight','upload.delivery_freight')
with c.cursor() as x:
    for name in ['_new_input_cost','_new_input_freight']:x.execute('ALTER TABLE `'+name+'` RENAME COLUMN id TO 序号ID')

clone('_new_esale_product','adjust_price.esale_product');copy('_new_esale_product','adjust_price.esale_product')
clone('_new_esale_system','adjust_price.esale_system')
excluded=['可报名活动商品数量','已知商品底价数量','已报活动商品数量','异常活动数量','待商家审核数量','即将自动退售后单','退货退款初审时间','退货退款终审时间']
with c.cursor() as x:
    x.execute('ALTER TABLE _new_esale_system '+','.join('DROP COLUMN `'+n+'`' for n in excluded))
copy('_new_esale_system','adjust_price.esale_system')

clone('_new_aftersale_case','adjust_price.ops_after_sale_review_daily')
clone('_new_aftersale_node','adjust_price.ops_after_sale_review_node')
with c.cursor() as x:
    x.execute("ALTER TABLE _new_aftersale_case ADD 客户 VARCHAR(64) NOT NULL,ADD 平台 VARCHAR(32) NOT NULL,ADD 店铺 VARCHAR(128) NOT NULL DEFAULT '', DROP PRIMARY KEY,ADD PRIMARY KEY(客户,平台,店铺,日期,售后单号)")
    x.execute("ALTER TABLE _new_aftersale_node ADD 客户 VARCHAR(64) NOT NULL,ADD 平台 VARCHAR(32) NOT NULL,ADD 店铺 VARCHAR(128) NOT NULL DEFAULT '', DROP PRIMARY KEY,ADD PRIMARY KEY(客户,平台,店铺,售后单号,节点主体,节点操作,节点时间)")
for r in rows('adjust_price.ops_after_sale_review_daily'):insert('_new_aftersale_case',dict(r,客户='保民',平台='美团',店铺='北辰保民美团普药店'))
for r in rows('adjust_price.ops_after_sale_review_node'):insert('_new_aftersale_node',dict(r,客户='保民',平台='美团',店铺='北辰保民美团普药店'))
for r in tmall:
    observed=r['最近观察时间'];assert observed is not None
    insert('_new_aftersale_case',{'客户':r['客户'],'平台':r['平台'],'店铺':'北辰保民天猫店','日期':observed.date(),'售后单号':r['平台售后单号'],'售后状态':r['平台原始状态'],'商品信息':r['商品名称'],'退款金额':r['退款金额'],'售后类型':r['售后类型'],'申请原因':r['原因'],'申请时间':r['申请时间'],'商家审核时间':r['初审时间'],'系统审核时间':r['终审时间']})
clone('_new_aftersale_status_snapshot','adjust_price.esale_system')
with c.cursor() as x:
    x.execute('SHOW COLUMNS FROM _new_aftersale_status_snapshot');names=[r[0] for r in x.fetchall()]
    keep={'客户','平台','日期','待商家审核数量','即将自动退售后单','退货退款初审时间','退货退款终审时间'}
    x.execute('ALTER TABLE _new_aftersale_status_snapshot '+','.join('DROP COLUMN `'+n+'`' for n in names if n not in keep))
copy('_new_aftersale_status_snapshot','adjust_price.esale_system')

with c.cursor() as x:
    x.execute('''CREATE TABLE _new_ops_collect_run (
    批次ID BIGINT AUTO_INCREMENT PRIMARY KEY,店铺编码 VARCHAR(64) NULL,平台 VARCHAR(32) NULL,阶段 VARCHAR(64) NULL,
    开始时间 DATETIME NULL,结束时间 DATETIME NULL,状态 VARCHAR(16) NULL,行数 INT NULL,说明 VARCHAR(1000) NULL,
    采集UUID CHAR(36) NULL UNIQUE, KEY idx_run(店铺编码,阶段,开始时间)) CHARACTER SET utf8mb4''')
    x.execute('''CREATE TABLE _new_ops_import_run (
    批次ID CHAR(36) PRIMARY KEY,店铺编码 VARCHAR(64) NOT NULL,文件名 VARCHAR(255) NOT NULL,文件摘要 CHAR(64) NOT NULL,
    行数 INT NOT NULL,更新前数据 JSON NOT NULL,上传数据 JSON NOT NULL,导入时间 DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) CHARACTER SET utf8mb4 COMMENT='沿用原ops_cost_import字段；空店铺编码表示客户级导入，客户保留在上传数据中' ''')
for r in old_runs:
    insert('_new_ops_collect_run',{'店铺编码':r['店铺编码'],'平台':r['平台'],'阶段':r['采集程序'],'开始时间':r['开始时间'],'结束时间':r['结束时间'],'状态':r['状态'],'行数':r['记录数'],'说明':r['说明'],'采集UUID':r['采集批次ID']})
for source,records in [('upload.sku_cost',cost),('upload.delivery_freight',freight)]:
    content=canonical(records);insert('_new_ops_import_run',{'批次ID':str(uuid.uuid4()),'店铺编码':'','文件名':'mysql:'+source,'文件摘要':hashlib.sha256(content.encode()).hexdigest(),'行数':len(records),'更新前数据':'[]','上传数据':content})

# Before publishing, verify source counts and exact field selections.
with c.cursor() as x:
    expected={'_new_input_cost':len(cost),'_new_input_freight':len(freight),'_new_aftersale_case':1820+len(tmall),'_new_aftersale_node':5407,'_new_ops_collect_run':len(old_runs),'_new_ops_import_run':2}
    for table,count in expected.items():x.execute('SELECT COUNT(*) FROM `'+table+'`');assert x.fetchone()[0]==count,(table,count)
    x.execute('SHOW COLUMNS FROM _new_esale_system');assert not (set(r[0] for r in x.fetchall()) & set(excluded))
    # Remove dependencies on the rejected dimension/UUID/import schema.
    x.execute("SELECT TABLE_NAME,CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA='ec_dw' AND CONSTRAINT_TYPE='FOREIGN KEY'")
    for table,name in x.fetchall():x.execute('ALTER TABLE `'+table+'` DROP FOREIGN KEY `'+name+'`')
    for name in ['aftersale_case','aftersale_node','aftersale_status_snapshot','dim_client','dim_store','input_cost','input_freight','ops_collect_run','ops_import_run','product','store_metric_window','store_status_snapshot']:
        x.execute('DROP TABLE `'+name+'`')
    x.execute('RENAME TABLE '+','.join('`'+name+'` TO `'+name.removeprefix('_new_')+'`' for name in ['_new_input_cost','_new_input_freight','_new_esale_product','_new_esale_system','_new_aftersale_case','_new_aftersale_node','_new_aftersale_status_snapshot','_new_ops_collect_run','_new_ops_import_run']))
    x.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='ec_dw' ORDER BY table_name");tables=[r[0] for r in x.fetchall()];ddl=[]
    for table in tables:
        x.execute('SELECT COUNT(*) FROM `'+table+'`');print(table,x.fetchone()[0])
        x.execute('SHOW CREATE TABLE `'+table+'`');ddl.append(x.fetchone()[1]+';')
    Path('/opt/subsidy/schema-v2.sql').write_text('SET NAMES utf8mb4;\n\n'+'\n\n'.join(ddl))
print('Original models published. Other source databases unchanged; old writers remain paused.')
