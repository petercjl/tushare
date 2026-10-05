import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
from qt_research import s02_cache


class CachePreparationTests(unittest.TestCase):
    def test_year_partition_warmup_and_invalid_range(self):
        with patch.object(s02_cache, 'SYMBOLS', ['518880.SH']):
            cases = s02_cache.plan('2020-01-01', '2023-12-31')
        self.assertEqual(len(cases), 16)
        self.assertLess(cases[0][1]['start_date'], '20191101')
        daily = [p for api, p in cases if api == 'fund_daily']
        self.assertEqual(daily[1]['start_date'], '20200101')
        self.assertEqual(daily[-1]['end_date'], '20231231')
        with self.assertRaises(ValueError):
            s02_cache.plan('2024-01-01', '2023-01-01')

    def test_cache_records_resume_and_existing_snapshot_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'cache'
            args = SimpleNamespace(out=out, start='2020-01-01', end='2020-01-03', resume=False)
            def query(name, **params):
                day = params['start_date']
                values = dict(ts_code=params.get('ts_code'), cal_date=day, is_open=1,
                              trade_date=day, close=1, vol=100, amount=10, adj_factor=1,
                              nav_date=day, ann_date=day, unit_nav=1)
                return pd.DataFrame([values])
            client = SimpleNamespace(query=query)
            with patch.object(s02_cache, 'SYMBOLS', ['518880.SH']), patch.object(s02_cache, 'api', return_value=client), patch.object(s02_cache, 'settings', return_value=dict(provider='fixture')), patch.object(s02_cache.time, 'sleep'):
                result = s02_cache.prepare(args)
                self.assertEqual(result['queries'], 7)
                report = json.loads((out/'report.json').read_text())
                self.assertEqual(len(report['results']), 7)
                with self.assertRaises(FileExistsError):
                    s02_cache.prepare(args)
                (out/'report.json').unlink()
                args.resume = True
                client.query = lambda *_a, **_k: self.fail('Verified cache should be reused')
                self.assertEqual(s02_cache.prepare(args)['queries'], 7)
