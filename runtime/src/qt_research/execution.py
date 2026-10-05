"""Explicit simplified bar model: protected limits, shared volume, per-order fee.

No order book/queue reconstruction. Zero-volume or invalid bars never fill.
Orders match only when submitted strictly before the bar timestamp.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR

from .ledger import Fill, money


@dataclass(frozen=True)
class ExecutionConfig:
    commission_rate: Decimal = Decimal("0.0002")
    minimum_commission: Decimal = Decimal("5")
    participation: Decimal = Decimal("0.25")
    slippage: Decimal = Decimal("0")

    def __post_init__(self):
        for key in ("commission_rate", "minimum_commission", "participation", "slippage"):
            object.__setattr__(self, key, money(getattr(self, key)))
        if self.commission_rate < 0 or self.minimum_commission < 0:
            raise ValueError("Invalid commission")
        if not 0 < self.participation <= 1 or not 0 <= self.slippage < 1:
            raise ValueError("Invalid participation or slippage")

    commission_exempt_symbols: tuple = ()

    def fee_total(self, gross, symbol=None):
        if gross == 0 or symbol in self.commission_exempt_symbols:
            return Decimal("0")
        return max(self.minimum_commission, gross * self.commission_rate)


@dataclass(frozen=True)
class Bar:
    time: str
    symbol: str
    price: Decimal
    volume: int

    def __post_init__(self):
        dt = datetime.fromisoformat(self.time)
        if dt.utcoffset() is None:
            raise ValueError("Bar requires timezone")
        object.__setattr__(self, "price", money(self.price))
        if self.price <= 0 or type(self.volume) is not int or self.volume < 0:
            raise ValueError("Invalid bar price or volume")


class BarMatcher:
    def __init__(self, book, config=None):
        self.book = book
        self.config = config or ExecutionConfig()
        self._submitted = {}
        self._bars = {}
        self._last_time = None

    def submit(self, request, time):
        dt = datetime.fromisoformat(time)
        if (self.book._session is None or dt.utcoffset() is None or
                dt.date().isoformat() != self.book._session.isoformat()):
            raise ValueError("Order time requires current session and timezone")
        if self._last_time is not None and dt < self._last_time:
            raise ValueError("Order time cannot precede observed bars")
        if request.order_id in self._submitted and self._submitted[request.order_id] != dt:
            raise ValueError("Conflicting order timestamp")
        order = self.book.submit(request)
        self._submitted[request.order_id] = dt
        return order

    def match(self, bar):
        dt = datetime.fromisoformat(bar.time)
        key = (dt, bar.symbol)
        if key in self._bars:
            prior_bar, entries = self._bars[key]
            if prior_bar != bar:
                raise ValueError("Conflicting bar replay")
            return entries
        if self.book._session is None or dt.date() != self.book._session:
            raise ValueError("Bar is outside current session")
        if self._last_time is not None and dt < self._last_time:
            raise ValueError("Bar time cannot move backwards")
        self._last_time = dt
        capacity = int((Decimal(bar.volume) * self.config.participation).to_integral_value(rounding=ROUND_FLOOR))
        entries = []
        for order in self.book.active_orders:
            request = order.request
            if request.symbol != bar.symbol or self._submitted[request.order_id] >= datetime.fromisoformat(bar.time):
                continue
            qty = min(order.remaining, capacity)
            if qty <= 0:
                continue
            sign = 1 if request.side == "buy" else -1
            price = bar.price * (1 + sign * self.config.slippage)
            if ((request.side == "buy" and price > request.limit_price) or
                    (request.side == "sell" and price < request.limit_price)):
                continue
            fee = max(Decimal("0"), self.config.fee_total(order.gross + price * qty, request.symbol) - order.fees)
            if fee + order.fees > request.fee_budget:
                self.book.cancel(request.order_id, "fee_budget_exhausted")
                continue
            fill = Fill("fill:" + request.order_id + ":" + bar.time, request.order_id,
                        bar.time, request.symbol, request.side, qty, price, fee)
            entries.append(self.book.apply_fill(fill))
            capacity -= qty
        self._bars[key] = (bar, tuple(entries))
        return tuple(entries)
