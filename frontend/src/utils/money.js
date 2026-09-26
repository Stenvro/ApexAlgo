// Money formatting that never assumes a currency.
//
// Every cash amount in the UI (PnL, capital, fees, notional, wallet values)
// is printed through `fmtMoney(value, ccy)` with the currency it is actually
// denominated in. Fiat gets its symbol (€1,234.56), stablecoins and other
// crypto print the code after the number (1,234.56 USDT · 0.0213 BTC). An
// unknown currency prints the bare number and marks itself as such — the UI
// never falls back to "$".
//
// The cash currency of a market comes from the API where the backend sends
// it (`cash_currency` on positions/orders/bots, `markets[symbol].settle` from
// /api/data/symbols); `currencyOf(symbol)` is the fallback derived from the
// ccxt symbol form: spot BASE/QUOTE → QUOTE, swap BASE/QUOTE:SETTLE → SETTLE.

const FIAT_SYMBOL = { USD: '$', EUR: '€', GBP: '£', JPY: '¥', CHF: 'CHF ', AUD: 'A$', CAD: 'C$', BRL: 'R$', TRY: '₺' };
// Codes that are money-like (2 decimals by default); everything else is
// treated as a crypto cash currency with significant-digit formatting.
const STABLE = new Set(['USDT', 'USDC', 'USDE', 'DAI', 'FDUSD', 'TUSD', 'BUSD', 'USDD', 'PYUSD', 'EURC', 'EURT', 'USDG', 'UST']);

export const isFiat = (ccy) => !!ccy && Object.prototype.hasOwnProperty.call(FIAT_SYMBOL, ccy);
export const isStable = (ccy) => !!ccy && STABLE.has(ccy);
/** Crypto cash (BTC/ETH/…): prices in it are small, so amounts need more precision. */
export const isCryptoCash = (ccy) => !!ccy && !isFiat(ccy) && !isStable(ccy);

/** Normalise a currency code ('usdt' → 'USDT'); null/empty → null. */
export const normCcy = (ccy) => {
  const s = String(ccy ?? '').trim().toUpperCase();
  return s || null;
};

/**
 * Parts of a ccxt symbol: { base, quote, settle } — settle is null on spot.
 * 'BTC/USDT:USDT' → { base:'BTC', quote:'USDT', settle:'USDT' }
 * 'BTC/USD:BTC'   → { base:'BTC', quote:'USD',  settle:'BTC' }   (inverse)
 * Also accepts the dash form used in API paths (BTC-USDT).
 */
export const symbolParts = (symbol) => {
  const s = String(symbol ?? '').trim().toUpperCase();
  if (!s) return { base: null, quote: null, settle: null };
  const [pair, settleRaw] = s.split(':');
  const [base, quote] = pair.split(/[/-]/);
  return { base: base || null, quote: quote || null, settle: settleRaw || null };
};

/** Cash currency a market settles in: SETTLE on a swap, QUOTE on spot; null when unknown. */
export const currencyOf = (symbol) => {
  const { quote, settle } = symbolParts(symbol);
  return settle || quote || null;
};

/**
 * Contract kind of a market from its symbol: 'spot', 'linear' (settle ==
 * quote) or 'inverse' (settle == base). Prefer `markets[symbol].kind` from
 * /api/data/symbols when available — this is the fallback.
 */
export const contractKindOf = (symbol) => {
  const { base, quote, settle } = symbolParts(symbol);
  if (!settle) return 'spot';
  if (settle === base && settle !== quote) return 'inverse';
  return 'linear';
};

/** Cash currency of an API row (position/order/bot): the server field, else derived from the symbol. */
export const rowCurrency = (row) => normCcy(row?.cash_currency) || currencyOf(row?.symbol) || null;

/** Cash currency of a bot: server field, backtest summary, else the first whitelisted pair. */
export const botCurrency = (bot) => {
  if (!bot) return null;
  const s = bot.settings || {};
  const sym = (Array.isArray(s.symbols) && s.symbols[0]) || s.symbol || null;
  return normCcy(bot.cash_currency) || normCcy(bot.last_backtest_summary?.cash_currency) || normCcy(s.last_backtest_summary?.cash_currency) || currencyOf(sym);
};

const trimZeros = (str) => (str.includes('.') ? str.replace(/\.?0+$/, '') : str);

/**
 * Format a number in a currency.
 *   fmtMoney(1234.5, 'USDT')            → '1,234.50 USDT'
 *   fmtMoney(1234.5, 'EUR')             → '€1,234.50'
 *   fmtMoney(0.0213, 'BTC')             → '0.0213 BTC'   (6–8 significant, trailing zeros trimmed)
 *   fmtMoney(-12.3, 'USDT', {sign:true})→ '-12.30 USDT'; positive gets '+'
 *   fmtMoney(5, null)                   → '5.00'  (caller should add title="unit unknown", see fmtMoneyTitle)
 * `digits`: fixed decimals for fiat/stable (default 2); crypto cash ignores it
 * unless `digits` is given explicitly.
 */
export const fmtMoney = (value, ccy, { digits, sign = false, compact = false } = {}) => {
  const code = normCcy(ccy);
  const n = Number(value);
  const v = Number.isFinite(n) ? n : 0;
  const abs = Math.abs(v);
  const prefix = sign ? (v > 0 ? '+' : v < 0 ? '-' : '') : (v < 0 ? '-' : '');
  let num;
  if (code && isCryptoCash(code) && digits === undefined) {
    // 6 significant digits, at most 8 decimals, trailing zeros trimmed
    if (abs === 0) num = '0';
    else {
      const decimals = Math.min(8, Math.max(2, 6 - Math.floor(Math.log10(abs)) - 1));
      num = trimZeros(abs.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals }));
    }
  } else {
    const d = digits === undefined ? 2 : digits;
    num = compact && abs >= 1e6
      ? `${(abs / 1e6).toLocaleString('en-US', { maximumFractionDigits: 2 })}M`
      : abs.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  if (!code) return `${prefix}${num}`;
  if (isFiat(code)) return `${prefix}${FIAT_SYMBOL[code]}${num}`;
  return `${prefix}${num} ${code}`;
};

/** `title` hint for an amount whose currency is unknown (null ccy); undefined otherwise. */
export const fmtMoneyTitle = (ccy) => (normCcy(ccy) ? undefined : 'unit unknown');

/**
 * Sum `pick(row)` per currency: { USDT: 123.4, BTC: 0.01 }.
 * `ccyOf(row)` defaults to `rowCurrency`; rows with an unknown currency land
 * under the key '?' so they are never silently added to a real currency.
 */
export const sumByCurrency = (rows, pick, ccyOf = rowCurrency) => {
  const out = {};
  for (const r of rows || []) {
    const c = ccyOf(r) || '?';
    out[c] = (out[c] || 0) + (Number(pick(r)) || 0);
  }
  return out;
};

/** Distinct currencies of a set of rows, stable order (fiat/stable first, then alphabetical). */
export const currenciesOf = (rows, ccyOf = rowCurrency) => {
  const set = new Set();
  for (const r of rows || []) set.add(ccyOf(r) || '?');
  return [...set].sort((a, b) => (isCryptoCash(a) ? 1 : 0) - (isCryptoCash(b) ? 1 : 0) || a.localeCompare(b));
};

/** Contract kind of a position/order row: the server field, else derived from the symbol. */
export const rowKind = (row) => row?.contract_kind || contractKindOf(row?.symbol);

/**
 * Notional of a position in its cash currency. Spot and linear swaps:
 * size × price (quote/settle units). Inverse contracts settle in the base
 * coin, so the notional in cash is simply the base size.
 */
export const positionNotional = (pos, price = pos?.entry_price) =>
  rowKind(pos) === 'inverse' ? (Number(pos?.amount) || 0) : (Number(pos?.amount) || 0) * (Number(price) || 0);

/** Capital locked by a position = notional / leverage, in its cash currency. */
export const positionMargin = (pos) => positionNotional(pos) / Math.max(1, Number(pos?.leverage) || 1);

/**
 * Unrealised PnL of a position at `price`, in its cash currency.
 * Linear/spot: ±(price − entry) × size. Inverse (cash = base coin): the
 * base value of a fixed quote notional moves with 1/price, so
 * pnl = size × (price − entry) / price for a long (mirrored for a short).
 */
export const positionPnl = (pos, price) => {
  const entry = Number(pos?.entry_price) || 0;
  const size = Number(pos?.amount) || 0;
  const p = Number(price) || 0;
  if (!entry || !p) return 0;
  const dir = pos?.side === 'short' ? -1 : 1;
  if (rowKind(pos) === 'inverse') return dir * size * (p - entry) / p;
  return dir * (p - entry) * size;
};

/** Render a per-currency sum as text: '1,234.56 USDT · 0.02 BTC'. */
export const fmtByCurrency = (sums, opts) =>
  Object.entries(sums).map(([c, v]) => fmtMoney(v, c === '?' ? null : c, opts)).join(' · ');

// ── Market metadata from /api/data/symbols/{exchange} (`markets[symbol]`) ──

/** Contract kind of a symbol: `markets[symbol].kind` when the API sent it, else derived from the symbol. */
export const marketKind = (markets, symbol) => markets?.[symbol]?.kind || contractKindOf(symbol);

/** Cash currency of a symbol: `markets[symbol].settle` (swap) / `.quote` (spot), else derived from the symbol. */
export const marketCurrency = (markets, symbol) => {
  const m = markets?.[symbol];
  return normCcy(m?.settle || m?.quote) || currencyOf(symbol);
};

/**
 * Cash-currency picture of a whitelist: the code shared by every pair, or
 * 'mixed' when the pairs settle in different currencies; plus the pairs that
 * are inverse contracts (margin and PnL in the base coin).
 */
export const whitelistCurrency = (markets, symbols) => {
  const ccys = [...new Set((symbols || []).map(s => marketCurrency(markets, s)).filter(Boolean))];
  const quotes = [...new Set((symbols || []).map(s => symbolParts(s).quote).filter(Boolean))];
  const inverse = (symbols || []).filter(s => marketKind(markets, s) === 'inverse');
  return {
    cashCurrency: ccys.length === 1 ? ccys[0] : (ccys.length ? 'mixed' : null),
    cashCurrencies: ccys,
    quoteCurrency: quotes.length === 1 ? quotes[0] : (quotes.length ? 'mixed' : null),
    hasInverse: inverse.length > 0,
    inverseBases: [...new Set(inverse.map(s => symbolParts(s).base).filter(Boolean))],
  };
};
