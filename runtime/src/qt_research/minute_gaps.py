"""Supplement observed session gaps with actual gateway records, no interpolation."""
import hashlib
import json
from pathlib import Path
import shutil
import time
import pandas as pd
import pyarrow.parquet as pq
from tushare_env.gateway import Gateway, write_new
from .s02_local import checked_file


def grid(day):
    return pd.date_range(day+pd.Timedelta('9h30min'),day+pd.Timedelta('11h30min'),freq='min').append(
        pd.date_range(day+pd.Timedelta('13h1min'),day+pd.Timedelta('15h'),freq='min'))


def intervals(times):
    groups=[]
    for t in times:
        if groups and t-groups[-1][-1]==pd.Timedelta('1min'):groups[-1].append(t)
        else:groups.append([t])
    return groups


def run(args):
    source,out=Path(args.minutes).resolve(),Path(args.out).resolve()
    if out.exists():raise FileExistsError('Complete cache exists; choose a new path')
    manifest=json.loads((source/'manifest.json').read_text())
    # Preflight every original before creating the new destination.
    files=[(item,checked_file(source,item['file'],item['sha256'])) for item in manifest['files']]
    out.mkdir(parents=True,mode=0o700);patches=out/'patches';patches.mkdir()
    provider=Gateway(args.usage_file);records=[];evidence=[]
    for item,file in files:
        f=pq.read_table(file).to_pandas(ignore_metadata=True)
        additions=[]
        selected=f[(f.trade_date>=pd.Timestamp(args.start))&(f.trade_date<=pd.Timestamp(args.end))]
        for day,part in selected.groupby('trade_date'):
            missing=grid(day).difference(pd.DatetimeIndex(part.trade_time))
            for times in intervals(missing):
                params=dict(ts_code=item['symbol'],freq='1min',start_date=str(times[0]),end_date=str(times[-1]))
                name=item['symbol']+'-'+str(day.date())+'-'+times[0].strftime('%H%M')+'.parquet'
                cache=getattr(args,'patch_cache',None)
                cached=Path(cache)/name if cache else None
                if cached and cached.is_file():
                    frame=pd.read_parquet(cached)
                else:
                    for attempt in range(3):
                        try:
                            frame=provider.query('stk_mins',**params);break
                        except RuntimeError as exc:
                            if attempt==2 or not any(x in str(exc).lower() for x in ['timed out','timeout','429','502','503','504']):raise
                            time.sleep(2**attempt)
                required={'ts_code','trade_time','open','high','low','close','vol','amount'}
                if not required<=set(frame.columns):raise ValueError('Missing gap response fields')
                frame.trade_time=pd.to_datetime(frame.trade_time);frame=frame.sort_values('trade_time')
                if not frame.ts_code.eq(item['symbol']).all() or not pd.DatetimeIndex(frame.trade_time).equals(pd.DatetimeIndex(times)):
                    raise ValueError('Gap response does not cover exact requested minute interval')
                if frame[list(required)].isna().any().any() or (frame[['open','high','low','close']]<=0).any().any() or (frame[['vol','amount']]<0).any().any():
                    raise ValueError('Invalid actual gap bars')
                if (frame.high+1e-10<frame[['open','close','low']].max(axis=1)).any() or (frame.low-1e-10>frame[['open','close','high']].min(axis=1)).any():raise ValueError('Gap OHLC incoherent')
                p=patches/name
                with p.open('xb') as h:frame.to_parquet(h,index=False)
                evidence.append(dict(api='stk_mins',provider='configured user-supplied GET gateway',params=params,rows=len(frame),
                                     file=str(p.relative_to(out)),sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
                if cached and cached.is_file():evidence[-1]['reused_patch_sha256']=hashlib.sha256(cached.read_bytes()).hexdigest()
                frame['trade_date']=frame.trade_time.dt.normalize();frame['adj_factor']=float('nan')
                additions.append(frame[f.columns]);time.sleep(.25)
                print(json.dumps(dict(symbol=item['symbol'],day=str(day.date()),added_rows=len(frame))),flush=True)
        dest=out/item['file']
        if additions:
            f=pd.concat([f,*additions],ignore_index=True).sort_values('trade_time')
            if f.trade_time.duplicated().any():raise ValueError('Supplement introduced duplicate minute')
            with dest.open('xb') as h:f.to_parquet(h,index=False)
        else:
            with file.open('rb') as a,dest.open('xb') as b:shutil.copyfileobj(a,b)
        dest.chmod(0o600)
        record=dict(item,sha256=hashlib.sha256(dest.read_bytes()).hexdigest(),bytes=dest.stat().st_size,rows=len(f),
                    original_sha256=item['sha256'],null_counts={k:int(v) for k,v in f.isna().sum().items() if v},
                    daily_row_counts={str(k):int(v) for k,v in f.groupby('trade_date').size().value_counts().items()})
        records.append(record)
    write_new(out/'manifest.json',dict(manifest,files=records,supplements=evidence,
                                      original_manifest_sha256=hashlib.sha256((source/'manifest.json').read_bytes()).hexdigest(),
                                      factor_policy='supplement has no native factor; S02 requires verified SDK daily factors'))
    return dict(out=str(out),intervals=len(evidence),added_rows=sum(x['rows'] for x in evidence))
