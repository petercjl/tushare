import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tushare_env.gateway import Gateway, redact, MinutePending

class GatewayTests(unittest.TestCase):
    def provider(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)/'usage'
        path.write_text('卡号：tsr_test_private_fixture\nU="https://example.test/tushare/pro"')
        return Gateway(path)

    def test_reads_header_credential_and_decodes_market_data(self):
        provider = self.provider()
        with patch('requests.get') as call:
            call.return_value.json.return_value = {'code':0,'data':{'fields':['cal_date'],'items':[['20240108']]}}
            frame = provider.query('trade_cal',start_date='20240108')
            self.assertEqual(len(frame),1)
            kwargs = call.call_args.kwargs
            self.assertEqual(kwargs['headers']['X-API-Key'],'tsr_test_private_fixture')
            self.assertNotIn('X-API-Key',kwargs['params'])
            self.assertNotIn('verify',kwargs)  # requests default verifies TLS

    def test_permission_and_secret_error_redaction(self):
        provider = self.provider()
        with patch('requests.get') as call:
            call.return_value.json.return_value = {'code':40203,'msg':'denied '+provider.key}
            with self.assertRaises(RuntimeError) as result:
                provider.query('etf_mins')
            self.assertIn('40203',str(result.exception))
            self.assertNotIn(provider.key,str(result.exception))

    def test_non_data_api_and_credential_override_rejected(self):
        provider = self.provider()
        with self.assertRaises(ValueError):provider.query('p_save')
        with self.assertRaises(ValueError):provider.query('trade_cal',token='other')

    def test_card_format_redacted(self):
        self.assertNotIn('tsr_',redact('header tsr_test_private_fixture'))

    def test_http202_is_pending_not_permission_denial(self):
        provider = self.provider()
        with patch('requests.get') as call:
            call.return_value.status_code=202
            call.return_value.json.return_value = {'ok':False,'error':'minute_data_pending'}
            with self.assertRaises(MinutePending):provider.query('stk_mins')

    def test_gateway_daily_units_cannot_silently_use_legacy_multiplier(self):
        import json
        from qt_research.local_cli import load_daily
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'report.json').write_text(json.dumps({'provider':'user-supplied HTTP GET gateway','results':[]}))
            with self.assertRaisesRegex(ValueError,'units'):
                load_daily(directory)

    def test_five_minute_coverage_does_not_require_one_minute_bars(self):
        import pandas as pd
        from qt_research.public_tushare import describe
        times = pd.date_range('2025-12-31 09:35','2025-12-31 11:30',freq='5min').append(pd.date_range('2025-12-31 13:05','2025-12-31 15:00',freq='5min'))
        frame = pd.DataFrame({'ts_code':'501018.SH','trade_time':times,'open':1.,'high':1.,'low':1.,'close':1.,'vol':100,'amount':100})
        result = describe(frame,'etf_mins',dict(ts_code='501018.SH',freq='5min',start_date='2025-12-31 09:00',end_date='2025-12-31 15:01'))
        self.assertEqual(result['missing_standard_minutes'],[])
        self.assertTrue(result['has_1400'])
