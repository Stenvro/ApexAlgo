"""Exit rules shared by the backtest and the live tick: stop losses, take
profits and the strategy sell/cover signal, evaluated on one candle.

One direction-parameterised body serves both sides. With `dir = +1` (long)
every formula reduces literally to the original spot code; `dir = -1` (short)
mirrors it: a stop loss sits ABOVE the entry and is hit by the candle high, a
take profit BELOW and is hit by the low, trailing levels anchor to the lowest
price reached so far (stored in the same `highest_price` slot — opposite
extreme) and the strategy exit is the `cover` signal sized by
`trade_settings.cover`."""
import logging
from backend.engine import pnl
from backend.engine.sizing import _num

logger = logging.getLogger("apexalgo.bot_manager")

VALID_EXIT_TYPES = {'percentage', 'trailing', 'atr', 'fixed'}


def _better(dir, a, b):
    """The fill that favours the position: the higher price for a long, the
    lower for a short."""
    return max(a, b) if dir > 0 else min(a, b)


def _worse(dir, a, b):
    """The fill that goes against the position (gap-through-trigger fills)."""
    return min(a, b) if dir > 0 else max(a, b)


def _reached_favorable(dir, price, level):
    """`price` is at or beyond `level` in the profitable direction."""
    return price >= level if dir > 0 else price <= level


def _reached_adverse(dir, price, level):
    """`price` is at or beyond `level` in the losing direction."""
    return price <= level if dir > 0 else price >= level


def _adverse_level(dir, base, pct):
    """`base` moved `pct` percent against the position."""
    return base * (1 - dir * (pct / 100))


def _favorable_level(dir, base, pct):
    """`base` moved `pct` percent in favour of the position."""
    return base * (1 + dir * (pct / 100))


def _parse_rule(rule, i, kind, prefix):
    """(value, type) of one SL/TP rule, or None when it must be skipped."""
    try:
        val = float(rule.get('value', 0))
    except (ValueError, TypeError):
        return None
    if val <= 0:
        return None
    rtype = rule.get('type', '')
    if rtype not in VALID_EXIT_TYPES:
        logger.warning("Invalid %s type '%s' for %s_%d, skipping", kind, rtype, prefix, i)
        return None
    return val, rtype


def check_exits(position_states, states_lock, open_position, row_close, row_high, row_low, is_sell_signal, bot_settings, current_atr=0.0, row_open=None, side="long"):
    """Stop-loss / take-profit / strategy-exit events for one position on one
    candle. `position_states` (keyed by position id) carries the trailing
    extreme and the exits already fired; `states_lock` guards it across
    threads. `side="short"` evaluates the mirrored rule set; `is_sell_signal`
    is then the cover signal."""
    dir = pnl.direction(side)
    trade_settings = bot_settings.get("trade_settings", {})
    entry_settings = pnl.entry_cfg(trade_settings, side)
    events = []
    if row_open is None:
        row_open = row_close
    # The candle extreme that moves against / in favour of the position
    adverse = row_low if dir > 0 else row_high
    favorable = row_high if dir > 0 else row_low

    with states_lock:
        state = position_states.get(open_position.id)
        if not state or state.get('entry_price') != open_position.entry_price:
            # Restore persisted state from DB, or initialize fresh
            persisted_extreme = open_position.highest_price or open_position.entry_price
            persisted_exits = set(open_position.triggered_exits or [])
            state = {
                'entry_price': open_position.entry_price,
                'highest_price': persisted_extreme,  # lowest price reached for a short
                'triggered_exits': persisted_exits
            }
            position_states[open_position.id] = state
        # Trailing levels anchor to the extreme reached BEFORE this candle; the
        # current candle's extreme is folded in afterwards so one candle cannot
        # both move the trail and trigger it against its own range.
        prev_extreme = state['highest_price']
        triggered_exits = set(state['triggered_exits'])

    sl_hit = False
    for i, sl in enumerate(entry_settings.get("stop_losses", [])):
        sl_id = f"sl_{i}"
        if sl_id in triggered_exits: continue
        parsed = _parse_rule(sl, i, "stop_loss", "sl")
        if parsed is None: continue
        sl_val, sl_type = parsed

        if sl_type == 'percentage':
            trigger_price = _adverse_level(dir, open_position.entry_price, sl_val)
        elif sl_type == 'trailing':
            trigger_price = _adverse_level(dir, prev_extreme, sl_val)
        elif sl_type == 'atr':
            if not (current_atr > 0): continue
            trigger_price = prev_extreme - dir * (sl_val * current_atr)
        else:
            trigger_price = sl_val

        if _reached_adverse(dir, adverse, trigger_price):
            sl_close_type = sl.get('close_amount_type', 'percentage')
            sl_close_val = float(sl.get('close_amount_value', 100))
            events.append({
                'qty_pct': sl_close_val,
                'close_amount_type': sl_close_type,
                'reason': "stop_loss",
                # A gap through the trigger fills at the (worse) open, not the trigger
                'price': _worse(dir, trigger_price, row_open),
                'id': sl_id
            })
            sl_hit = True

    if not sl_hit:
        tps = []
        for i, tp in enumerate(entry_settings.get("take_profits", [])):
            tp_id = f"tp_{i}"
            if tp_id in triggered_exits: continue
            parsed = _parse_rule(tp, i, "take_profit", "tp")
            if parsed is None: continue
            tp_val, tp_type = parsed

            tp_close_type = tp.get('close_amount_type', 'percentage')
            tp_close_val = float(tp.get('close_amount_value', 100))

            if tp_type == 'percentage':
                t_price = _favorable_level(dir, open_position.entry_price, tp_val)
                if _reached_favorable(dir, favorable, t_price):
                    # A gap through the target fills at the (better) open price
                    tps.append({'id': tp_id, 'price': _better(dir, t_price, row_open), 'pct': tp_close_val, 'close_amount_type': tp_close_type})
            elif tp_type == 'trailing':
                # Trailing TP: price must first move tp_val% in favour of the
                # position, then we close when it retraces tp_val% from the
                # extreme reached.
                activation_price = _favorable_level(dir, open_position.entry_price, tp_val)
                if _reached_favorable(dir, prev_extreme, activation_price):
                    # Once activated, trail behind the extreme
                    t_price = _adverse_level(dir, prev_extreme, tp_val)
                    if _reached_adverse(dir, adverse, t_price):
                        tps.append({'id': tp_id, 'price': _worse(dir, t_price, row_open), 'pct': tp_close_val, 'close_amount_type': tp_close_type})
            elif tp_type == 'atr':
                if not (current_atr > 0): continue
                t_price = prev_extreme - dir * (tp_val * current_atr)
                if _reached_adverse(dir, adverse, t_price):
                    tps.append({'id': tp_id, 'price': _worse(dir, t_price, row_open), 'pct': tp_close_val, 'close_amount_type': tp_close_type})
            else:
                t_price = tp_val
                if _reached_favorable(dir, favorable, t_price):
                    tps.append({'id': tp_id, 'price': _better(dir, t_price, row_open), 'pct': tp_close_val, 'close_amount_type': tp_close_type})

        # Best fill first: highest price for a long, lowest for a short
        tps = sorted(tps, key=lambda x: dir * x['price'], reverse=True)

        for tp in tps:
            events.append({
                'qty_pct': tp['pct'],
                'close_amount_type': tp.get('close_amount_type', 'percentage'),
                'reason': "take_profit",
                'price': tp['price'],
                'id': tp['id']
            })

    if not events and is_sell_signal:
        exit_settings = pnl.exit_cfg(trade_settings, side)
        pct_to_close = _num(exit_settings.get('amount_value'), 100) if exit_settings.get('amount_type') == 'percentage' else 100
        events.append({
            'qty_pct': pct_to_close,
            'reason': "strategy",
            'price': row_close,
            'id': 'strategy_cover' if dir < 0 else 'strategy_sell'
        })

    with states_lock:
        state['highest_price'] = _better(dir, state['highest_price'], favorable)
        # Persist state back to DB for crash recovery
        open_position.highest_price = state['highest_price']

    return events
