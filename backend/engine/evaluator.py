import logging

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from backend.engine.indicator_registry import (
    SOURCE_ICHIMOKU,
    SOURCE_VOLUME,
    SOURCE_VOLUME_SMA,
    get_spec,
)

logger = logging.getLogger("apexalgo.evaluator")

class NodeEvaluator:
    def __init__(self, settings: dict):
        self.settings = settings
        self.df = pd.DataFrame()
        self.entry_trigger = settings.get("entry_node")
        self._resolve_cache = {}
        self._nan_masks = {}
        self._resolving = set()

    def _calculate_indicators(self):
        """Calculates all indicators on the DataFrame using pandas_ta."""
        nodes = self.settings.get("nodes", {})

        for node_id, node in nodes.items():
            if node.get("class") == "indicator":
                method = str(node.get("method", "rsi")).lower()
                params = node.get("params", {})
                out_idx = int(node.get("output_idx", 0))

                spec = get_spec(method)
                if spec is None:
                    logger.warning("Node '%s': indicator method '%s' is not supported, skipping.", node_id, method)
                    self.df[node_id] = np.nan
                    continue

                if spec.source == SOURCE_VOLUME:
                    self.df[node_id] = self.df['volume'] if 'volume' in self.df.columns else np.nan
                    continue
                elif spec.source == SOURCE_VOLUME_SMA:
                    if 'volume' in self.df.columns:
                        length = params.get('length', 14) if isinstance(params, dict) else 14
                        self.df[node_id] = ta.sma(self.df['volume'], length=length)
                    else:
                        self.df[node_id] = np.nan
                    continue
                elif spec.source == SOURCE_ICHIMOKU:
                    try:
                        p = params if isinstance(params, dict) else {}
                        result = self.df.ta.ichimoku(
                            tenkan=p.get('tenkan', 9),
                            kijun=p.get('kijun', 26),
                            senkou=p.get('senkou', 52)
                        )
                        # ichimoku returns a tuple: (span_df, lookahead_df)
                        ich_df = result[0] if isinstance(result, tuple) else result
                        if isinstance(ich_df, pd.DataFrame) and not ich_df.empty:
                            # Chikou span is the close shifted backwards (a future
                            # value at each row) — blank it so it can't be used
                            # as a look-ahead condition input.
                            ich_df = ich_df.copy()
                            for col in ich_df.columns:
                                if str(col).upper().startswith("ICS"):
                                    ich_df[col] = np.nan
                            if out_idx < len(ich_df.columns):
                                self.df[node_id] = ich_df.iloc[:, out_idx]
                            else:
                                self.df[node_id] = ich_df.iloc[:, 0]
                        else:
                            self.df[node_id] = np.nan
                    except Exception as e:
                        logger.warning("Could not compute ichimoku: %s", e)
                        self.df[node_id] = np.nan
                    continue

                if hasattr(self.df.ta, method):
                    res = None
                    try:
                        if isinstance(params, list) and len(params) > 0:
                            res = getattr(self.df.ta, method)(*params)
                        elif isinstance(params, dict) and len(params) > 0:
                            res = getattr(self.df.ta, method)(**params)
                        elif isinstance(params, (int, float, str)):
                            res = getattr(self.df.ta, method)(params)
                        else:
                            res = getattr(self.df.ta, method)()

                    except Exception as e:
                        logger.warning("Invalid params for '%s' (%s), retrying with defaults. (%s)", method, params, e)
                        try:
                            res = getattr(self.df.ta, method)()
                        except Exception as e2:
                            logger.error("Could not compute indicator '%s': %s", method, e2)
                            continue

                    if res is None:
                        continue

                    if isinstance(res, pd.DataFrame):
                        if not res.empty:
                            if out_idx < len(res.columns):
                                self.df[node_id] = res.iloc[:, out_idx]
                            else:
                                logger.warning("Indicator '%s' output_idx %d exceeds columns (%d), using column 0", method, out_idx, len(res.columns))
                                self.df[node_id] = res.iloc[:, 0]
                    else:
                        self.df[node_id] = res

        try:
            if 'high' in self.df and 'low' in self.df and 'close' in self.df:
                h = pd.to_numeric(self.df['high'], errors='coerce')
                l = pd.to_numeric(self.df['low'], errors='coerce')
                c = pd.to_numeric(self.df['close'], errors='coerce')
                self.df['atr'] = ta.atr(h, l, c, length=14)
            else:
                self.df['atr'] = np.nan
        except Exception:
            self.df['atr'] = np.nan

    def resolve_node(self, node_id: str) -> pd.Series:
        if not node_id:
            return pd.Series(False, index=self.df.index)

        if node_id in self._resolve_cache:
            return self._resolve_cache[node_id]

        # A cyclic graph (only reachable via an unvalidated import) must not recurse forever
        if node_id in self._resolving:
            logger.warning("resolve_node: cycle detected at node '%s'; treating as False", node_id)
            return pd.Series(False, index=self.df.index)
        self._resolving.add(node_id)
        try:
            return self._resolve_node_inner(node_id)
        finally:
            self._resolving.discard(node_id)

    def _resolve_node_inner(self, node_id: str) -> pd.Series:
        nodes = self.settings.get("nodes", {})
        node = nodes.get(node_id)
        if not node:
            logger.warning("resolve_node: node '%s' not found in settings", node_id)
            return pd.Series(False, index=self.df.index)

        node_class = node.get("class")
        result = None
        nan_mask = None

        if node_class == "indicator":
            result = self.df[node_id] if node_id in self.df.columns else pd.Series(np.nan, index=self.df.index)
            nan_mask = result.isna()

        elif node_class == "price_data":
            price_type = node.get("type", "close").lower()
            offset = int(node.get("offset", 0))
            result = self.df[price_type].shift(offset) if price_type in self.df.columns else pd.Series(np.nan, index=self.df.index)
            nan_mask = result.isna()

        elif node_class == "condition":
            left_s = self.resolve_operand(node.get("left"))
            nan_mask = left_s.isna()
            op = node.get("operator")

            if op == "increasing":
                result = left_s > left_s.shift(1)
            elif op == "decreasing":
                result = left_s < left_s.shift(1)
            elif op == "increasing_for":
                n = self._streak_length(node.get("right"))
                inc = (left_s > left_s.shift(1)).astype(int)
                result = (inc.rolling(window=n, min_periods=n).sum() == n)
            elif op == "decreasing_for":
                n = self._streak_length(node.get("right"))
                dec = (left_s < left_s.shift(1)).astype(int)
                result = (dec.rolling(window=n, min_periods=n).sum() == n)
            else:
                right_s = self.resolve_operand(node.get("right"))
                nan_mask = nan_mask | right_s.isna()

                if op == "cross_above":
                    result = (left_s.shift(1) <= right_s.shift(1)) & (left_s > right_s)
                elif op == "cross_below":
                    result = (left_s.shift(1) >= right_s.shift(1)) & (left_s < right_s)
                elif op == ">": result = left_s > right_s
                elif op == "<": result = left_s < right_s
                elif op == ">=": result = left_s >= right_s
                elif op == "<=": result = left_s <= right_s
                elif op == "==": result = left_s == right_s
                elif op == "!=": result = left_s != right_s

            if result is None:
                result = pd.Series(False, index=self.df.index)
            else:
                # Warm-up NaN in either operand means the condition is unknown, not true
                result = result.fillna(False).astype(bool) & ~nan_mask

        elif node_class == "logic":
            # Cast to bool explicitly: a child node may return a numeric series instead of a boolean one
            left_resolved = self.resolve_node(node.get("left"))
            right_resolved = self.resolve_node(node.get("right")) if node.get("right") else pd.Series(False, index=self.df.index)

            # Fill NaN with False before boolean conversion to avoid propagating NaN as True
            left_s = left_resolved.fillna(False).astype(bool) if isinstance(left_resolved, pd.Series) else pd.Series(bool(left_resolved), index=self.df.index)
            right_s = right_resolved.fillna(False).astype(bool) if isinstance(right_resolved, pd.Series) else pd.Series(bool(right_resolved), index=self.df.index)

            # Propagate the NaN mask from child nodes so inverting gates
            # (not/nand/nor) can't turn warm-up NaN into a True signal
            false_mask = pd.Series(False, index=self.df.index)
            left_mask = self._nan_masks.get(node.get("left"), false_mask)
            right_mask = self._nan_masks.get(node.get("right"), false_mask) if node.get("right") else false_mask
            nan_mask = left_mask | right_mask

            op = node.get("operator", "and").lower()

            if op == "and": result = left_s & right_s
            elif op == "or": result = left_s | right_s
            elif op == "xor": result = left_s ^ right_s
            elif op == "nand": result = ~(left_s & right_s)
            elif op == "nor": result = ~(left_s | right_s)
            elif op == "not": result = ~left_s
            else: result = pd.Series(False, index=self.df.index)

            result = result & ~nan_mask

        if result is None:
            result = pd.Series(np.nan, index=self.df.index)

        if nan_mask is not None:
            self._nan_masks[node_id] = nan_mask
        self._resolve_cache[node_id] = result
        return result

    @staticmethod
    def _streak_length(operand) -> int:
        """Window for increasing_for/decreasing_for: a numeric literal, clamped
        to >= 1. A series (node ref) is not a length — fall back to 2 rather than
        peeking at its last value (which would be a look-ahead)."""
        if operand is None or isinstance(operand, bool):
            return 2
        try:
            n = int(float(operand))
        except (ValueError, TypeError):
            return 2
        return max(1, n)

    def resolve_operand(self, operand) -> pd.Series:
        if isinstance(operand, (int, float)):
            return pd.Series(float(operand), index=self.df.index)
        if isinstance(operand, str):
            if operand in self.df.columns:
                return self.df[operand]
            return self.resolve_node(operand)
        try:
            return pd.Series(float(operand), index=self.df.index)
        except (ValueError, TypeError):
            return pd.Series(np.nan, index=self.df.index)
