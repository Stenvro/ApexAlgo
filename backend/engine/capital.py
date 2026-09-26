"""Per-currency capital pools.

A bot's money lives in exactly one cash currency per bot (the validator
refuses whitelists that mix USDT- and BTC-settled pairs), but the pool is
keyed by currency anyway so that a EUR spot bot, a USDT linear bot and a
BTC-margined inverse bot all keep their books in their own unit and no code
path ever adds a BTC amount to a USDT amount.

`backtest_capital` in the settings stays a scalar; it is interpreted in the
bot's cash currency (`CapitalPools.for_bot`).

Spot margin is deliberately not implemented: `lock` never lets `cash` go
negative (the backtest halts entries at zero, live sizing is capped by the
verified balance). A spot-margin implementation would relax that floor
here — and nowhere else.
"""
from dataclasses import dataclass, field


@dataclass
class Pool:
    currency: str
    start: float = 0.0
    cash: float = 0.0     # free, not locked in open positions
    locked: float = 0.0   # margin + entry fees of open positions (informational)


@dataclass
class CapitalPools:
    pools: dict = field(default_factory=dict)

    @classmethod
    def for_bot(cls, currency: str, start: float) -> "CapitalPools":
        """A bot's single pool: `backtest_capital` in its cash currency."""
        p = cls()
        p.pools[currency] = Pool(currency=currency, start=float(start), cash=float(start))
        return p

    def pool(self, currency: str) -> Pool:
        cur = str(currency or "").upper()
        if cur not in self.pools:
            self.pools[cur] = Pool(currency=cur)
        return self.pools[cur]

    def cash(self, currency: str) -> float:
        return self.pool(currency).cash

    def start(self, currency: str) -> float:
        return self.pool(currency).start

    def lock(self, currency: str, amount: float) -> None:
        """Take `amount` (margin + fee) out of the free cash. Floors at zero:
        no spot margin (see module docstring)."""
        p = self.pool(currency)
        p.cash = max(p.cash - amount, 0.0)
        p.locked += amount

    def release(self, currency: str, amount: float, locked: float | None = None) -> None:
        """Return `amount` (margin + PnL − exit fee) to the free cash and
        forget `locked` (what the closed layer had taken out)."""
        p = self.pool(currency)
        p.cash += amount
        if locked is not None:
            p.locked = max(p.locked - locked, 0.0)

    def forget(self, currency: str, locked: float) -> None:
        """A layer's locked capital is gone for good (liquidation)."""
        p = self.pool(currency)
        p.locked = max(p.locked - locked, 0.0)

    def charge(self, currency: str, amount: float) -> None:
        """Book a cash flow that is not a fill: a funding payment (negative)
        or receipt (positive). The free cash may go below zero — the
        exchange takes funding from the margin balance, and a negative
        balance blocks new entries exactly like a depleted pool."""
        self.pool(currency).cash += float(amount or 0.0)

    def drain(self, currency: str) -> float:
        """Cross-margin liquidation: the whole wallet is gone. Returns the
        free cash that was lost on top of the positions' margins."""
        p = self.pool(currency)
        lost = max(p.cash, 0.0)
        p.cash = 0.0
        p.locked = 0.0
        return lost

    def currencies(self) -> list:
        return list(self.pools)

    def single_currency(self) -> str | None:
        return next(iter(self.pools)) if len(self.pools) == 1 else None
