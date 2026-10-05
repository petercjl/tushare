"""Offline five-minute OHLCV aggregation with immutable cache provenance."""
import hashlib
import json
import os
from pathlib import Path
import pandas as pd
from qt_research.public_tushare import describe
from .gateway import write_new


def aggregate(frame, params):
    checked = describe(frame, 'etf_mins', dict(params, freq='1min'))
    if checked['state'] != 'nonempty' or checked['missing_standard_minutes']:
        raise ValueError('Complete one-minute session required')
    times = pd.to_datetime(frame.trade_time)
    if times.dt.normalize().nunique() != 1:
        raise ValueError('One trading session required per source file')
    regular = pd.date_range(times.min().normalize()+pd.Timedelta(hours=9,minutes=31), periods=120,freq='min').append(pd.date_range(times.min().normalize()+pd.Timedelta(hours=13,minutes=1), periods=120,freq='min'))
    allowed = regular.append(pd.DatetimeIndex([times.min().normalize()+pd.Timedelta(hours=9,minutes=30)]))
    if not times.isin(allowed).all():
        raise ValueError('Unsupported timestamp outside regular session or opening auction')
    work = frame.assign(trade_time=times).sort_values('trade_time')
    work['bucket'] = work.trade_time.dt.ceil('5min')
    result = work.groupby('bucket',sort=True).agg(ts_code=('ts_code','first'),open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),vol=('vol','sum'),amount=('amount','sum')).reset_index().rename(columns={'bucket':'trade_time'})
    result['freq'] = '5MIN'
    summary = describe(result,'etf_mins',dict(params,freq='5min'))
    if summary['missing_standard_minutes']:
        raise ValueError('Incomplete aggregated five-minute session')
    return result, summary


def run(source, out):
    source = Path(source).resolve()
    target = Path(out)
    if target.exists(): raise FileExistsError('Output exists; choose a new directory')
    manifest = source/'report.json'
    report = json.loads(manifest.read_text())
    prepared = []
    for item in report['results']:
        if item['api'] not in ('stk_mins','etf_mins') or item.get('state') != 'nonempty': continue
        if item['params'].get('freq') != '1min': raise ValueError('Only one-minute input supported')
        file = (source/item['file']).resolve()
        if not file.is_relative_to(source): raise ValueError('Source file outside manifest directory')
        if hashlib.sha256(file.read_bytes()).hexdigest() != item['sha256']: raise ValueError('Source hash mismatch')
        frame, summary = aggregate(pd.read_parquet(file), item['params'])
        prepared.append((item, frame, summary))
    if not prepared: raise ValueError('No complete one-minute sessions available')
    target.mkdir(parents=True,mode=0o700)
    output={'provider':'local aggregation of cached one-minute market data','source_manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest(),'source_provider':report.get('provider'),'freq':'5min','bar_time':'right endpoint; opening auction retained separately at 09:30','results':[]}
    for index,(item,frame,summary) in enumerate(prepared):
        file=target/f'{index:03d}-5min-{item["params"]["ts_code"]}.parquet'
        fd=os.open(file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as stream: frame.to_parquet(stream,index=False)
        output['results'].append(dict(summary,api='local_resample',params=dict(item['params'],freq='5min'),file=file.name,sha256=hashlib.sha256(file.read_bytes()).hexdigest(),source_file=item['file'],source_sha256=item['sha256']))
    write_new(target/'report.json',output)
    return {'sessions':len(prepared),'rows':sum(len(frame) for _,frame,_ in prepared),'report':str((target/'report.json').resolve())}
