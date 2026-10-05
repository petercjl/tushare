import unittest
import pandas as pd
from tushare_env.resample import aggregate

class ResampleTests(unittest.TestCase):
    def data(self):
        times=pd.date_range('2024-01-08 09:30', '2024-01-08 11:30',freq='min').append(pd.date_range('2024-01-08 13:01','2024-01-08 15:00',freq='min'))
        return pd.DataFrame(dict(ts_code='518880.SH',trade_time=times,open=1.,high=2.,low=0.5,close=1.5,vol=100,amount=150)),dict(ts_code='518880.SH',start_date='2024-01-08 09:00',end_date='2024-01-08 15:01')

    def test_session_and_quantity_conserved_with_auction_separate(self):
        data,params=self.data();result,summary=aggregate(data,params)
        self.assertEqual(len(result),49)
        self.assertEqual(result.vol.sum(),data.vol.sum())
        self.assertEqual(result.amount.sum(),data.amount.sum())
        self.assertEqual(result.iloc[0].vol,100)
        self.assertEqual(result.iloc[1].vol,500)
        self.assertEqual(result.iloc[1].open,1.)
        self.assertEqual(result.iloc[1].close,1.5)
        self.assertEqual(summary['missing_standard_minutes'],[])
        self.assertTrue(summary['has_1400'])

    def test_missing_minute_rejected_before_aggregation(self):
        data,params=self.data()
        with self.assertRaisesRegex(ValueError,'Complete'):
            aggregate(data.drop(index=1),params)
