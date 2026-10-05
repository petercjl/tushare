"""Long-only execution ledger; matching, settlement and reservations are adapters.

Values use Decimal. Sell P&L uses average entry cost including buy fees.
This module consumes confirmed simulated fills, rather than inferring fills from
targets. It supports arbitrary positive integer fill quantities (partial fills).
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping


def money(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Amount must be finite")
    return result


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    time: str
    symbol: str
    side: str
    quantity: int
    price: Decimal
    fee: Decimal = Decimal("0")

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip() for v in
               (self.fill_id, self.order_id, self.time, self.symbol)):
            raise ValueError("Fill identifiers, time and symbol are required")
        if self.side not in ("buy", "sell"):
            raise ValueError("Side must be buy or sell")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("Quantity must be a positive integer")
        object.__setattr__(self, "price", money(self.price))
        object.__setattr__(self, "fee", money(self.fee))
        if self.price <= 0 or self.fee < 0:
            raise ValueError("Price must be positive and fee nonnegative")


@dataclass(frozen=True)
class Position:
    quantity: int
    cost: Decimal


@dataclass(frozen=True)
class Entry:
    fill: Fill
    cash_after: Decimal
    quantity_after: int
    cost_after: Decimal
    realized_pnl: Decimal


class AccountLedger:
    def __init__(self, initial_cash):
        self.initial_cash = money(initial_cash)
        if self.initial_cash <= 0:
            raise ValueError("Initial cash must be positive")
        self._cash = self.initial_cash
        self._positions = {}
        self._entries = {}
        self._cash_credits = {}

    def credit_cash(self, event_id, amount):
        """Idempotent positive cash distribution, separate from trade fills."""
        amount = money(amount)
        if not event_id or amount < 0:
            raise ValueError("Invalid cash distribution")
        if event_id in self._cash_credits:
            if self._cash_credits[event_id] != amount:
                raise ValueError("Conflicting cash distribution")
            return
        self._cash_credits[event_id] = amount
        self._cash += amount

    @property
    def cash(self):
        return self._cash

    @property
    def positions(self):
        return dict(self._positions)

    @property
    def entries(self):
        return tuple(self._entries.values())

    @property
    def realized_pnl(self):
        return sum((e.realized_pnl for e in self.entries), Decimal("0"))

    def apply_fill(self, fill):
        """Apply atomically; identical fill_id replay is idempotent.

        Execution timestamps are recorded, not scheduled here. The event engine
        must supply fills in chronological order and enforce security rules.
        """
        if not isinstance(fill, Fill):
            raise TypeError("Expected Fill")
        prior = self._entries.get(fill.fill_id)
        if prior is not None:
            if prior.fill != fill:
                raise ValueError("Conflicting duplicate fill_id")
            return prior
        old = self._positions.get(fill.symbol, Position(0, Decimal("0")))
        gross = fill.price * fill.quantity
        realized = Decimal("0")
        if fill.side == "buy":
            cash = self.cash - gross - fill.fee
            if cash < 0:
                raise ValueError("Insufficient cash including fees")
            position = Position(old.quantity + fill.quantity,
                                old.cost + gross + fill.fee)
        else:
            if fill.quantity > old.quantity:
                raise ValueError("Insufficient position")
            allocated = (old.cost if fill.quantity == old.quantity else
                         old.cost * fill.quantity / old.quantity)
            realized = gross - fill.fee - allocated
            cash = self.cash + gross - fill.fee
            if cash < 0:
                raise ValueError("Insufficient cash for sell fee")
            position = Position(old.quantity - fill.quantity, old.cost - allocated)
        entry = Entry(fill, cash, position.quantity, position.cost, realized)
        # All validations precede mutation, so failed fills leave no ledger entry.
        self._cash = cash
        if position.quantity:
            self._positions[fill.symbol] = position
        else:
            self._positions.pop(fill.symbol, None)
        self._entries[fill.fill_id] = entry
        return entry

    def equity(self, prices: Mapping):
        """Mark held securities only; missing prices fail explicitly."""
        total = self.cash
        for symbol, position in self._positions.items():
            if symbol not in prices:
                raise ValueError("Missing valuation price: " + symbol)
            price = money(prices[symbol])
            if price <= 0:
                raise ValueError("Valuation price must be positive")
            total += position.quantity * price
        return total
