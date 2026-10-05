import unittest
from decimal import Decimal as D
from qt_research.ledger import AccountLedger, Fill


def fill(identifier, side="buy", quantity=100, price="10", fee="5"):
    return Fill(identifier, "order-" + identifier, "2024-01-02T14:00:00+08:00",
                "TEST.SH", side, quantity, price, fee)


class LedgerTests(unittest.TestCase):
    def test_initial_equity_and_first_fee(self):
        account = AccountLedger(2000)
        self.assertEqual(account.equity({}), D("2000"))
        account.apply_fill(fill("b1"))
        self.assertEqual(account.cash, D("995"))
        self.assertEqual(account.positions["TEST.SH"].cost, D("1005"))
        self.assertEqual(account.equity({"TEST.SH": 10}), D("1995"))

    def test_partial_sell_and_total_reconciliation(self):
        account = AccountLedger(5000)
        account.apply_fill(fill("b1", quantity=100))
        account.apply_fill(fill("b2", quantity=100, price="12"))
        first = account.apply_fill(fill("s1", "sell", 50, "13"))
        self.assertEqual(first.realized_pnl, D("92.5"))
        self.assertEqual(first.cost_after, D("1657.5"))
        second = account.apply_fill(fill("s2", "sell", 150, "9"))
        self.assertEqual(second.realized_pnl, D("-312.5"))
        self.assertEqual(account.positions, {})
        self.assertEqual(account.cash, D("4780"))
        self.assertEqual(account.realized_pnl, D("-220"))
        self.assertEqual(account.cash - account.initial_cash, account.realized_pnl)

    def test_odd_partial_fills_and_same_order(self):
        account = AccountLedger(5000)
        for identifier, qty in (("a", 375), ("b", 25)):
            account.apply_fill(Fill(identifier, "same-order", "2024-01-02",
                                    "TEST.SH", "buy", qty, "10", "1"))
        self.assertEqual(account.positions["TEST.SH"].quantity, 400)
        self.assertEqual(account.cash, D("998"))

    def test_replayed_fill_and_conflict(self):
        account = AccountLedger(2000)
        record = fill("b1")
        first = account.apply_fill(record)
        self.assertIs(account.apply_fill(record), first)
        with self.assertRaises(ValueError):
            account.apply_fill(fill("b1", price="11"))
        self.assertEqual(len(account.entries), 1)
        self.assertEqual(account.cash, D("995"))

    def test_rejected_fill_is_atomic(self):
        account = AccountLedger(1000)
        for record in (fill("too-much"), fill("no-position", "sell")):
            with self.assertRaises(ValueError):
                account.apply_fill(record)
            self.assertEqual(account.cash, D("1000"))
            self.assertEqual(account.positions, {})
            self.assertEqual(account.entries, ())

    def test_bad_amounts_and_quantities(self):
        for qty in (0, -1, 1.5, True):
            with self.assertRaises(ValueError):
                fill("a", quantity=qty)
        for price in ("NaN", "Infinity", "0", "-1"):
            with self.assertRaises(ValueError):
                fill("a", price=price)
        with self.assertRaises(ValueError):
            fill("a", fee="-1")

    def test_missing_mark_and_read_only_views(self):
        account = AccountLedger(2000)
        account.apply_fill(fill("a"))
        view = account.positions
        view.clear()
        self.assertEqual(account.positions["TEST.SH"].quantity, 100)
        with self.assertRaises(ValueError):
            account.equity({})
        with self.assertRaises(ValueError):
            account.equity({"TEST.SH": "NaN"})
        self.assertEqual(account.equity({"TEST.SH": 11}), D("2095"))


if __name__ == "__main__":
    unittest.main()
