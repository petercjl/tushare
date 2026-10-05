"""Prepare immutable annual S02 inputs using the configured read-only SDK."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import time
import pandas as pd
from tushare_env.runtime import api, settings, safe_error
from .s02_local import SYMBOLS


def plan(start, end):
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start > end:
        raise ValueError('Start must not exceed end')
    warm = start - pd.Timedelta(days=120)
    cases = [('trade_cal', dict(exchange='SSE', start_date=warm.strftime('%Y%m%d'), end_date=end.strftime('%Y%m%d')))]
    for symbol in SYMBOLS:
        for year in range(warm.year, end.year + 1):
            lo = max(warm, pd.Timestamp(year, 1, 1)).strftime('%Y%m%d')
            hi = min(end, pd.Timestamp(year, 12, 31)).strftime('%Y%m%d')
            for name in ('fund_daily', 'fund_adj', 'fund_nav'):
                cases.append((name, dict(ts_code=symbol, start_date=lo, end_date=hi)))
    return cases


def prepare(args):
    out = Path(args.out)
    cases = plan(args.start, args.end)
    spec = dict(schema=1, start=args.start, end=args.end, cases=cases, provider=settings()['provider'])
    if out.exists():
        if not args.resume or (out/'report.json').exists():
            raise FileExistsError('Cache exists; use a new directory or resume an incomplete cache')
        saved = json.loads((out/'plan.json').read_text())
        if saved != json.loads(json.dumps(spec)):
            raise ValueError('Resume plan differs')
    else:
        out.mkdir(parents=True, mode=0o700)
        with (out/'plan.json').open('x') as f:
            json.dump(spec, f)
    client = api()
    reusable = {}
    for root in getattr(args, 'reuse', None) or []:
        root = Path(root).resolve()
        previous = json.loads((root/'report.json').read_text())
        for item in previous['results']:
            if item.get('params') and item.get('provider') == spec['provider']:
                source = (root/item['file']).resolve()
                if not source.is_relative_to(root) or hashlib.sha256(source.read_bytes()).hexdigest() != item['sha256']:
                    raise ValueError('Reusable cache checksum/path mismatch')
                key = (item['api'], json.dumps(item['params'], sort_keys=True))
                reusable[key] = (source, item)
    results = []
    required = {'trade_cal': {'cal_date','is_open'}, 'fund_daily': {'ts_code','trade_date','close','vol','amount'},
                'fund_adj': {'ts_code','trade_date','adj_factor'}, 'fund_nav': {'ts_code','nav_date','ann_date','unit_nav'}}
    for i, (name, params) in enumerate(cases):
        folder = out/f'{i:03d}-{name}'
        if folder.exists():
            record = json.loads((folder/'manifest.json').read_text())
            if record['api'] != name or record['params'] != params:
                raise ValueError('Cached query differs from plan')
            file = out/record['file']
            if hashlib.sha256(file.read_bytes()).hexdigest() != record['sha256']:
                raise ValueError('Cached query checksum mismatch')
        else:
            reused = reusable.get((name, json.dumps(params, sort_keys=True)))
            if reused:
                frame = pd.read_parquet(reused[0])
            else:
                time.sleep(.25)
                try:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        frame = client.query(name, **params)
                except Exception as exc:
                    raise RuntimeError(safe_error(exc)) from None
            if not required[name] <= set(frame.columns):
                raise ValueError('Missing columns: '+name)
            key = 'nav_date' if name == 'fund_nav' else 'cal_date' if name == 'trade_cal' else 'trade_date'
            days = frame[key].astype(str)
            if not frame.empty and ((days < params['start_date']) | (days > params['end_date'])).any():
                raise ValueError('Returned dates outside request: '+name)
            if name != 'trade_cal' and not frame.empty and not frame.ts_code.eq(params['ts_code']).all():
                raise ValueError('Returned symbol differs: '+name)
            if frame.duplicated([key]).any():
                raise ValueError('Duplicate daily rows: '+name)
            folder.mkdir(mode=0o700)
            file = folder/'data.parquet'
            with file.open('xb') as f:
                frame.to_parquet(f, index=False)
            record = dict(api=name,params=params,provider=spec['provider'],state='empty' if frame.empty else 'nonempty',
                          rows=len(frame),file=str(file.relative_to(out)),sha256=hashlib.sha256(file.read_bytes()).hexdigest())
            if reused:
                record['reused_source'] = dict(path=str(reused[0]),sha256=reused[1]['sha256'])
            with (folder/'manifest.json').open('x') as f:
                json.dump(record,f)
        results.append(record)
        print(json.dumps(dict(progress=f'{i+1}/{len(cases)}',api=name,symbol=params.get('ts_code'),rows=record['rows'])),flush=True)
    report = dict(schema=1, purpose='S02 annual offline inputs with 120-day warm-up', results=results, errors=0)
    with (out/'report.json').open('x') as f:
        json.dump(report,f,ensure_ascii=False,indent=2)
    return dict(snapshot=str(out.resolve()),queries=len(results),rows=sum(r['rows'] for r in results))
