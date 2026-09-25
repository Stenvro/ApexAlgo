# ApexAlgo Strategy Builder — AI Prompt Context

> Paste this file into any AI assistant (or point it at the raw GitHub URL) and ask for a strategy. Everything it produces will then be **directly importable** into ApexAlgo (Bot Manager → Import) and, more importantly, will be designed around how the engine actually trades — not around indicator folklore.

**If you are the AI reading this:** you are designing a real trading system that will run on real money. Sections 1–3 tell you how the engine executes; section 4 is the design playbook you must follow; section 5 is the exact file format; section 6 has four vetted templates to start from (three long-only spot, one long/short perpetual). Work through the checklist at the end of section 4 before you output anything.

---

## 1. What ApexAlgo is — and how it executes

ApexAlgo is a no-code trading bot for **spot** markets and **perpetual swaps** (leverage 1–10×) — linear contracts settled in a stablecoin (`BTC/USDT:USDT`) or inverse, coin-margined contracts settled in the base coin (`BTC/USD:BTC`). Every bot keeps its books in one **cash currency**: the quote of its spot pairs (USDT, USDC, EUR, …) or the settle currency of its perpetuals; all pairs of a bot must share it, and `backtest_capital`, `fixed` sizes, PnL and the guards are all in that currency. The default and the vetted templates are long-only spot; on a perpetual the graph may also short. A strategy is a graph of nodes:

```
Indicator / Price Data ──> Condition ──> Logic Gate (optional) ──> Action (BUY / SELL, and on perps SHORT / COVER)
                                                                      └──> Take Profit / Stop Loss nodes (attached to the BUY or SHORT action)
```

The engine evaluates the whole graph once per **closed candle**, per pair. Knowing exactly what happens on that tick is what separates strategies that look good from strategies that make money:

| Mechanic | What the engine does |
|---|---|
| **Evaluation moment** | On the close of each candle. `offset: 0` on a price node means *the candle that just closed*, not a still-forming candle. |
| **Entry fill** | At the close price of the signal candle, plus `slippage` %. Fee is charged on the notional. |
| **Direction** | `BUY` opens a long, `SELL` closes it. On a perpetual market (`market_type: swap`) `SHORT` opens a short and `COVER` closes it; long and short never coexist on a pair (the conflicting signal is ignored, BUY wins when both fire). Spot bots cannot short (validator error). Leverage only on swaps (`leverage`, default 1). |
| **Positions per pair** | **Pyramiding up to `max_positions`.** A BUY signal while a position is already open on that pair opens another layer until the limit is reached (`per_pair` = per symbol, `global` = across all pairs of the bot). Each layer has its own SL/TP levels (anchored to its own entry) and is exited independently; a strategy **SELL signal flattens every open layer on that pair**. A layer opened on a candle is not exit-checked until the next candle. `max_positions: 1` gives the classic one-position-per-pair behaviour. Backtest and live apply the same rule. |
| **Re-entry** | The moment a position closes, the next candle whose entry condition is true opens a new one. A *state* condition (e.g. `RSI < 30`) that stays true for 10 candles will therefore re-enter immediately after every exit — see §4.2. |
| **Exit checks** | Every candle while a position is open, in this order: **stop-losses** (against the candle low) → **take-profits** (against high/low) → **strategy SELL signal** (at close). Only one group fires per candle; a hit stop-loss suppresses take-profits on the same candle. For a short every level is mirrored: the stop sits above the entry and is hit by the high, the target below and is hit by the low, the strategy exit is the COVER signal. |
| **Trailing anchor** | Trailing levels use the highest high (lowest low for a short) reached **before** the current candle, so one candle cannot both raise the trail and trigger it against its own low. |
| **Leverage (swaps)** | An entry locks `notional / leverage` plus the entry fee on the notional; PnL is on the full notional. `profit_pct` is the return on that locked capital (margin + entry fee) — at 5× a 2% price move is a ~10% `profit_pct`. Linear contracts: PnL = `±(exit − entry) × amount`. Inverse contracts (`BTC/USD:BTC`): `amount` is contracts of `contract_size` USD, PnL = `±contracts × contract_size × (1/entry − 1/exit)` in the base coin. Funding payments are **not** modelled. |
| **Liquidation (swaps)** | Linear: a long is liquidated when the candle low reaches `entry × (1 − (1 − 0.5%) / leverage)`, a short when the high reaches `entry × (1 + (1 − 0.5%) / leverage)`; a 1× long never liquidates, a 1× short liquidates at +99.5%. Inverse: `entry × lev / (lev + 1 − 0.5%)` (long) and `entry × lev / (lev − 1 + 0.5%)` (short). Order on a candle that reaches the level: SL/TP are evaluated **first**; a stop that fills before the level (e.g. at the open) is a normal exit. Only when no exit fills, or the fill would be at or beyond the liquidation price, is the layer liquidated — the margin and the entry fee are lost, no exit fee. Same rule in backtest, forward test and live. |
| **Gaps** | If a candle opens beyond the trigger, the fill is at the open (worse for stops, better for targets). |
| **Partial exits** | `close_amount_value` is a % of the *original* position. Each TP/SL tier fires once. A 100% tier closes whatever is left. |
| **Sizing** | `amount_type: percentage` = % of the **free cash remaining** in the shared pool (all pairs of the bot share one pool; free = not locked in open positions, *not* mark-to-market equity). Sizes compound: with 50% on two pairs the first entry takes 50% of the pool, the second 50% of what is left (25%), so the bot is never fully invested by percentage sizing alone. Entries are clamped so margin + entry fee fit the pool and to `max_order_value` (quote notional). `fixed` = a fixed amount in the bot's cash currency. |
| **Cooldown** | `cooldown_trades` new entries per `cooldown_candles` candles, counted **per symbol** and in candles (the window is the last N stored candles of that pair, in the backtest and live alike). |
| **Warm-up** | Any indicator that is still NaN makes its condition *false*, never true. A 200-EMA eats the first 200 candles of the lookback. |
| **Backtest ≙ live** | Paper/live trading runs the same node graph and the same exit rules on the same closed candles. What differs is fills (real order book) and sizing (see `live_allocation_pct`). |
| **Guards** | `max_drawdown` (peak-to-trough on mark-to-market equity) and `max_capital_loss` (loss of starting capital) stop or wind down the bot — in the backtest *and* live. A backtest that breaches the guard prevents the bot from going live. |

---

## 2. Node reference

### 2.1 Configuration nodes (one of each)

**Main Configuration** — name, timeframe (must be supported by the exchange, §5.3), `max_positions` + scope, cooldown, `max_drawdown`, `max_capital_loss`, `drawdown_action`, `drawdown_cooldown_days`, `max_order_value`, `live_allocation_pct`, execution mode.
**Asset Whitelist** — pairs as `BASE/QUOTE`, e.g. `BTC/USDC, ETH/USDC` (perpetuals as `BASE/QUOTE:SETTLE`). All pairs share one capital pool and must settle in the same cash currency (`BTC/USDT` + `ETH/BTC` is rejected).
**Backtest Engine** — run on start, start capital, lookback (candles).
**Exchange Routing** — with an API key: exchange + sandbox/live derived from the key. Without: pick a *data exchange*; the bot forward-tests locally (no orders). Supported: OKX, Binance, Bitvavo, Coinbase, Crypto.com, Kraken, KuCoin.

### 2.2 Indicators

`method` is the key, `params` must use **exactly these IDs** (they are passed straight to pandas-ta; a wrong name silently falls back to defaults), `output_idx` selects the line.

The authoritative list lives in `backend/engine/indicator_registry.py` and is served at `GET /api/indicators` (same names, param ids, defaults and output order as below). The validator rejects unknown methods, out-of-range `output_idx` and disabled outputs at import time; unknown param ids only produce a warning.

**Trend / overlap**

| Key | Params (default) | output_idx → line |
|---|---|---|
| `sma` `ema` `wma` `dema` `tema` `linreg` `midpoint` | `length` (14) | 0 |
| `kama` | `length` (10), `fast` (2), `slow` (30) | 0 |
| `supertrend` | `length` (10), `multiplier` (3.0) | 0 trend line, **1 direction (+1 up / −1 down)**, 2 long band, 3 short band |
| `macd` | `fast` (12), `slow` (26), `signal` (9) | 0 MACD line, 1 histogram, 2 signal |
| `adx` | `length` (14) | 0 ADX, 1 +DI, 2 −DI |
| `psar` | `af0` (0.02), `af` (0.2) | 0 long, 1 short, 2 AF, 3 reversal |
| `ichimoku` | `tenkan` (9), `kijun` (26), `senkou` (52) | 0 span A, 1 span B, 2 tenkan, 3 kijun. **4 (chikou) is disabled — look-ahead.** |
| `vortex` | `length` (14) | 0 VI+, 1 VI− (oscillator around 1.0; VI+ crossing above VI− = trend turning up) |

**Momentum (oscillators)**

| Key | Params (default) | output_idx → line | Typical range |
|---|---|---|---|
| `rsi` | `length` (14) | 0 | 0–100; 30/70 |
| `stoch` | `k` (14), `d` (3), `smooth_k` (3) | 0 %K, 1 %D | 0–100; 20/80 |
| `stochrsi` | `length` (14), `rsi_length` (14), `k` (3), `d` (3) | 0 %K, 1 %D | 0–100 |
| `cci` | `length` (14) | 0 | ±100 |
| `mfi` | `length` (14) | 0 | 0–100; 20/80 |
| `willr` | `length` (14) | 0 | −100–0; −80/−20 |
| `roc` `mom` | `length` (10) | 0 | around 0 |
| `tsi` | `fast` (13), `slow` (25), `signal` (13) | 0 TSI, 1 signal | ±25 |
| `uo` | `fast` (7), `medium` (14), `slow` (28) | 0 | 0–100 |
| `ao` | `fast` (5), `slow` (34) | 0 | around 0 |
| `ppo` | `fast` (12), `slow` (26), `signal` (9) | 0 PPO, 1 histogram, 2 signal | % around 0 |
| `fisher` | `length` (9) | 0 fisher, 1 signal | ±2 |
| `cmo` | `length` (14) | 0 | ±50 |

**Volatility**

| Key | Params (default) | output_idx → line |
|---|---|---|
| `bbands` | `length` (20), `std` (2.0) | 0 lower, 1 middle, 2 upper, 3 bandwidth, 4 percent-b (0 = at lower band, 1 = at upper) |
| `atr` | `length` (14) | 0 (in price units) |
| `natr` | `length` (14) | 0 (ATR as % of price — use this to reason about stop distances) |
| `kc` | `length` (20), `scalar` (2.0) | 0 lower, 1 basis, 2 upper |
| `donchian` | `lower_length` (20), `upper_length` (20) | 0 lower, 1 middle, 2 upper |
| `accbands` | `length` (20) | 0 lower, 1 middle, 2 upper |
| `massi` | `fast` (9), `slow` (25) | 0 |

**Volume** — `volume` (raw), `vma` (`length` 14), `obv`, `vwap`, `cmf` (`length` 20, ±0.1), `ad`, `adosc` (`fast` 3, `slow` 10), `eom` (`length` 14), `pvt`. All output_idx 0.

**Statistics** — `variance` `stdev` `slope` (`length` 14), `zscore` (`length` 30), `entropy` (`length` 10), `kurtosis` `skew` (`length` 30), `log_return` (`length` 1). All output_idx 0.

The backend enforces this allowlist — never invent a method name.

### 2.3 Price data node

`type`: `open` `high` `low` `close` `volume`; `offset`: candles back (0 = just-closed candle, 1 = the one before). Negative offsets are rejected (look-ahead). Use offsets to express "closes above yesterday's high": `close(0) > high(1)`.

### 2.4 Condition node

`left` is a node id; `right` is a node id **or a static number**.

| Operator | True when | Type |
|---|---|---|
| `>` `<` `>=` `<=` | comparison holds on this candle | **state** (stays true for many candles) |
| `==` `!=` | exact equality — only sensible against integer-valued lines such as `supertrend` direction (`== 1`) | state |
| `cross_above` | left was ≤ right on the previous candle and is > right now | **event** (true for one candle) |
| `cross_below` | left was ≥ right and is now < right | event |
| `increasing` / `decreasing` | left rose / fell versus the previous candle (no `right`) | state |
| `increasing_for` / `decreasing_for` | left rose / fell on each of the last N candles; N is the static number in `right` | state |

### 2.5 Logic gate node

`and` `or` `xor` `nand` `nor` (two inputs), `not` (only `left`). Gates chain freely: `entry_gate = and(trend_ok, and(trigger, volume_ok))`. Warm-up NaN never turns into `true`, even through `not`.

### 2.6 Action node and risk nodes

BUY action: order type, sizing, fee, slippage, and the attached TP/SL tiers. SELL action: closes the position (partially with `amount_value < 100`). On a perpetual market the action's direction can also be SHORT (opens a short; same fields and TP/SL ports as BUY, stored as `trade_settings.short`) or COVER (closes the short, `trade_settings.cover`). The table below is written for a long; for a short every level is mirrored (stop above entry hit by the high, target below hit by the low, trailing anchored to the lowest low).

| TP/SL `type` | Stop-loss meaning | Take-profit meaning |
|---|---|---|
| `percentage` | `value` % below **entry** | `value` % above **entry** |
| `trailing` | `value` % below the **highest high** since entry | activates once price is `value` % above entry, then closes when price drops `value` % from the peak |
| `atr` | peak − `value` × ATR(14) (ATR length is fixed at 14 — this is a volatility trailing stop) | same formula (rarely useful as a TP) |
| `fixed` | absolute price | absolute price |

Tiers: multiple TP/SL entries on one BUY action, each with `close_amount_value` in %. Example scale-out: TP 4% close 50%, TP 8% close 100%, trailing SL 3% close 100%.

---

## 3. Wiring patterns

```
Regime filter:   [close] ─┐                       (state)
                          ├─> [close > ema200] ──┐
                 [ema200] ─┘                     ├─> [AND] ──> [BUY] ──> [SL trailing 3%]
Trigger:         [ema20] ──> [close cross_above ema20] ┘               ──> [TP 6% close 50%]
Exit signal:     [ema20] ──> [close cross_below ema20] ──> [SELL 100%]
```

A strategy therefore has up to four parts — **regime filter** (state), **trigger** (event), **exit plan** (SELL signal and/or TP/SL) and **risk caps** (drawdown/capital-loss guards, cooldown). Every good strategy has at least a trigger, a stop-loss and a guard.

---

## 4. Design playbook — read this before designing

### 4.1 Start from costs, not from indicators

Every round trip costs `2 × fee + 2 × slippage`. With realistic spot fees (0.1% fee, 0.05% slippage) that is **0.3% per trade**; on Coinbase/Kraken retail tiers (0.25–0.4%) it is 0.6–0.9%. A strategy whose average winner is 1% and whose win rate is 55% is a loser after costs.

Rules of thumb:
- Set `fee` to the user's real exchange fee (Binance/OKX/KuCoin ~0.1, Bitvavo 0.15–0.25, Coinbase/Kraken 0.25–0.6). Never leave fee at 0 "to see the raw edge" — the validator warns about this for a reason.
- The average expected profit per trade must be **at least 3–5× the round-trip cost**. That immediately rules out 1m/5m strategies with tight targets for retail accounts.
- Prefer **fewer, larger moves**: 1h–1d timeframes, targets measured in multiples of ATR, trailing exits that let trends run.

### 4.2 Triggers must be events, filters must be states

The most common way generated strategies fail: the entry is a *state* (`RSI < 30`, `close > EMA`, `ADX > 25`) instead of an *event*. Because the engine re-enters the candle after every exit, a state entry produces a burst of back-to-back losing trades whenever the state persists — each paying full costs.

- **Trigger (event):** `cross_above` / `cross_below`, or `increasing` on a rolling extreme (`donchian` upper `increasing` = a new 20-candle high was set on this candle). Indicators have no `offset`, and a channel *includes* the current candle, so `close > donchian_upper` can never be true — use `increasing` for breakouts.
- **Filter (state):** `close > ema200`, `adx > 20`, `supertrend direction == 1`, `natr < 6`.
- Combine: `AND(filter, trigger)`. Never `AND(state, state)` as an entry unless you add a cooldown (`cooldown_trades: 1, cooldown_candles: N`) and understand the churn.
- If the user insists on an oversold entry, use `rsi cross_above 30` (turning back up), never `rsi < 30` (still falling — catching knives) — and tell them this family tested negative (§4.3/§4.9).

### 4.3 Match the strategy to the regime and filter for it

Crypto trends hard and then chops for months; a single logic works in one regime and bleeds in the other. Pick one and **filter out the other**:

| Strategy family | Works in | Filter | Exit | Measured (§4.9) |
|---|---|---|---|---|
| **Trend following** — Supertrend flip, EMA cross, Donchian channel breakout, on **1d or 4h** | multi-week trends | usually none needed: the opposite signal is the filter | opposite signal as SELL **plus** a wide trailing SL (12–15% on 1d, ATR 3× on 4h) as disaster stop | **+15 to +38%** with 24–32% DD across all parameter neighbours — the only family that was robustly profitable |
| Pullback in trend — close crosses back above EMA20 while > EMA200 + ADX, RSI cross_above 30–35 in uptrend | trends with rhythm | `close > ema200`, `adx > 20` | ATR trailing + partial TP | **−11 to −20%** on 4h: stops hit 3× as often as targets. Not recommended without a proven edge |
| Mean reversion — Bollinger re-entry, %b, RSI cross_above 30, in `adx < 25` | ranges | `adx < 25`, RSI not collapsing | fixed TP at mean/upper band + hard SL | **−16 to −19%** on 1h and 4h, every variant. Long-only spot with 0.3–0.6% round-trip cost cannot afford the stop-outs. Only build this if the user insists, and say it tested negative |
| Momentum — MACD histogram cross 0 with EMA200 filter | early trends | `close > ema200` | opposite cross + trailing | **−22%** on 1d: too many false turns in chop |

Default to **trend following on 1d, with 4h as the "more active" option**. Don't mix families in one graph ("RSI oversold AND breakout") — the conditions rarely coincide and the bot does nothing, or the filter cancels the edge. Adding an `ema200` filter to a trend follower *reduced* results in our tests (it delays entries after every bear market) — the opposite-signal exit already keeps the bot out of downtrends.

### 4.4 Size stops from volatility, not from round numbers

A 2% stop on BTC 1h is inside the noise (1h NATR is ~0.5–1%, so a 2× ATR stop is 1–2%; on 4h it is 2–4%, on 1d 5–9%). A 2% stop on a **1d** chart is guaranteed to be hit by noise; a 10% stop on **15m** never triggers and the trade dies by a thousand cuts.

| Timeframe | Typical BTC/ETH NATR(14) | Reasonable SL | Reasonable first TP |
|---|---|---|---|
| 15m | 0.3–0.6% | 1–1.5% or ATR 2× | 1.5–3% (costs make this marginal) |
| 1h | 0.5–1.2% | 1.5–3% or ATR 2–2.5× | 3–6% |
| 4h | 1.2–2.5% | 3–6% or ATR 2.5–3× | 6–12% |
| 1d | 2.5–5% | 6–12% or ATR 3× | 12–25% or trailing only |

Altcoins run 1.5–3× these numbers — widen stops or drop them from the whitelist. Prefer the `atr` stop type for multi-pair bots: it self-adjusts per pair.

Aim for **reward:risk ≥ 1.5** on fixed targets, and let trailing stops handle the fat tail in trend systems. If a stop and a target are both fixed and the target is smaller than the stop, the win rate must exceed 60% after costs — very few crypto signals deliver that.

### 4.5 Keep it simple; more indicators = more overfit

- 1 filter + 1 trigger + 1 exit rule beats 5 indicators. Every extra condition halves the trade count and doubles the chance the backtest is fitting noise.
- Default parameters (14, 20, 50, 200) are fine. Do not "optimize" to 13/27/183 — that is curve fitting.
- Use the same parameters across all whitelisted pairs. If it only works on one pair, it does not work.
- Do not stack multiple oscillators (RSI + Stoch + CCI) — they measure the same thing.

### 4.6 Position sizing and portfolio guards

- **`amount_value` 25–50%** per trade for 2–4 pairs, so the pool can hold several positions; 100% on a single pair is acceptable for a pure trend follower with a trailing stop.
- Always set **`max_drawdown`** with **`drawdown_action: "block_entries"`** and **`max_capital_loss` 30–40**. Size the drawdown limit to the strategy: daily trend following on BTC/ETH at 50% sizing (≈ 75% invested when both are open — the second entry is 50% of the remaining cash) runs 25–32% drawdowns in normal bear phases (buy & hold ran 59% in the same window), so set **30–35** there; 15–20 is right only for low-exposure or short-holding strategies. A limit below the strategy's natural drawdown makes the backtest pause entries for weeks (`entries_blocked_days` in the summary shows this) and the live bot will do the same.
- `close_all` is right only for strategies whose exits are *not* trend-following.
- Add a light **cooldown** (e.g. `cooldown_trades: 1, cooldown_candles: 3–6`) to trend systems to stop whipsaw re-entries around a flat moving average.
- Multi-pair: 2–3 liquid majors (`BTC`, `ETH`, optionally `SOL`). Correlation is high, so treat them as one bet when choosing `amount_value`. Adding SOL to the daily templates lowered the return in our tests; more pairs ≠ more diversification here.

### 4.7 Backtest hygiene — how to read the result

- `backtest_lookback` must cover **several regimes** — at least one bull and one bear phase: ≥ 4 000 candles on 1h (≈ 6 months, still thin), ≥ 4 000–6 000 on 4h (≈ 2–3 years), ≥ 700–1 000 on 1d (≈ 2–3 years). A 2 000-candle 4h window is a single regime and proves nothing. Fewer than ~20 closed trades means the statistics are noise.
- Check the exchange actually has that much history (OKX/Crypto.com USDC pairs may be young; Kraken serves only the last 720 candles). The backend logs a warning when less is available.
- Judge on **profit factor > 1.3, max drawdown < ~25%, and return vs. buy & hold** on the same window. A 40% return with 45% drawdown during a bull run is worse than holding.
- The equity curve should rise across the whole window, not in one lucky trade. If one trade makes half the profit, the strategy is a lottery ticket.
- The first run should be **forward test / sandbox**. Only after weeks of paper results that match the backtest should the user consider live.

### 4.8 Common mistakes the validator will *not* catch (but the market will)

| Mistake | Why it hurts | Fix |
|---|---|---|
| State entry (`rsi < 30`) | re-enters every candle after each exit | `rsi cross_above 30` |
| `AND` of two rarely-coinciding states | 0–5 trades in a year | one filter + one event |
| TP 1–2% on 1h with 0.25% fees | costs eat the edge | TP ≥ 3–5× round trip, or trailing |
| 2% stop on 1d, 10% stop on 15m | noise stops / no protection | size from NATR (§4.4) |
| Trailing stop on a mean-reversion trade | gives back the bounce | fixed TP at the mean + hard SL |
| No stop at all, only a SELL signal | one crash = account | always a SL tier |
| `ema200` with `backtest_lookback: 300` | 200 candles warm-up, 100 candles tested | lookback ≥ 10× longest length |
| `==` on a float line (`rsi == 30`) | never exactly equal | `cross_above` / `>=` |
| `increasing_for` without a number in `right` | defaults to 2, probably not what you meant | put N (e.g. `3`) in `right` |
| `ichimoku` output 4 (chikou) | look-ahead — disabled, always false | use 0–3 |
| Mixing cash currencies (`BTC/USDT` + `ETH/BTC`) | one pool cannot hold two currencies — the validator rejects it | one quote (spot) / settle (swap) currency per bot |
| `4h` on Coinbase, `3m` on Kraken | unsupported timeframe → import error | see §5.3 |
| `max_drawdown: 0` | nothing stops a broken bot | 15–30 + `block_entries` + `max_capital_loss` |

### 4.9 What we measured (Binance USDC pairs, fee 0.1%, slippage 0.05%, $1 000, run in the real engine, Sept 2026)

Windows: 1d = 1 000 candles (Dec 2023 → Sep 2026, includes the 2024–25 bull run and the 2026 drawdown); 4h = 6 000 candles (same period); "4h short" = 2 000 candles (Oct 2025 → Sep 2026, bear phase: BTC −31%, ETH −35%, SOL −45%). Buy & hold 50/50 BTC/ETH over the 1d window: **+49.5% with a 58.6% max drawdown**.

**Re-verified on engine v2.0.0 (17 Sep 2026)** — the three shipped templates in `examples/`, run exactly as shipped with `scripts/verify_examples.py` (window 22 Dec 2023 → 16 Sep 2026). Trade count, win rate and max drawdown reproduce the rows below; the return differs by a few points only because the window end (and therefore the price the last open position is flattened at) has moved. With `--one-per-pair` (the pre-v2 behaviour) Donchian gives 21 trades / 52% / +17.5% / DD 23.7% — pyramiding adds one layer on repeated breakouts; the two cross-based templates never fire a BUY while in a position, so pyramiding does not change them.

| Template (as shipped) | TF | Trades | Win % | Return | Max DD | Buy & hold |
|---|---|---|---|---|---|---|
| `Supertrend_Trend_1d` (BTC+ETH, 50%, `max_positions` 2 global) | 1d | 26 | 42 | +26.0% | 32.0% | BTC +73%, ETH +4% |
| `Donchian_Breakout_1d` (BTC+ETH, 50%, `max_positions` 2 global) | 1d | 20 | 60 | +19.9% | 26.3% | BTC +73%, ETH +4% |
| `EMA_Cross_4h` (BTC+ETH+SOL, 33%, `max_positions` 3 global) | 4h | 153 | 37 | +14.2% | 26.3% | BTC +75%, ETH +7%, SOL −5% |
| `Supertrend_LongShort_Perp_1d` (BTC+ETH USDT perps, 30%, 1×, `max_positions` 2 global) — measured 25 Sep 2026, window 30 Dec 2023 → 24 Sep 2026, fee 0.05% | 1d | 49 (26 long / 23 short) | 37 | +50.8% | 29.1% | BTC +100%, ETH +17% |

**Long/short on perpetuals (measured 25 Sep 2026, Binance USDT-settled perps, taker fee 0.05%, window 30 Dec 2023 → 24 Sep 2026):** the same Supertrend(10, 3) flip, but *short while the direction is down* instead of flat. Entries are the trend **state** (`st_dir > 0` / `< 0`), exits the flip: a long and a short never coexist on a pair, so on the flip candle the old side is closed and the new side opens on the next candle from the state signal; with the flip as the only exit the state re-enters exactly once per trend. A 20% disaster stop replaces the 15% trail — a trailing stop on the state entry re-enters after every stop-out and turned the 1× result into −0.3% at 89 trades.

| Variant (BTC+ETH, `max_positions` 2 global) | Lev | Trades | Win % | Return | Max DD |
|---|---|---|---|---|---|
| Supertrend(10, 3) state long/short, SL 20%, 30% size | 1× | 49 | 37 | **+50.8%** | 29.1% |
| same, 40% size | 1× | 49 | 35 | +52.7% | 31.2% |
| same, 50% size | 1× | 48 | 35 | +59.2% | 35.7% |
| same, BTC+ETH+SOL at 33% | 1× | 78 | 33 | +38.5% | 32.5% |
| Neighbours (10, 3.5) / (12, 3) / (14, 3) / (7, 3) / (10, 2.5), 40% | 1× | 42–75 | 30–38 | +33 / +22 / +46 / +14 / +12% | 31–34% |
| Supertrend(14, 4) state long/short | 1× | 34 | 41 | +11.5% | 36.2% |
| Supertrend(10, 3) state long/short, 40% | **2×** | 46 | 37 | +180% | 35.7% |
| every neighbour above at 2× — (10, 2.5), (10, 3.5), (12, 3), (14, 4) | 2× | 10–18 | 14–22 | **−30 to −39%** (stopped by `max_capital_loss`) | 33–46% |
| EMA 21/55 state long/short, ATR 3× stop, 4h and 1d | 1–2× | 16–182 | 10–34 | −33 to −41% (1d 21/55 at 1× the only positive: +8.9%) | 32–38% |
| Supertrend(10, 3) state long/short on 4h, SL 15% | 2× | 43 | 23 | −35% | 31.8% |

Read the 2× rows correctly: the shipped parameters happen to survive 2× leverage and every neighbour blows through the capital-loss stop — that is path luck, not an edge, which is why the template ships at **1×**. Leverage doubles the trade-level swings, and a 35% max-drawdown guard sized for 1× stops the bot before the good trend arrives. The shorts do not add return in this window (the 1d bull run rewards the long side; a long-only Supertrend gives +26% at 50% size) — they cut the time flat and the 2026 drawdown, which is what makes the smaller 30% size hold up. Funding is not simulated (§1); at 1× and multi-week holds it is a few percent a year against whichever side is crowded.

Original measurements (engine v1, one position per pair):

| Strategy (pairs, size) | TF | Trades | Win % | Return | Max DD |
|---|---|---|---|---|---|
| Supertrend(10, 3) flip, trailing SL 15% (BTC+ETH, 50%) | 1d | 26 | 42 | **+28.8%** | 32.0% |
| Supertrend(14, 4) flip, trailing SL 15% | 1d | 13 | 54 | +29.7% | 25.8% |
| Supertrend(10, 2.5) flip, trailing SL 15% | 1d | 32 | 34 | +23.2% | 39.1% |
| Supertrend(10, 3) flip, no SL | 1d | 25 | 44 | +21.0% | 27.6% |
| Supertrend(10, 3) flip + `close > ema200` filter | 1d | 12 | 42 | +1.2% | 24.1% |
| Supertrend(10, 3) flip (BTC+ETH+SOL, 33%) | 1d | 37 | 41 | +13.4% | 31.1% |
| Donchian 55 `increasing` in / 20 `decreasing` out, trailing 15% | 1d | 21 | 52 | **+23.7%** | 23.7% |
| Donchian 55 / 10, trailing 15% | 1d | 23 | 52 | +37.6% | 25.7% |
| Donchian 40 / 20, trailing 15% | 1d | 25 | 44 | +17.0% | 30.3% |
| Donchian 20 / 10 + volume > VMA20, trailing 12% | 1d | 41 | 42 | +23.0% | 29.5% |
| EMA 21/55 cross, trailing SL 15% | 1d | 16 | 38 | +29.4% | 26.7% |
| EMA 21/55 cross, ATR 3× trailing (BTC+ETH+SOL, 33%) | 4h | 153 | 37 | **+15.3%** | 26.3% |
| EMA 20/50 cross, ATR 3× trailing (3 pairs) | 4h | 170 | 38 | +12.3% | 29.5% |
| EMA 21/55 cross, ATR 3× trailing (BTC+ETH, 50%) | 4h | 105 | 36 | +10.5% | 34.4% |
| Supertrend(10, 3) flip (3 pairs, 33%) | 4h | 202 | 35 | +19.3% | — |
| MACD hist cross 0 + `close > ema200`, trailing 12% | 1d | 32 | 25 | −22.5% | 34.0% |
| Pullback: `close cross_above ema20` + ema200 + ADX>20, ATR 3× SL, TP 8%/50% | 4h | 174 | 28 | −11.5% | 20.6% |
| Same pullback, 4h short window | 4h | 45 | 22 | −12.0% | — |
| RSI cross_above 35 + ema200, SL 5%, TP 8% (3 pairs) | 4h | 51 | 31 | −19.8% | — |
| Bollinger lower-band re-entry + ADX<25, TP 3%, SL 2.5% (Coinbase 0.25% fee) | 1h | 81 | 59 | −18.6% | 15.1% |
| Same, exit at upper band | 1h | 71 | 52 | −16.1% | — |
| Same on Binance 4h, TP 6%, SL 4% | 4h | 81 | 59 | −18.6% | — |

Take-aways: (1) simple trend following with an opposite-signal exit works across every parameter neighbour → not curve-fit; (2) it returns roughly half of buy & hold with well under half the drawdown — that is the honest pitch, do not promise more; (3) every "smart" addition (EMA200 filter, extra pairs, pullback entries, mean reversion, tight targets) made results worse; (4) win rates are 35–55% — a good strategy here loses more often than it wins and pays for it with large winners. Tell the user this so they don't switch it off after four losses.

### 4.10 Output checklist (do this before answering)

1. One strategy family; regime filter (state) + trigger (event) + exit plan + SL tier + guards.
2. Every param id and output_idx matches §2.2; every operator matches §2.4; timeframe matches §5.3 for the chosen `data_exchange`.
3. Fee/slippage set to the exchange's real numbers; expected move per trade ≥ 3–5× round-trip cost.
4. SL/TP sized for the timeframe (§4.4); reward:risk ≥ 1.5 if both are fixed.
5. `backtest_lookback` covers several regimes and the exchange has that history; longest indicator length ≤ lookback / 10.
6. `is_sandbox: true`, `api_execution: false`, `api_key_name: null`, `max_order_value` present.
7. After the JSON, tell the user in 3–5 lines: what regime it targets, what the failure mode is, what to look for in the backtest (trade count, PF, DD vs. B&H), and that it must forward-test first. If the request cannot be expressed (shorting on a spot market, multi-timeframe in one graph, time-of-day rules), say so instead of approximating.

---

## 5. Bot import file format (`.apex.json`)

Output exactly **one** valid JSON document in a fenced code block. Use only methods, operators and field names from this document — the backend validates on import and rejects unknown methods, unsupported timeframes, invalid operators and negative offsets. Give nodes short descriptive ids (`ema200`, `entry_gate`). Set `ui_layout` to `{"nodes": [], "edges": []}` — the editor rebuilds the layout. Duplicate names are auto-renamed on import; validation errors come back as a list — fix and re-emit.

### 5.1 File structure

```json
{
  "apex_version": "1.0",
  "exported_at": "2026-09-09T12:00:00Z",
  "bot": {
    "name": "Strategy Name",
    "is_sandbox": true,
    "strategy": "node_evaluator",
    "settings": {
      "symbol": "BTC/USDC",
      "symbols": ["BTC/USDC", "ETH/USDC"],
      "timeframe": "4h",
      "max_positions": 2,
      "max_positions_scope": "global",
      "cooldown_trades": 1,
      "cooldown_candles": 3,
      "max_drawdown": 20,
      "drawdown_action": "block_entries",
      "drawdown_cooldown_days": 7,
      "max_capital_loss": 30,
      "max_order_value": 1000,
      "live_allocation_pct": 100,
      "api_execution": false,
      "backtest_on_start": true,
      "backtest_capital": 1000,
      "backtest_lookback": 1500,
      "api_key_name": null,
      "data_exchange": "binance",
      "trade_settings": { "...": "see 5.4" },
      "nodes": { "...": "see 5.5" },
      "ui_layout": { "nodes": [], "edges": [] },
      "entry_node": "entry_gate",
      "exit_node": "exit_signal"
    }
  }
}
```

### 5.2 Settings fields

| Field | Type | Description |
|---|---|---|
| `symbol` | string | Primary symbol (first in whitelist) |
| `symbols` | string[] | All pairs; one cash currency per bot (same quote on spot, same settle on swaps — validator error otherwise) |
| `timeframe` | string | Must be supported by `data_exchange` (§5.3) |
| `max_positions` | int ≥ 1 | Open positions (layers) allowed; > 1 enables pyramiding on repeated BUY signals (§2) |
| `max_positions_scope` | `per_pair` / `global` | `per_pair` = limit per symbol, `global` = limit across all pairs of the bot |
| `cooldown_trades` / `cooldown_candles` | int | Max new entries per window of N candles, per symbol (0 = off). Whole numbers; `"5.0"` is coerced with a warning |
| `max_drawdown` | % | Peak-to-trough on mark-to-market equity, checked after the backtest and after every closed live position. 0 = off |
| `drawdown_action` | `close_all` / `block_entries` | `close_all`: close everything and stop (a backtest breach prevents go-live). `block_entries`: skip new entries until drawdown < half the limit, or — once flat — until `drawdown_cooldown_days` passed; then the peak resets. Exits keep working. Simulated in the backtest too |
| `drawdown_cooldown_days` | 0–365 | Flat time before entries resume under `block_entries` (default 7) |
| `max_capital_loss` | % | Hard stop on loss of starting capital, independent of drawdown. With `block_entries` the bot winds down (no entries, exits finish, then stops). Required > 0 for live bots using `block_entries` |
| `max_order_value` | quote notional | Cap on the notional of one entry in the pair's **quote** currency (USDC on `BTC/USDC`, USD on `BTC/USD:BTC`; on swaps notional = margin × leverage). Applied in the backtest, forward test and live alike, so it is part of the strategy (changing it counts as a new variant). 0 = off; required > 0 for live |
| `live_allocation_pct` | 1–100 | Paper/live: share of the exchange wallet (free cash currency + deployed by all bots on the key) this bot may deploy; split it between bots sharing a key. Snapshot at go-live becomes `live_starting_capital`, the base for live guards |
| `api_execution` | bool | `true` = orders via API key |
| `backtest_on_start` / `backtest_capital` / `backtest_lookback` | bool / cash amount / candles | Backtest settings; `backtest_capital` is in the bot's cash currency (USDC for `BTC/USDC`, BTC for `BTC/USD:BTC`) |
| `api_key_name` | string/null | Saved key name (null = forward test) |
| `data_exchange` | string | `okx` `binance` `bitvavo` `coinbase` `cryptocom` `kraken` `kucoin` `bybit` `gateio` `bitget` `mexc` `htx` `bingx` |
| `market_type` | `spot` / `swap` | Default `spot`. `swap` = perpetuals; symbols then use the `BASE/QUOTE:SETTLE` form — linear `BTC/USDT:USDT` (settled in the quote stablecoin) or inverse `BTC/USD:BTC` (coin-margined, settled in the base; BingX has no inverse contracts) — and the key must be a swap key. The kind is decided per symbol. A spot bot rejects `:SETTLE` symbols, a swap bot requires them. Not every exchange offers swaps (§5.3) |
| `leverage` | 1–10 (int) | Swaps only (spot must be 1; Kraken max 5). Position notional = margin × leverage; `max_order_value` caps the notional. Warning above 3× (liquidation ≈ `(1 − 0.5%) / leverage` from entry — a wider stop never fires) |
| `margin_mode` | `isolated` / `cross` | Swaps only, default `isolated`. Sent to the exchange before the first order; the simulation treats both the same (per-position liquidation) |
| `short_node` / `cover_node` | string/null | Node ids for the SHORT and COVER signals (like `entry_node`/`exit_node`). Only on `swap` (validator error on spot); omit for long-only bots. A short without `cover_node`, SL or TP only warns |

### 5.3 Exchange timeframes and history

| Exchange | Timeframes | History |
|---|---|---|
| OKX | `1m 3m 5m 15m 30m 1h 2h 4h 6h 12h 1d 1w 1M` | full since listing (USDC pairs listed Aug 2025) |
| Binance | `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M` | full |
| Coinbase | `1m 5m 15m 30m 1h 2h 6h 1d` (**no 4h**) | full |
| Kraken | `1m 5m 15m 30m 1h 4h 1d 1w` | **only the last 720 candles** per timeframe |
| Bitvavo | `1m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d` | full |
| KuCoin | `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 1w` | full |
| Crypto.com | `1m 5m 15m 30m 1h 2h 4h 6h 12h 1d 1w` | full since listing |

Bybit, Gate, Bitget, MEXC, HTX and BingX were added later; their timeframes come from `GET /api/data/timeframes/{exchange}` (the builder filters the dropdown). Perpetual swaps (`market_type: swap`) are available on OKX, Binance, Kraken, KuCoin, Bybit, Gate, Bitget, HTX and BingX — not on Bitvavo, Coinbase, Crypto.com or MEXC. All of them serve linear contracts; inverse (coin-margined) contracts on all but BingX.

For long daily backtests prefer Binance or Coinbase data; for Kraken use ≤ 720 candles.

### 5.4 Trade settings

```json
"trade_settings": {
  "entry": {
    "order_type": "market",
    "amount_type": "percentage",
    "amount_value": 50,
    "fee": 0.1,
    "slippage": 0.05,
    "take_profits": [
      { "type": "percentage", "value": 8.0, "close_amount_type": "percentage", "close_amount_value": 50 }
    ],
    "stop_losses": [
      { "type": "atr", "value": 3.0, "close_amount_type": "percentage", "close_amount_value": 100 }
    ]
  },
  "exit": { "order_type": "market", "amount_type": "percentage", "amount_value": 100, "fee": 0.1, "slippage": 0.05 }
}
```

`fee`/`slippage` are percentages (`0.1` = 0.1%). `amount_type` `percentage` (% of the free cash left in the pool, see §1 Sizing) or `fixed` (an amount in the bot's cash currency). On swaps the amount is the margin; the notional is `leverage` times bigger. TP/SL semantics in §2.6.

Shorts (swap only): add `"short"` (same shape as `entry`, its TP/SL are the short's levels) and `"cover"` (same shape as `exit`) next to them and set `short_node`/`cover_node`. When `short` is omitted the short leg reuses `entry`; when `cover` is omitted it reuses `exit`.

### 5.5 Node definitions

```json
"ema200":     { "class": "indicator", "method": "ema", "params": { "length": 200 }, "output_idx": 0 },
"st_dir":     { "class": "indicator", "method": "supertrend", "params": { "length": 10, "multiplier": 3.0 }, "output_idx": 1 },
"close":      { "class": "price_data", "type": "close", "offset": 0 },
"prev_high":  { "class": "price_data", "type": "high", "offset": 1 },
"uptrend":    { "class": "condition", "left": "close", "operator": ">", "right": "ema200" },
"st_up":      { "class": "condition", "left": "st_dir", "operator": "==", "right": 1 },
"trigger":    { "class": "condition", "left": "close", "operator": "cross_above", "right": "ema20" },
"entry_gate": { "class": "logic", "operator": "and", "left": "uptrend", "right": "trigger" }
```

`entry_node` / `exit_node` point at the final node of each chain (the BUY and SELL triggers). `exit_node` is optional when TP/SL do all the exiting.

---

## 6. Vetted templates

Four complete files, identical to `examples/*.apex.json` in the repository, all imported and backtested in the real engine (results in §4.9, re-verified with `scripts/verify_examples.py`; `tests/golden/` pins their order stream on synthetic data so an engine change can never silently alter them). Three are long-only spot, the fourth is the long/short perpetual version of the first. Adapt pairs, fee and sizes; keep the structure. Each is deliberately minimal — every addition we tried made them worse.

### 6.1 Supertrend trend follower — 1d, flip in / flip out, 15% disaster trail

Backtest Dec 2023 → Sep 2026, BTC+ETH: **+28.8%, max DD 32.0%, 26 trades, 42% win rate** (buy & hold: +49.5% / 58.6% DD).

```json
{
  "apex_version": "1.0",
  "exported_at": "2026-09-09T12:00:00Z",
  "bot": {
    "name": "Supertrend Trend 1d",
    "is_sandbox": true,
    "strategy": "node_evaluator",
    "settings": {
      "symbol": "BTC/USDC",
      "symbols": ["BTC/USDC", "ETH/USDC"],
      "timeframe": "1d",
      "max_positions": 2,
      "max_positions_scope": "global",
      "cooldown_trades": 1,
      "cooldown_candles": 5,
      "max_drawdown": 35,
      "drawdown_action": "block_entries",
      "drawdown_cooldown_days": 14,
      "max_capital_loss": 40,
      "max_order_value": 1000,
      "live_allocation_pct": 100,
      "api_execution": false,
      "backtest_on_start": true,
      "backtest_capital": 1000,
      "backtest_lookback": 1000,
      "api_key_name": null,
      "data_exchange": "binance",
      "trade_settings": {
        "entry": {
          "order_type": "market", "amount_type": "percentage", "amount_value": 50,
          "fee": 0.1, "slippage": 0.05,
          "take_profits": [],
          "stop_losses": [
            { "type": "trailing", "value": 15.0, "close_amount_type": "percentage", "close_amount_value": 100 }
          ]
        },
        "exit": { "order_type": "market", "amount_type": "percentage", "amount_value": 100, "fee": 0.1, "slippage": 0.05 }
      },
      "nodes": {
        "st_dir":    { "class": "indicator", "method": "supertrend", "params": { "length": 10, "multiplier": 3.0 }, "output_idx": 1 },
        "flip_up":   { "class": "condition", "left": "st_dir", "operator": "cross_above", "right": 0 },
        "flip_down": { "class": "condition", "left": "st_dir", "operator": "cross_below", "right": 0 }
      },
      "ui_layout": { "nodes": [], "edges": [] },
      "entry_node": "flip_up",
      "exit_node": "flip_down"
    }
  }
}
```

The Supertrend direction line is +1/−1; crossing 0 is the flip. Buys the day the trend turns up, sells the day it turns down; the 15% trail only matters in a crash between daily closes. Fails in months-long chop (several −3…−8% flips in a row) — that is where the 32% drawdown comes from. Neighbours (10/2.5, 14/4, no stop) all made +21…+30%, so the parameters are not fragile.

### 6.2 Donchian channel breakout — 1d, new 55-day high in, new 20-day low out

Backtest Dec 2023 → Sep 2026, BTC+ETH: **+23.7%, max DD 23.7%, 21 trades, 52% win rate**. Neighbours: 55/10 +37.6%, 40/20 +17.0%.

```json
{
  "apex_version": "1.0",
  "exported_at": "2026-09-09T12:00:00Z",
  "bot": {
    "name": "Donchian Breakout 1d",
    "is_sandbox": true,
    "strategy": "node_evaluator",
    "settings": {
      "symbol": "BTC/USDC",
      "symbols": ["BTC/USDC", "ETH/USDC"],
      "timeframe": "1d",
      "max_positions": 2,
      "max_positions_scope": "global",
      "cooldown_trades": 1,
      "cooldown_candles": 5,
      "max_drawdown": 30,
      "drawdown_action": "block_entries",
      "drawdown_cooldown_days": 14,
      "max_capital_loss": 35,
      "max_order_value": 1000,
      "live_allocation_pct": 100,
      "api_execution": false,
      "backtest_on_start": true,
      "backtest_capital": 1000,
      "backtest_lookback": 1000,
      "api_key_name": null,
      "data_exchange": "binance",
      "trade_settings": {
        "entry": {
          "order_type": "market", "amount_type": "percentage", "amount_value": 50,
          "fee": 0.1, "slippage": 0.05,
          "take_profits": [],
          "stop_losses": [
            { "type": "trailing", "value": 15.0, "close_amount_type": "percentage", "close_amount_value": 100 }
          ]
        },
        "exit": { "order_type": "market", "amount_type": "percentage", "amount_value": 100, "fee": 0.1, "slippage": 0.05 }
      },
      "nodes": {
        "dc_upper55":   { "class": "indicator", "method": "donchian", "params": { "lower_length": 55, "upper_length": 55 }, "output_idx": 2 },
        "dc_lower20":   { "class": "indicator", "method": "donchian", "params": { "lower_length": 20, "upper_length": 20 }, "output_idx": 0 },
        "new_55d_high": { "class": "condition", "left": "dc_upper55", "operator": "increasing" },
        "new_20d_low":  { "class": "condition", "left": "dc_lower20", "operator": "decreasing" }
      },
      "ui_layout": { "nodes": [], "edges": [] },
      "entry_node": "new_55d_high",
      "exit_node": "new_20d_low"
    }
  }
}
```

Classic turtle logic. The channel *includes* the current candle, so `close > upper` can never be true — a breakout is expressed as the channel top `increasing` (a new 55-day high was set today). Enters late and exits late by design; the win rate is the highest of the three because it only trades established trends. Expect 6–10 trades per pair per year.

### 6.3 EMA cross with ATR trail — 4h, the more active option

Backtest Dec 2023 → Sep 2026, BTC+ETH+SOL at 33%: **+15.3%, max DD 26.3%, 153 trades, 37% win rate**. Neighbours: 20/50 +12.3%; BTC+ETH only +10.5%.

```json
{
  "apex_version": "1.0",
  "exported_at": "2026-09-09T12:00:00Z",
  "bot": {
    "name": "EMA Cross 4h",
    "is_sandbox": true,
    "strategy": "node_evaluator",
    "settings": {
      "symbol": "BTC/USDC",
      "symbols": ["BTC/USDC", "ETH/USDC", "SOL/USDC"],
      "timeframe": "4h",
      "max_positions": 3,
      "max_positions_scope": "global",
      "cooldown_trades": 1,
      "cooldown_candles": 5,
      "max_drawdown": 30,
      "drawdown_action": "block_entries",
      "drawdown_cooldown_days": 7,
      "max_capital_loss": 35,
      "max_order_value": 1000,
      "live_allocation_pct": 100,
      "api_execution": false,
      "backtest_on_start": true,
      "backtest_capital": 1000,
      "backtest_lookback": 6000,
      "api_key_name": null,
      "data_exchange": "binance",
      "trade_settings": {
        "entry": {
          "order_type": "market", "amount_type": "percentage", "amount_value": 33,
          "fee": 0.1, "slippage": 0.05,
          "take_profits": [],
          "stop_losses": [
            { "type": "atr", "value": 3.0, "close_amount_type": "percentage", "close_amount_value": 100 }
          ]
        },
        "exit": { "order_type": "market", "amount_type": "percentage", "amount_value": 100, "fee": 0.1, "slippage": 0.05 }
      },
      "nodes": {
        "ema21":       { "class": "indicator", "method": "ema", "params": { "length": 21 }, "output_idx": 0 },
        "ema55":       { "class": "indicator", "method": "ema", "params": { "length": 55 }, "output_idx": 0 },
        "golden_cross": { "class": "condition", "left": "ema21", "operator": "cross_above", "right": "ema55" },
        "death_cross":  { "class": "condition", "left": "ema21", "operator": "cross_below", "right": "ema55" }
      },
      "ui_layout": { "nodes": [], "edges": [] },
      "entry_node": "golden_cross",
      "exit_node": "death_cross"
    }
  }
}
```

Roughly one trade per pair per week. The ATR 3× trailing stop (≈ 4–7% on 4h) closes most trades — 134 of 153 exits were the trail, at +0.9% average — while the death cross only catches slow rolls. Lower return than the daily systems but shallower single-trade losses and faster feedback for a forward test. Run at least 6 000 candles; a 2 000-candle window covers one regime only.

### 6.4 Supertrend long/short on perpetuals — 1d, 1×, long in uptrend, short in downtrend

Backtest 30 Dec 2023 → 24 Sep 2026, BTC+ETH USDT-settled perps at 30%: **+50.8%, max DD 29.1%, 49 trades (26 long / 23 short), 37% win rate**, 0 liquidations, entries never blocked (buy & hold: BTC +100%, ETH +17%). Neighbours at 1× all positive (+12 to +46%); at 2× only these exact parameters survive, so it ships at 1× (§4.9).

```json
{
  "apex_version": "1.0",
  "exported_at": "2026-09-25T12:00:00Z",
  "bot": {
    "name": "Supertrend Long/Short Perp 1d",
    "is_sandbox": true,
    "strategy": "node_evaluator",
    "settings": {
      "symbol": "BTC/USDT:USDT",
      "symbols": [
        "BTC/USDT:USDT",
        "ETH/USDT:USDT"
      ],
      "timeframe": "1d",
      "market_type": "swap",
      "leverage": 1,
      "margin_mode": "isolated",
      "max_positions": 2,
      "max_positions_scope": "global",
      "cooldown_trades": 1,
      "cooldown_candles": 5,
      "max_drawdown": 35,
      "drawdown_action": "block_entries",
      "drawdown_cooldown_days": 14,
      "max_capital_loss": 40,
      "max_order_value": 1000,
      "live_allocation_pct": 100,
      "api_execution": false,
      "backtest_on_start": true,
      "backtest_capital": 1000,
      "backtest_lookback": 1000,
      "api_key_name": null,
      "data_exchange": "binance",
      "trade_settings": {
        "entry": {
          "order_type": "market",
          "amount_type": "percentage",
          "amount_value": 30,
          "fee": 0.05,
          "slippage": 0.05,
          "take_profits": [],
          "stop_losses": [
            {
              "type": "percentage",
              "value": 20.0,
              "close_amount_type": "percentage",
              "close_amount_value": 100
            }
          ]
        },
        "exit": {
          "order_type": "market",
          "amount_type": "percentage",
          "amount_value": 100,
          "fee": 0.05,
          "slippage": 0.05
        },
        "short": {
          "order_type": "market",
          "amount_type": "percentage",
          "amount_value": 30,
          "fee": 0.05,
          "slippage": 0.05,
          "take_profits": [],
          "stop_losses": [
            {
              "type": "percentage",
              "value": 20.0,
              "close_amount_type": "percentage",
              "close_amount_value": 100
            }
          ]
        },
        "cover": {
          "order_type": "market",
          "amount_type": "percentage",
          "amount_value": 100,
          "fee": 0.05,
          "slippage": 0.05
        }
      },
      "nodes": {
        "st_dir": {
          "class": "indicator",
          "method": "supertrend",
          "params": {
            "length": 10,
            "multiplier": 3.0
          },
          "output_idx": 1
        },
        "trend_up": {
          "class": "condition",
          "left": "st_dir",
          "operator": ">",
          "right": 0
        },
        "trend_down": {
          "class": "condition",
          "left": "st_dir",
          "operator": "<",
          "right": 0
        },
        "flip_up": {
          "class": "condition",
          "left": "st_dir",
          "operator": "cross_above",
          "right": 0
        },
        "flip_down": {
          "class": "condition",
          "left": "st_dir",
          "operator": "cross_below",
          "right": 0
        }
      },
      "ui_layout": {
        "nodes": [],
        "edges": []
      },
      "entry_node": "trend_up",
      "exit_node": "flip_down",
      "short_node": "trend_down",
      "cover_node": "flip_up"
    }
  }
}
```

The same flip as §6.1, made two-sided: `trend_up` (`st_dir > 0`) is the BUY, `flip_down` the SELL, `trend_down` the SHORT and `flip_up` the COVER. The entries are states rather than crosses on purpose — a long and a short never coexist on a pair, so on the flip candle the old side is closed and the new side opens one candle later from the state; with the flip as the only strategy exit the state fires once per trend, and the 20% stop is a disaster stop only (a trailing stop here would re-enter after every stop-out — tested, −0.3%). `market_type: swap` with `:USDT` symbols, `leverage: 1`, `margin_mode: isolated`, taker fee 0.05% on both legs. Runs as a forward test without a key; live needs a key bound to the swap market (§5). Do not raise the leverage without re-running the backtest: the 2× rows in §4.9 show why.

---

## 7. Example prompt

> "Design an ApexAlgo strategy for BTC/USDC and ETH/USDC on Binance. I want a daily trend follower: buy when the trend turns up, sell when it turns down, with a wide disaster stop. 50% of capital per pair, drawdown guard 35% with block_entries and capital loss 40%. 1000 candles backtest, $1000, fee 0.1%. Output the `.apex.json` and tell me what to check in the backtest."

The reply should be one JSON block that passes the checklist in §4.10, followed by a short note on regime, failure mode and what to verify before forward testing — including that a good trend follower loses more trades than it wins.
