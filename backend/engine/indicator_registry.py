"""Single source of truth for the indicators the strategy builder exposes.

The evaluator dispatches by ``method`` name onto the pandas_ta accessor and
selects output columns positionally (``output_idx``), so the registry only
has to *describe* each indicator — names, parameter ids/defaults, output
order and chart pane — plus mark the few methods that are not a plain
``df.ta.<method>(**params)`` call. The frontend palette is generated from
``GET /api/indicators`` so the two sides cannot drift.

Adding an indicator = one ``IndicatorSpec`` entry here. Do not rename an
existing method, param id or reorder ``outputs``: they are serialized into
bot settings and would silently change how saved bots evaluate.
"""

from dataclasses import dataclass

# Chart pane. "overlay" = on the price chart, "oscillator" = separate pane,
# "volume" = volume pane.
PANES = ("overlay", "oscillator", "volume")

# How the evaluator computes the method. Anything but "ta" is a special case.
SOURCE_TA = "ta"                # getattr(df.ta, method)(**params)
SOURCE_VOLUME = "volume"        # raw df['volume']
SOURCE_VOLUME_SMA = "volume_sma"  # ta.sma(df['volume'], length)
SOURCE_ICHIMOKU = "ichimoku"    # df.ta.ichimoku(...) tuple; chikou blanked


@dataclass(frozen=True)
class Param:
    id: str
    label: str
    default: float


@dataclass(frozen=True)
class IndicatorSpec:
    method: str
    label: str
    category: str
    pane: str
    # Output line labels, in the exact column order pandas_ta returns them.
    # Index into this list == ``output_idx`` in bot settings.
    outputs: tuple[str, ...] = ("Main",)
    params: tuple[Param, ...] = ()
    source: str = SOURCE_TA
    # Output indices that must not be used as condition inputs. Ichimoku's
    # chikou span is the close shifted backwards — a future value at each
    # row — so the evaluator blanks it and the UI greys it out.
    disabled_outputs: tuple[int, ...] = ()

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "label": self.label,
            "category": self.category,
            "pane": self.pane,
            "outputs": list(self.outputs),
            "params": [{"id": p.id, "label": p.label, "default": p.default} for p in self.params],
            "disabled_outputs": list(self.disabled_outputs),
        }


def _len(default: int = 14, label: str = "Length") -> tuple[Param, ...]:
    return (Param("length", label, default),)


TREND = "Trend & Overlap"
MOMENTUM = "Momentum"
VOLATILITY = "Volatility"
VOLUME = "Volume"
STATISTICS = "Statistics"

# Order here is the palette order in the builder.
_SPECS: tuple[IndicatorSpec, ...] = (
    # --- Trend & Overlap -------------------------------------------------
    IndicatorSpec("sma", "SMA (Simple Moving Avg)", TREND, "overlay", params=_len()),
    IndicatorSpec("ema", "EMA (Exponential Moving Avg)", TREND, "overlay", params=_len()),
    IndicatorSpec("wma", "WMA (Weighted Moving Avg)", TREND, "overlay", params=_len()),
    IndicatorSpec("dema", "DEMA (Double EMA)", TREND, "overlay", params=_len()),
    IndicatorSpec("tema", "TEMA (Triple EMA)", TREND, "overlay", params=_len()),
    IndicatorSpec("kama", "KAMA (Kaufman Adaptive MA)", TREND, "overlay",
                  params=(Param("length", "Length", 10), Param("fast", "Fast SC", 2), Param("slow", "Slow SC", 30))),
    IndicatorSpec("linreg", "Linear Regression", TREND, "overlay", params=_len()),
    IndicatorSpec("midpoint", "Midpoint (HL/2)", TREND, "overlay", params=_len()),
    IndicatorSpec("supertrend", "Supertrend", TREND, "overlay",
                  outputs=("Trend", "Direction", "Long", "Short"),
                  params=(Param("length", "ATR Length", 10), Param("multiplier", "Multiplier", 3.0))),
    IndicatorSpec("macd", "MACD", TREND, "oscillator",
                  outputs=("MACD Line", "Histogram", "Signal Line"),
                  params=(Param("fast", "Fast Length", 12), Param("slow", "Slow Length", 26), Param("signal", "Signal Length", 9))),
    IndicatorSpec("adx", "ADX (Average Directional Index)", TREND, "oscillator",
                  outputs=("ADX", "DMP (+DI)", "DMN (-DI)"), params=_len()),
    IndicatorSpec("psar", "Parabolic SAR", TREND, "overlay",
                  outputs=("Long", "Short", "AF", "Reversal"),
                  params=(Param("af0", "AF Step", 0.02), Param("af", "AF Max", 0.2))),
    IndicatorSpec("ichimoku", "Ichimoku Cloud", TREND, "overlay",
                  # pandas_ta column order is ISA, ISB, ITS, IKS, ICS — labels must match positions
                  outputs=("Span A (Senkou A)", "Span B (Senkou B)", "Conversion (Tenkan)", "Base (Kijun)", "Chikou"),
                  params=(Param("tenkan", "Tenkan", 9), Param("kijun", "Kijun", 26), Param("senkou", "Senkou", 52)),
                  source=SOURCE_ICHIMOKU, disabled_outputs=(4,)),
    IndicatorSpec("vortex", "Vortex Indicator", TREND, "oscillator",
                  outputs=("VI+", "VI-"), params=_len()),
    # --- Momentum ----------------------------------------------------------
    IndicatorSpec("rsi", "RSI (Relative Strength Index)", MOMENTUM, "oscillator", params=_len()),
    IndicatorSpec("stoch", "Stochastic Oscillator", MOMENTUM, "oscillator",
                  outputs=("%K", "%D"),
                  params=(Param("k", "%K Length", 14), Param("d", "%D Length", 3), Param("smooth_k", "Smooth %K", 3))),
    IndicatorSpec("stochrsi", "Stochastic RSI", MOMENTUM, "oscillator",
                  outputs=("%K", "%D"),
                  params=(Param("length", "RSI Length", 14), Param("rsi_length", "Stoch Length", 14),
                          Param("k", "%K", 3), Param("d", "%D", 3))),
    IndicatorSpec("cci", "CCI (Commodity Channel Index)", MOMENTUM, "oscillator", params=_len()),
    IndicatorSpec("mfi", "MFI (Money Flow Index)", MOMENTUM, "oscillator", params=_len()),
    IndicatorSpec("willr", "Williams %R", MOMENTUM, "oscillator", params=_len()),
    IndicatorSpec("roc", "ROC (Rate of Change)", MOMENTUM, "oscillator", params=_len(10)),
    IndicatorSpec("mom", "Momentum", MOMENTUM, "oscillator", params=_len(10)),
    IndicatorSpec("tsi", "TSI (True Strength Index)", MOMENTUM, "oscillator",
                  outputs=("TSI", "Signal"),
                  params=(Param("fast", "Fast", 13), Param("slow", "Slow", 25), Param("signal", "Signal", 13))),
    IndicatorSpec("uo", "Ultimate Oscillator", MOMENTUM, "oscillator",
                  params=(Param("fast", "Fast", 7), Param("medium", "Medium", 14), Param("slow", "Slow", 28))),
    IndicatorSpec("ao", "Awesome Oscillator", MOMENTUM, "oscillator",
                  params=(Param("fast", "Fast", 5), Param("slow", "Slow", 34))),
    IndicatorSpec("ppo", "PPO (Percentage Price Osc)", MOMENTUM, "oscillator",
                  outputs=("PPO", "Histogram", "Signal"),
                  params=(Param("fast", "Fast", 12), Param("slow", "Slow", 26), Param("signal", "Signal", 9))),
    IndicatorSpec("fisher", "Fisher Transform", MOMENTUM, "oscillator",
                  outputs=("Fisher", "Signal"), params=_len(9)),
    IndicatorSpec("cmo", "CMO (Chande Momentum)", MOMENTUM, "oscillator", params=_len()),
    # --- Volatility --------------------------------------------------------
    IndicatorSpec("bbands", "Bollinger Bands", VOLATILITY, "overlay",
                  outputs=("Lower Band", "Mid Band", "Upper Band", "Bandwidth", "Percent"),
                  params=(Param("length", "Length", 20), Param("std", "Std Dev", 2.0))),
    IndicatorSpec("atr", "ATR (Average True Range)", VOLATILITY, "oscillator", params=_len()),
    IndicatorSpec("natr", "NATR (Normalized ATR %)", VOLATILITY, "oscillator", params=_len()),
    IndicatorSpec("kc", "Keltner Channels", VOLATILITY, "overlay",
                  outputs=("Lower", "Mid", "Upper"),
                  params=(Param("length", "Length", 20), Param("scalar", "Multiplier", 2.0))),
    IndicatorSpec("donchian", "Donchian Channels", VOLATILITY, "overlay",
                  outputs=("Lower", "Mid", "Upper"),
                  params=(Param("lower_length", "Lower Length", 20), Param("upper_length", "Upper Length", 20))),
    IndicatorSpec("accbands", "Acceleration Bands", VOLATILITY, "overlay",
                  outputs=("Lower", "Mid", "Upper"), params=_len(20)),
    IndicatorSpec("massi", "Mass Index", VOLATILITY, "oscillator",
                  params=(Param("fast", "Fast", 9), Param("slow", "Slow", 25))),
    # --- Volume ------------------------------------------------------------
    IndicatorSpec("volume", "Raw Volume", VOLUME, "volume", source=SOURCE_VOLUME),
    IndicatorSpec("vma", "VMA (Volume Moving Avg)", VOLUME, "volume", params=_len(), source=SOURCE_VOLUME_SMA),
    IndicatorSpec("obv", "On-Balance Volume (OBV)", VOLUME, "volume"),
    IndicatorSpec("vwap", "VWAP", VOLUME, "overlay"),
    IndicatorSpec("cmf", "Chaikin Money Flow", VOLUME, "oscillator", params=_len(20)),
    IndicatorSpec("ad", "Accumulation/Distribution", VOLUME, "volume"),
    IndicatorSpec("adosc", "AD Oscillator (Chaikin)", VOLUME, "oscillator",
                  params=(Param("fast", "Fast", 3), Param("slow", "Slow", 10))),
    IndicatorSpec("eom", "Ease of Movement", VOLUME, "oscillator", params=_len()),
    IndicatorSpec("pvt", "Price Volume Trend", VOLUME, "volume"),
    # --- Statistics --------------------------------------------------------
    IndicatorSpec("variance", "Variance", STATISTICS, "oscillator", params=_len()),
    IndicatorSpec("stdev", "Standard Deviation", STATISTICS, "oscillator", params=_len()),
    IndicatorSpec("zscore", "Z-Score", STATISTICS, "oscillator", params=_len(30)),
    IndicatorSpec("slope", "Slope (Linear Reg)", STATISTICS, "oscillator", params=_len()),
    IndicatorSpec("entropy", "Entropy", STATISTICS, "oscillator", params=_len(10)),
    IndicatorSpec("kurtosis", "Kurtosis", STATISTICS, "oscillator", params=_len(30)),
    IndicatorSpec("skew", "Skewness", STATISTICS, "oscillator", params=_len(30)),
    IndicatorSpec("log_return", "Log Return", STATISTICS, "oscillator", params=_len(1)),
)

REGISTRY: dict[str, IndicatorSpec] = {s.method: s for s in _SPECS}
assert len(REGISTRY) == len(_SPECS), "duplicate indicator method in registry"
assert all(s.pane in PANES for s in _SPECS)

# Kept for callers that only need the allowlist (validator, evaluator).
ALLOWED_INDICATOR_METHODS = frozenset(REGISTRY)


def get_spec(method: str) -> IndicatorSpec | None:
    return REGISTRY.get(str(method).lower())


def registry_payload() -> dict:
    """Shape served by GET /api/indicators."""
    categories: list[str] = []
    for s in _SPECS:
        if s.category not in categories:
            categories.append(s.category)
    return {
        "categories": categories,
        "indicators": [s.to_dict() for s in _SPECS],
    }
