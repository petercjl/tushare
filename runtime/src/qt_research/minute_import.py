"""Import the user supplied eight-fund minute set without modifying originals."""
import hashlib
import json
from pathlib import Path
import shutil
import pandas as pd
import pyarrow.parquet as pq
from .s02_local import SYMBOLS


def run(args):
    source, out = Path(args.source).resolve(), Path(args.out).resolve()
    if out.exists(): raise FileExistsError('Minute cache exists; choose a new directory')
    records=[]
    for symbol in SYMBOLS:
        p=source/(symbol+'.parquet')
        digest=hashlib.sha256(p.read_bytes()).hexdigest()
        f=pq.read_table(p).to_pandas(ignore_metadata=True)
        required={'ts_code','open','high','low','close','vol','amount','adj_factor','trade_date','trade_time'}
        if not required <= set(f.columns) or f.empty: raise ValueError('Missing minute columns/data: '+symbol)
        times=pd.to_datetime(f.trade_time);days=pd.to_datetime(f.trade_date)
        # S02 replaces the minute factor with the verified SDK daily factor.
        # Preserve and disclose a missing raw factor rather than imputing it.
        invalid=(f[['open','high','low','close','adj_factor']]<=0).any(axis=1)|(f[['vol','amount']]<0).any(axis=1)|f[list(required-{'adj_factor'})].isna().any(axis=1)
        invalid |= (f.high+1e-10<f[['open','close','low']].max(axis=1))|(f.low-1e-10>f[['open','close','high']].min(axis=1))
        if invalid.any() or times.duplicated().any() or not times.is_monotonic_increasing or not times.dt.normalize().equals(days) or not f.ts_code.eq(symbol).all():
            raise ValueError('Invalid minute identity/OHLC/time: '+symbol)
        records.append(dict(symbol=symbol,file=p.name,sha256=digest,bytes=p.stat().st_size,rows=len(f),
                            columns=list(f.columns),first_time=str(times.min()),last_time=str(times.max()),
                            source_path=str(p),null_counts={k:int(v) for k,v in f.isna().sum().items() if v},
                            daily_row_counts={str(k):int(v) for k,v in f.groupby('trade_date').size().value_counts().items()}))
    out.mkdir(parents=True,mode=0o700)
    for item in records:
        p=source/item['file'];q=out/item['file']
        with p.open('rb') as a,q.open('xb') as b: shutil.copyfileobj(a,b)
        q.chmod(0o600)
        if hashlib.sha256(q.read_bytes()).hexdigest()!=item['sha256']: raise ValueError('Copy checksum differs')
    manifest=dict(source='user-supplied offline Parquet files',originals_preserved=True,files=records,
                  units='retained without scaling; daily reference reconciliation required by runner')
    with (out/'manifest.json').open('x') as f:json.dump(manifest,f,ensure_ascii=False,indent=2)
    return dict(out=str(out),files=len(records),rows=sum(x['rows'] for x in records))
