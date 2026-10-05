"""Read-only public Tushare doctor and bounded historical coverage probe."""
import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import time

SYMBOLS = ['518880.SH', '159985.SZ', '501018.SH', '161226.SZ',
           '513100.SH', '159915.SZ', '511220.SH', '511880.SH']
ENDPOINT = 'https://api.waditu.com/dataapi'


def credential(path):
    lines = Path(path).read_text().splitlines()
    if len(lines) == 1 and re.fullmatch(r'[0-9a-fA-F]{40,100}', lines[0].strip()):
        return lines[0].strip()
    candidates = set()
    for i, line in enumerate(lines):
        label = line.lower()
        if 'tushare' in label and ('公版' in label or 'public' in label):
            # Stop at next nonempty line: never drift into a different credential block.
            following = next((s.strip() for s in lines[i+1:] if s.strip()), '')
            if re.fullmatch(r'[0-9a-fA-F]{40,100}', following):
                candidates.add(following)
    if len(candidates) != 1:
        raise ValueError('Expected exactly one token under an explicit public Tushare label')
    return candidates.pop()


def describe(frame, api, kwargs):
    import pandas as pd
    if not isinstance(frame, pd.DataFrame):
        raise ValueError('SDK result is not a DataFrame')
    summary = {'state': 'empty' if frame.empty else 'nonempty',
               'rows': len(frame), 'columns': list(frame.columns)}
    if frame.empty:
        return summary
    if api != 'etf_mins':
        return summary
    required = {'ts_code', 'trade_time', 'open', 'high', 'low', 'close', 'vol', 'amount'}
    if not required <= set(frame.columns):
        raise ValueError('Minute schema missing required fields')
    times = pd.to_datetime(frame.trade_time, errors='raise')
    if times.duplicated().any():
        raise ValueError('Duplicate minute timestamps')
    if not frame.ts_code.eq(kwargs['ts_code']).all():
        raise ValueError('Unexpected returned symbol')
    if ((times < pd.Timestamp(kwargs['start_date'])) | (times > pd.Timestamp(kwargs['end_date']))).any():
        raise ValueError('Minute outside requested interval')
    import numpy as np
    prices = frame[['open', 'high', 'low', 'close']].apply(pd.to_numeric, errors='raise')
    quantities = frame[['vol', 'amount']].apply(pd.to_numeric, errors='raise')
    if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
        raise ValueError('Invalid OHLC prices')
    if not np.isfinite(quantities.to_numpy()).all() or (quantities < 0).any().any():
        raise ValueError('Invalid volume or amount')
    if (prices.high < prices[['open', 'close', 'low']].max(axis=1)).any() or (prices.low > prices[['open', 'close', 'high']].min(axis=1)).any():
        raise ValueError('Inconsistent OHLC range')
    minutes = {'1min': 1, '5min': 5}.get(kwargs.get('freq', '1min'))
    if minutes is None: raise ValueError('Unsupported coverage frequency')
    expected = pd.date_range(times.min().normalize() + pd.Timedelta(hours=9, minutes=30 + minutes),
                             times.min().normalize() + pd.Timedelta(hours=11, minutes=30), freq=f'{minutes}min').append(
               pd.date_range(times.min().normalize() + pd.Timedelta(hours=13, minutes=minutes),
                             times.min().normalize() + pd.Timedelta(hours=15), freq=f'{minutes}min'))
    missing = expected.difference(pd.DatetimeIndex(times))
    summary.update(first_time=str(times.min()), last_time=str(times.max()),
                   missing_standard_minutes=[str(x) for x in missing],
                   has_1400=bool((times.dt.strftime('%H:%M:%S') == '14:00:00').any()),
                   volume_unit='shares', amount_unit='CNY',
                   sample=frame.sort_values('trade_time').head(2).to_dict(orient='records'))
    return summary


def write_new(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


class HttpProvider:
    """Official language-neutral API, with HTTP and API errors checked explicitly."""
    def __init__(self, token, timeout=12):
        self.token, self.timeout = token, timeout

    def query(self, api_name, **kwargs):
        import pandas as pd
        import requests
        response = requests.post('https://api.tushare.pro', json={
            'api_name': api_name, 'token': self.token, 'params': kwargs, 'fields': ''
        }, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get('code') != 0:
            raise RuntimeError(f"API code {payload.get('code')}: {payload.get('msg')}")
        data = payload['data']
        return pd.DataFrame(data['items'], columns=data['fields'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['doctor', 'probe'])
    parser.add_argument('--token-file')
    parser.add_argument('--sdk-url', help='Explicit HTTPS endpoint from environment configuration')
    parser.add_argument('--usage-file', help='Read HTTPS SDK endpoint assignment from user-provided instructions; never execute them')
    parser.add_argument('--transport', choices=['sdk', 'http'], default='sdk')
    parser.add_argument('--out')
    parser.add_argument('--days', nargs='+', default=['20240108', '20250108'])
    parser.add_argument('--symbols', nargs='+', choices=SYMBOLS, default=SYMBOLS)
    args = parser.parse_args()
    import tushare as ts
    endpoint = ENDPOINT
    if args.usage_file or args.sdk_url:
        if args.transport != 'sdk':
            parser.error('--usage-file applies to sdk transport')
        usage = Path(args.usage_file).read_text() if args.usage_file else ''
        urls = re.findall(r'pro\._DataApi__http_url\s*=\s*[\"\'](https://[^\"\']+)[\"\']', usage)
        if args.sdk_url:
            urls = [args.sdk_url]
        if len(urls) != 1:
            parser.error('Expected one explicit HTTPS SDK endpoint assignment')
        from urllib.parse import urlsplit
        parsed = urlsplit(urls[0])
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            parser.error('SDK endpoint must contain no embedded credentials, query or fragment')
        endpoint = urls[0]
    environment = {'python_entry': os.sys.executable, 'public_sdk': importlib.metadata.version('tushare'),
                   'public_entry': ts.__file__, 'endpoint': endpoint if args.transport == 'sdk' else 'https://api.tushare.pro',
                   'transport': args.transport,
                   'sdk_default_endpoint': 'http://api.waditu.com/dataapi',
                   'transport_note': 'Explicit HTTPS endpoint override on verified SDK 1.4.29',
                   'endpoint_source': 'environment configuration' if args.sdk_url else 'user-provided usage file' if args.usage_file else 'public Tushare'}
    if args.command == 'doctor':
        print(json.dumps(environment, ensure_ascii=False, indent=2)); return
    if not args.token_file or not args.out:
        parser.error('--token-file and --out required')
    for day in args.days:
        value = dt.datetime.strptime(day, '%Y%m%d').date()
        if value >= dt.date.today():
            parser.error('Probe dates must be historical')
    out = Path(args.out)
    if out.exists():
        raise FileExistsError('Output exists; use a new snapshot directory')
    token = credential(args.token_file)
    if args.transport == 'sdk':
        api = ts.pro_api(token, timeout=12)
        # SDK exposes no public URL setter. Keep token in memory; do not call set_token.
        api._DataApi__http_url = endpoint
    else:
        api = HttpProvider(token)
    out.mkdir(parents=True, mode=0o700)
    report = {'created_at': dt.datetime.now().astimezone().isoformat(), 'environment': environment,
              'purpose': 'bounded connectivity and historical minute coverage sample', 'results': []}
    day = args.days[0]
    cases = [('trade_cal', {'exchange': 'SSE', 'start_date': day, 'end_date': day})]
    for name in ['fund_daily', 'fund_nav', 'fund_adj']:
        cases.append((name, {'ts_code': '518880.SH', 'start_date': day, 'end_date': day}))
    for day in args.days:
        date = dt.datetime.strptime(day, '%Y%m%d').strftime('%Y-%m-%d')
        for symbol in args.symbols:
            cases.append(('etf_mins', {'ts_code': symbol, 'freq': '1min',
                                     'start_date': date + ' 09:00:00', 'end_date': date + ' 15:01:00'}))
    for number, (name, kwargs) in enumerate(cases):
        if number:
            time.sleep(1)
        entry = {'api': name, 'params': kwargs}
        began = time.monotonic()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                frame = api.query(name, **kwargs)
            entry.update(describe(frame, name, kwargs))
            dest = out / f'{number:03d}-{name}-{kwargs.get("ts_code", "SSE")}.parquet'
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                frame.to_parquet(stream, index=False)
            entry.update(file=dest.name, sha256=hashlib.sha256(dest.read_bytes()).hexdigest())
        except Exception as exc:
            message = str(exc).replace(token, '[REDACTED]')
            message = re.sub(r'[0-9a-fA-F]{40,100}', '[REDACTED]', message)
            entry.update(state='error', error_type=type(exc).__name__, error=message[:1200])
        entry['elapsed_seconds'] = round(time.monotonic() - began, 3)
        report['results'].append(entry)
        print(json.dumps({k: v for k, v in entry.items() if k not in {'sample', 'columns', 'missing_standard_minutes'}}, ensure_ascii=False), flush=True)
        if name == 'trade_cal' and entry['state'] != 'nonempty':
            report['stopped_reason'] = 'Calendar connectivity failed; minute calls skipped'
            break
        if name == 'etf_mins' and entry['state'] == 'error' and any(s in entry['error'] for s in ['没有访问', '权限', '每分钟最多', '每小时最多']):
            report['stopped_reason'] = 'Minute access or quota denied; remaining requests skipped'
            break
    report['errors'] = sum(r['state'] == 'error' for r in report['results'])
    write_new(out / 'report.json', report)
    print(json.dumps({'report': str(out / 'report.json'), 'queries': len(report['results']), 'errors': report['errors']}, ensure_ascii=False))

    return 1 if report['errors'] else 0

if __name__ == '__main__':
    raise SystemExit(main())
