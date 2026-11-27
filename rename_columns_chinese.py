"""Final naming convention: English tables and Chinese columns; preserves data."""
import sys
from pathlib import Path
sys.path.insert(0,'/opt/subsidy/.ec-phase1-stage')
from migrate import admin
tables={'客户':'dim_client','店铺':'dim_store','商品':'product','补贴':'subsidy','input_成本':'input_cost','input_运费':'input_freight','商家利润设置':'merchant_profit_setting','售后单':'aftersale_case','售后节点':'aftersale_node','售后状态监控':'aftersale_status_snapshot','店铺经营统计':'store_metric_window','店铺状态快照':'store_status_snapshot','采集批次':'ops_collect_run','导入批次':'ops_import_run'}
mapping='''activity_end_at=活动结束时间,activity_id=活动ID,activity_name=活动名称,activity_start_at=活动开始时间,applied_at=申请时间,artifact_path=证据路径,artifact_sha256=证据摘要,barcode=条码,business_date=业务日期,case_id=售后记录ID,case_type=售后类型,charge_basis=计费单位,client_name=客户,code_type=编码类型,collector_code=采集程序,cost_id=成本记录ID,cost_source=成本来源,cost_unit=成本单位,created_at=创建时间,currency=币种,current_price=当前售价,data_quality=数据质量,deadline_at=截止时间,deadline_type=截止类型,enabled=是否启用,event_at=事件时间,file_name=来源文件,file_sha256=文件摘要,final_review_at=终审时间,finished_at=结束时间,first_observed_at=首次观察时间,first_review_at=初审时间,freight_amount=运费,freight_id=运费记录ID,freight_type=运费类型,import_id=导入批次ID,import_type=导入类型,last_observed_at=最近观察时间,listing_status=售卖状态,merchant_code=商家编码,merchant_contribution=商家承担,message=说明,metric_code=指标编码,metric_value=指标数值,node_action=节点操作,node_actor=节点主体,node_id=节点ID,normalized_status=标准状态,numeric_value=数值,observed_at=观察时间,operation_fee_rate=代运营费率,operator_name=操作人,order_id=订单号,original_price=原价,package_quantity=包装数量,platform=平台,platform_case_id=平台售后单号,platform_fee_rate=平台扣点,platform_product_id=平台商品ID,platform_sku_id=平台规格ID,platform_store_id=平台店铺ID,price=价格,product_code=商品编码,product_id=商品ID,product_name=商品名称,profit_rate=利润点,quantity_max=数量上限,quantity_min=数量下限,reason=原因,redline_factor=红线价格系数,refund_amount=退款金额,region=地区,required_quantity=门槛数量,row_count=记录数,run_id=采集批次ID,setting_id=设置ID,snapshot_id=快照ID,source_date=来源日期,source_fields=来源原始字段,source_item_key=源商品键,source_node_key=源节点键,source_record_key=源记录键,source_reported_at=平台报告时间,source_row_key=来源行键,source_status=平台原始状态,source_table=来源表,source_version=源码版本,specification=规格,started_at=开始时间,statistic_id=统计ID,status=状态,stock=库存,store_code=店铺编码,store_name=店铺名称,store_scope=店铺作用域,subsidized_price=补贴后价,subsidy_amount=补贴金额,subsidy_id=补贴记录ID,subsidy_status=补贴状态,suggested_price=建议售价,text_value=文本值,threshold_price=门槛价格,unit=单位,unit_cost=成本价,updated_at=更新时间,valid_from=生效日期,valid_to=失效日期,window_end_at=窗口结束时间,window_start_at=窗口开始时间,window_type=时间窗口类型'''
cols=dict(item.split('=') for item in mapping.split(','));c=admin('ec_dw');counts={}
with c.cursor() as x:
    x.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='ec_dw'");existing={r[0] for r in x.fetchall()}
    if existing==set(tables):x.execute('RENAME TABLE '+','.join('`'+old+'` TO `'+new+'`' for old,new in tables.items()))
    else:assert existing==set(tables.values())
    for table in tables.values():
        x.execute('SELECT COUNT(*) FROM `'+table+'`');counts[table]=x.fetchone()[0]
        x.execute('SHOW COLUMNS FROM `'+table+'`');names=[r[0] for r in x.fetchall()]
        changed=[n for n in names if n in cols]
        assert all(n in cols or n in cols.values() for n in names)
        if changed:
            x.execute("SELECT tc.CONSTRAINT_NAME,cc.CHECK_CLAUSE FROM information_schema.TABLE_CONSTRAINTS tc JOIN information_schema.CHECK_CONSTRAINTS cc ON cc.CONSTRAINT_SCHEMA=tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME=tc.CONSTRAINT_NAME WHERE tc.TABLE_SCHEMA='ec_dw' AND tc.TABLE_NAME=%s AND tc.CONSTRAINT_TYPE='CHECK'",(table,));checks=x.fetchall()
            for name,expr in checks:x.execute('ALTER TABLE `'+table+'` DROP CHECK `'+name+'`')
            x.execute('ALTER TABLE `'+table+'` '+','.join('RENAME COLUMN `'+n+'` TO `'+cols[n]+'`' for n in changed))
            for name,expr in checks:
                for old,new in cols.items():expr=expr.replace('`'+old+'`','`'+new+'`')
                x.execute('ALTER TABLE `'+table+'` ADD CONSTRAINT `'+name+'` CHECK ('+expr+')')
        x.execute('SELECT COUNT(*) FROM `'+table+'`');assert x.fetchone()[0]==counts[table]
        print(table,counts[table])
    x.execute("SELECT User,Host FROM mysql.user WHERE User='ec_platform_collect'")
    for user,host in x.fetchall():
        x.execute('REVOKE ALL PRIVILEGES, GRANT OPTION FROM %s@%s',(user,host))
        for table in ['dim_client','dim_store']:x.execute('GRANT SELECT ON ec_dw.'+table+' TO %s@%s',(user,host))
        for table in ['ops_collect_run','product','subsidy','aftersale_case','aftersale_node','aftersale_status_snapshot','store_metric_window','store_status_snapshot']:x.execute('GRANT SELECT,INSERT,UPDATE ON ec_dw.'+table+' TO %s@%s',(user,host))
    x.execute('SELECT 客户,平台,红线价格系数 FROM merchant_profit_setting ORDER BY 平台');print('Profit defaults',x.fetchall())
    ddl=[]
    for table in sorted(tables.values()):x.execute('SHOW CREATE TABLE `'+table+'`');ddl.append(x.fetchone()[1]+';')
    Path('/opt/subsidy/schema-v2.sql').write_text('SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\n\n'+'\n\n'.join(ddl)+'\nSET FOREIGN_KEY_CHECKS=1;\n')
print('All row counts preserved; every column renamed; source databases unchanged')
