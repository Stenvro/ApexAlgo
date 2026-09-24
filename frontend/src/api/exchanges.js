// Supported exchanges, loaded once from the backend registry
// (GET /api/keys/exchanges). backend/core/exchange_registry.py is the single
// source of truth; the static fallback below only bridges the first render
// and an unreachable backend, so an exchange added there shows up everywhere
// (key form, data manager, builder) without touching the frontend.
//
// Shape of each entry:
//   { id, name, needs_passphrase, has_sandbox, keys_url, sandbox_note,
//     markets: { spot: {has_sandbox, max_leverage, leverage_in_order, note}, swap?: {...} } }
import { useEffect, useSyncExternalStore } from 'react';
import { apiClient } from './client';

// Market types per exchange mirror backend ExchangeSpec.markets (spot always;
// swap = USDT-margined perpetuals where ApexAlgo supports them)
const SPOT = { spot: { has_sandbox: false, max_leverage: 1 } };
const fb = (id, name, needs_passphrase, has_sandbox, swap) => ({
  id, name, needs_passphrase, has_sandbox,
  markets: swap ? { spot: { has_sandbox, max_leverage: 1 }, swap: { has_sandbox: swap.has_sandbox, max_leverage: swap.max_leverage || 10 } } : { spot: { ...SPOT.spot, has_sandbox } },
});
export const FALLBACK_EXCHANGES = [
  fb('okx', 'OKX', true, true, { has_sandbox: true }),
  fb('binance', 'Binance', false, true, { has_sandbox: true }),
  fb('bitvavo', 'Bitvavo', false, false),
  fb('coinbase', 'Coinbase', false, false),
  fb('cryptocom', 'Crypto.com', false, true),
  fb('kraken', 'Kraken', false, false, { has_sandbox: true, max_leverage: 5 }),
  fb('kucoin', 'KuCoin', true, false, { has_sandbox: false }),
  fb('bybit', 'Bybit', false, true, { has_sandbox: true }),
  fb('gateio', 'Gate', false, true, { has_sandbox: true }),
  fb('bitget', 'Bitget', true, true, { has_sandbox: true }),
  fb('mexc', 'MEXC', false, false),
  fb('htx', 'HTX', false, false, { has_sandbox: false }),
  fb('bingx', 'BingX', false, true, { has_sandbox: true }),
];

let cache = null;
let inflight = null;
const listeners = new Set();
const notify = () => listeners.forEach((fn) => fn());

/** Fetches the list once; concurrent callers share the request. Failed loads are not cached. */
export function loadExchanges() {
  if (cache) return Promise.resolve(cache);
  if (!inflight) {
    inflight = apiClient.get('/api/keys/exchanges')
      .then((res) => {
        if (Array.isArray(res.data) && res.data.length) cache = res.data;
        return cache || FALLBACK_EXCHANGES;
      })
      .finally(() => { inflight = null; notify(); });
  }
  return inflight;
}

const subscribe = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
const snapshot = () => cache || FALLBACK_EXCHANGES;

/** React hook: the exchange list (fallback until loaded, then the backend registry). */
export function useExchanges() {
  const exchanges = useSyncExternalStore(subscribe, snapshot);
  useEffect(() => { if (!cache) loadExchanges().catch(() => {}); }, []);
  return exchanges;
}

/** Display name for an exchange id ('okx' → 'OKX'); falls back to the upper-cased id. */
export function exchangeName(id) {
  const hit = snapshot().find((e) => e.id === id);
  return hit ? hit.name : String(id || '').toUpperCase();
}

/** Market capabilities of `exchangeId` for `marketType` ('spot' | 'swap'); null when unsupported. */
export function marketCaps(exchangeId, marketType = 'spot') {
  const hit = snapshot().find((e) => e.id === exchangeId);
  if (!hit) return null;
  const markets = hit.markets || { spot: { has_sandbox: !!hit.has_sandbox, max_leverage: 1 } };
  return markets[marketType] || null;
}
