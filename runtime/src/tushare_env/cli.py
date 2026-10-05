import argparse
import contextlib
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import sys
from .runtime import ROOT, api, settings, safe_error


def main():
    parser = argparse.ArgumentParser(description='Independent Tushare development environment')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    query = sub.add_parser('query', help='One read-only data API call into a new immutable cache directory')
    query.add_argument('api')
    query.add_argument('--params', default='{}')
    query.add_argument('--out', required=True)
    probe = sub.add_parser('probe', help='Bounded historical coverage probe')
    probe.add_argument('--out', required=True)
    probe.add_argument('--days', nargs='+', default=['20240108', '20250108'])
    gateway = sub.add_parser('gateway-probe', help='Test user-provided GET/X-API-Key gateway and cache minute samples')
    gateway.add_argument('--usage-file', required=True)
    gateway.add_argument('--freq', choices=['1min','5min'], default='1min')
    gateway.add_argument('--out', required=True)
    from qt_research.public_tushare import SYMBOLS
    gateway.add_argument('--symbols', nargs='+', choices=SYMBOLS, default=SYMBOLS)
    gateway.add_argument('--days', nargs='+', default=['20240108','20250108'])
    resample = sub.add_parser('resample-minutes', help='Aggregate complete cached one-minute sessions into five-minute bars')
    resample.add_argument('--source', required=True)
    resample.add_argument('--out', required=True)
    bt = sub.add_parser('backtest', help='Offline baseline run, inspect or doctor')
    bt.add_argument('args', nargs=argparse.REMAINDER)
    research = sub.add_parser('research', help='Strategy versions, runs, import, scan and database backup')
    research.add_argument('args', nargs=argparse.REMAINDER)
    dashboard = sub.add_parser('dashboard', help='Local read-only research Web service')
    dashboard.add_argument('args', nargs=argparse.REMAINDER)
    mcp = sub.add_parser('mcp')
    mcp.add_argument('action', choices=['probe', 'serve'])
    args = parser.parse_args()
    try:
        if args.command == 'doctor':
            output = {'root': str(ROOT), 'python': sys.executable,
                      'versions': {n: importlib.metadata.version(n) for n in ['tushare','pandas','pyarrow','openpyxl','plotly','mcp']},
                      'provider': settings()['provider'], 'sdk_url': settings()['sdk_url'],
                      'token_file_exists': Path(settings()['token_file']).is_file(),
                      'skill_installed': (Path.home()/'.codex/skills/tushare').exists()}
        elif args.command == 'query':
            if not re.fullmatch(r'[a-z][a-z0-9_]*', args.api):
                raise ValueError('Invalid data API name')
            registry = json.loads((ROOT/'config/read-api-registry.json').read_text())
            if args.api not in registry:
                raise ValueError('Data API not in verified read-only registry')
            params = json.loads(args.params)
            if not isinstance(params, dict) or any(k in params for k in ['token','api_name','ts_type_name']):
                raise ValueError('Parameters must be an object without credential or routing overrides')
            out = Path(args.out)
            if out.exists(): raise FileExistsError('Output exists; use a new cache directory')
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                if args.api == 'pro_bar':
                    import tushare as ts
                    frame = ts.pro_bar(api=api(), **params)
                    if frame is None: raise ValueError('pro_bar returned no data')
                else:
                    frame = api().query(args.api, **params)
            out.mkdir(parents=True, mode=0o700)
            file = out / 'data.parquet'
            fd = os.open(file, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as stream: frame.to_parquet(stream, index=False)
            output = {'api': args.api, 'params': params, 'rows': len(frame), 'columns': list(frame.columns),
                      'file': str(file.resolve()), 'provider': settings()['provider'],
                      'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
            dest = out/'manifest.json'
            fd = os.open(dest, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as stream: json.dump(output, stream, ensure_ascii=False, indent=2)
        elif args.command == 'probe':
            from qt_research import public_tushare
            sys.argv = ['public_tushare','probe','--token-file',settings()['token_file'],
                        '--sdk-url',settings()['sdk_url'],'--out',args.out,'--days',*args.days]
            return public_tushare.main()
        elif args.command == 'gateway-probe':
            from .gateway import probe
            return probe(args.usage_file,args.out,args.days,args.symbols,args.freq)
        elif args.command == 'resample-minutes':
            from .resample import run
            output = run(args.source, args.out)
        elif args.command == 'backtest':
            from qt_research import local_cli
            sys.argv = ['local_cli', *args.args]
            return local_cli.main()
        elif args.command == 'research':
            from qt_research.research_catalog import main as research_main
            output = research_main(args.args)
        elif args.command == 'dashboard':
            from qt_research.dashboard import main as dashboard_main
            output = dashboard_main(args.args)
        else:
            from . import mcp_bridge
            return mcp_bridge.main(args.action)
        print(json.dumps({'ok': True, 'result': output}, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({'ok':False,'error':{'code':type(exc).__name__,'message':safe_error(exc)}},ensure_ascii=False))
        return 1
