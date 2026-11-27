import datetime as dt
import json
import uuid
from pathlib import Path
from contract import COLLECTOR,TZ,prepare,checksum,canonical

class Writer:
    def __init__(self,connection):
        self.conn=connection;self.run_id=None;self.lock=None;self.replayed=False

    def start(self,document,sha=None):
        self.capture=document['run_uuid'];self.store=document['store_code']
        self.lock='ecdwp1:'+self.capture
        with self.conn.cursor() as cur:
            cur.execute('SELECT GET_LOCK(%s,0)',(self.lock,))
            if cur.fetchone()[0]!=1:raise RuntimeError('This capture is already being processed')
            cur.execute('SELECT `客户`,`平台`,`平台店铺ID` FROM dim_store WHERE 店铺编码=%s',(self.store,))
            row=cur.fetchone()
            if not row or row!=('保民','美团',document['platform_store_id']):raise ValueError('Store registry does not match verified source identity')
            cur.execute('SELECT `批次ID`,capture_sha256 FROM ops_collect_run WHERE published_capture_uuid=%s',(self.capture,))
            row=cur.fetchone()
            if row:
                if sha is None or row[1]!=sha:raise ValueError('Capture UUID already published with different or missing evidence hash')
                self.run_id=row[0];self.replayed=True;return self.run_id
            cur.execute('SELECT COUNT(*) FROM ops_collect_run WHERE capture_uuid=%s',(self.capture,));attempt=cur.fetchone()[0]+1
            cur.execute("UPDATE ops_collect_run SET 状态='failed',结束时间=NOW(),说明='Interrupted attempt superseded by replay' WHERE capture_uuid=%s AND 状态='running'",(self.capture,))
            cur.execute('INSERT INTO ops_collect_run (`店铺编码`,`平台`,`阶段`,`开始时间`,`状态`,`行数`,run_uuid,capture_uuid,collector_code,source_version,attempt_no) VALUES (%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s)',
                        (self.store,'美团',COLLECTOR,dt.datetime.now(TZ).replace(tzinfo=None),'running',str(uuid.uuid4()),self.capture,COLLECTOR,document['source_version'],attempt))
            self.run_id=cur.lastrowid
        self.conn.commit();return self.run_id

    def finish(self,document,path):
        path=Path(path);raw=path.read_bytes();sha=checksum(raw)
        if canonical(json.loads(raw))!=canonical(document):raise ValueError('Evidence file disagrees with parsed capture')
        if self.replayed:return {'state':'success','run_id':self.run_id,'replayed':True,'rows':2,'artifact':str(path),'sha256':sha}
        windows=prepare(document);status='success' if all(w['status']=='success' for w in windows) else 'partial'
        try:
            with self.conn.cursor() as cur:
                for window in windows:
                    audit=window['audit'];published=status=='success'
                    cur.execute('INSERT INTO ops_collect_result (run_id,store_code,feed_code,window_type,window_start,window_end,compare_start,compare_end,observed_at,status,expected_rows,actual_rows,page_count,attempts,error_code,error_message) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (self.run_id,self.store,'product_analysis.'+window['label'],window['window_type'],window['start'],window['end'],window['compare_start'],window['compare_end'],window['observed_at'].replace(tzinfo=None),'success' if published else 'partial' if window['status']=='partial' else 'skipped',audit['expected_rows'],audit['actual_rows'],len(audit['pages']),audit['attempts'],None if published else 'MISSING_CORE_METRIC' if window['status']=='partial' else 'TASK_NOT_PUBLISHED',None if published else 'Task not published because a required window has unavailable core metrics'))
                    result_id=cur.lastrowid
                    cur.execute('INSERT INTO ods_collect_payload (result_id,artifact_path,sha256,byte_length,json_pointer,schema_version,saved_at) VALUES (%s,%s,%s,%s,%s,1,%s)',
                        (result_id,str(path),sha,len(raw),'/windows/'+window['label'],dt.datetime.now(TZ).replace(tzinfo=None)))
                    if not published:continue
                    m=window['metrics']
                    cur.execute('INSERT INTO fact_store_trade_window (result_id,store_code,platform,platform_store_id,business_date,window_type,window_start,window_end,compare_start,compare_end,transaction_amount,amount_ring_ratio,average_sold_skus,average_on_sale_skus,average_available_skus,product_turnover_rate,tirzepatide_amount,non_tirzepatide_ratio,currency,amount_unit,observed_at) VALUES ('+','.join(['%s']*21)+')',
                        (result_id,self.store,'美团',document['platform_store_id'],window['end'],window['window_type'],window['start'],window['end'],window['compare_start'],window['compare_end'],m['amount'],m['ring_ratio'],m['average_sold_skus'],m['average_on_sale_skus'],m['average_available_skus'],m['product_turnover_rate'],m['tirzepatide_amount'],m['non_tirzepatide_ratio'],'CNY','yuan',window['observed_at'].replace(tzinfo=None)))
                cur.execute('UPDATE ops_collect_run SET 状态=%s,结束时间=%s,行数=%s,说明=%s,capture_sha256=%s,published_capture_uuid=%s WHERE 批次ID=%s',
                    (status,dt.datetime.now(TZ).replace(tzinfo=None),2 if status=='success' else 0,'Direct ec_dw platform collection; imported/input tables untouched',sha,self.capture if status=='success' else None,self.run_id))
            self.conn.commit()
        except Exception:
            self.conn.rollback();raise
        return {'state':status,'run_id':self.run_id,'replayed':False,'rows':2 if status=='success' else 0,'artifact':str(path),'sha256':sha}

    def fail(self,error,path=None):
        if self.run_id is None or self.replayed:return
        self.conn.rollback()
        message=(type(error).__name__+': '+str(error))[:450]
        with self.conn.cursor() as cur:
            cur.execute("UPDATE ops_collect_run SET 状态='failed',结束时间=%s,说明=%s,capture_sha256=%s WHERE 批次ID=%s AND 状态<>'success'",
                (dt.datetime.now(TZ).replace(tzinfo=None),message,checksum(Path(path).read_bytes()) if path and Path(path).exists() else None,self.run_id))
        self.conn.commit()

    def close(self):
        if self.lock:
            try:
                with self.conn.cursor() as cur:cur.execute('SELECT RELEASE_LOCK(%s)',(self.lock,))
            except Exception:pass
        self.conn.close()
