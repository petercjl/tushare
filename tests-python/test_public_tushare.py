import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from qt_research.public_tushare import credential, describe, HttpProvider

class PublicDataTests(unittest.TestCase):
    def read_key(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'keys'
            path.write_text(text)
            return credential(path)

    def test_separates_broker_and_public(self):
        self.assertEqual(self.read_key('山证tushare\n' + 'a'*56 + '\n公版tushare\n' + 'b'*56), 'b'*56)

    def test_ambiguous_public_keys_fail(self):
        with self.assertRaises(ValueError):
            self.read_key('public tushare\n' + 'a'*56 + '\n公版tushare\n' + 'b'*56)

    def test_never_uses_broker_as_fallback(self):
        with self.assertRaises(ValueError):
            self.read_key('public tushare\nmissing\n山证tushare\n' + 'a'*56)

    def test_http_api_denial_is_not_empty_data(self):
        with patch('requests.post') as post:
            post.return_value.json.return_value = {'code': 40101, 'msg': 'invalid'}
            with self.assertRaisesRegex(RuntimeError, '40101'):
                HttpProvider('dummy').query('trade_cal')

    def test_minute_bounds_and_duplicates(self):
        frame = pd.DataFrame([dict(ts_code='518880.SH', trade_time='2024-01-08 14:00:00',
                                   open=4, high=4, low=4, close=4, vol=100, amount=400)])
        params = dict(ts_code='518880.SH', start_date='2024-01-08 09:00:00', end_date='2024-01-08 15:00:00')
        self.assertTrue(describe(frame, 'etf_mins', params)['has_1400'])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            describe(pd.concat([frame, frame]), 'etf_mins', params)
        params['end_date'] = '2024-01-08 13:00:00'
        with self.assertRaisesRegex(ValueError, 'outside'):
            describe(frame, 'etf_mins', params)
