import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.request import urlopen,Request
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from qt_research.research_catalog import Catalog,tracked_run
from qt_research.dashboard import handler,curve

class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.cat=Catalog(self.root/'research.sqlite3')
        self.source=self.root/'source.py';self.source.write_text('VERSION=1\n')
    def tearDown(self):self.tmp.cleanup()
    def result(self,name='run'):
        p=self.root/name;p.mkdir()
        (p/'manifest.json').write_text(json.dumps(dict(strategy='test',start='2024-01-02',end='2024-01-03',initial_cash=100000)))
        (p/'summary.json').write_text(json.dumps(dict(metrics=dict(strategy_return=.1),data_errors=1,performance_valid=False)))
        (p/'equity.csv').write_text('date,equity,strategy_return\n2024-01-02,100000,0\n2024-01-03,110000,0.1\n')
        checks={x.name:hashlib.sha256(x.read_bytes()).hexdigest() for x in p.iterdir()}
        (p/'checksums.json').write_text(json.dumps(checks));return p
    def test_immutable_version_snapshot(self):
        self.cat.register('test');a=self.cat.version('test',self.source)
        self.source.write_text('VERSION=2\n');b=self.cat.version('test',self.source)
        self.assertNotEqual(a,b)
        first=next(v for v in self.cat.rows('versions') if v['id']==a)
        self.assertEqual(Path(first['code_path']).read_text(),'VERSION=1\n')
    def test_idempotent_import_and_warning(self):
        p=self.result();a=self.cat.import_local(p);self.assertEqual(a,self.cat.import_local(p))
        self.assertEqual(self.cat.stats()['runs'],1);r=self.cat.detail(a)
        self.assertEqual((r['status'],r['valid'],r['errors']),('warning',0,1))
        self.assertEqual(len(r['artifacts']),4)
    def test_unknown_validity_not_claimed_pass(self):
        p=self.result();(p/'summary.json').write_text('{"metrics":{}}')
        c=json.loads((p/'checksums.json').read_text());c['summary.json']=hashlib.sha256((p/'summary.json').read_bytes()).hexdigest();(p/'checksums.json').write_text(json.dumps(c))
        r=self.cat.detail(self.cat.import_local(p));self.assertIsNone(r['valid'])
    def test_tampering_rejected(self):
        p=self.result();self.cat.import_local(p);(p/'equity.csv').write_text('tampered')
        with self.assertRaises(ValueError):self.cat.import_local(p)
    def test_existing_out_prevents_new_run(self):
        with self.assertRaises(FileExistsError):self.cat.begin('test',self.source,{},self.result())
        self.assertEqual(self.cat.stats()['runs'],0)
    def test_failure_tracked_before_engine_returns(self):
        args=SimpleNamespace(command='run',out=str(self.root/'failed'))
        with patch('qt_research.research_catalog.Catalog',return_value=self.cat):
            with self.assertRaises(RuntimeError):tracked_run(args,lambda _:(_ for _ in ()).throw(RuntimeError('fixture failure')))
        r=self.cat.rows('runs')[0];self.assertEqual(r['status'],'failed');self.assertEqual(r['error'],'fixture failure')
        self.assertTrue(r['params']['engine_source_hashes'])
    def test_backup_consistent_and_no_overwrite(self):
        self.cat.import_local(self.result());target=self.root/'backup.sqlite3';self.cat.backup(target)
        self.assertEqual(Catalog(target).stats(),self.cat.stats())
        with self.assertRaises(FileExistsError):self.cat.backup(target)
    def test_web_reads_catalog_and_blocks_arbitrary_files(self):
        rid=self.cat.import_local(self.result());s=ThreadingHTTPServer(('127.0.0.1',0),handler(self.cat,0));port=s.server_port
        s.RequestHandlerClass=handler(self.cat,port);t=threading.Thread(target=s.serve_forever,daemon=True);t.start()
        base=f'http://127.0.0.1:{port}'
        try:
            with urlopen(base+'/api/curve?id='+rid) as f:self.assertEqual(len(json.load(f)),2)
            with self.assertRaises(HTTPError) as e:urlopen(base+'/artifact?run='+rid+'&id=../../config/settings.json')
            self.assertEqual(e.exception.code,404)
            with self.assertRaises(HTTPError) as e:urlopen(Request(base+'/api/catalog',headers={'Host':'evil.example'}))
            self.assertEqual(e.exception.code,403)
            with self.assertRaises(HTTPError) as e:urlopen(Request(base+'/api/catalog',data=b'{}'))
            self.assertEqual(e.exception.code,501)
        finally:s.shutdown();s.server_close();t.join()


class CurveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def cat(self,files):
        arts=[]
        for i,(name,body) in enumerate(files.items()):
            p=self.root/str(i);p.write_text(body)
            arts.append(dict(id=str(i),name=name,path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
        return SimpleNamespace(detail=lambda _:dict(params=dict(initial_cash=100000),artifacts=arts))
    def test_local_curve_percent_units_and_missing_columns(self):
        c=self.cat({'equity.csv':'date,equity,strategy_return,benchmark_return,excess_return\n2024-01-02,98000,-0.02,-0.05,0.031578947\n2024-01-03,99000,-0.01,,\n'})
        rows=curve(c,'fixture')
        self.assertEqual(rows[0]['value'],-2)
        self.assertEqual(rows[0]['benchmark_value'],-5)
        self.assertAlmostEqual(rows[0]['excess_value'],3.1578947)
        self.assertIsNone(rows[1]['benchmark_value']);self.assertIsNone(rows[1]['excess_value'])
    def test_joinquant_export_join_and_no_double_percent_conversion(self):
        log=''.join(json.dumps(x)+'\n' for x in [dict(event='daily_account',dt='2024-01-02 15:10:00',equity=99830),dict(event='daily_account',dt='2024-01-03 15:10:00',equity=99000)])
        c=self.cat({'export/clean/logs.audit.jsonl':log,'export/clean/result.normalized.csv':'date,strategy_return_pct,benchmark_return_pct,excess_return_pct\n2024-01-02,-9,-1.3,1.14\n'})
        rows=curve(c,'fixture')
        self.assertAlmostEqual(rows[0]['value'],-.17)
        self.assertEqual(rows[0]['benchmark_value'],-1.3);self.assertEqual(rows[0]['excess_value'],1.14)
        self.assertIsNone(rows[1]['benchmark_value'])
    def test_joinquant_export_integrity_checked(self):
        c=self.cat({'export/clean/logs.audit.jsonl':'','export/clean/result.normalized.csv':'date,benchmark_return_pct\n'})
        (self.root/'1').write_text('changed')
        with self.assertRaises(ValueError):curve(c,'fixture')

if __name__=='__main__':unittest.main()
