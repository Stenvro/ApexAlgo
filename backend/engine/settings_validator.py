import logging
import re

from backend.core.exchange_registry import get_exchange_timeframes
from backend.engine.indicator_registry import get_spec

logger = logging.getLogger("apexalgo.settings_validator")
SYMBOL_PATTERN = re.compile(r'^[A-Z0-9]+/[A-Z0-9]+$')
MAX_PRICE_OFFSET = 500
VALID_EXIT_TYPES = {'percentage', 'trailing', 'atr', 'fixed'}
VALID_AMOUNT_TYPES = {'percentage', 'fixed'}
VALID_CLOSE_AMOUNT_TYPES = {'percentage', 'fixed'}
VALID_CONDITION_OPS = {'>', '<', '>=', '<=', '==', '!=', 'cross_above', 'cross_below', 'increasing', 'decreasing', 'increasing_for', 'decreasing_for'}
VALID_LOGIC_OPS = {'and', 'or', 'xor', 'nand', 'nor', 'not'}
VALID_PRICE_TYPES = {'open', 'high', 'low', 'close', 'volume'}
VALID_DRAWDOWN_ACTIONS = {'close_all', 'block_entries'}


def validate_bot_settings(settings: dict, exchange_id: str | None = None) -> dict:
    """Validate bot settings and return errors and warnings.

    Parameters
    ----------
    settings : dict
        Bot settings to validate.
    exchange_id : str, optional
        Resolved exchange ID for timeframe validation. Falls back to
        settings['data_exchange'] or 'okx' if not provided.

    Returns dict with 'errors' (list of blocking issues) and
    'warnings' (list of non-blocking issues).
    """
    errors = []
    warnings = []
    nodes = settings.get("nodes", {})

    # Symbols — normalize and validate BASE/QUOTE format, write back normalized values
    symbols = settings.get("symbols", [])
    if symbols:
        normalized_symbols = []
        for sym in symbols:
            norm = str(sym).strip().upper().replace('-', '/')
            if not SYMBOL_PATTERN.match(norm):
                errors.append(f"Invalid symbol '{sym}'. Use BASE/QUOTE, e.g. BTC/USDC.")
            normalized_symbols.append(norm)
        settings["symbols"] = normalized_symbols
    else:
        if settings.get("symbol"):
            warnings.append("Using single 'symbol' field; consider using 'symbols' list.")
        else:
            errors.append("No trading symbols configured.")
    if settings.get("symbol"):
        norm = str(settings["symbol"]).strip().upper().replace('-', '/')
        if not SYMBOL_PATTERN.match(norm):
            errors.append(f"Invalid symbol '{settings['symbol']}'. Use BASE/QUOTE, e.g. BTC/USDC.")
        settings["symbol"] = norm

    # Timeframe — validated against the exchange's supported timeframes
    tf = settings.get("timeframe")
    if tf:
        eid = exchange_id or settings.get("data_exchange", "okx")
        supported = get_exchange_timeframes(eid)
        if supported and tf not in supported:
            errors.append(f"Exchange '{eid}' does not support timeframe '{tf}'. Supported: {', '.join(sorted(supported.keys()))}")

    # Entry/exit node references
    entry_node = settings.get("entry_node")
    exit_node = settings.get("exit_node")
    if entry_node and entry_node not in nodes:
        errors.append(f"entry_node '{entry_node}' not found in nodes.")
    if exit_node and exit_node not in nodes:
        errors.append(f"exit_node '{exit_node}' not found in nodes.")
    if not entry_node and not exit_node:
        warnings.append("No entry_node or exit_node configured. Bot will not generate signals.")
    if exit_node and not entry_node:
        warnings.append("exit_node is configured without an entry_node; bot will never buy.")

    # Max positions
    max_pos = settings.get("max_positions", 1)
    if isinstance(max_pos, (int, float)) and max_pos < 1:
        errors.append("max_positions must be >= 1.")

    # Drawdown handling (both optional; missing keys keep the legacy behaviour)
    dd_action = settings.get("drawdown_action", "close_all")
    if dd_action not in VALID_DRAWDOWN_ACTIONS:
        errors.append(f"Invalid drawdown_action '{dd_action}'. Use 'close_all' or 'block_entries'.")
    try:
        max_capital_loss = float(settings.get("max_capital_loss") or 0)
        if max_capital_loss < 0 or max_capital_loss >= 100:
            errors.append("max_capital_loss must be between 0 (off) and 100.")
    except (ValueError, TypeError):
        max_capital_loss = 0
        errors.append(f"max_capital_loss '{settings.get('max_capital_loss')}' is not a valid number.")
    try:
        cooldown_days = float(settings.get("drawdown_cooldown_days", 7) or 0)
        if cooldown_days < 0 or cooldown_days > 365:
            errors.append("drawdown_cooldown_days must be between 0 and 365.")
    except (ValueError, TypeError):
        errors.append(f"drawdown_cooldown_days '{settings.get('drawdown_cooldown_days')}' is not a valid number.")
    if dd_action == "block_entries":
        try:
            _dd_limit = float(settings.get("max_drawdown") or 0)
        except (ValueError, TypeError):
            _dd_limit = 0
        if _dd_limit <= 0:
            warnings.append("drawdown_action is 'block_entries' but max_drawdown is 0 — it will never trigger.")
        if settings.get("api_execution") and max_capital_loss <= 0:
            # Blocking entries does not cap losses on positions that are still
            # open, so a live bot needs the principal guard as its hard stop
            errors.append("Live execution with drawdown_action 'block_entries' requires max_capital_loss > 0 as the hard stop.")

    # Validate each node
    for node_id, node in nodes.items():
        node_class = node.get("class")

        if node_class == "indicator":
            method = str(node.get("method", "")).lower()
            spec = get_spec(method) if method else None
            if method and spec is None:
                errors.append(f"Node '{node_id}': indicator method '{method}' is not supported.")
            elif spec is not None:
                params = node.get("params")
                if isinstance(params, dict):
                    known = {p.id for p in spec.params}
                    for pid in params:
                        if pid not in known:
                            warnings.append(f"Node '{node_id}': '{method}' has no parameter '{pid}' (ignored by pandas_ta or falls back to defaults).")
                try:
                    out_idx = int(node.get("output_idx", 0))
                except (ValueError, TypeError):
                    errors.append(f"Node '{node_id}': output_idx '{node.get('output_idx')}' is not a valid integer.")
                else:
                    if out_idx < 0 or out_idx >= len(spec.outputs):
                        errors.append(f"Node '{node_id}': '{method}' has {len(spec.outputs)} output(s); output_idx {out_idx} is out of range.")
                    elif out_idx in spec.disabled_outputs:
                        errors.append(f"Node '{node_id}': '{method}' output '{spec.outputs[out_idx]}' is a look-ahead value and cannot be used.")

        elif node_class == "price_data":
            price_type = node.get("type", "close")
            if price_type not in VALID_PRICE_TYPES:
                errors.append(f"Node '{node_id}': invalid price type '{price_type}'.")
            try:
                offset = int(node.get("offset", 0))
            except (ValueError, TypeError):
                errors.append(f"Node '{node_id}': offset '{node.get('offset')}' is not a valid integer.")
            else:
                if offset < 0:
                    errors.append(f"Node '{node_id}': negative offset ({offset}) would reference future candles (look-ahead).")
                elif offset > MAX_PRICE_OFFSET:
                    errors.append(f"Node '{node_id}': offset {offset} exceeds the maximum of {MAX_PRICE_OFFSET}.")

        elif node_class == "condition":
            op = node.get("operator")
            if op and op not in VALID_CONDITION_OPS:
                errors.append(f"Node '{node_id}': invalid condition operator '{op}'.")
            _validate_operand_ref(node.get("left"), node_id, "left", nodes, warnings)
            if op not in ("increasing", "decreasing"):
                _validate_operand_ref(node.get("right"), node_id, "right", nodes, warnings)

        elif node_class == "logic":
            op = node.get("operator", "and").lower()
            if op not in VALID_LOGIC_OPS:
                errors.append(f"Node '{node_id}': invalid logic operator '{op}'.")
            _validate_operand_ref(node.get("left"), node_id, "left", nodes, warnings)
            if op != "not":
                _validate_operand_ref(node.get("right"), node_id, "right", nodes, warnings)

    # Trade settings
    trade_settings = settings.get("trade_settings", {})
    entry_ts = trade_settings.get("entry", {})

    # Entry amount
    amount_type = entry_ts.get("amount_type", "percentage")
    if amount_type not in VALID_AMOUNT_TYPES:
        errors.append(f"Invalid entry amount_type '{amount_type}'.")
    amount_value = entry_ts.get("amount_value")
    if amount_value is not None:
        try:
            if float(amount_value) <= 0:
                warnings.append("Entry amount_value is <= 0.")
        except (ValueError, TypeError):
            errors.append(f"Entry amount_value '{amount_value}' is not a valid number.")

    # Stop losses
    for i, sl in enumerate(entry_ts.get("stop_losses", [])):
        sl_type = sl.get("type", "")
        if sl_type not in VALID_EXIT_TYPES:
            errors.append(f"Stop loss #{i}: invalid type '{sl_type}'.")
        try:
            if float(sl.get("value", 0)) <= 0:
                warnings.append(f"Stop loss #{i}: value is <= 0.")
        except (ValueError, TypeError):
            errors.append(f"Stop loss #{i}: value is not a valid number.")
        cat = sl.get("close_amount_type", "percentage")
        if cat not in VALID_CLOSE_AMOUNT_TYPES:
            errors.append(f"Stop loss #{i}: invalid close_amount_type '{cat}'.")

    # Take profits
    for i, tp in enumerate(entry_ts.get("take_profits", [])):
        tp_type = tp.get("type", "")
        if tp_type not in VALID_EXIT_TYPES:
            errors.append(f"Take profit #{i}: invalid type '{tp_type}'.")
        try:
            if float(tp.get("value", 0)) <= 0:
                warnings.append(f"Take profit #{i}: value is <= 0.")
        except (ValueError, TypeError):
            errors.append(f"Take profit #{i}: value is not a valid number.")
        cat = tp.get("close_amount_type", "percentage")
        if cat not in VALID_CLOSE_AMOUNT_TYPES:
            errors.append(f"Take profit #{i}: invalid close_amount_type '{cat}'.")

    # Live allocation: share of the exchange wallet this bot may deploy
    if settings.get("live_allocation_pct") not in (None, ""):
        try:
            alloc = float(settings.get("live_allocation_pct"))
            if alloc <= 0 or alloc > 100:
                errors.append("live_allocation_pct must be between 0 (exclusive) and 100.")
        except (ValueError, TypeError):
            errors.append(f"live_allocation_pct '{settings.get('live_allocation_pct')}' is not a valid number.")

    # API execution
    if settings.get("api_execution") and not settings.get("api_key_name"):
        errors.append("api_execution is enabled but no api_key_name specified.")

    if settings.get("api_execution") and settings.get("api_key_name"):
        try:
            max_order_value = float(settings.get("max_order_value") or 0)
        except (ValueError, TypeError):
            max_order_value = 0
        if max_order_value <= 0:
            errors.append("Live execution requires max_order_value > 0 as a safety cap.")

    if settings.get("api_execution"):
        try:
            entry_fee = float(entry_ts.get("fee") or 0)
        except (ValueError, TypeError):
            entry_fee = 0
        if entry_fee <= 0:
            warnings.append("Backtest without fees is optimistic; set your exchange's real fee in trade_settings.entry.fee.")

    # API key reference reminder
    if settings.get("api_key_name"):
        warnings.append(f"Bot references API key '{settings['api_key_name']}'. Verify this key exists and has correct permissions.")

    return {"errors": errors, "warnings": warnings}


def _validate_operand_ref(operand, node_id: str, side: str, nodes: dict, warnings: list):
    """Check that a node operand reference is valid."""
    if operand is None:
        return
    if isinstance(operand, (int, float)):
        return
    if isinstance(operand, str):
        try:
            float(operand)
            return
        except (ValueError, TypeError):
            pass
        if operand not in nodes:
            warnings.append(f"Node '{node_id}': {side} references '{operand}' which is not in nodes.")
