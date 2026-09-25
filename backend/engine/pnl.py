"""Side-aware trade economics shared by the backtest, the live tick, the
drawdown guard and the trades router. A long is `+1`, a short `-1`; every
formula for the long side reduces to the pre-shorts spot code so long-only
bots stay byte-identical."""

from backend.engine import contracts as _contracts

# Maintenance margin ratio used by the liquidation approximation; the
# exchange-specific tiers are ignored (documented in the backtest summary).
# Lives in `contracts`; re-exported for the callers/tests that read it here.
MAINTENANCE_MARGIN = _contracts.MAINTENANCE_MARGIN

SIDES = ("long", "short")


def direction(side):
    """+1 for a long (or unknown/legacy `None`), -1 for a short."""
    return -1 if side == "short" else 1


def is_short(side):
    return side == "short"


def price_pnl(side, entry_price, exit_price, amount, spec=None):
    """Gross PnL in the cash currency before fees: what the position gained
    between `entry_price` and `exit_price` on `amount` units. Linear/spot
    when no `spec` is given; a `ContractSpec` routes inverse contracts
    through their own formula."""
    if spec is not None:
        return spec.pnl_cash(side, amount, entry_price, exit_price)
    return direction(side) * (exit_price - entry_price) * amount


def liquidation_price(side, entry_price, leverage, spec=None):
    """Price where the margin is exhausted up to the maintenance margin:
    `entry*(1-(1-MMR)/lev)` for a long, `entry*(1+(1-MMR)/lev)` for a short.
    A 1x long can lose at most its margin (None: no liquidation level); a 1x
    short is liquidated once the price roughly doubles (`entry*(1+(1-MMR))`).
    With a `spec` the contract kind decides (inverse has its own curve)."""
    if spec is not None:
        return spec.liquidation_price(side, entry_price, leverage)
    if not entry_price:
        return None
    lev = max(float(leverage or 1), 1.0)
    if lev <= 1 and not is_short(side):
        return None
    move = (1 - MAINTENANCE_MARGIN) / lev
    return entry_price * (1 + move) if is_short(side) else entry_price * (1 - move)


def locked_capital(entry_price, amount, leverage=1, fee=0.0, spec=None):
    """Capital taken out of the pool to open `amount` units at `entry_price`:
    margin (`notional/leverage`) plus the entry fee on the full notional.
    Reduces to `entry*qty*(1+fee)` on spot (leverage 1). This is the single
    basis of `profit_pct` in the backtest, the live/forward tick and the
    manual close. With a `spec` the contract kind decides the notional."""
    if spec is not None and spec.is_inverse:
        return spec.locked_capital(amount, entry_price, leverage, fee)
    notional = float(entry_price or 0.0) * float(amount or 0.0)
    lev = max(float(leverage or 1), 1.0)
    return notional / lev + notional * float(fee or 0.0)


def close_epsilon(original_amount):
    """Tolerance below which a remaining amount counts as fully closed:
    relative to the original size so dust on low-priced coins (SHIB-sized
    quantities) still closes, with an absolute floor for tiny positions."""
    return max(1e-9, float(original_amount or 0.0) * 1e-6)


def liquidated(side, liq_price, row_high, row_low):
    """Whether one candle's range touched the liquidation level."""
    if liq_price is None:
        return False
    return row_high >= liq_price if is_short(side) else row_low <= liq_price


def entry_cfg(trade_settings, side):
    """Opening-leg config for `side`: `trade_settings.short` (falling back to
    `entry`) for a short, `trade_settings.entry` for a long."""
    if is_short(side):
        return trade_settings.get("short") or trade_settings.get("entry", {})
    return trade_settings.get("entry", {})


def exit_cfg(trade_settings, side):
    """Closing-leg config for `side`: `trade_settings.cover` (falling back to
    `exit`) for a short, `trade_settings.exit` for a long."""
    if is_short(side):
        return trade_settings.get("cover") or trade_settings.get("exit", {})
    return trade_settings.get("exit", {})


def open_order_side(side):
    """Exchange order side that opens a position of `side`."""
    return "sell" if is_short(side) else "buy"


def close_order_side(side):
    """Exchange order side that closes a position of `side`."""
    return "buy" if is_short(side) else "sell"
