import unittest
from types import SimpleNamespace as NS
from decimal import Decimal
import pandas as pd
from qt_research.nav_availability import apply_evidence, load_evidence
from qt_research.s02_local import S02Runtime, ROOT
from qt_research.ledger import AccountLedger
from qt_research.ledger import Position


class S02AdapterTests(unittest.TestCase):
    def runtime(self):
        symbol='518880.SS'
        dates=pd.to_datetime(['2024-01-05','2024-01-08','2024-01-09'])
        daily=pd.DataFrame({'close':[100.,110.,9999.],'volume':[1000.,2000.,999999.],'factor':[1.,1.1,20.]},index=dates)
        minutes=pd.DataFrame({'close':[110.,9999.],'vol':[100.,999999.]},index=pd.to_datetime(['2024-01-08 14:00','2024-01-08 14:01']))
        data=NS(start=pd.Timestamp('2024-01-08'),calendar=pd.DatetimeIndex(dates),daily={symbol:daily},sessions={symbol:{dates[1]:minutes}},nav=pd.DataFrame(),dividends=[])
        r=S02Runtime(data,ROOT/'strategies/S02-local-v1/strategy.py',100000)
        r.time=pd.Timestamp('2024-01-08 14:00');r.sync()
        return r

    def test_daily_history_excludes_current_day_and_future_factor(self):
        r=self.runtime();f=r.history(45,'1d',['close','volume'],'518880.SS',fq='dypre')
        self.assertEqual(list(f.index),[pd.Timestamp('2024-01-05')])
        self.assertAlmostEqual(f.close.iloc[0],100/1.1)
        self.assertEqual(f.volume.iloc[0],1000)
        with self.assertRaisesRegex(ValueError,'unavailable'):
            r.history(45,'1d',['close'],'518880.SS',include=True)

    def test_current_minute_volume_excludes_next_minute(self):
        r=self.runtime();f=r.history(242,'1m',['volume'],'518880.SS',include=True)
        self.assertEqual(len(f),1)
        self.assertEqual(f.volume.sum(),100)
        with self.assertRaisesRegex(ValueError,'blocked'):
            r.get_price('518880.SS','20240105','20240108','1d',['close'])

    def test_same_day_nav_announcement_unavailable(self):
        r=self.runtime();r.s.g.nav_asof=r.time.date()
        r.data.nav=pd.DataFrame([dict(ts_code='518880.SH',ann_date='20240108',nav_date='20240103',unit_nav=100.)])
        result=r.fetch_nav('518880.SS','2024-01-03','2024-01-03')
        self.assertEqual(result['2024-01-03']['status'],'missing')
        r.data.nav['ann_date']='20240105'
        self.assertEqual(r.fetch_nav('518880.SS','2024-01-03','2024-01-03')['2024-01-03']['status'],'ok')

    def test_missing_nav_is_error_and_retains_identifying_message(self):
        r=self.runtime();r.log('HUMAN|净值缺失|518880.SS 2024-01-03 历史单位净值不可用')
        self.assertEqual(len(r.errors),1)
        self.assertEqual(r.errors[0]['level'],'ERROR')
        self.assertIn('2024-01-03',r.errors[0]['message'])

    def test_independent_publication_preserves_original_announcement_and_blocks_future(self):
        record=dict(unit_nav=5.7344, status='missing', announcement_date='2024-10-25', available_at='2024-10-26 00:00:00')
        witness=dict(unit_nav=5.7344, available_at='2024-10-09 00:00:00', source_url='https://www.sge.com.cn/test')
        self.assertIs(apply_evidence(record, witness, '2024-10-08'), record)
        corrected=apply_evidence(record, witness, '2024-10-10')
        self.assertEqual(corrected['status'],'ok')
        self.assertEqual(corrected['announcement_date'],'2024-10-25')
        self.assertEqual(record['status'],'missing')
        with self.assertRaisesRegex(ValueError,'value changed'):
            apply_evidence(record, dict(witness,unit_nav=6.), '2024-10-10')

    def test_publication_override_used_only_for_matching_symbol_and_value(self):
        r=self.runtime();r.s.g.nav_asof=r.time.date()
        r.data.nav=pd.DataFrame([dict(ts_code='518880.SH',ann_date='20240125',nav_date='20240103',unit_nav=100.)])
        r.data.nav_evidence={('518880.SH','20240103'):dict(unit_nav=100.,available_at='2024-01-05 00:00:00',source_url='https://www.sge.com.cn/test')}
        self.assertEqual(r.fetch_nav('518880.SS','2024-01-03','2024-01-03')['2024-01-03']['status'],'ok')
        self.assertTrue(any(e['event']=='nav_publication_verified' for e in r.events))
        r.data.nav_evidence={('513100.SH','20240103'):dict(unit_nav=100.,available_at='2024-01-05 00:00:00')}
        self.assertEqual(r.fetch_nav('518880.SS','2024-01-03','2024-01-03')['2024-01-03']['status'],'missing')

    def test_cash_credit_idempotency_conflict_and_negative_rejection(self):
        ledger=AccountLedger(1000)
        ledger.credit_cash('dividend-1',20);ledger.credit_cash('dividend-1',20)
        self.assertEqual(ledger.cash,Decimal('1020'))
        with self.assertRaises(ValueError):ledger.credit_cash('dividend-1',21)
        with self.assertRaises(ValueError):ledger.credit_cash('dividend-2',-1)
        self.assertEqual(ledger.cash,Decimal('1020'))

    def test_dividend_receivable_not_spendable_before_payment(self):
        r=self.runtime();day=r.time.normalize()
        action=dict(id='fixture',symbol='518880.SS',ann=day-pd.Timedelta(days=3),record=day-pd.Timedelta(days=1),ex=day,pay=day+pd.Timedelta(days=1),per_share=0.1)
        r.data.dividends=[action];r.entitlements['fixture']=100
        r.distributions(day)
        self.assertEqual(r.book.ledger.cash,Decimal('100000'))
        self.assertEqual(r.receivables['fixture'],Decimal('10'))
        r.distributions(day+pd.Timedelta(days=1))
        self.assertEqual(r.book.ledger.cash,Decimal('100010'))
        self.assertFalse(r.receivables)

    def test_distribution_cost_adjustment_scales_with_remaining_position(self):
        r=self.runtime()
        r.dividend_cost_adjustments['518880.SS']=Decimal('20')
        r.book.ledger._positions['518880.SS']=Position(50,Decimal('5000'))
        r.adjust_distribution_cost_after_sell('518880.SS',100)
        self.assertEqual(r.dividend_cost_adjustments['518880.SS'],Decimal('10'))
        r.book.ledger._positions.clear()
        r.adjust_distribution_cost_after_sell('518880.SS',50)
        self.assertEqual(r.dividend_cost_adjustments['518880.SS'],Decimal('0'))
