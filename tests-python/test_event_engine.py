import unittest
from decimal import Decimal as D
from qt_research.event_engine import run_events, FixedQuantityStrategy
from qt_research.execution import Bar
from qt_research.orders import SecurityRule


def bar(day, minute, price=10, volume=400):
    return Bar(f"2024-01-{day:02d}T14:{minute:02d}:00+08:00", "ETF", price, volume)


class EventTests(unittest.TestCase):
    def test_next_event_fill_initial_fee_and_determinism(self):
        bars = [bar(2, 0), bar(2, 1), bar(2, 2, 11)]
        rules = {"ETF": SecurityRule(same_day_sell=True)}
        first = run_events(bars, FixedQuantityStrategy("ETF", 100), rules, initial_cash=2000)
        second = run_events(bars, FixedQuantityStrategy("ETF", 100), rules, initial_cash=2000)
        self.assertEqual(first.equity, second.equity)
        self.assertEqual(first.equity[0]["equity"], "2000")
        self.assertEqual(first.equity[1]["equity"], "2000")
        self.assertEqual(first.equity[2]["equity"], "1995")
        self.assertEqual(D(first.equity[-1]["equity"]), D("2095"))
        self.assertEqual(first.book.ledger.entries[0].fill.time, bars[1].time)

    def test_strategy_history_excludes_current_and_future(self):
        seen = []
        class Probe:
            def decide(self, context):
                seen.append((context.time, context.history))
                with self_outer.assertRaises(TypeError):
                    context.prices["ETF"] = 999
                return {}
        self_outer = self
        run_events([bar(2, 0), bar(2, 1)], Probe(), {"ETF": SecurityRule()})
        self.assertEqual(len(seen[0][1]), 0)
        self.assertEqual(len(seen[1][1]), 1)
        self.assertTrue(all(b.time < seen[1][0] for b in seen[1][1]))

    def test_target_changed_cancels_remaining_and_can_sell(self):
        class Switch:
            def decide(self, context):
                return {"ETF": 100 if context.time.endswith("14:00:00+08:00") else 0}
        result = run_events([bar(2, 0), bar(2, 1, volume=200), bar(2, 2)], Switch(),
                            {"ETF": SecurityRule(same_day_sell=True)}, initial_cash=2000)
        self.assertEqual(result.book.orders["order-000001"].status, "cancelled")
        self.assertEqual(result.book.orders["order-000001"].filled, 50)
        self.assertEqual(result.book.ledger.positions, {})
        self.assertEqual(result.book.ledger.cash, D("1990"))

    def test_partial_pursuit_without_duplicate_and_end_release(self):
        result = run_events([bar(2, 0), bar(2, 1, volume=100), bar(2, 2, volume=100)],
                            FixedQuantityStrategy("ETF", 100), {"ETF": SecurityRule()}, initial_cash=2000)
        self.assertEqual(len(result.book.orders), 1)
        self.assertEqual(result.book.ledger.positions["ETF"].quantity, 50)
        self.assertEqual(result.book.reserved_cash, 0)

    def test_bad_sequence_and_duplicate_rejected(self):
        for bars in ([bar(2, 1), bar(2, 0)], [bar(2, 0), bar(2, 0)]):
            with self.assertRaises(ValueError):
                run_events(bars, FixedQuantityStrategy("ETF", 100), {"ETF": SecurityRule()})

    def test_daily_gtc_and_day_order_expiry_are_explicit(self):
        bars = [bar(2, 0), bar(3, 0)]
        rules = {"ETF": SecurityRule()}
        for day_orders, expected in ((True, 0), (False, 1)):
            result = run_events(bars, FixedQuantityStrategy("ETF", 100), rules, 2000, day_orders=day_orders)
            self.assertEqual(len(result.book.ledger.entries), expected)


if __name__ == "__main__":
    unittest.main()
