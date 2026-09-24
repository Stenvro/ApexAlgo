"""Side-aware trade economics shared by the backtest, the live tick, the
drawdown guard and the trades router. A long is `+1`, a short `-1`; every
formula for the long side reduces to the pre-shorts spot code so long-only
bots stay byte-identical."""

# Maintenance margin ratio used by the liquidation approximation; the
# exchange-specific tiers are ignored (documented in the backtest summary)
MAINTENANCE_MARGIN = 0.005

SIDES = ("long", "short")


def direction(side):
    """+1 for a long (or unknown/legacy `None`), -1 for a short."""
    return -1 if side == "short" else 1


def is_short(side):
    return side == "short"


def price_pnl(side, entry_price, exit_price, amount):
    """Gross PnL in quote before fees: what the position gained between
    `entry_price` and `exit_price` on `amount` units."""
    return direction(side) * (exit_price - entry_price) * amount


def liquidation_price(side, entry_price, leverage):
    """Price where the margin is exhausted up to the maintenance margin:
    `entry*(1-(1-MMR)/lev)` for a long, `entry*(1+(1-MMR)/lev)` for a short.
    None at 1x (a long can lose at most its margin; a short is never
    liquidated at 1x in this model — the exposure is bounded by the pool)."""
    if leverage <= 1 or not entry_price:
        return None
    move = (1 - MAINTENANCE_MARGIN) / leverage
    return entry_price * (1 + move) if is_short(side) else entry_price * (1 - move)


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
