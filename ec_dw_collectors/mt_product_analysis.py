#!/usr/bin/env python3
"""One verified store, direct ec_dw writes. Does not invoke legacy persistence."""
import argparse
import datetime as dt
import importlib
import json
import os
import sys
import uuid
from pathlib import Path
from contract import COLLECTOR,TZ,business_evidence,canonical,checksum,prepare
from writer import Writer

def write_state(path,result):
    if not path:return
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(canonical(result),encoding='utf-8');os.replace(temporary,path)

def save(document,root):
    root=Path(root).resolve();folder=root/'meituan'/'mt-baomin'/document['started_at'][:10];folder.mkdir(parents=True,exist_ok=True,mode=0o750)
    path=folder/(str(uuid.UUID(document['run_uuid']))+'.json');temporary=path.with_suffix('.tmp')
    raw=canonical(document).encode('utf-8')
    if path.exists():
        if path.read_bytes()!=raw:raise ValueError('Refusing to overwrite original evidence')
        return path
    with temporary.open('xb') as handle:handle.write(raw)
    os.chmod(temporary,0o640);os.replace(temporary,path);return path

def source_identity(path,store_code):
    c=json.loads(Path(path).read_text());settings=c.get('meituan_store') or c.get('store') or {}
    if store_code!='mt-baomin' or str(settings.get('poi_id',''))!='32581900' or settings.get('client')!='保民' or settings.get('platform')!='美团':
        raise ValueError('Explicit source configuration for Baomin Meituan is required; no default store fallback')
    return str(settings['poi_id'])

def run(args):
    import pymysql
    config=json.loads(Path(args.dw_config).read_text())
    if config.get('database')!='ec_dw':raise ValueError('Production collector must connect to ec_dw')
    allowed={'host','port','user','password','database','charset'}
    if set(config)-allowed:raise ValueError('Unknown database config keys')
    if config.get('user') in ['root','ec_mid_app']:raise ValueError('Dedicated platform collector account required')
    poi=source_identity(args.source_config,args.store_code)
    sys.path.insert(0,str(Path(args.source_dir).resolve()))
    source=importlib.import_module('meituan_product_analysis')
    source_file=Path(source.__file__);version=checksum(source_file.read_bytes())
    document=json.loads(Path(args.replay).read_text()) if args.replay else {
        'schema_version':1,'run_uuid':str(uuid.uuid4()),'collector':COLLECTOR,'source_version':version,
        'store_code':args.store_code,'platform_store_id':poi,'started_at':dt.datetime.now(TZ).isoformat(),
        'windows':{},'source_unit_contract':'existing product-analysis parser outputs CNY/yuan; ratios are fractions'}
    if document['store_code']!=args.store_code or document['platform_store_id']!=poi:raise ValueError('Replay store mismatch')
    if args.replay:prepare(document)
    conn=pymysql.connect(**config,autocommit=False,connect_timeout=10)
    writer=Writer(conn);artifact=Path(args.replay).resolve() if args.replay else None
    try:
        writer.start(document,checksum(artifact.read_bytes()) if artifact else None)
        if writer.replayed:return writer.finish(document,artifact)
        if not args.replay:
            write_state(args.state_file,dict(state='running',collector=COLLECTOR,store_code=args.store_code,started_at=document['started_at'],run_id=writer.run_id))
            def observed(label,evidence):document['windows'][label]=business_evidence(evidence)
            source.collect(dt.datetime.fromisoformat(document['started_at']),args.port,poi,on_period=observed)
            if checksum(source_file.read_bytes())!=version:raise RuntimeError('Source collector changed during capture')
            document['finished_at']=dt.datetime.now(TZ).isoformat()
            artifact=save(document,args.raw_root)
        result=writer.finish(document,artifact)
        result.update(collector=COLLECTOR,store_code=args.store_code,poi_id=poi,collected_at=document['finished_at'],
          daily_transaction_amount=document['windows']['daily']['metrics']['amount'],
          near_30_transaction_amount=document['windows']['near_30']['metrics']['amount'],
          daily_tirzepatide_amount=document['windows']['daily']['metrics']['tirzepatide_amount'],
          near_30_tirzepatide_amount=document['windows']['near_30']['metrics']['tirzepatide_amount'])
        if not args.replay:write_state(args.state_file,result)
        return result
    except Exception as error:
        if not args.replay:
            document['finished_at']=dt.datetime.now(TZ).isoformat();document['collection_error']=type(error).__name__
            if artifact is None:artifact=save(document,args.raw_root)
        writer.fail(error,artifact)
        raise
    finally:writer.close()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-config',required=True);parser.add_argument('--dw-config',required=True)
    parser.add_argument('--source-dir',default='/opt/subsidy');parser.add_argument('--store-code',required=True)
    parser.add_argument('--port',type=int,default=9333);parser.add_argument('--raw-root',default='/var/lib/ec-collectors/raw')
    parser.add_argument('--state-file');parser.add_argument('--replay')
    args=parser.parse_args()
    try:
        result=run(args);print(canonical(result));return 0 if result['state']=='success' else 2
    except Exception as error:
        result={'state':'failed','collector':COLLECTOR,'store_code':args.store_code,'finished_at':dt.datetime.now(TZ).isoformat(),'error_type':type(error).__name__,'message':str(error)[:400]}
        if not args.replay:
            try:write_state(args.state_file,result)
            except OSError:pass
        print(canonical(result),file=sys.stderr);return 1

# EC_DW_RESET_PAUSED_20261126
if __name__ == '__main__':
    raise SystemExit('ec_dw has been cleared; writer paused pending new schema')

if __name__=='__main__':raise SystemExit(main())
