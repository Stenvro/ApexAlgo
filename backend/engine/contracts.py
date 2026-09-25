"""Contract economics: what a position of ``qty`` on a symbol is worth, what
it costs to open, what it pays out and in which currency.

Three kinds of instrument exist in ApexAlgo:

``spot``
    ``BTC/EUR``: ``qty`` is a base amount, everything is priced and settled
    in the quote currency (EUR).
``linear``
    ``BTC/USDT:USDT``: a perpetual settled in its quote (stablecoin) currency.
    ``qty`` is a base amount (the exchange counts contracts of
    ``contract_size`` base units; the broker converts). PnL in the settle
    currency is ``qty * (exit - entry)``, exactly like spot.
``inverse``
    ``BTC/USD:BTC``: a coin-margined perpetual. ``qty`` is a number of
    contracts worth ``contract_size`` *quote* units (USD) each, margin and
    PnL are in the base coin (BTC) and the PnL is
    ``contracts * size * (1/entry - 1/exit)`` — non-linear in price.

Every money path of the engine (backtest, forward, paper, live) goes through
a ``ContractSpec`` so that an amount of money never travels as a bare float
without a known currency (``cash_currency``). The spot/linear formulas are
kept literally identical to the pre-inverse engine so existing bots and the
golden backtests stay byte-for-byte the same.

``qty`` semantics are the spec's: base units for spot/linear, contracts for
inverse. ``Position.amount`` stores exactly that, ``Position.contract_kind``
and ``Position.contract_size`` say how to read it back.
"""
from dataclasses import dataclass

from backend.engine.symbols import base_of, is_derivative, normalize, quote_of, settle_of

KINDS = ("spot", "linear", "inverse")

# Flat maintenance margin ratio: the fallback of the liquidation formulas
# when no exchange tier is stored for the pair (`engine/tiers.mmr_for`);
# the backtest summary reports which one applied as `mmr_source`
MAINTENANCE_MARGIN = 0.005


def _direction(side):
    return -1 if side == "short" else 1


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    kind: str                 # "spot" | "linear" | "inverse"
    base: str
    quote: str
    settle: str | None        # None on spot
    contract_size: float = 1.0  # linear: base units per contract; inverse: quote units per contract

    # ── identity ────────────────────────────────────────────────────────
    @property
    def cash_currency(self) -> str:
        """Currency the position is funded in and PnL is paid in."""
        return self.settle or self.quote

    @property
    def is_inverse(self) -> bool:
        return self.kind == "inverse"

    @property
    def is_spot(self) -> bool:
        return self.kind == "spot"

    @property
    def market_type(self) -> str:
        return "spot" if self.is_spot else "swap"

    # ── notional ────────────────────────────────────────────────────────
    def notional_cash(self, qty, price) -> float:
        """Value of `qty` at `price` in the cash currency."""
        qty = float(qty or 0.0)
        price = float(price or 0.0)
        if self.is_inverse:
            return qty * self.contract_size / price if price else 0.0
        return price * qty

    def notional_quote(self, qty, price) -> float:
        """Value of `qty` at `price` in the quote currency (USD on
        ``BTC/USD:BTC``); this is what `max_order_value` caps."""
        qty = float(qty or 0.0)
        if self.is_inverse:
            return qty * self.contract_size
        return float(price or 0.0) * qty

    def base_amount(self, qty, price) -> float:
        """`qty` expressed in base units (coins)."""
        if self.is_inverse:
            return self.notional_cash(qty, price)
        return float(qty or 0.0)

    # ── sizing ──────────────────────────────────────────────────────────
    def qty_for_cash(self, cash, price, leverage=1) -> float:
        """Position size (spec units) that `cash` buys as margin at
        `leverage`. Spot/linear keep the historical `cash / price * lev`
        evaluation order."""
        price = float(price or 0.0)
        if price <= 0:
            return 0.0
        lev = float(leverage or 1)
        if self.is_inverse:
            qty = float(cash) * price / self.contract_size
        else:
            qty = float(cash) / price
        if lev != 1.0:
            qty *= lev
        return qty

    # Plan name; identical to qty_for_cash (contracts on inverse, base otherwise)
    contracts_for_cash = qty_for_cash

    def amount_for_cash(self, cash, price, leverage=1) -> float:
        """Base amount (coins) that `cash` buys — spot/linear compatibility."""
        return self.base_amount(self.qty_for_cash(cash, price, leverage), price)

    def qty_for_quote_notional(self, quote_notional, price) -> float:
        """Size whose quote notional is `quote_notional` (max_order_value cap)."""
        price = float(price or 0.0)
        if self.is_inverse:
            return float(quote_notional) / self.contract_size
        return float(quote_notional) / price if price else 0.0

    def max_affordable_qty(self, cash, price, leverage=1, fee=0.0) -> float:
        """Largest size whose locked capital (margin + entry fee on the
        notional) fits in `cash`; spot reduces to `cash / (price * (1 + fee))`."""
        lev = max(float(leverage or 1), 1.0)
        fee = float(fee or 0.0)
        price = float(price or 0.0)
        if self.is_inverse:
            if price <= 0:
                return 0.0
            # notional_cash = qty*size/price → qty = cash*price / (size*(1/lev+fee))
            return float(cash) * price / (self.contract_size * (1 / lev + fee))
        return float(cash) / (price * (1 / lev + fee))

    # ── money in / out ──────────────────────────────────────────────────
    def margin(self, qty, entry_price, leverage=1) -> float:
        """Cash locked as margin (the full notional on spot / 1x)."""
        lev = max(float(leverage or 1), 1.0)
        n = self.notional_cash(qty, entry_price)
        return n if lev == 1.0 and self.is_spot else n / lev

    def fee_cash(self, qty, price, fee_rate) -> float:
        """Fee on the notional of `qty` at `price`, in the cash currency."""
        return self.notional_cash(qty, price) * float(fee_rate or 0.0)

    def locked_capital(self, qty, entry_price, leverage=1, fee=0.0) -> float:
        """What leaves the pool on open: margin plus the entry fee on the
        notional. Spot keeps ``notional * (1 + fee)`` literally."""
        n = self.notional_cash(qty, entry_price)
        fee = float(fee or 0.0)
        if self.is_spot:
            return n * (1 + fee)
        lev = max(float(leverage or 1), 1.0)
        return n / lev + n * fee

    def pnl_cash(self, side, qty, entry_price, exit_price) -> float:
        """Gross PnL in the cash currency before fees."""
        qty = float(qty or 0.0)
        entry_price = float(entry_price or 0.0)
        exit_price = float(exit_price or 0.0)
        d = _direction(side)
        if self.is_inverse:
            if not entry_price or not exit_price:
                return 0.0
            return d * qty * self.contract_size * (1 / entry_price - 1 / exit_price)
        return d * (exit_price - entry_price) * qty

    def close_return(self, side, qty, entry_price, exit_price, leverage=1, fee=0.0) -> float:
        """What comes back to the pool when `qty` closes at `exit_price`:
        the sale proceeds net of fee on spot; margin + PnL − exit fee on
        derivatives."""
        if self.is_spot:
            return float(exit_price) * float(qty) * (1 - float(fee or 0.0))
        return self.margin(qty, entry_price, leverage) + self.pnl_cash(side, qty, entry_price, exit_price) \
            - self.fee_cash(qty, exit_price, fee)

    def realized_pnl(self, side, qty, entry_price, exit_price, leverage=1, entry_fee=0.0, exit_fee=0.0) -> float:
        """Net PnL of closing `qty`: fees on both legs included."""
        if self.is_spot or side != "short":
            # Long legs: proceeds/return minus what was locked (spot-identical form)
            return self.close_return(side, qty, entry_price, exit_price, leverage, exit_fee) \
                - self.locked_capital(qty, entry_price, leverage, entry_fee)
        return self.pnl_cash(side, qty, entry_price, exit_price) \
            - self.fee_cash(qty, entry_price, entry_fee) - self.fee_cash(qty, exit_price, exit_fee)

    def mark_value(self, positions, price, leverage=1) -> float:
        """Mark-to-market value of open `positions` (objects with
        `side/entry_price/amount`) at `price`, in the cash currency."""
        if self.is_spot:
            return sum(float(p.amount or 0.0) for p in positions) * float(price)
        return sum(self.margin(p.amount, p.entry_price, leverage)
                   + self.pnl_cash(p.side, p.amount, p.entry_price, price) for p in positions)

    # ── liquidation ─────────────────────────────────────────────────────
    def liquidation_price(self, side, entry_price, leverage, mmr=MAINTENANCE_MARGIN):
        """Price at which the margin is exhausted up to the maintenance
        margin. Linear: ``entry*(1 ∓ (1-mmr)/lev)``; a 1x linear/spot long has
        no liquidation level (None). Inverse long: ``entry*lev/(lev+1-mmr)``,
        inverse short: ``entry*lev/(lev-1+mmr)``."""
        if not entry_price:
            return None
        entry_price = float(entry_price)
        lev = max(float(leverage or 1), 1.0)
        short = side == "short"
        if self.is_inverse:
            return entry_price * lev / (lev - 1 + mmr) if short else entry_price * lev / (lev + 1 - mmr)
        if lev <= 1 and not short:
            return None
        move = (1 - mmr) / lev
        return entry_price * (1 + move) if short else entry_price * (1 - move)

    def liquidation_loss(self, qty, entry_price, leverage=1, entry_fee=0.0) -> float:
        """Cash lost on liquidation: the whole margin plus the entry fee
        already paid (no exit fee is charged)."""
        return self.margin(qty, entry_price, leverage) + self.fee_cash(qty, entry_price, entry_fee)

    # ── exchange plumbing ───────────────────────────────────────────────
    def to_contracts(self, qty) -> float:
        """Exchange order amount: linear contracts of `contract_size` base
        units, contracts as-is on inverse, base amount on spot."""
        qty = float(qty or 0.0)
        if self.kind == "linear" and self.contract_size and self.contract_size != 1.0:
            return qty / self.contract_size
        return qty

    def from_contracts(self, contracts) -> float:
        """Inverse of `to_contracts`."""
        contracts = float(contracts or 0.0)
        if self.kind == "linear" and self.contract_size and self.contract_size != 1.0:
            return contracts * self.contract_size
        return contracts

    def fee_cash_from_fill(self, fee, price):
        """Convert a ccxt fee dict (`{cost, currency}`) into the cash
        currency at `price`; None when it cannot be valued."""
        if not isinstance(fee, dict):
            return None
        cost = fee.get("cost")
        if cost is None:
            return None
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            return None
        cur = str(fee.get("currency") or "").upper()
        cash = self.cash_currency
        if not cur or cur == cash:
            return cost
        price = float(price or 0.0)
        if price <= 0:
            return None
        if cur == self.base and cash == self.quote:
            return cost * price
        if cur == self.quote and cash == self.base:
            return cost / price
        return None


def spec_from_symbol(symbol, contract_size=None) -> ContractSpec:
    """Fallback spec from the unified symbol alone: spot without ``:``,
    linear when the settle equals the quote, inverse when it equals the
    base. `contract_size` defaults to 1 (one base unit / one quote unit)."""
    sym = normalize(symbol)
    base, quote, settle = base_of(sym), quote_of(sym), settle_of(sym)
    if not is_derivative(sym):
        kind = "spot"
    elif settle == base:
        kind = "inverse"
    else:
        kind = "linear"
    size = float(contract_size) if contract_size else 1.0
    return ContractSpec(symbol=sym, kind=kind, base=base, quote=quote, settle=settle, contract_size=size)


def spec_from_market(market: dict, symbol=None) -> ContractSpec:
    """Spec from a ccxt market dict (`market()` / `load_markets()` entry)."""
    if not market:
        return spec_from_symbol(symbol)
    sym = normalize(market.get("symbol") or symbol)
    base = str(market.get("base") or base_of(sym)).upper()
    quote = str(market.get("quote") or quote_of(sym)).upper()
    settle = market.get("settle")
    settle = str(settle).upper() if settle else settle_of(sym)
    if market.get("spot") or not (market.get("contract") or settle):
        kind = "spot"
    elif market.get("inverse") or (settle and settle == base and not market.get("linear")):
        kind = "inverse"
    else:
        kind = "linear"
    size = market.get("contractSize")
    try:
        size = float(size) if size else 1.0
    except (TypeError, ValueError):
        size = 1.0
    return ContractSpec(symbol=sym, kind=kind, base=base, quote=quote,
                        settle=settle if kind != "spot" else None, contract_size=size)


def spec_for(exchange_id, symbol, market=None) -> ContractSpec:
    """Spec for `symbol` on `exchange_id`: from the given market dict, else
    from the registry's cached markets, else from the symbol alone. Never
    touches the network."""
    from backend.core.exchange_registry import contract_spec
    return contract_spec(exchange_id, symbol, market=market)


def spec_for_instance(exchange, symbol) -> ContractSpec:
    """Spec from a ccxt instance whose markets are loaded (falls back to the
    symbol when the market is unknown)."""
    market = None
    try:
        markets = getattr(exchange, "markets", None) or {}
        market = markets.get(symbol) if isinstance(markets, dict) else None
        if market is None and hasattr(exchange, "market"):
            market = exchange.market(symbol)
    except Exception:
        market = None
    ex_id = getattr(exchange, "id", None)
    return spec_for(ex_id, symbol, market=market) if ex_id else spec_from_market(market, symbol)
