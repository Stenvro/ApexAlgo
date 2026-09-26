/**
 * Side-aware order helpers shared by the chart, analytics and dashboard.
 *
 * Orders carry `side` ('buy' | 'sell'), `market_type` ('spot' | 'swap') and
 * `reduce_only` (0/1). On a perpetual a sell that is not reduce-only opens a
 * short and a reduce-only buy covers one; spot orders never set reduce_only.
 */

/** A sell that opens a short (perps only). */
export const isShortOpen = (o) => !!o && o.side === 'sell' && o.market_type === 'swap' && !o.reduce_only;

/** A buy that closes a short (reduce-only buy). */
export const isCover = (o) => !!o && o.side === 'buy' && !!o.reduce_only;

/** Does this order open a position (long or short)? */
export const isOpeningOrder = (o) => (o?.side === 'buy' ? !o.reduce_only : isShortOpen(o));

/**
 * Execution-log label: BUY / SELL / SHORT / COVER.
 * @param {object} o order row
 * @returns {'BUY'|'SELL'|'SHORT'|'COVER'}
 */
export function orderAction(o) {
  if (isShortOpen(o)) return 'SHORT';
  if (isCover(o)) return 'COVER';
  return String(o?.side || '').toUpperCase() === 'SELL' ? 'SELL' : 'BUY';
}

/** Position side normalised to 'long' | 'short' (null side = long). */
export const positionSide = (p) => (p?.side === 'short' ? 'short' : 'long');

/** Short label for the entry price line per mode. */
export function modeTag(mode) {
  switch (mode) {
    case 'backtest': return 'BT';
    case 'forward_test': return 'FWD';
    case 'paper': return 'PAPER';
    case 'live': return 'LIVE';
    default: return String(mode || '').toUpperCase() || 'LIVE';
  }
}
