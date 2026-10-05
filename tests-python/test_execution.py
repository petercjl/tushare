import unittest
from decimal import Decimal as D
from qt_research.orders import OrderBook, OrderRequest, SecurityRule
from qt_research.execution import Bar, BarMatcher, ExecutionConfig
from qt_research.ledger import Fill

T = "2024-01-02T14:00:00+08:00"


class ExecutionTests(unittest.TestCase):
    def make(self, cash=5000, same_day=True):
        book = OrderBook(cash, {"ETF": SecurityRule(same_day_sell=same_day)})
        book.start_session("2024-01-02")
        return book, BarMatcher(book)

    def test_reservation_rejection_and_cancel(self):
        book, matcher = self.make(2000)
        req = OrderRequest("a", "ETF", "buy", 100, 10, 5)
        matcher.submit(req, T)
        self.assertEqual(book.available_cash, D("995"))
        rejected = matcher.submit(OrderRequest("b", "ETF", "buy", 100, 10, 5), T)
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(book.available_cash, D("995"))
        book.cancel("a")
        self.assertEqual(book.available_cash, D("2000"))
        self.assertEqual(matcher.submit(req, T).status, "cancelled")

    def test_partial_next_bar_fee_and_replay(self):
        book, matcher = self.make()
        matcher.submit(OrderRequest("a", "ETF", "buy", 400, 10, 5), T)
        first = Bar("2024-01-02T14:01:00+08:00", "ETF", 10, 1500)
        matcher.match(first)
        self.assertEqual(book.orders["a"].filled, 375)
        self.assertEqual(book.orders["a"].remaining, 25)
        self.assertEqual(book.reserved_cash, D("250"))
        self.assertEqual(book.available_cash, D("995"))
        matcher.match(first)
        matcher.match(Bar("2024-01-02T14:02:00+08:00", "ETF", 10, 100))
        self.assertEqual(book.orders["a"].status, "filled")
        self.assertEqual(book.orders["a"].fees, D("5"))
        self.assertEqual(book.ledger.cash, D("995"))
        self.assertEqual(len(book.ledger.entries), 2)

    def test_commission_exempt_symbol_partial_fills(self):
        book, _ = self.make(2000)
        matcher = BarMatcher(book, ExecutionConfig(commission_exempt_symbols=("ETF",)))
        matcher.submit(OrderRequest("exempt", "ETF", "buy", 100, 10, 0), T)
        matcher.match(Bar("2024-01-02T14:01:00+08:00", "ETF", 10, 200))
        matcher.match(Bar("2024-01-02T14:02:00+08:00", "ETF", 10, 200))
        self.assertEqual(book.orders["exempt"].status, "filled")
        self.assertEqual(book.orders["exempt"].fees, D("0"))
        self.assertEqual(book.ledger.cash, D("1000"))
        self.assertEqual(matcher.config.fee_total(D("1000"), "OTHER"), D("5"))

    def test_volume_shared_and_same_timestamp_not_filled(self):
        book, matcher = self.make()
        for oid in ("a", "b"):
            matcher.submit(OrderRequest(oid, "ETF", "buy", 100, 10, 5), T)
        self.assertEqual(matcher.match(Bar(T, "ETF", 10, 10000)), ())
        matcher.match(Bar("2024-01-02T14:01:00+08:00", "ETF", 10, 600))
        self.assertEqual(book.orders["a"].filled, 100)
        self.assertEqual(book.orders["b"].filled, 50)

    def test_limit_slippage_zero_volume(self):
        book, _ = self.make()
        matcher = BarMatcher(book, ExecutionConfig(slippage="0.01"))
        matcher.submit(OrderRequest("a", "ETF", "buy", 100, 10, 5), T)
        for time, volume in (("14:01", 0), ("14:02", 10000)):
            matcher.match(Bar("2024-01-02T" + time + ":00+08:00", "ETF", 10, volume))
        self.assertEqual(book.orders["a"].filled, 0)

    def test_t1_unlock_and_sell_reservation(self):
        book, matcher = self.make(same_day=False)
        matcher.submit(OrderRequest("a", "ETF", "buy", 100, 10, 5), T)
        matcher.match(Bar("2024-01-02T14:01:00+08:00", "ETF", 10, 400))
        self.assertEqual(book.available_quantity("ETF"), 0)
        self.assertEqual(book.submit(OrderRequest("s0", "ETF", "sell", 100, 10, 5)).status, "rejected")
        book.start_session("2024-01-03")
        self.assertEqual(book.available_quantity("ETF"), 100)
        book.submit(OrderRequest("s1", "ETF", "sell", 100, 10, 5))
        self.assertEqual(book.available_quantity("ETF"), 0)
        self.assertEqual(book.submit(OrderRequest("s2", "ETF", "sell", 100, 10, 5)).status, "rejected")
        book.cancel("s1")
        self.assertEqual(book.available_quantity("ETF"), 100)

    def test_invalid_and_overfill_leave_state_unchanged(self):
        book, matcher = self.make()
        matcher.submit(OrderRequest("a", "ETF", "buy", 100, 10, 5), T)
        before = (book.available_cash, book.orders)
        for quantity, price, fee in ((101, 10, 5), (100, 11, 5), (100, 10, 6)):
            with self.assertRaises(ValueError):
                book.apply_fill(Fill("bad", "a", T, "ETF", "buy", quantity, price, fee))
            self.assertEqual((book.available_cash, book.orders), before)
        self.assertEqual(book.submit(OrderRequest("bad-lot", "ETF", "buy", 25, 10, 5)).status, "rejected")

    def test_partial_cancel_release_and_idempotent_fill(self):
        book, matcher = self.make()
        matcher.submit(OrderRequest("a", "ETF", "buy", 400, 10, 5), T)
        entry = matcher.match(Bar("2024-01-02T14:01:00+08:00", "ETF", 10, 1500))[0]
        book.cancel("a")
        self.assertEqual(book.available_cash, D("1245"))
        book.apply_fill(entry.fill)
        self.assertEqual(len(book.ledger.entries), 1)
        with self.assertRaises(ValueError):
            book.apply_fill(Fill("new", "a", T, "ETF", "buy", 25, 10, 0))

    def test_fee_threshold_accumulates_per_order(self):
        book, _ = self.make(30000)
        matcher = BarMatcher(book, ExecutionConfig(commission_rate="0.001"))
        matcher.submit(OrderRequest("a", "ETF", "buy", 2000, 10, 20), T)
        matcher.match(Bar("2024-01-02T14:01:00+08:00", "ETF", 10, 1000))
        self.assertEqual(book.orders["a"].fees, D("5"))
        matcher.match(Bar("2024-01-02T14:02:00+08:00", "ETF", 10, 7000))
        self.assertEqual(book.orders["a"].fees, D("20"))
        self.assertEqual(book.ledger.cash, D("9980"))

    def test_price_improvement_releases_cash_and_time_goes_forward(self):
        book, matcher = self.make(2000)
        matcher.submit(OrderRequest("a", "ETF", "buy", 100, 10, 5), T)
        matcher.match(Bar("2024-01-02T14:01:00+08:00", "ETF", 9, 200))
        self.assertEqual(book.available_cash, D("1045"))
        with self.assertRaises(ValueError):
            matcher.match(Bar("2024-01-02T13:59:00+08:00", "ETF", 9, 1000))
        with self.assertRaises(ValueError):
            matcher.match(Bar("2024-01-03T14:02:00+08:00", "ETF", 9, 1000))

    def test_odd_remainder_not_split_between_sell_orders(self):
        book, matcher = self.make()
        matcher.submit(OrderRequest("a", "ETF", "buy", 400, 10, 5), T)
        matcher.match(Bar("2024-01-02T14:01:00+08:00", "ETF", 10, 1500))
        book.cancel("a")
        self.assertEqual(book.submit(OrderRequest("s1", "ETF", "sell", 175, 10, 5)).status, "accepted")
        self.assertEqual(book.submit(OrderRequest("s2", "ETF", "sell", 175, 10, 5)).status, "rejected")


if __name__ == "__main__":
    unittest.main()
