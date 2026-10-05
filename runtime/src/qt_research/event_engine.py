"""Deterministic local share backtest. Strategies receive observed data only.

This first adapter treats each input bar as one matching event at its price.
Daily bars match at the NEXT daily close; they cannot reconstruct intraday S02.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby
from types import MappingProxyType
from decimal import Decimal

from .orders import OrderBook, OrderRequest
from .execution import BarMatcher, ExecutionConfig


@dataclass(frozen=True)
class StrategyContext:
    time: str
    history: tuple
    prices: object
    positions: object
    cash: Decimal
    available_cash: Decimal


@dataclass
class RunResult:
    book: OrderBook
    equity: list
    positions: list
    signals: list
    events: list


class FixedQuantityStrategy:
    def __init__(self, symbol, shares):
        self.symbol, self.shares = symbol, shares

    def decide(self, context):
        return {self.symbol: self.shares}


def run_events(bars, strategy, rules, initial_cash=50000, execution=None, limit_buffer="0.01", day_orders=True):
    bars = tuple(bars)
    if not bars:
        raise ValueError("No market events")
    times = [datetime.fromisoformat(b.time) for b in bars]
    if times != sorted(times):
        raise ValueError("Input events must be chronological")
    if len({(t, b.symbol) for t, b in zip(times, bars)}) != len(bars):
        raise ValueError("Duplicate market event")
    if any(b.symbol not in rules for b in bars):
        raise ValueError("Market event lacks explicit security rule")
    config = execution or ExecutionConfig()
    buffer = Decimal(str(limit_buffer))
    if not buffer.is_finite() or not 0 <= buffer < 1:
        raise ValueError("Invalid limit buffer")
    book = OrderBook(initial_cash, rules)
    matcher = BarMatcher(book, config)
    equity = [{"time": (times[0] - timedelta(microseconds=1)).isoformat(),
               "cash": str(book.ledger.cash), "equity": str(book.ledger.cash),
               "reserved_cash": "0", "nav": 1.0, "baseline": True}]
    holdings, signals, events = [], [], []
    history, prices, last_targets = [], {}, {}
    sequence = 0
    event_cursor = 0
    def drain(time):
        nonlocal event_cursor
        for event in book.events[event_cursor:]:
            events.append(dict(event, time=event.get("time", time)))
        event_cursor = len(book.events)
    for dt, group in groupby(bars, key=lambda b: datetime.fromisoformat(b.time)):
        current = tuple(group)
        session = dt.date()
        if book._session != session:
            if day_orders:
                for order in book.active_orders:
                    book.cancel(order.request.order_id, "day_order_expired")
            book.start_session(session.isoformat())
        for bar in current:
            matcher.match(bar)
            prices[bar.symbol] = bar.price
        drain(dt.isoformat())
        context = StrategyContext(dt.isoformat(), tuple(history), MappingProxyType(dict(prices)),
                                  MappingProxyType({s: p.quantity for s, p in book.ledger.positions.items()}),
                                  book.ledger.cash, book.available_cash)
        targets = strategy.decide(context)
        if targets is not None:
            targets = dict(targets)
            if any(s not in rules or type(q) is not int or q < 0 for s, q in targets.items()):
                raise ValueError("Targets require known securities and nonnegative integer shares")
            complete = {s: targets.get(s, 0) for s in rules}
            if complete != last_targets:
                signals.append({"time": dt.isoformat(), "targets": complete})
                events.append({"event": "signal", "time": dt.isoformat(), "targets": complete})
                for order in book.active_orders:
                    if complete[order.request.symbol] != last_targets.get(order.request.symbol):
                        book.cancel(order.request.order_id, "target_changed")
                last_targets = complete
                drain(dt.isoformat())
        # Existing targets are pursued after each event; active orders retain their
        # remaining quantity, avoiding duplicate orders on subsequent bars.
        for side in ("sell", "buy"):
            for symbol, target in last_targets.items():
                if symbol not in prices or any(o.request.symbol == symbol for o in book.active_orders):
                    continue
                held = book.ledger.positions.get(symbol)
                qty_held = held.quantity if held else 0
                delta = target - qty_held
                if (side == "sell" and delta >= 0) or (side == "buy" and delta <= 0):
                    continue
                rule = rules[symbol]
                price = prices[symbol] * (1 + buffer if side == "buy" else 1 - buffer)
                if side == "buy":
                    qty = delta // rule.lot * rule.lot
                    affordable = int(book.available_cash / price) // rule.lot * rule.lot
                    qty = min(qty, affordable)
                    while qty and qty * price + config.fee_total(qty * price) > book.available_cash:
                        qty -= rule.lot
                else:
                    qty = min(-delta, book.available_quantity(symbol))
                    odd = qty % rule.lot
                    if odd and odd != qty_held % rule.lot:
                        qty -= odd
                if qty <= 0:
                    events.append({"event": "target_pending", "time": dt.isoformat(),
                                   "symbol": symbol, "target": target, "held": qty_held,
                                   "reason": "cash_settlement_or_lot_constraint"})
                    continue
                sequence += 1
                fee_budget = config.fee_total(qty * price * (Decimal("1.1") if side == "sell" else 1))
                request = OrderRequest(f"order-{sequence:06d}", symbol, side, qty, price, fee_budget)
                order = matcher.submit(request, dt.isoformat())
                events.append({"event": "submit", "time": dt.isoformat(), "order_id": request.order_id,
                               "symbol": symbol, "side": side, "quantity": qty,
                               "status": order.status})
                drain(dt.isoformat())
        value = book.ledger.equity(prices)
        equity.append({"time": dt.isoformat(), "cash": str(book.ledger.cash), "equity": str(value),
                       "reserved_cash": str(book.reserved_cash), "nav": float(value / book.ledger.initial_cash),
                       "baseline": False})
        for symbol, pos in book.ledger.positions.items():
            holdings.append({"time": dt.isoformat(), "symbol": symbol, "quantity": pos.quantity,
                             "cost": str(pos.cost), "market_value": str(prices[symbol] * pos.quantity),
                             "available": book.available_quantity(symbol)})
        history.extend(current)
    for order in book.active_orders:
        book.cancel(order.request.order_id, "backtest_ended")
    drain(times[-1].isoformat())
    equity[-1]["reserved_cash"] = str(book.reserved_cash)
    return RunResult(book, equity, holdings, signals, events)
