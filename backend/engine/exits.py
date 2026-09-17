"""Exit rules shared by the backtest and the live tick: stop losses, take
profits and the strategy sell signal, evaluated on one candle."""
import logging
from backend.engine.sizing import _num

logger = logging.getLogger("apexalgo.bot_manager")

VALID_EXIT_TYPES = {'percentage', 'trailing', 'atr', 'fixed'}


def check_exits(position_states, states_lock, open_position, row_close, row_high, row_low, is_sell_signal, bot_settings, current_atr=0.0, row_open=None):
    """Stop-loss / take-profit / strategy-sell events for one position on one
    candle. `position_states` (keyed by position id) carries the trailing peak
    and the exits already fired; `states_lock` guards it across threads."""
    trade_settings = bot_settings.get("trade_settings", {})
    entry_settings = trade_settings.get("entry", {})
    events = []
    if row_open is None:
        row_open = row_close

    with states_lock:
        state = position_states.get(open_position.id)
        if not state or state.get('entry_price') != open_position.entry_price:
            # Restore persisted state from DB, or initialize fresh
            persisted_highest = open_position.highest_price or open_position.entry_price
            persisted_exits = set(open_position.triggered_exits or [])
            state = {
                'entry_price': open_position.entry_price,
                'highest_price': persisted_highest,
                'triggered_exits': persisted_exits
            }
            position_states[open_position.id] = state
        # Trailing levels anchor to the peak reached BEFORE this candle; the
        # current candle's high is folded in afterwards so one candle cannot
        # both raise the trail and trigger it against its own low.
        prev_highest = state['highest_price']
        triggered_exits = set(state['triggered_exits'])

    sl_hit = False
    for i, sl in enumerate(entry_settings.get("stop_losses", [])):
        sl_id = f"sl_{i}"
        if sl_id in triggered_exits: continue

        try:
            sl_val = float(sl.get('value', 0))
        except (ValueError, TypeError):
            continue

        if sl_val <= 0: continue

        sl_type = sl.get('type', '')
        if sl_type not in VALID_EXIT_TYPES:
            logger.warning("Invalid stop_loss type '%s' for sl_%d, skipping", sl_type, i)
            continue

        if sl_type == 'percentage':
            trigger_price = open_position.entry_price * (1 - (sl_val/100))
        elif sl_type == 'trailing':
            trigger_price = prev_highest * (1 - (sl_val/100))
        elif sl_type == 'atr':
            if not (current_atr > 0): continue
            trigger_price = prev_highest - (sl_val * current_atr)
        else:
            trigger_price = sl_val

        if row_low <= trigger_price:
            sl_close_type = sl.get('close_amount_type', 'percentage')
            sl_close_val = float(sl.get('close_amount_value', 100))
            events.append({
                'qty_pct': sl_close_val,
                'close_amount_type': sl_close_type,
                'reason': "stop_loss",
                # A gap below the trigger fills at the open, not the trigger
                'price': min(trigger_price, row_open),
                'id': sl_id
            })
            sl_hit = True

    if not sl_hit:
        tps = []
        for i, tp in enumerate(entry_settings.get("take_profits", [])):
            tp_id = f"tp_{i}"
            if tp_id in triggered_exits: continue

            try:
                tp_val = float(tp.get('value', 0))
            except (ValueError, TypeError):
                continue

            if tp_val <= 0: continue

            tp_type = tp.get('type', '')
            if tp_type not in VALID_EXIT_TYPES:
                logger.warning("Invalid take_profit type '%s' for tp_%d, skipping", tp_type, i)
                continue

            tp_close_type = tp.get('close_amount_type', 'percentage')
            tp_close_val = float(tp.get('close_amount_value', 100))

            if tp_type == 'percentage':
                t_price = open_position.entry_price * (1 + (tp_val/100))
                if row_high >= t_price:
                    # A gap above the target fills at the (better) open price
                    tps.append({'id': tp_id, 'price': max(t_price, row_open), 'pct': tp_close_val, 'close_amount_type': tp_close_type})
            elif tp_type == 'trailing':
                # Trailing TP: price must first rise above entry by tp_val%, then
                # we close when price drops tp_val% from the highest price reached.
                activation_price = open_position.entry_price * (1 + (tp_val / 100))
                if prev_highest >= activation_price:
                    # Once activated, trail below the peak
                    t_price = prev_highest * (1 - (tp_val / 100))
                    if row_low <= t_price:
                        tps.append({'id': tp_id, 'price': min(t_price, row_open), 'pct': tp_close_val, 'close_amount_type': tp_close_type})
            elif tp_type == 'atr':
                if not (current_atr > 0): continue
                t_price = prev_highest - (tp_val * current_atr)
                if row_low <= t_price:
                    tps.append({'id': tp_id, 'price': min(t_price, row_open), 'pct': tp_close_val, 'close_amount_type': tp_close_type})
            else:
                t_price = tp_val
                if row_high >= t_price:
                    tps.append({'id': tp_id, 'price': max(t_price, row_open), 'pct': tp_close_val, 'close_amount_type': tp_close_type})

        tps = sorted(tps, key=lambda x: x['price'], reverse=True)

        for tp in tps:
            events.append({
                'qty_pct': tp['pct'],
                'close_amount_type': tp.get('close_amount_type', 'percentage'),
                'reason': "take_profit",
                'price': tp['price'],
                'id': tp['id']
            })

    if not events and is_sell_signal:
        exit_settings = trade_settings.get("exit", {})
        pct_to_close = _num(exit_settings.get('amount_value'), 100) if exit_settings.get('amount_type') == 'percentage' else 100
        events.append({
            'qty_pct': pct_to_close,
            'reason': "strategy",
            'price': row_close,
            'id': 'strategy_sell'
        })

    with states_lock:
        state['highest_price'] = max(state['highest_price'], row_high)
        # Persist state back to DB for crash recovery
        open_position.highest_price = state['highest_price']

    return events
