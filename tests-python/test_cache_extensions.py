import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import patch
import pandas as pd
from qt_research import minute_import, minute_gaps, s02_cache


class CacheExtensionTests(unittest.TestCase):
    def test_verified_reuse_does_not_query_or_modify_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=Path(tmp)/'old';new=Path(tmp)/'new'
            def query(name,**p):
                d=p['start_date']
                return pd.DataFrame([dict(ts_code=p.get('ts_code'),trade_date=d,nav_date=d,cal_date=d,
                    is_open=1,close=1.,vol=1.,amount=.1,adj_factor=1.,unit_nav=1.,ann_date=d)])
            with patch.object(s02_cache,'SYMBOLS',['518880.SH']),patch.object(s02_cache,'settings',return_value={'provider':'fixture'}),patch.object(s02_cache.time,'sleep'):
                with patch.object(s02_cache,'api',return_value=NS(query=query)):
                    s02_cache.prepare(NS(out=old,start='2020-01-01',end='2020-01-03',resume=False))
                originals={str(p):p.read_bytes() for p in old.rglob('*') if p.is_file()}
                with patch.object(s02_cache,'api',return_value=NS(query=lambda *_a,**_p:self.fail('Cache should satisfy all requests'))):
                    s02_cache.prepare(NS(out=new,start='2020-01-01',end='2020-01-03',resume=False,reuse=[old]))
                self.assertTrue(all(p.read_bytes()==originals[str(p)] for p in old.rglob('*') if p.is_file()))

    def test_import_preserves_missing_raw_factor_and_tiny_rounding(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source';source.mkdir();out=Path(tmp)/'out'
            f=pd.DataFrame(dict(ts_code=['518880.SH'],trade_time=pd.to_datetime(['2026-07-30 09:30']),trade_date=pd.to_datetime(['2026-07-30']),
                                open=[1.],high=[1.],low=[1.],close=[1.+1e-15],vol=[100.],amount=[100.],adj_factor=[float('nan')]))
            p=source/'518880.SH.parquet';f.to_parquet(p,index=False);before=p.read_bytes()
            with patch.object(minute_import,'SYMBOLS',['518880.SH']):minute_import.run(NS(source=source,out=out))
            self.assertEqual(before,p.read_bytes());self.assertEqual(before,(out/p.name).read_bytes())
            self.assertEqual(json.loads((out/'manifest.json').read_text())['files'][0]['null_counts']['adj_factor'],1)
            with self.assertRaises(FileExistsError):minute_import.run(NS(source=source,out=out))

    def test_gap_download_adds_exact_real_bars_and_rejects_incomplete_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source';source.mkdir();day=pd.Timestamp('2026-07-30');times=minute_gaps.grid(day)
            f=pd.DataFrame(dict(ts_code='513100.SH',trade_time=times,trade_date=day,open=1.,high=1.,low=1.,close=1.,vol=100.,amount=100.,adj_factor=1.))
            p=source/'513100.SH.parquet';f.iloc[61:].to_parquet(p,index=False)
            manifest={'files':[dict(symbol='513100.SH',file=p.name,sha256=hashlib.sha256(p.read_bytes()).hexdigest())]}
            (source/'manifest.json').write_text(json.dumps(manifest));before=p.read_bytes()
            args=NS(minutes=source,out=Path(tmp)/'complete',usage_file='fixture',start='2026-07-30',end='2026-07-30')
            client=NS(query=lambda *_a,**_p:f.iloc[:61].drop(columns=['trade_date','adj_factor']).copy())
            with patch.object(minute_gaps,'Gateway',return_value=client),patch.object(minute_gaps.time,'sleep'):
                self.assertEqual(minute_gaps.run(args)['added_rows'],61)
            result=pd.read_parquet(Path(args.out)/p.name)
            self.assertEqual(len(result),241);self.assertEqual(result.adj_factor.isna().sum(),61);self.assertEqual(p.read_bytes(),before)
            args.out=Path(tmp)/'rejected';client.query=lambda *_a,**_p:f.iloc[:60].copy()
            with patch.object(minute_gaps,'Gateway',return_value=client),self.assertRaises(ValueError):minute_gaps.run(args)
