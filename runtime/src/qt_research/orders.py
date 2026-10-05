"""Single-threaded simulated order book owning cash and sell reservations.

Limit prices bound purchase cash. An order fee budget is explicit. All simulated
fills enter the ledger through this book; external ledger mutations are invalid.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from .ledger import AccountLedger, Fill, money

ACTIVE = {"accepted", "partially_filled"}


@dataclass(frozen=True)
class SecurityRule:
    lot: int = 100
    same_day_sell: bool = False

    def __post_init__(self):
        if type(self.lot) is not int or self.lot <= 0:
            raise ValueError("Lot must be a positive integer")
        if type(self.same_day_sell) is not bool:
            raise ValueError("same_day_sell must be boolean")


@dataclass(frozen=True)
class OrderRequest:
    order_id: str
    symbol: str
    side: str
    quantity: int
    limit_price: Decimal
    fee_budget: Decimal = Decimal("0")

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip() for v in
               (self.order_id, self.symbol)):
            raise ValueError("Order and symbol identifiers required")
        if self.side not in {"buy", "sell"}:
            raise ValueError("Invalid side")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("Quantity must be a positive integer")
        object.__setattr__(self, "limit_price", money(self.limit_price))
        object.__setattr__(self, "fee_budget", money(self.fee_budget))
        if self.limit_price <= 0 or self.fee_budget < 0:
            raise ValueError("Invalid price or fee budget")


@dataclass(frozen=True)
class Order:
    request: OrderRequest
    status: str
    filled: int = 0
    gross: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    reason: str = ""

    @property
    def remaining(self):
        return self.request.quantity - self.filled

    @property
    def reserved_cash(self):
        if self.status not in ACTIVE or self.request.side != "buy":
            return Decimal("0")
        return self.remaining * self.request.limit_price + self.request.fee_budget - self.fees


class OrderBook:
    def __init__(self, initial_cash, rules):
        self.ledger = AccountLedger(initial_cash)
        self.rules = dict(rules)
        if not self.rules or any(not isinstance(r, SecurityRule) for r in self.rules.values()):
            raise ValueError("Explicit security rules required")
        self._orders = {}
        self._locked = {}
        self._session = None
        self._events = []

    @property
    def orders(self):
        return dict(self._orders)

    @property
    def events(self):
        return tuple(dict(e) for e in self._events)

    @property
    def active_orders(self):
        return tuple(o for o in self._orders.values() if o.status in ACTIVE)

    @property
    def reserved_cash(self):
        return sum((o.reserved_cash for o in self.active_orders), Decimal("0"))

    @property
    def available_cash(self):
        return self.ledger.cash - self.reserved_cash

    def available_quantity(self, symbol):
        pos = self.ledger.positions.get(symbol)
        held = pos.quantity if pos else 0
        reserved = sum(o.remaining for o in self.active_orders
                       if o.request.side == "sell" and o.request.symbol == symbol)
        return held - self._locked.get(symbol, 0) - reserved

    def start_session(self, session):
        session = date.fromisoformat(str(session))
        if self._session and session < self._session:
            raise ValueError("Session cannot move backwards")
        if session != self._session:
            self._locked.clear()
            self._session = session
            self._events.append({"event": "session", "session": session.isoformat()})

    def submit(self, request):
        if not isinstance(request, OrderRequest):
            raise TypeError("Expected OrderRequest")
        if self._session is None:
            raise ValueError("Start a trading session first")
        prior = self._orders.get(request.order_id)
        if prior:
            if prior.request != request:
                raise ValueError("Conflicting order_id")
            return prior
        reason = ""
        rule = self.rules.get(request.symbol)
        if rule is None:
            reason = "unknown_security"
        elif request.side == "buy":
            if request.quantity % rule.lot:
                reason = "invalid_buy_lot"
            elif request.quantity * request.limit_price + request.fee_budget > self.available_cash:
                reason = "insufficient_available_cash"
        else:
            available = self.available_quantity(request.symbol)
            held = self.ledger.positions.get(request.symbol)
            held_qty = held.quantity if held else 0
            # Include the entire odd remainder or sell only whole lots.
            remainder = request.quantity % rule.lot
            if request.quantity > available:
                reason = "insufficient_available_position"
            elif remainder and remainder != held_qty % rule.lot:
                reason = "invalid_sell_remainder"
            elif remainder and any(o.request.side == "sell" and
                                   o.request.symbol == request.symbol and
                                   o.request.quantity % rule.lot
                                   for o in self.active_orders):
                reason = "sell_remainder_already_reserved"
        order = Order(request, "rejected" if reason else "accepted", reason=reason)
        self._orders[request.order_id] = order
        self._record(order)
        return order

    def _record(self, order):
        self._events.append({"event": "order", "session": self._session.isoformat(),
                             "order_id": order.request.order_id, "status": order.status,
                             "filled": order.filled, "remaining": order.remaining,
                             "reason": order.reason})

    def cancel(self, order_id, reason="user_cancel"):
        order = self._orders[order_id]
        if order.status in ACTIVE:
            order = replace(order, status="cancelled", reason=reason)
            self._orders[order_id] = order
            self._record(order)
        return order

    def apply_fill(self, fill):
        # Identical fills remain idempotent even after cancellation/completion.
        prior = next((e for e in self.ledger.entries if e.fill.fill_id == fill.fill_id), None)
        if prior:
            if prior.fill != fill:
                raise ValueError("Conflicting fill_id")
            return prior
        order = self._orders[fill.order_id]
        request = order.request
        fill_date = datetime.fromisoformat(fill.time).date()
        if fill_date != self._session:
            raise ValueError("Fill is outside current session")
        if order.status not in ACTIVE:
            raise ValueError("Order is terminal")
        if (fill.symbol, fill.side) != (request.symbol, request.side):
            raise ValueError("Fill does not match order")
        if fill.quantity > order.remaining:
            raise ValueError("Overfill")
        if ((fill.side == "buy" and fill.price > request.limit_price) or
                (fill.side == "sell" and fill.price < request.limit_price)):
            raise ValueError("Fill violates limit")
        if order.fees + fill.fee > request.fee_budget:
            raise ValueError("Fill exceeds fee budget")
        if fill.side == "buy":
            # Never spend another order's reservation.
            if fill.quantity * fill.price + fill.fee > self.available_cash + order.reserved_cash:
                raise ValueError("Fill consumes reserved cash")
        entry = self.ledger.apply_fill(fill)
        filled = order.filled + fill.quantity
        updated = replace(order, filled=filled, gross=order.gross + fill.quantity * fill.price,
                          fees=order.fees + fill.fee,
                          status="filled" if filled == request.quantity else "partially_filled")
        self._orders[fill.order_id] = updated
        if fill.side == "buy" and not self.rules[fill.symbol].same_day_sell:
            self._locked[fill.symbol] = self._locked.get(fill.symbol, 0) + fill.quantity
        self._events.append({"event": "fill", "fill_id": fill.fill_id,
                             "order_id": fill.order_id, "time": fill.time,
                             "quantity": fill.quantity, "price": str(fill.price),
                             "fee": str(fill.fee), "cash_after": str(entry.cash_after)})
        self._record(updated)
        return entry
