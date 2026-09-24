// Supported exchanges, loaded once from the backend registry
// (GET /api/keys/exchanges). backend/core/exchange_registry.py is the single
// source of truth; the static fallback below only bridges the first render
// and an unreachable backend, so an exchange added there shows up everywhere
// (key form, data manager, builder) without touching the frontend.
//
// Shape of each entry:
//   { id, name, needs_passphrase, has_sandbox, keys_url, sandbox_note }
import { useEffect, useSyncExternalStore } from 'react';
import { apiClient } from './client';

export const FALLBACK_EXCHANGES = [
  { id: 'okx', name: 'OKX', needs_passphrase: true, has_sandbox: true },
  { id: 'binance', name: 'Binance', needs_passphrase: false, has_sandbox: true },
  { id: 'bitvavo', name: 'Bitvavo', needs_passphrase: false, has_sandbox: false },
  { id: 'coinbase', name: 'Coinbase', needs_passphrase: false, has_sandbox: false },
  { id: 'cryptocom', name: 'Crypto.com', needs_passphrase: false, has_sandbox: true },
  { id: 'kraken', name: 'Kraken', needs_passphrase: false, has_sandbox: false },
  { id: 'kucoin', name: 'KuCoin', needs_passphrase: true, has_sandbox: false },
  { id: 'bybit', name: 'Bybit', needs_passphrase: false, has_sandbox: true },
  { id: 'gateio', name: 'Gate', needs_passphrase: false, has_sandbox: true },
  { id: 'bitget', name: 'Bitget', needs_passphrase: true, has_sandbox: true },
  { id: 'mexc', name: 'MEXC', needs_passphrase: false, has_sandbox: false },
  { id: 'htx', name: 'HTX', needs_passphrase: false, has_sandbox: false },
  { id: 'bingx', name: 'BingX', needs_passphrase: false, has_sandbox: true },
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
