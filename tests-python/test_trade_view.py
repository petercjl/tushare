import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from openpyxl import Workbook
from qt_research.trade_view import trade_rows, select


class TradeViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def local(self):
        wb = Workbook()
        ws = wb.active
        ws.title = '成交'
        ws.append(['order_id', 'time', 'symbol', 'side', 'quantity', 'price', 'fee', 'realized_pnl'])
        ws.append(['buy1', '2024-01-02T14:01:00+08:00', '518880.SS', 'buy', 100, 4, 5, 0])
        ws.append(['buy1', '2024-01-02T14:02:00+08:00', '518880.SS', 'buy', 300, 5, 0, 0])
        ws.append(['sell1', '2024-01-03T14:01:00+08:00', '518880.SS', 'sell', 400, 6, 5, 490])
        ws = wb.create_sheet('委托')
        ws.append(['order_id', 'quantity', 'filled', 'status', 'limit_price'])
        ws.append(['buy1', 400, 400, 'filled', 5])
        ws.append(['sell1', 500, 400, 'cancelled', 6])
        path = self.root / 'trades.xlsx'
        wb.save(path)
        run = dict(id='local', strategy_id='test', platform='local', artifacts=[dict(id='excel', name='transactions.xlsx')])
        cat = SimpleNamespace(detail=lambda _: run, rows=lambda _: [])
        return cat, lambda *_: path

    def test_partial_fills_weighted_price_and_signed_sales(self):
        cat, verified = self.local()
        rows, _ = trade_rows(cat, 'local', verified)
        self.assertEqual(len(rows), 2)
        buy, sell = rows
        self.assertEqual((buy['quantity'], buy['price'], buy['value'], buy['fee'], buy['fill_count']), (400, 4.75, 1900, 5, 2))
        self.assertEqual(buy['time'], '14:01:00')
        self.assertIn('14:02:00', buy['last_update'])
        self.assertEqual((sell['quantity'], sell['value'], sell['pnl']), (-400, -2400, 490))
        self.assertEqual(sell['status'], '部分成交后取消')
        raw, _ = trade_rows(cat, 'local', verified, group='fill')
        self.assertEqual(len(raw), 3)
        self.assertEqual(sum(x['fee'] for x in raw), sum(x['fee'] for x in rows))

    def test_filter_before_pagination_and_invalid_dates(self):
        rows = [dict(date='2024-01-02', time='14:00:00', side='buy', symbol='518880.SS', security_name='黄金ETF'),
                dict(date='2024-01-03', time='14:00:00', side='sell', symbol='518880.SS', security_name='黄金ETF')]
        x = select(rows, start='2024-01-03', side='sell', keyword='黄金', limit=1)
        self.assertEqual(x['total'], 1)
        self.assertEqual(x['rows'][0]['date'], '2024-01-03')
        self.assertEqual(select(rows, keyword='不存在')['total'], 0)
        self.assertEqual(select(rows, offset=1, limit=1)['rows'][0]['side'], 'sell')
        with self.assertRaises(ValueError):
            select(rows, start='2024-02-01', end='2024-01-01')

    def test_joinquant_native_amount_and_missing_fill_evidence(self):
        path = self.root / 'jq.csv'
        with path.open('w') as f:
            w = csv.DictWriter(f, fieldnames=['date', 'order_time', 'code', 'side', 'security_name', 'filled_amount', 'filled_price', 'filled_value', 'close_pnl', 'fee'])
            w.writeheader()
            w.writerow(dict(date='2024-01-03', order_time='14:00:00', code='513100.XSHG', side='卖', security_name='纳指ETF', filled_amount=-100, filled_price=1.2, filled_value=-120, close_pnl=10, fee=5))
        run = dict(platform='joinquant', artifacts=[dict(id='csv', name='export/clean/transactions.normalized.csv')])
        cat = SimpleNamespace(detail=lambda _: run)
        rows, platform = trade_rows(cat, 'jq', lambda *_: path)
        self.assertEqual(platform, 'joinquant')
        self.assertEqual((rows[0]['quantity'], rows[0]['value'], rows[0]['pnl']), (-100, -120, 10))
        self.assertIsNone(rows[0]['fill_count'])
        self.assertEqual(rows[0]['side'], 'sell')
