// Whitelist helpers shared by the builder canvas and the whitelist node.
// One default pair everywhere — the templates ship their own lists.
export const DEFAULT_PAIR = 'BTC/USDT';

// "btc-usdt, eth/usdt ,ETH/USDT" → ["BTC/USDT", "ETH/USDT"]
export const parsePairs = (text) => {
  const out = [];
  for (const raw of String(text ?? '').split(/[,\n;]+/)) {
    const s = raw.trim().toUpperCase().replace('-', '/');
    if (s && !out.includes(s)) out.push(s);
  }
  return out;
};
