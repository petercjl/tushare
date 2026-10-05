"""Read-only HTTP GET / X-API-Key adapter, from user-provided usage notes."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
from qt_research.public_tushare import SYMBOLS, describe

ALLOWED = {'trade_cal', 'fund_daily', 'fund_nav', 'fund_adj', 'etf_mins', 'stk_mins'}

def redact(text, key=''):
    if key: text = text.replace(key, '[REDACTED]')
    return re.sub(r'tsr_[A-Za-z0-9_-]+', '[REDACTED]', str(text))

class MinutePending(RuntimeError):
    pass

class Gateway:
    def __init__(self, usage_file):
        text = Path(usage_file).read_text()
        match = re.search(r'(?m)^卡号[：:]\s*(\S+)', text)
        url = re.search(r'U\s*=\s*"(https://[^"\s]+)"', text)
        if not match or not url: raise ValueError('Expected card label and HTTPS U endpoint in usage file')
        self.key = match.group(1)
        self.url = url.group(1).rstrip('/')
        parts = urlsplit(self.url)
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('Service URL must not contain credentials')

    def query(self, name, **params):
        import requests
        import pandas as pd
        if name not in ALLOWED: raise ValueError('Gateway data API not allowlisted')
        if any(k in params for k in ('token', 'key', 'api_key', 'url')):
            raise ValueError('Credentials and routing are managed outside data parameters')
        try:
            response = requests.get(self.url + '/' + name, params=params,
                                    headers={'X-API-Key': self.key}, timeout=(8,20))
            response.raise_for_status()
            payload = response.json()
            if response.status_code == 202 and payload.get('error') == 'minute_data_pending':
                raise MinutePending('HTTP 202: minute_data_pending')
            if payload.get('code') != 0:
                raise RuntimeError(f"API code {payload.get('code')}: {payload.get('msg', payload.get('message', payload.get('error', '')))}")
            data = payload['data']
            frame = pd.DataFrame(data['items'], columns=data['fields'])
            if self.key in frame.to_json(): raise ValueError('Credential unexpectedly present in data response')
            return frame
        except MinutePending:
            raise
        except Exception as exc:
            raise RuntimeError(redact(str(exc), self.key)) from None

def write_new(path, payload):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)

def probe(usage_file, out, days, symbols=SYMBOLS, freq="1min"):
    if freq not in ("1min", "5min"): raise ValueError("Supported probe frequencies: 1min, 5min")
    provider = Gateway(usage_file)
    target = Path(out)
    if target.exists(): raise FileExistsError('Output exists; choose a new directory')
    for day in days:
        if dt.datetime.strptime(day,'%Y%m%d').date() >= dt.date.today():
            raise ValueError('Probe requires historical dates')
    target.mkdir(parents=True, mode=0o700)
    first = days[0]
    cases = [('trade_cal', dict(exchange='SSE',start_date=first,end_date=first))]
    for name in ('fund_daily','fund_nav','fund_adj'):
        cases.append((name, dict(ts_code='518880.SH',start_date=first,end_date=first)))
    for day in days:
        date = dt.datetime.strptime(day,'%Y%m%d').strftime('%Y-%m-%d')
        for symbol in symbols:
            cases.append(('stk_mins', dict(ts_code=symbol,freq=freq,
                         start_date=date+' 09:00:00',end_date=date+' 15:01:00')))
    report = {'created_at':dt.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
              'provider':'user-supplied HTTP GET gateway', 'endpoint':provider.url,
              'tls_verified':True, 'authentication':'X-API-Key read from usage file',
              'days':days, 'symbols':symbols, 'freq':freq, 'results':[],
              'scope':'sample sessions only, not full two-year coverage'}
    for number,(name,params) in enumerate(cases):
        if number: time.sleep(1)
        item = {'api':name,'params':params}
        try:
            for attempt in range(3):
                try:
                    frame = provider.query(name,**params)
                    item['attempts'] = attempt+1
                    break
                except MinutePending:
                    if attempt == 2: raise
                    time.sleep(2**(attempt+1))
            # stk_mins returns the same OHLCV schema; validate times and units as minutes.
            item.update(describe(frame,'etf_mins' if name=='stk_mins' else name,params))
            file=target/f'{number:03d}-{name}-{params.get("ts_code","SSE")}.parquet'
            fd=os.open(file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'wb') as stream:frame.to_parquet(stream,index=False)
            item.update(file=file.name,sha256=hashlib.sha256(file.read_bytes()).hexdigest())
        except MinutePending as exc:
            item.update(state='pending',attempts=3,error=str(exc))
        except Exception as exc:
            item.update(state='error',error_type=type(exc).__name__,error=redact(str(exc),provider.key))
        report['results'].append(item)
        print(json.dumps({k:v for k,v in item.items() if k not in ('columns','sample','missing_standard_minutes')},ensure_ascii=False),flush=True)
        if name=='trade_cal' and item['state']!='nonempty':
            report['stopped_reason']='Calendar failed; later queries skipped';break
        if name=='stk_mins' and item['state']=='error' and any(s in item['error'] for s in ('权限','每分钟最多','每小时最多')):
            report['stopped_reason']='Minute access or quota denied; remaining queries skipped';break
    report['pending']=sum(x['state']=='pending' for x in report['results'])
    report['errors']=sum(x['state']=='error' for x in report['results'])
    write_new(target/'report.json',report)
    print(json.dumps({'report':str(target/'report.json'),'queries':len(report['results']),
                      'errors':report['errors'],'pending':report['pending'],'minute_rows':sum(x.get('rows',0) for x in report['results'] if x['api']=='stk_mins')},ensure_ascii=False))
    return 1 if report['errors'] or report['pending'] else 0
