import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { apiClient } from '../api/client';
import { humanizeApiError } from '../api/errors';
import PageShell from './ui/PageShell';
import GlowPanel from './ui/GlowPanel';
import SectionHeader from './ui/SectionHeader';
import Button from './ui/Button';
import Badge from './ui/Badge';
import EmptyState from './ui/EmptyState';
import { Input, Select } from './ui/Input';
import { SkeletonCard } from './ui/Skeleton';
import { toast } from './ui/Toast';
import { confirmDialog } from './ui/ConfirmDialog';
import { useExchanges } from '../api/exchanges';
import { fmtMoney, normCcy, isCryptoCash } from '../utils/money';

/* Deterministic avatar color per exchange (token values); unknown ids get the neutral class in the avatar */
const AVATAR_COLORS = {
  okx: 'text-info border-info/30 bg-info/10',
  binance: 'text-accent border-accent/30 bg-accent/10',
  bitvavo: 'text-success border-success/30 bg-success/10',
  coinbase: 'text-info border-info/30 bg-info/10',
  cryptocom: 'text-purple border-purple/30 bg-purple/10',
  kraken: 'text-purple border-purple/30 bg-purple/10',
  kucoin: 'text-success border-success/30 bg-success/10',
  bybit: 'text-accent border-accent/30 bg-accent/10',
  gateio: 'text-info border-info/30 bg-info/10',
  bitget: 'text-info border-info/30 bg-info/10',
  mexc: 'text-success border-success/30 bg-success/10',
  htx: 'text-purple border-purple/30 bg-purple/10',
  bingx: 'text-info border-info/30 bg-info/10',
};

const IconKeyEmpty = (
  <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" d="M15 7a4 4 0 11-4 4c0-.35.04-.7.13-1.03L4 17v3h3l1-1v-2h2v-2h2l1.87-1.87c.33.09.68.13 1.13.13a4 4 0 000-8z" />
    <circle cx="16" cy="8" r="1" fill="currentColor" stroke="none" />
  </svg>
);
const IconCheck = (
  <svg className="w-3 h-3 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5} aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
  </svg>
);
const IconBlock = (
  <svg className="w-3 h-3 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.2} aria-hidden="true">
    <circle cx="12" cy="12" r="8" /><path strokeLinecap="round" d="M6.5 6.5l11 11" />
  </svg>
);

const qty = (v) => {
  const n = Number(v) || 0;
  if (n === 0) return '0';
  if (n >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (n >= 1) return n.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return n.toLocaleString(undefined, { maximumFractionDigits: 8 });
};

function ExchangeAvatar({ exchange, name }) {
  const initial = (name || exchange || '?').charAt(0).toUpperCase();
  return (
    <div
      className={`w-9 h-9 rounded-lg border flex items-center justify-center font-bold text-sm shrink-0 ${AVATAR_COLORS[exchange] || 'text-muted border-border bg-raised'}`}
      aria-hidden="true"
    >
      {initial}
    </div>
  );
}

/* Wallet contents valued in the exchange's valuation currency (the API's
   `valuation_currency`; older backends valued in USD), largest first. Assets
   without a spot market against that currency stay unvalued ("n/a") rather
   than counting as zero. */
function WalletPanel({ wallet, exchangeLabel = 'the exchange' }) {
  const [showDust, setShowDust] = useState(false);
  const ccy = normCcy(wallet?.valuation_currency) || 'USD';
  const val = (v) => fmtMoney(v, ccy);
  const rows = useMemo(() => {
    const entries = Object.entries(wallet?.balances || {}).map(([coin, d]) => ({ coin, ...d }));
    entries.sort((a, b) => (b.usd_value ?? -1) - (a.usd_value ?? -1) || b.total - a.total);
    return entries;
  }, [wallet]);
  // "Dust" = worth less than one unit of the valuation currency; meaningless
  // when that currency is a coin (1 BTC is not dust), so no folding then
  const dustLimit = isCryptoCash(ccy) ? 0 : 1;
  const dust = rows.filter(r => r.usd_value !== null && r.usd_value !== undefined && r.usd_value < dustLimit);
  const visible = showDust ? rows : rows.filter(r => !dust.includes(r));
  const total = wallet?.total_usd || 0;

  if (rows.length === 0) return <span className="text-xs text-muted">Wallet is empty.</span>;

  return (
    <div>
      <div className="flex items-end justify-between mb-3 flex-wrap gap-2">
        <div>
          <p className="text-3xs font-bold uppercase tracking-wider text-muted">Estimated value <span className="text-faint normal-case tracking-normal">in {ccy}</span></p>
          <p className="text-xl font-num font-bold text-text leading-none mt-1">{val(total)}</p>
        </div>
        <div className="text-right">
          <p className="text-3xs text-faint font-num">{rows.length} asset{rows.length === 1 ? '' : 's'}{wallet.unpriced?.length ? ` · ${wallet.unpriced.length} unvalued` : ''}</p>
          {dust.length > 0 && (
            <button type="button" onClick={() => setShowDust(v => !v)} className="text-3xs text-muted hover:text-text underline-offset-2 hover:underline">
              {showDust ? 'hide' : 'show'} {dust.length} dust (&lt;{fmtMoney(dustLimit, ccy, { digits: 0 })})
            </button>
          )}
        </div>
      </div>
      <div className="overflow-x-auto rounded-md border border-border">
        <table className="w-full text-left">
          <thead className="bg-bg/60">
            <tr className="text-3xs font-bold uppercase tracking-wider text-muted">
              <th className="px-3 py-2">Asset</th>
              <th className="px-3 py-2 text-right">Available</th>
              <th className="px-3 py-2 text-right">In orders</th>
              <th className="px-3 py-2 text-right">Value ({ccy})</th>
              <th className="px-3 py-2 w-24">Share</th>
            </tr>
          </thead>
          <tbody className="text-xs font-num">
            {visible.map(r => {
              const share = total > 0 && r.usd_value ? Math.min(100, (r.usd_value / total) * 100) : 0;
              return (
                <tr key={r.coin} className="border-t border-border/50 hover:bg-overlay/50">
                  <td className="px-3 py-2 font-bold text-text">{r.coin}</td>
                  <td className="px-3 py-2 text-right text-text">{qty(r.free)}</td>
                  <td className={`px-3 py-2 text-right ${r.used > 0 ? 'text-warn' : 'text-faint'}`}>{r.used > 0 ? qty(r.used) : '—'}</td>
                  <td className="px-3 py-2 text-right text-text-secondary">{r.usd_value !== null && r.usd_value !== undefined ? val(r.usd_value) : <span className="text-faint" title={`Not valued: ${exchangeLabel} has no spot market to price ${r.coin} in ${ccy}, so it is left out of the estimated total`}>n/a</span>}</td>
                  <td className="px-3 py-2">
                    <div className="h-1 rounded-full bg-border overflow-hidden">
                      <div className="h-full bg-success rounded-full" style={{ width: `${share}%` }} />
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function Settings() {
  const [keys, setKeys] = useState([]);
  const exchanges = useExchanges();
  const [initialLoading, setInitialLoading] = useState(true);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [deletingKey, setDeletingKey] = useState(null);
  const [lastChecked, setLastChecked] = useState(null);

  const [balances, setBalances] = useState({});
  const [fetchingBalanceFor, setFetchingBalanceFor] = useState(null);

  const [keyName, setKeyName] = useState('');
  const [selectedExchange, setSelectedExchange] = useState('okx');
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [isSandbox, setIsSandbox] = useState(true);
  // A key is bound to one market type: spot or perpetual swaps
  const [marketType, setMarketType] = useState('spot');

  const exchangeInfo = exchanges.find(e => e.id === selectedExchange) || exchanges[0];
  const exchangeNames = useMemo(() => Object.fromEntries(exchanges.map(e => [e.id, e.name])), [exchanges]);
  const needsPassphrase = !!exchangeInfo.needs_passphrase;
  const markets = exchangeInfo.markets || { spot: { has_sandbox: !!exchangeInfo.has_sandbox } };
  const marketInfo = markets[marketType] || markets.spot || {};
  const hasSwap = !!markets.swap;
  // Sandbox availability differs per market (Binance: spot testnet ≠ futures testnet)
  const hasSandbox = marketType === 'spot' ? !!exchangeInfo.has_sandbox : !!marketInfo.has_sandbox;

  const [swapModal, setSwapModal] = useState(null);
  const [swapFrom, setSwapFrom] = useState('USDC');
  const [swapTo, setSwapTo] = useState('BTC');
  const [swapAmount, setSwapAmount] = useState('');
  const [amountType, setAmountType] = useState('from');

  const fetchKeys = useCallback(async () => {
    setRefreshing(true);
    try {
      const response = await apiClient.get('/api/keys');
      setKeys(Array.isArray(response.data) ? response.data : []);
      setLastChecked(new Date());
    } catch (err) {
      toast.error(humanizeApiError(err));
    }
    setRefreshing(false);
    setInitialLoading(false);
  }, []);

  useEffect(() => {
    fetchKeys(); // eslint-disable-line react-hooks/set-state-in-effect -- initial data fetch on mount
  }, [fetchKeys]);

  // Exchanges without a testnet can only be added as Live
  useEffect(() => {
    if (!hasSandbox) setIsSandbox(false); // eslint-disable-line react-hooks/set-state-in-effect -- derived from exchange capability
  }, [hasSandbox]);
  // Spot-only exchange selected → the market type falls back to spot
  useEffect(() => {
    if (!hasSwap && marketType !== 'spot') setMarketType('spot'); // eslint-disable-line react-hooks/set-state-in-effect -- derived from exchange capability
  }, [hasSwap, marketType]);

  const handleSave = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      await apiClient.post('/api/keys', {
        name: keyName.trim(),
        exchange: selectedExchange,
        api_key: apiKey.trim(),
        api_secret: apiSecret.trim(),
        passphrase: needsPassphrase ? passphrase : '',
        is_sandbox: hasSandbox ? isSandbox : false,
        market_type: marketType,
      });
      toast.success(`Key '${keyName.trim()}' verified and securely stored.`);
      setKeyName('');
      setApiKey('');
      setApiSecret('');
      setPassphrase('');
      fetchKeys();
    } catch (err) {
      toast.error(humanizeApiError(err, 'An unexpected error occurred.'));
    }
    setLoading(false);
  };

  const handleDeleteClick = async (k) => {
    const linked = k.bots || [];
    if (linked.length) {
      // The server refuses (409) while any bot references the key — a keyless
      // live bot could no longer send exits for its real positions
      await confirmDialog({
        title: 'Key is in use',
        message: `'${k.name}' is linked to ${linked.length} bot${linked.length === 1 ? '' : 's'}: ${linked.map(b => b.name + (b.is_active ? ' (running)' : '')).join(', ')}.\n\nSwitch those bots to another key in the builder, or delete them, before removing this key.`,
        confirmText: 'OK',
        type: 'info',
      });
      return;
    }
    const ok = await confirmDialog({
      title: 'Delete Connection',
      message: `Permanently delete the key '${k.name}'?`,
      confirmText: 'Delete Key',
      type: 'danger',
    });
    if (!ok) return;
    setDeletingKey(k.name);
    try {
      await apiClient.delete(`/api/keys/${encodeURIComponent(k.name)}`);
      setBalances(prev => {
        const newBal = { ...prev };
        delete newBal[k.name];
        return newBal;
      });
      fetchKeys();
      toast.success(`Key '${k.name}' deleted`);
    } catch (err) {
      toast.error(humanizeApiError(err));
    }
    setDeletingKey(null);
  };

  const loadWallet = useCallback(async (kName) => {
    const response = await apiClient.get(`/api/keys/${encodeURIComponent(kName)}/balance`);
    return response.data;
  }, []);

  const handleFetchBalance = async (kName) => {
    if (balances[kName]) {
      setBalances(prev => {
        const newBal = { ...prev };
        delete newBal[kName];
        return newBal;
      });
      return;
    }
    setFetchingBalanceFor(kName);
    try {
      const data = await loadWallet(kName);
      setBalances(prev => ({ ...prev, [kName]: data }));
    } catch (err) {
      toast.error(humanizeApiError(err, `Failed to fetch balance for ${kName}`));
    }
    setFetchingBalanceFor(null);
  };

  // Keep open wallet views current: silently refresh every 12s in the
  // background (no spinner, keep last known values on a failed fetch)
  const openBalanceKeys = Object.keys(balances).sort().join(',');
  useEffect(() => {
    if (!openBalanceKeys) return;
    const keyNames = openBalanceKeys.split(',');
    const t = setInterval(() => {
      keyNames.forEach(async (kName) => {
        try {
          const data = await loadWallet(kName);
          setBalances(prev => (prev[kName] ? { ...prev, [kName]: data } : prev));
        } catch { /* keep last known values */ }
      });
    }, 12000);
    return () => clearInterval(t);
  }, [openBalanceKeys, loadWallet]);

  // One token per opened swap form: a retry of the same intent (network hiccup,
  // double click) returns the first result instead of a second market order
  const swapTokenRef = useRef(null);
  const openSwapModal = async (kName) => {
    swapTokenRef.current = null;
    setSwapModal(kName);
    if (!balances[kName]) {
      try {
        const data = await loadWallet(kName);
        setBalances(prev => ({ ...prev, [kName]: data }));
      } catch { /* silent */ }
    }
  };

  const walletBalancesFor = (kName) => balances[kName]?.balances || null;

  const handleMaxClick = () => {
    const wb = walletBalancesFor(swapModal);
    if (!wb || !wb[swapFrom]) {
      toast.warn(`You don't have any ${swapFrom} in this wallet.`);
      return;
    }
    setAmountType('from');
    setSwapAmount(wb[swapFrom].free);
  };

  // Best-effort public last price for the confirm line; null when unavailable
  const quoteSwap = async (exchange, from, to) => {
    for (const [symbol, invert] of [[`${to}/${from}`, false], [`${from}/${to}`, true]]) {
      try {
        const { data } = await apiClient.get(`/api/data/market-info/${encodeURIComponent(symbol)}`, { params: { exchange } });
        const last = Number(data?.last);
        if (last > 0) return { symbol, last, toPerFrom: invert ? last : 1 / last };
      } catch { /* try the other direction */ }
    }
    return null;
  };

  const executeSwap = async (e) => {
    e.preventDefault();
    const currentWallet = swapModal;
    const amount = parseFloat(swapAmount);
    if (!(amount > 0)) { toast.warn('Enter an amount greater than zero.'); return; }
    const from = swapFrom.trim().toUpperCase();
    const to = swapTo.trim().toUpperCase();
    const key = keys.find(k => k.name === currentWallet);

    setLoading(true);
    const q = await quoteSwap(key?.exchange, from, to);
    setLoading(false);
    const fromQty = amountType === 'from' ? amount : (q ? amount / q.toPerFrom : null);
    const toQty = amountType === 'from' ? (q ? amount * q.toPerFrom : null) : amount;
    const fmt = (v) => (v == null ? '?' : Number(v).toLocaleString(undefined, { maximumFractionDigits: 6 }));
    const ok = await confirmDialog({
      title: key?.is_sandbox ? 'Execute sandbox market swap' : 'Execute real market swap',
      message:
        `Sell ~${fmt(fromQty)} ${from} → receive ~${fmt(toQty)} ${to}` +
        (q ? ` at last ~${fmt(q.last)} (${q.symbol})` : ' (no live quote available — the fill price is whatever the market gives)') +
        `\n\nMarket order on ${key?.exchange?.toUpperCase() || 'the exchange'} via '${currentWallet}'. This is irreversible.`,
      confirmText: 'Place market order',
      type: 'danger',
    });
    if (!ok) return;

    setLoading(true);
    if (!swapTokenRef.current) swapTokenRef.current = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    try {
      await apiClient.post(`/api/keys/${encodeURIComponent(currentWallet)}/swap`, {
        from_asset: from,
        to_asset: to,
        amount,
        amount_type: amountType,
        idempotency_key: swapTokenRef.current,
      });
      setSwapModal(null);
      toast.success('Market order executed. Updating balance…');
      setTimeout(async () => {
        try {
          const data = await loadWallet(currentWallet);
          setBalances(prev => ({ ...prev, [currentWallet]: data }));
        } catch { /* silent */ }
      }, 1500);
    } catch (err) {
      setSwapModal(null);
      toast.error(humanizeApiError(err));
    }
    setLoading(false);
  };

  const connectedCount = keys.filter(k => k.is_active).length;
  const liveKeyCount = keys.filter(k => !k.is_sandbox).length;

  return (
    <PageShell>
      {/* Swap modal — overlay layer (z-200): the confirmDialog (Modal, z-300) must stack above it */}
      {swapModal && (
        <div className="fixed inset-0 z-[200] flex items-center justify-center p-4" role="dialog" aria-modal="true">
          <div className="absolute inset-0 backdrop" onClick={() => setSwapModal(null)} />
          <div className="relative modal-enter terminal-card max-w-md w-full shadow-pop">
            <div className="px-5 py-4 border-b border-border flex justify-between items-center">
              <div>
                <h3 className="text-xs font-bold uppercase tracking-wider text-text">Market Execution</h3>
                <p className="text-muted text-2xs mt-0.5">Routing via: <span className="text-accent font-bold">{swapModal}</span></p>
              </div>
              <button
                onClick={() => setSwapModal(null)}
                title="Close"
                aria-label="Close"
                className="text-muted hover:text-danger transition-colors font-bold"
              >
                &#10005;
              </button>
            </div>

            <form onSubmit={executeSwap} className="p-5 space-y-5">
              <div className="grid grid-cols-2 gap-4">
                <Input label="From Asset (Sell)" mono required value={swapFrom} onChange={e => setSwapFrom(e.target.value.toUpperCase())} placeholder="USDC" />
                <Input label="To Asset (Buy)" mono required value={swapTo} onChange={e => setSwapTo(e.target.value.toUpperCase())} placeholder="SOL" />
              </div>

              <div>
                <div className="flex justify-between items-end mb-1.5">
                  <span className="text-2xs font-bold uppercase tracking-wider text-muted">Trade Size</span>
                  {walletBalancesFor(swapModal)?.[swapFrom] && (
                    <span className="text-3xs text-muted font-num">Avail: {qty(walletBalancesFor(swapModal)[swapFrom].free)} {swapFrom}</span>
                  )}
                </div>
                <div className="flex bg-inset border border-border rounded-md overflow-hidden focus-within:border-accent/70 transition-colors duration-200">
                  <select
                    value={amountType}
                    onChange={e => setAmountType(e.target.value)}
                    className="bg-raised text-muted text-2xs uppercase font-bold px-2.5 py-2 border-r border-border outline-none cursor-pointer hover:text-text"
                  >
                    <option value="from">Spend ({swapFrom})</option>
                    <option value="to">Receive ({swapTo})</option>
                  </select>
                  <input
                    type="number"
                    step="any"
                    required
                    value={swapAmount}
                    onChange={e => setSwapAmount(e.target.value)}
                    className="w-full bg-transparent text-text font-num px-3 py-2 text-xs focus:outline-none placeholder-faint"
                    placeholder="0.00"
                  />
                  <button
                    type="button"
                    onClick={handleMaxClick}
                    title="Use full available balance"
                    className="bg-overlay hover:bg-border text-text text-3xs font-bold uppercase px-3 transition-colors border-l border-border"
                  >
                    MAX
                  </button>
                </div>
              </div>

              <div className="pt-4 border-t border-border">
                <Button type="submit" fullWidth loading={loading}>Execute Order</Button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Exchange Connections */}
      <GlowPanel glowColor="accent">
        <SectionHeader
          title="Exchange Connections"
          subtitle={keys.length
            ? `${connectedCount}/${keys.length} connected · ${liveKeyCount} live · ${keys.length - liveKeyCount} sandbox${lastChecked ? ` · checked ${lastChecked.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : ''}`
            : 'Encrypted API keys stored locally'}
          accentColor="neutral"
          action={
            <Button variant="secondary" size="sm" loading={refreshing} onClick={fetchKeys} title="Re-verify every key against its exchange">
              Test connections
            </Button>
          }
        />

        <div className="mt-5">
        {initialLoading ? (
          <div className="space-y-3">
            <SkeletonCard />
            <SkeletonCard />
          </div>
        ) : keys.length === 0 ? (
          <div className="border border-border border-dashed rounded-lg bg-inset/40">
            <EmptyState
              icon={IconKeyEmpty}
              title="No exchange keys configured"
              description="Add an API key below to enable live and paper trading, balance checks, and market execution. Bots without a key run in forward-test mode."
            />
          </div>
        ) : (
          <div className="space-y-3">
            {keys.map((k, index) => {
              const linked = k.bots || [];
              const runningLinked = linked.filter(b => b.is_active);
              return (
                <div key={k.name} className={`flex flex-col bg-inset/50 p-4 border rounded-lg transition-all duration-200 hover:border-border-strong fade-in-delay-${Math.min(index + 1, 6)} ${k.is_active ? 'border-border' : 'border-danger/30'}`}>
                  <div className="flex items-center justify-between gap-3 flex-wrap">
                    <div className="flex items-center gap-3 min-w-0">
                      <ExchangeAvatar exchange={k.exchange} name={exchangeNames[k.exchange]} />
                      <div className="flex flex-col min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-text font-bold text-sm truncate">{k.name}</span>
                          {k.is_active
                            ? <Badge variant="success" dot>Connected</Badge>
                            : <Badge variant="danger" dot pulse>Error</Badge>}
                          <Badge variant={k.is_sandbox ? 'info' : 'accent'}>{k.is_sandbox ? 'Sandbox' : 'Live'}</Badge>
                          {k.market_type === 'swap' && <Badge variant="warn" title="Perpetual swaps — bots on this key trade with leverage; each pair is linear (settled in the quote) or inverse (settled in the base coin), shown per pair in the builder">Perps</Badge>}
                        </div>
                        <div className="flex items-center gap-2 flex-wrap mt-1 text-2xs text-muted">
                          <span className="uppercase font-bold tracking-wider">{exchangeNames[k.exchange] || k.exchange}</span>
                          {k.latency_ms !== null && k.latency_ms !== undefined && (
                            <span className="font-num text-faint" title="Round-trip time of the last balance check">{k.latency_ms} ms</span>
                          )}
                          {k.created_at && (
                            <span className="font-num text-faint">added {new Date(k.created_at).toLocaleDateString()}</span>
                          )}
                        </div>
                      </div>
                    </div>

                    <div className="flex gap-2 items-center shrink-0">
                      {k.is_active && (
                        <>
                          <Button variant="secondary" size="sm" title="Execute a market swap through this key" onClick={() => openSwapModal(k.name)}>
                            Trade
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            loading={fetchingBalanceFor === k.name}
                            title={balances[k.name] ? 'Hide wallet' : 'Show wallet (auto-refreshes every 12s)'}
                            onClick={() => handleFetchBalance(k.name)}
                          >
                            {balances[k.name] ? 'Hide Wallet' : 'Wallet'}
                          </Button>
                        </>
                      )}
                      <Button variant="danger" size="sm" loading={deletingKey === k.name} title="Delete this connection" onClick={() => handleDeleteClick(k)}>
                        Delete
                      </Button>
                    </div>
                  </div>

                  {!k.is_active && k.error_msg && (
                    <p className="mt-3 text-2xs text-danger bg-danger/[0.06] border border-danger/20 rounded-md px-3 py-2">{k.error_msg}</p>
                  )}

                  {/* Which bots depend on this key */}
                  <div className="mt-3 flex items-center gap-2 flex-wrap">
                    <span className="text-3xs font-bold uppercase tracking-wider text-faint">Used by</span>
                    {linked.length === 0 ? (
                      <span className="text-2xs text-muted">no bots yet — assign it in the builder's bot config node</span>
                    ) : linked.map(b => (
                      <span key={b.name} className="inline-flex items-center gap-1.5 text-3xs font-bold text-text-secondary bg-bg/60 border border-border rounded-sm px-1.5 py-0.5" title={b.is_active ? 'running' : 'stopped'}>
                        <span className={`w-1.5 h-1.5 rounded-full ${b.is_active ? 'bg-success animate-pulse' : 'bg-faint/40'}`} />
                        {b.name}
                        {b.live && <span className="text-accent">live</span>}
                      </span>
                    ))}
                    {runningLinked.length > 0 && <span className="text-3xs text-success font-num">{runningLinked.length} running</span>}
                  </div>

                  {balances[k.name] && (
                    <div className="mt-4 pt-4 border-t border-border/50 fade-in">
                      <WalletPanel wallet={balances[k.name]} exchangeLabel={exchangeNames[k.exchange] || k.exchange} />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        </div>
      </GlowPanel>

      {/* Configure API Key */}
      <GlowPanel>
        <SectionHeader
          title="Add Connection"
          subtitle="Credentials are verified against the exchange, then encrypted at rest (Fernet)"
          accentColor="neutral"
        />
        <div className="grid grid-cols-1 lg:grid-cols-[1fr_300px] gap-6 mt-5">
          <form onSubmit={handleSave} className="grid grid-cols-1 md:grid-cols-2 gap-4 content-start">
            <Select label="Exchange" value={selectedExchange} onChange={e => setSelectedExchange(e.target.value)}>
              {exchanges.map(ex => (
                <option key={ex.id} value={ex.id}>{ex.name}</option>
              ))}
            </Select>
            <Select
              label="Market"
              value={marketType}
              onChange={e => setMarketType(e.target.value)}
              disabled={!hasSwap}
              hint={hasSwap
                ? (marketType === 'swap' ? `Perpetual swaps (linear or inverse, per pair), up to ${marketInfo.max_leverage || 1}× in ApexAlgo. Bots on this key trade swaps only.` : 'Spot wallet. A key is bound to one market — add a second key for perpetuals.')
                : `${exchangeInfo.name}: spot only in ApexAlgo.`}
            >
              <option value="spot">Spot</option>
              {hasSwap && <option value="swap">Perpetual swaps</option>}
            </Select>
            <Input
              label="Connection Name"
              required
              value={keyName}
              onChange={e => setKeyName(e.target.value)}
              placeholder="e.g. Binance main"
              hint="How bots refer to this key in the builder."
            />
            <Input
              label="API Key"
              type="password"
              mono
              required
              value={apiKey}
              onChange={e => setApiKey(e.target.value)}
              placeholder="••••••••••••••••"
              autoComplete="off"
            />
            <Input
              label="Secret Key"
              type="password"
              mono
              required
              value={apiSecret}
              onChange={e => setApiSecret(e.target.value)}
              placeholder="••••••••••••••••"
              autoComplete="off"
            />
            {needsPassphrase && (
              <div className="col-span-1 md:col-span-2">
                <Input
                  label="Passphrase"
                  type="password"
                  mono
                  required
                  value={passphrase}
                  onChange={e => setPassphrase(e.target.value)}
                  placeholder="API Passphrase"
                  hint={`${exchangeInfo.name} keys carry a passphrase you chose when creating the key.`}
                  autoComplete="off"
                />
              </div>
            )}

            <div className="col-span-1 md:col-span-2 flex items-center justify-between gap-4 pt-4 border-t border-border mt-2 flex-wrap">
              <label className={`flex items-center group ${hasSandbox ? 'cursor-pointer' : 'cursor-not-allowed opacity-60'}`} title={hasSandbox ? 'Route through the exchange testnet/demo environment' : `${exchangeInfo.name} has no testnet — this key runs against your real account`}>
                <input
                  type="checkbox"
                  checked={hasSandbox && isSandbox}
                  disabled={!hasSandbox}
                  onChange={e => setIsSandbox(e.target.checked)}
                  className="w-3.5 h-3.5 accent-accent bg-inset border-border rounded-sm cursor-pointer disabled:cursor-not-allowed"
                />
                <span className="ml-2 text-xs text-muted group-hover:text-text transition-colors font-bold uppercase tracking-wider">
                  Sandbox / Testnet
                </span>
                {!hasSandbox && <span className="ml-2 text-3xs text-warn font-bold uppercase">not available on {exchangeInfo.name}</span>}
              </label>
              <Button type="submit" loading={loading}>
                {loading ? 'Verifying…' : `Verify & Save${hasSandbox && isSandbox ? '' : ' (live)'}`}
              </Button>
            </div>
          </form>

          {/* Per-exchange setup guide + safety checklist */}
          <aside className="bg-inset/50 border border-border rounded-lg p-4 space-y-4 self-start">
            <div>
              <p className="text-3xs font-bold uppercase tracking-wider text-muted">Setting up {exchangeInfo.name}</p>
              {exchangeInfo.keys_url ? (
                <a href={exchangeInfo.keys_url} target="_blank" rel="noreferrer noopener" className="text-xs text-info hover:underline break-all mt-1 block">
                  Create an API key on {exchangeInfo.name} ↗
                </a>
              ) : (
                <p className="text-xs text-muted mt-1">Create an API key in your {exchangeInfo.name} account settings.</p>
              )}
              <ul className="mt-2 space-y-1 text-2xs text-text-secondary">
                <li className="flex items-center gap-1.5"><span className="text-success">{IconCheck}</span>Enable <b>read</b> + <b>{marketType === 'swap' ? 'futures/derivatives trade' : 'spot trade'}</b> permissions</li>
                {marketType === 'swap' && marketInfo.note && <li className="flex items-start gap-1.5"><span className="text-info mt-0.5">{IconCheck}</span><span>{marketInfo.note}</span></li>}
                <li className="flex items-center gap-1.5"><span className="text-danger">{IconBlock}</span>Leave <b>withdrawal</b> disabled — the bot never needs it</li>
                <li className="flex items-center gap-1.5"><span className="text-success">{IconCheck}</span>Restrict the key to this machine's IP if the exchange allows it</li>
                {needsPassphrase && <li className="flex items-center gap-1.5"><span className="text-success">{IconCheck}</span>Note the passphrase you set — it is required here</li>}
                {exchangeInfo.sandbox_note && <li className="flex items-start gap-1.5"><span className="text-info mt-0.5">{IconCheck}</span><span>{exchangeInfo.sandbox_note}</span></li>}
                {!hasSandbox && <li className="flex items-start gap-1.5"><span className="text-warn mt-0.5">{IconBlock}</span><span>No testnet{marketType === 'swap' ? ' for perpetuals' : ''}: use forward test in the bot (simulated fills on real prices) before enabling live orders.</span></li>}
              </ul>
            </div>
            <div className="border-t border-border pt-3">
              <p className="text-3xs font-bold uppercase tracking-wider text-muted">Going live safely</p>
              <ul className="mt-2 space-y-1 text-2xs text-text-secondary list-disc list-inside">
                <li>Set <b>max order value</b> in the bot config — required for live orders.</li>
                <li>Start with a small balance; bots size trades from the free balance capped by their capital.</li>
                <li>Keys are stored encrypted and never leave this server except to the exchange.</li>
              </ul>
            </div>
          </aside>
        </div>
      </GlowPanel>
    </PageShell>
  );
}
