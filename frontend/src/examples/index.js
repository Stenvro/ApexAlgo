/**
 * Bundled example strategies — imported straight from the repo's `examples/`
 * directory (single source of truth; the Docker image copies it to
 * /app/examples). Each entry is a ready-to-POST payload for `/api/bots/import`.
 * All were backtested in the engine on Binance BTC/ETH(/SOL), Dec 2023 –
 * Sep 2026 (USDC spot; USDT-settled perps for the long/short one); see
 * STRATEGY_CONTEXT.md §4.9 for the numbers.
 */
import supertrendTrend from '../../../examples/Supertrend_Trend_1d.apex.json';
import donchianBreakout from '../../../examples/Donchian_Breakout_1d.apex.json';
import emaCross from '../../../examples/EMA_Cross_4h.apex.json';
import supertrendLongShort from '../../../examples/Supertrend_LongShort_Perp_1d.apex.json';

export const EXAMPLE_BOTS = [
  {
    id: 'supertrend-trend-1d',
    name: 'Supertrend Trend 1d',
    description: 'Daily trend follower: buys when the Supertrend flips up, sells when it flips down. 15% disaster trail.',
    payload: supertrendTrend,
  },
  {
    id: 'donchian-breakout-1d',
    name: 'Donchian Breakout 1d',
    description: 'Turtle-style breakout: new 55-day high in, new 20-day low out. Few trades, high win rate.',
    payload: donchianBreakout,
  },
  {
    id: 'ema-cross-4h',
    name: 'EMA Cross 4h',
    description: 'More active EMA 21/55 crossover on 4h with an ATR trailing stop. Roughly one trade per pair per week.',
    payload: emaCross,
  },
  {
    id: 'supertrend-longshort-perp-1d',
    name: 'Supertrend Long/Short Perp 1d',
    description: 'Perpetual swaps, 1x: long while the daily Supertrend points up, short while it points down. Needs a swap key to trade live.',
    payload: supertrendLongShort,
  },
];
