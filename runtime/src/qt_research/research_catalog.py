"""Versioned research catalog. Files remain authoritative, SQLite indexes them."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager

from qt_research.paths import ROOT, ENGINE
DB = ROOT / 'state/research.sqlite3'

def now():
    return datetime.now(timezone.utc).isoformat()

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)

class Catalog:
    def __init__(self, db=DB):
        self.path=Path(db).resolve()
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if not self.path.exists():
            fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        with self.connection() as c:
            version=c.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0,1):raise ValueError('Unsupported catalog schema version')
            c.executescript('''
            CREATE TABLE IF NOT EXISTS strategies(id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,description TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,strategy_id TEXT NOT NULL REFERENCES strategies(id),code_hash TEXT NOT NULL,code_path TEXT,scope TEXT NOT NULL,created TEXT NOT NULL,UNIQUE(strategy_id,code_hash));
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,strategy_id TEXT NOT NULL REFERENCES strategies(id),version_id TEXT REFERENCES versions(id),platform TEXT NOT NULL,status TEXT NOT NULL,started TEXT NOT NULL,finished TEXT,out TEXT UNIQUE,params TEXT NOT NULL,metrics TEXT,valid INTEGER,errors INTEGER,error TEXT,manifest_hash TEXT);
            CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),name TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,UNIQUE(run_id,name));
            CREATE TABLE IF NOT EXISTS comparisons(id TEXT PRIMARY KEY,out TEXT UNIQUE NOT NULL,summary TEXT NOT NULL,local_run_id TEXT REFERENCES runs(id),jq_run_id TEXT REFERENCES runs(id),created TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS runs_strategy ON runs(strategy_id,started);
            PRAGMA user_version=1;
            ''')

    @contextmanager
    def connection(self):
        c=sqlite3.connect(self.path,timeout=30);c.row_factory=sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA journal_mode=WAL')
        try:
            with c:yield c
        finally:c.close()

    def register(self, strategy_id, name=None, kind='strategy', description=''):
        if kind not in ('strategy','probe','baseline'):raise ValueError('Invalid strategy kind')
        with self.connection() as c:
            c.execute('INSERT OR IGNORE INTO strategies VALUES(?,?,?,?,?)',(strategy_id,name or strategy_id,kind,description,now()))
            if name is not None:
                c.execute('UPDATE strategies SET name=?,kind=?,description=? WHERE id=?',(name,kind,description,strategy_id))
        return strategy_id

    def version(self, strategy_id, source=None, expected=None, scope='strategy source'):
        data=Path(source).read_bytes() if source and Path(source).is_file() else None
        h=hashlib.sha256(data).hexdigest() if data is not None else expected
        if expected and h!=expected:data=None;h=expected
        if not h:return None
        target=None
        if data is not None:
            target=self.path.parent/'code'/f'{h}.py';target.parent.mkdir(exist_ok=True,mode=0o700)
            if target.exists():
                if digest(target)!=h:raise ValueError('Code snapshot was modified')
            else:
                fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(fd,'wb') as f:f.write(data)
        identity=hashlib.sha256((strategy_id+'|'+h).encode()).hexdigest()[:24]
        with self.connection() as c:
            c.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?,?,?,?)',(identity,strategy_id,h,str(target) if target else None,scope if target else 'recorded hash; original code unavailable',now()))
        return identity

    def begin(self, strategy_id, source, params, out):
        out=Path(out).resolve()
        if out.exists():raise FileExistsError('Output exists; use a new directory')
        self.register(strategy_id,kind='baseline' if strategy_id=='fixed-quantity-baseline' else 'strategy')
        if not Path(source).is_file():raise FileNotFoundError('Strategy source missing')
        v=self.version(strategy_id,source);rid=uuid.uuid4().hex
        # Capture the engine before execution, separately from strategy code.
        engine={p.name:digest(p) for p in sorted(ENGINE.glob('*.py'))}
        for p in sorted(ENGINE.glob('*.py')):
            h=engine[p.name];dest=self.path.parent/'engine'/f'{h}.py';dest.parent.mkdir(exist_ok=True,mode=0o700)
            if dest.exists():
                if digest(dest)!=h:raise ValueError('Engine snapshot modified')
            else:
                with dest.open('xb') as f:f.write(p.read_bytes())
                os.chmod(dest,0o600)
        params=dict(params,engine_source_hashes=engine,process_id=os.getpid())
        with self.connection() as c:
            c.execute('INSERT INTO runs(id,strategy_id,version_id,platform,status,started,out,params) VALUES(?,?,?,?,?,?,?,?)',(rid,strategy_id,v,'local','running',now(),str(out),dump(params)))
        return rid

    def fail(self,rid,error):
        with self.connection() as c:
            c.execute("UPDATE runs SET status='failed',finished=?,error=?,valid=0 WHERE id=? AND status='running'",(now(),str(error),rid))

    def import_local(self,out,rid=None):
        out=Path(out).resolve();manifest=json.loads((out/'manifest.json').read_text());summary=json.loads((out/'summary.json').read_text())
        checks=json.loads((out/'checksums.json').read_text())
        if not checks:raise ValueError('Empty result checksum manifest')
        if not {'manifest.json','summary.json'}.issubset(checks):raise ValueError('Missing required result checksums')
        for name,h in checks.items():
            if Path(name).name!=name or digest(out/name)!=h:raise ValueError('Result checksum mismatch: '+name)
        mh=digest(out/'manifest.json');sid=manifest['strategy'];self.register(sid,kind='baseline' if sid=='fixed-quantity-baseline' else 'strategy')
        source=ROOT/'strategies'/sid/'strategy.py'
        expected=manifest.get('strategy_source_sha256')
        v=self.version(sid,source,expected)
        with self.connection() as c:
            existing=c.execute('SELECT * FROM runs WHERE out=?',(str(out),)).fetchone()
            if existing and existing['status']!='running':
                if existing['manifest_hash']!=mh:raise ValueError('Previously indexed result changed')
                return existing['id']
            rid=rid or (existing['id'] if existing else uuid.uuid4().hex)
            valid=summary.get('performance_valid')
            if valid is None:valid=manifest.get('performance_valid')
            error_count=summary.get('data_errors')
            status='warning' if valid is False or (isinstance(error_count,int) and error_count>0) else 'completed'
            if not existing:
                c.execute('INSERT INTO runs(id,strategy_id,version_id,platform,status,started,out,params) VALUES(?,?,?,?,?,?,?,?)',(rid,sid,v,'local','importing',datetime.fromtimestamp((out/'manifest.json').stat().st_mtime,timezone.utc).isoformat(),str(out),dump(manifest)))
            else:
                if existing['strategy_id']!=sid:raise ValueError('Strategy identity changed during execution')
                params=json.loads(existing['params']);params.update(manifest)
                c.execute('UPDATE runs SET params=? WHERE id=?',(dump(params),rid))
            c.execute('UPDATE runs SET status=?,finished=?,metrics=?,valid=?,errors=?,manifest_hash=? WHERE id=?',(status,now(),dump(summary),None if valid is None else int(valid),error_count,mh,rid))
            for file in sorted(out.iterdir()):
                if file.is_file() and (file.name in checks or file.name=='checksums.json'):
                    c.execute('INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?)',(uuid.uuid4().hex,rid,file.name,str(file),digest(file)))
        return rid

    def import_jq(self,folder):
        folder=Path(folder).resolve();payload=json.loads((folder/'jq-show.json').read_text());result=json.loads((folder/'jq-result.json').read_text())
        if payload.get('status')!='done' and payload.get('state')!='done':
            # jqcli show uses nested backtest metadata on some releases.
            if 'done' not in str(payload.get('backtest',{})):raise ValueError('JoinQuant completion status not established')
        sid='S02-local-v1';self.register(sid);v=self.version(sid,folder/'jq-original-live.py',scope='JoinQuant original source')
        rid='jq-'+result['id'];metrics=payload['metrics']
        calibration=json.loads((ROOT/'reports/s02-jq-local-comparison-v3/summary.json').read_text())
        if calibration['jq_backtest_id']!=result['id']:raise ValueError('JoinQuant calibration metadata mismatch')
        params={k:calibration[k] for k in ('start','end','initial_cash')}
        params.update(frequency='minute',import_contract='verified S02 calibration export')
        with self.connection() as c:
            c.execute('INSERT OR IGNORE INTO runs(id,strategy_id,version_id,platform,status,started,finished,out,params,metrics,valid,manifest_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(rid,sid,v,'joinquant','completed',now(),now(),str(folder),dump(params),dump(dict(metrics=metrics)),None,digest(folder/'jq-result.json')))
            for p in [folder/'jq-show.json',folder/'jq-result.json',folder/'jq-original-live.py',*sorted((folder/'export/clean').glob('*'))]:
                if p.is_file():c.execute('INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?)',(uuid.uuid4().hex,rid,str(p.relative_to(folder)),str(p),digest(p)))
        return rid

    def import_comparison(self,out):
        out=Path(out).resolve();s=json.loads((out/'summary.json').read_text())
        for name,h in json.loads((out/'checksums.json').read_text()).items():
            if Path(name).name!=name or digest(out/name)!=h:raise ValueError('Comparison checksum mismatch')
        inputs=json.loads((out/'provenance.json').read_text());local=None
        with self.connection() as c:
            for p in inputs:
                if Path(p).name=='manifest.json':
                    row=c.execute('SELECT id FROM runs WHERE out=?',(str(Path(p).parent.resolve()),)).fetchone()
                    if row:local=row['id']
            jq='jq-'+s['jq_backtest_id'];jq=jq if c.execute('SELECT 1 FROM runs WHERE id=?',(jq,)).fetchone() else None
            identity=hashlib.sha256(str(out).encode()).hexdigest()[:24]
            c.execute('INSERT OR IGNORE INTO comparisons VALUES(?,?,?,?,?,?)',(identity,str(out),dump(s),local,jq,now()))
        return identity

    def scan(self):
        found=[];issues=[]
        for folder in sorted((ROOT/'strategies').iterdir()):
            if not folder.is_dir():continue
            self.register(folder.name,kind='probe' if any(w in folder.name for w in ('probe','crosscheck','query')) else 'strategy')
            source=folder/'strategy.py'
            if not source.exists():
                files=list(folder.glob('*.py'));source=files[0] if len(files)==1 else None
            self.version(folder.name,source)
        for folder in sorted((ROOT/'reports').iterdir()):
            if not folder.is_dir() or not (folder/'manifest.json').exists():continue
            try:found.append(self.import_local(folder))
            except Exception as e:issues.append(dict(path=str(folder),error=str(e)))
        jq=ROOT/'data/s02-jq-comparison-20261004-v1'
        if jq.exists():
            try:found.append(self.import_jq(jq))
            except Exception as e:issues.append(dict(path=str(jq),error=str(e)))
        for folder in sorted((ROOT/'reports').glob('s02-jq-local-comparison-*')):
            if folder.is_dir():
                try:self.import_comparison(folder)
                except Exception as e:issues.append(dict(path=str(folder),error=str(e)))
        return dict(imported=len(found),issues=issues,stats=self.stats())

    def stats(self):
        with self.connection() as c:
            return {t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ['strategies','versions','runs','comparisons']}

    def rows(self,table):
        if table not in ('strategies','versions','runs','comparisons'):raise ValueError('Unknown catalog table')
        with self.connection() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid DESC')]
        for row in rows:
            for k in ('params','metrics','summary'):
                if row.get(k):row[k]=json.loads(row[k])
        return rows

    def detail(self,rid):
        rows=self.rows('runs');run=next((r for r in rows if r['id']==rid),None)
        if run is None:raise KeyError('Run not found')
        with self.connection() as c:
            run['artifacts']=[dict(x) for x in c.execute('SELECT * FROM artifacts WHERE run_id=? ORDER BY name',(rid,))]
            v=c.execute('SELECT * FROM versions WHERE id=?',(run['version_id'],)).fetchone();run['version']=dict(v) if v else None
        return run

    def backup(self,out):
        out=Path(out)
        if out.exists():raise FileExistsError('Backup target exists')
        out.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(out,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        dest=sqlite3.connect(out)
        try:
            with self.connection() as src:src.backup(dest)
            assert dest.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        finally:dest.close()
        return dict(path=str(out.resolve()),sha256=digest(out))

def tracked_run(args,function):
    sid='S02-local-v1' if args.command=='s02' else 'fixed-quantity-baseline'
    source=ROOT/'strategies/S02-local-v1/strategy.py' if args.command=='s02' else ENGINE/'event_engine.py'
    cat=Catalog();rid=cat.begin(sid,source,vars(args),args.out)
    try:
        result=function(args);cat.import_local(args.out,rid)
        return dict(result,catalog_run_id=rid,catalog_database=str(cat.path))
    except BaseException as e:
        cat.fail(rid,e);raise

def main(argv=None):
    p=argparse.ArgumentParser(description='Strategy/version/run catalog');s=p.add_subparsers(dest='action',required=True)
    for name in ['scan','doctor','strategies','versions','runs','comparisons']:s.add_parser(name)
    show=s.add_parser('show');show.add_argument('run_id')
    imp=s.add_parser('import');imp.add_argument('--out',required=True)
    reg=s.add_parser('register');reg.add_argument('--id',required=True);reg.add_argument('--name',required=True);reg.add_argument('--kind',choices=['strategy','probe','baseline'],default='strategy');reg.add_argument('--source',required=True);reg.add_argument('--description',default='')
    backup=s.add_parser('backup');backup.add_argument('--out',required=True)
    args=p.parse_args(argv);cat=Catalog()
    if args.action=='scan':return cat.scan()
    if args.action=='doctor':
        with cat.connection() as c:integrity=c.execute('PRAGMA integrity_check').fetchone()[0]
        return dict(database=str(cat.path),schema=1,integrity=integrity,stats=cat.stats())
    if args.action in ('strategies','versions','runs','comparisons'):return cat.rows(args.action)
    if args.action=='show':return cat.detail(args.run_id)
    if args.action=='backup':return cat.backup(args.out)
    if args.action=='register':
        if not Path(args.source).is_file():raise FileNotFoundError('Strategy source missing')
        sid=cat.register(args.id,args.name,args.kind,args.description);v=cat.version(sid,args.source)
        if not v:raise FileNotFoundError('Strategy source missing')
        return dict(strategy_id=sid,version_id=v)
    return dict(run_id=cat.import_local(args.out))
