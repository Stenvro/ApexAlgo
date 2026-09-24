import React from 'react';
import { Handle, Position } from 'reactflow';
import { useIndicators } from './indicatorConfig';
import { useExchanges, marketCaps } from '../../api/exchanges';
import { DEFAULT_PAIR, parsePairs } from './pairs';

// All known timeframes with display labels
const ALL_TIMEFRAMES = [
  { value: '1m', label: '1 Minute' },
  { value: '3m', label: '3 Minutes' },
  { value: '5m', label: '5 Minutes' },
  { value: '15m', label: '15 Minutes' },
  { value: '30m', label: '30 Minutes' },
  { value: '1h', label: '1 Hour' },
  { value: '2h', label: '2 Hours' },
  { value: '4h', label: '4 Hours' },
  { value: '6h', label: '6 Hours' },
  { value: '12h', label: '12 Hours' },
  { value: '1d', label: '1 Day' },
  { value: '1w', label: '1 Week' },
  { value: '1M', label: '1 Month' },
];

// ==========================================
// CONFIGURATION NODES
// ==========================================

export const BotConfigNode = ({ id, data }) => (
  <div className="bg-raised/90 backdrop-blur-xl border border-purple rounded-xl shadow-lg w-[340px]">
    <div className="bg-purple/10 px-3 py-2 border-b border-purple/30 flex justify-between items-center">
      <span className="font-bold text-purple text-xs uppercase tracking-wider">MAIN CONFIGURATION</span>
      {data.onDelete && <button onClick={() => data.onDelete(id)} className="text-muted hover:text-danger transition-colors" aria-label="Remove main configuration block" title="Remove block">✕</button>}
    </div>
    <div className="p-4 bg-bg/80 rounded-b space-y-4">
      <div>
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Algorithm Name</label>
        <input 
          type="text" 
          className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag focus:border-purple outline-none"
          value={data.botName !== undefined ? data.botName : "Apex Strategy Alpha"}
          onChange={(e) => data.onChange(id, 'botName', e.target.value)}
        />
      </div>
      <div className="flex space-x-2">
        <div className="w-1/2">
            <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Data Interval</label>
            <select className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag focus:border-purple outline-none" value={data.timeframe !== undefined ? data.timeframe : "1m"} onChange={(e) => data.onChange(id, 'timeframe', e.target.value)}>
                {(data.supportedTimeframes
                    ? ALL_TIMEFRAMES.filter(tf => data.supportedTimeframes.includes(tf.value))
                    : ALL_TIMEFRAMES
                ).map(tf => (
                    <option key={tf.value} value={tf.value}>{tf.label}</option>
                ))}
            </select>
        </div>
        <div className="w-1/2">
            <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Max Positions</label>
            <input type="number" className="w-full bg-inset border border-border text-accent text-xs rounded-md p-2 nodrag focus:border-purple outline-none font-num text-center" value={data.maxPositions !== undefined ? data.maxPositions : 1} onChange={(e) => data.onChange(id, 'maxPositions', e.target.value === "" ? "" : parseInt(e.target.value))} />
        </div>
      </div>
      <div className="pt-2 border-t border-border">
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Position Limit Scope</label>
        <select className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag focus:border-purple outline-none" value={data.maxPositionsScope !== undefined ? data.maxPositionsScope : "per_pair"} onChange={(e) => data.onChange(id, 'maxPositionsScope', e.target.value)}>
          <option value="per_pair">Per Pair (up to N layers on each symbol)</option>
          <option value="global">Global (up to N open positions in total)</option>
        </select>
      </div>

      <div className="pt-2 border-t border-border">
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Max New Entries per X Candles (0 = Off)</label>
        <div className="flex space-x-2 items-center">
            <input type="number" placeholder="Max Entries" title="Max Entries" className="w-1/2 bg-inset border border-border text-accent text-xs rounded-md p-2 nodrag focus:border-purple outline-none font-num text-center" value={data.cooldownTrades !== undefined ? data.cooldownTrades : 0} onChange={(e) => data.onChange(id, 'cooldownTrades', e.target.value === "" ? "" : parseInt(e.target.value))} />
            <span className="text-3xs text-muted font-bold uppercase">PER</span>
            <input type="number" placeholder="Candles" title="Amount of Candles" className="w-1/2 bg-inset border border-border text-accent text-xs rounded-md p-2 nodrag focus:border-purple outline-none font-num text-center" value={data.cooldownCandles !== undefined ? data.cooldownCandles : 0} onChange={(e) => data.onChange(id, 'cooldownCandles', e.target.value === "" ? "" : parseInt(e.target.value))} />
        </div>
      </div>
      <div className="pt-2 border-t border-border">
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Max Drawdown % (0 = Off)</label>
        <input type="number" step="0.1" className="w-full bg-inset border border-border text-accent text-xs rounded-md p-2 nodrag focus:border-purple outline-none font-num text-center" value={data.maxDrawdown !== undefined ? data.maxDrawdown : 0} onChange={(e) => data.onChange(id, 'maxDrawdown', e.target.value === "" ? "" : parseFloat(e.target.value))} />
        <span className="text-3xs text-muted block mt-1">Peak-to-trough on the equity curve, checked after the backtest and after every closed trade</span>
      </div>
      <div className="pt-2 border-t border-border">
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Max Capital Loss % (0 = Off)</label>
        <input type="number" step="0.1" min="0" max="99" className="w-full bg-inset border border-border text-accent text-xs rounded-md p-2 nodrag focus:border-purple outline-none font-num text-center" value={data.maxCapitalLoss !== undefined ? data.maxCapitalLoss : 0} onChange={(e) => data.onChange(id, 'maxCapitalLoss', e.target.value === "" ? "" : parseFloat(e.target.value))} />
        <span className="text-3xs text-muted block mt-1">Loss of starting capital, independent of drawdown — never resumes. Required for live bots that block entries.</span>
      </div>
      <div className="pt-2 border-t border-border">
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">On Limit Breach (drawdown or capital loss)</label>
        <select className="w-full bg-inset border border-border text-accent text-xs rounded-md p-2 nodrag focus:border-purple outline-none" value={data.drawdownAction || 'close_all'} onChange={(e) => data.onChange(id, 'drawdownAction', e.target.value)}>
          <option value="close_all">Close all & stop (default)</option>
          <option value="block_entries">Block new entries, keep exits</option>
        </select>
        <span className="text-3xs text-muted block mt-1">{(data.drawdownAction || 'close_all') === 'block_entries' ? 'Drawdown: entries pause until it recovers below half the limit, or the bot has been flat for the cooldown (peak resets). Capital loss: entries stop for good, exits finish, then the bot stops.' : 'Market-closes every open position and stops the bot immediately'}</span>
      </div>
      {(data.drawdownAction || 'close_all') === 'block_entries' && (
        <div className="pt-2 border-t border-border">
          <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Drawdown Cooldown (days)</label>
          <input type="number" step="1" min="0" max="365" className="w-full bg-inset border border-border text-accent text-xs rounded-md p-2 nodrag focus:border-purple outline-none font-num text-center" value={data.drawdownCooldownDays !== undefined ? data.drawdownCooldownDays : 7} onChange={(e) => data.onChange(id, 'drawdownCooldownDays', e.target.value === "" ? "" : parseFloat(e.target.value))} />
          <span className="text-3xs text-muted block mt-1">How long the bot must stay flat before entries resume from a fresh peak. 0 = resume as soon as flat.</span>
        </div>
      )}
      <div className="pt-2 border-t border-border">
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Max Order Value USD (0 = Off)</label>
        <input type="number" step="1" className="w-full bg-inset border border-border text-accent text-xs rounded-md p-2 nodrag focus:border-purple outline-none font-num text-center" value={data.maxOrderValue !== undefined ? data.maxOrderValue : 0} onChange={(e) => data.onChange(id, 'maxOrderValue', e.target.value === "" ? "" : parseFloat(e.target.value))} />
        <span className="text-3xs text-muted block mt-1">Safety guard: rejects live orders exceeding this USD value</span>
      </div>
      <div className="pt-2 border-t border-border">
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Live Allocation % of Wallet</label>
        <input type="number" step="1" min="1" max="100" className="w-full bg-inset border border-border text-accent text-xs rounded-md p-2 nodrag focus:border-purple outline-none font-num text-center" value={data.liveAllocationPct !== undefined ? data.liveAllocationPct : 100} onChange={(e) => data.onChange(id, 'liveAllocationPct', e.target.value === "" ? "" : parseFloat(e.target.value))} />
        <span className="text-3xs text-muted block mt-1">Share of the exchange wallet (quote balance + open positions) this bot may deploy. Split it between bots that share one API key. Entry size % applies to what is still undeployed.</span>
      </div>
      <ExecutionModeSection id={id} data={data} />
    </div>
  </div>
);

// Execution mode + go-live checklist. `data.liveContext` is pushed in by the
// builder: { keyName, exchange, isSandbox, entryFee, marketType, leverage,
// marginMode } — null key = no exchange orders possible, so the "exchange"
// option is disabled rather than silently saving a bot that would fall back
// to forward test at start.
const ExecutionModeSection = ({ id, data }) => {
  const ctx = data.liveContext || {};
  const hasKey = !!ctx.keyName;
  const mode = data.executionMode !== undefined ? data.executionMode : 'paper';
  const wantsExchange = mode === 'exchange';
  const isLiveMoney = wantsExchange && hasKey && !ctx.isSandbox;
  const cap = Number(data.maxOrderValue) || 0;
  const fee = Number(ctx.entryFee);
  const isSwap = ctx.marketType === 'swap';
  const lev = isSwap ? (Number(ctx.leverage) || 1) : 1;
  // Isolated-margin liquidation sits roughly one margin's worth of adverse
  // move away: −100%/leverage on the notional (maintenance margin ignored)
  const liqPct = Math.round(100 / lev);
  const checks = [
    { ok: hasKey, label: hasKey ? `Key "${ctx.keyName}" on ${String(ctx.exchange || '').toUpperCase()} (${ctx.isSandbox ? 'sandbox → paper' : 'real → live'})` : 'Select an API key in the Exchange Routing block' },
    { ok: cap > 0, label: cap > 0 ? `Max order value $${cap} caps every order${isSwap ? ' (notional = margin × leverage)' : ''}` : `Set Max Order Value USD above (required for live orders${isSwap ? '; caps the notional, i.e. margin × leverage' : ''})` },
    { ok: fee > 0, label: fee > 0 ? `Entry fee ${fee}% modelled` : 'Entry fee is 0% — the backtest ignores what the exchange will charge' },
    ...(isSwap ? [{ ok: lev <= 3, label: `Perpetuals ${lev}× ${ctx.marginMode || 'isolated'} · liquidation ≈ −${liqPct}% from entry · funding not modelled` }] : []),
  ];
  return (
    <div className={`pt-2 border-t ${isLiveMoney ? 'border-accent/40' : 'border-border'}`}>
      <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Execution</label>
      <select
        className={`w-full bg-inset border text-xs rounded-md p-2 nodrag outline-none ${isLiveMoney ? 'border-accent text-accent focus:border-accent' : 'border-border text-text focus:border-purple'}`}
        value={mode}
        onChange={(e) => data.onChange(id, 'executionMode', e.target.value)}
      >
        <option value="paper">Forward test — simulated fills, no orders sent</option>
        <option value="exchange" disabled={!hasKey}>
          {hasKey ? (ctx.isSandbox ? 'Exchange orders — paper (sandbox key)' : 'Exchange orders — LIVE, real money') : 'Exchange orders — select an API key first'}
        </option>
      </select>
      {wantsExchange && hasKey ? (
        <ul className="mt-2 space-y-1">
          {checks.map((c, i) => (
            <li key={i} className={`flex items-start gap-1.5 text-3xs ${c.ok ? 'text-muted' : 'text-danger'}`}>
              <span aria-hidden="true" className="shrink-0">{c.ok ? '✓' : '✕'}</span>
              <span>{c.label}</span>
            </li>
          ))}
          {isLiveMoney && <li className="text-3xs text-accent font-bold pt-0.5">Every entry this bot takes is a real market order on {String(ctx.exchange || '').toUpperCase()}.</li>}
        </ul>
      ) : wantsExchange ? (
        <span className="text-3xs text-danger block mt-1">Exchange orders are selected but no API key is set — the bot would start in forward test. Select a key in the Exchange Routing block or switch to forward test.</span>
      ) : (
        <span className="text-3xs text-muted block mt-1">{hasKey ? 'Fills are simulated on live candles with the configured fee and slippage; nothing reaches the exchange.' : 'Without an API key the bot can only forward test.'}</span>
      )}
    </div>
  );
};

export const WhitelistNode = ({ id, data }) => {
  const pairs = parsePairs(data.pairs !== undefined ? data.pairs : DEFAULT_PAIR);
  // { exchange, symbols: string[], known: bool } pushed in by the builder;
  // `known` false = exchange unreachable, so nothing can be flagged
  const market = data.knownSymbols;
  const listed = market?.known ? new Set(market.symbols) : null;
  const unknown = listed ? pairs.filter(p => !listed.has(p)) : [];
  const exch = String(market?.exchange || '').toUpperCase();
  // Perpetual swaps use ccxt's BASE/QUOTE:SETTLE form; a spot pair on a swap
  // bot (or vice versa) is flagged before the validator would reject it
  const isSwap = market?.marketType === 'swap';
  const wrongForm = market ? pairs.filter(p => p.includes(':') !== isSwap) : [];
  return (
    <div className={`bg-raised/90 backdrop-blur-xl border rounded-xl shadow-lg min-w-[260px] max-w-[340px] ${unknown.length ? 'border-danger' : 'border-warn'}`}>
      <div className="bg-warn/10 px-3 py-2 border-b border-warn/30 flex justify-between items-center">
        <span className="font-bold text-warn text-xs uppercase tracking-wider">ASSET WHITELIST</span>
        {data.onDelete && <button onClick={() => data.onDelete(id)} className="text-muted hover:text-danger transition-colors" aria-label="Remove whitelist block">✕</button>}
      </div>
      <div className="p-4 bg-bg/80 rounded-b space-y-2">
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Tradeable Pairs (Comma Separated)</label>
        <textarea
          className={`w-full bg-inset border text-text text-xs rounded-md p-2 nodrag outline-none min-h-[60px] resize-none font-num ${unknown.length ? 'border-danger focus:border-danger' : 'border-border focus:border-warn'}`}
          placeholder={isSwap ? 'BTC/USDT:USDT, ETH/USDT:USDT' : 'BTC/USDT, ETH/USDT, SOL/USDT'}
          value={data.pairs !== undefined ? data.pairs : DEFAULT_PAIR}
          onChange={(e) => data.onChange(id, 'pairs', e.target.value)}
          onBlur={(e) => data.onChange(id, 'pairs', parsePairs(e.target.value).join(', '))}
          aria-invalid={unknown.length > 0}
        />
        {pairs.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {pairs.map(p => {
              const bad = listed && !listed.has(p);
              return (
                <span key={p} title={bad ? `${p} is not listed on ${exch}` : (listed ? `Listed on ${exch}` : undefined)}
                  className={`px-1.5 py-0.5 rounded text-3xs font-num font-bold border ${bad ? 'border-danger/50 bg-danger/10 text-danger' : 'border-border bg-inset text-text'}`}>
                  {bad ? '✕ ' : ''}{p}
                </span>
              );
            })}
          </div>
        )}
        <span className={`text-3xs block ${unknown.length || wrongForm.length ? 'text-danger' : 'text-muted'}`}>
          {wrongForm.length
            ? (isSwap
                ? `${wrongForm.join(', ')}: perpetual swaps use the BASE/QUOTE:SETTLE form (e.g. BTC/USDT:USDT).`
                : `${wrongForm.join(', ')} are perpetual swaps — a spot bot needs BASE/QUOTE pairs (or pick a perps market in the routing block).`)
            : unknown.length
            ? `${unknown.join(', ')} not listed on ${exch} — the bot cannot start with these.`
            : listed
              ? `${pairs.length} ${isSwap ? 'perpetual' : 'pair'}${pairs.length === 1 ? '' : 's'} · all listed on ${exch}${isSwap ? ' perps' : ''}`
              : (market ? `Could not load ${exch} markets — pairs are checked when the bot starts.` : 'Checked against the exchange in the routing block.')}
        </span>
      </div>
    </div>
  );
};

export const BacktestNode = ({ id, data }) => (
  <div className="bg-raised/90 backdrop-blur-xl border border-accent rounded-xl shadow-lg min-w-[280px]">
    <div className="bg-accent/10 px-3 py-2 border-b border-accent/30 flex justify-between items-center">
      <span className="font-bold text-accent text-xs uppercase tracking-wider">BACKTEST ENGINE</span>
      {data.onDelete && <button onClick={() => data.onDelete(id)} className="text-muted hover:text-danger transition-colors" aria-label="Remove backtest block" title="Remove block">✕</button>}
    </div>
    <div className="p-4 bg-bg/80 rounded-b space-y-4">
      <label className="flex items-center cursor-pointer nodrag">
        <input type="checkbox" className="form-checkbox h-4 w-4 text-accent rounded border-border bg-inset focus:ring-0 focus:ring-offset-0" checked={data.runOnStart !== false} onChange={(e) => data.onChange(id, 'runOnStart', e.target.checked)} />
        <span className="ml-3 text-xs text-text font-medium">Run historical backtest on start</span>
      </label>
      <div className="flex space-x-2 pt-2 border-t border-border">
        <div className="w-1/2">
            <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Start Capital</label>
            <input type="number" className="w-full bg-inset border border-border text-accent text-xs rounded-md p-2 nodrag focus:border-accent outline-none font-num text-center" value={data.capital !== undefined ? data.capital : 1000} onChange={(e) => data.onChange(id, 'capital', e.target.value === "" ? "" : parseFloat(e.target.value))} />
        </div>
        <div className="w-1/2">
            <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Candles (Lookback)</label>
            <input type="number" className="w-full bg-inset border border-border text-accent text-xs rounded-md p-2 nodrag focus:border-accent outline-none font-num text-center" value={data.lookback !== undefined ? data.lookback : 150} onChange={(e) => data.onChange(id, 'lookback', e.target.value === "" ? "" : parseInt(e.target.value))} />
        </div>
      </div>
    </div>
  </div>
);

export const ApiKeyNode = ({ id, data }) => {
  const exchanges = useExchanges();
  const selectedKey = data.apiKeyName || '';
  const keyRecord = data.availableKeys?.find(k => k.name === selectedKey);
  const derivedExchange = keyRecord?.exchange || null;
  // A key is bound to one market on the backend, so with a key selected the
  // market is read-only; without one the block picks it (swap only where the
  // registry says the data exchange has perps)
  const exchangeId = derivedExchange || data.dataExchange || 'okx';
  const keyMarket = keyRecord ? (keyRecord.market_type || 'spot') : null;
  const marketType = keyMarket || data.marketType || 'spot';
  const swapCaps = marketCaps(exchangeId, 'swap');
  const hasSwap = !!swapCaps;
  const isSwap = marketType === 'swap';
  const maxLev = Math.max(1, Number(swapCaps?.max_leverage) || 1);
  const leverage = Number(data.leverage) || 1;
  React.useEffect(() => {
    // Exchange or key changed underneath a swap choice the new one cannot serve
    if (!keyMarket && data.marketType === 'swap' && !hasSwap) data.onChange(id, 'marketType', 'spot');
  }, [keyMarket, data.marketType, hasSwap, id]); // eslint-disable-line react-hooks/exhaustive-deps -- data.onChange is stable

  return (
    <div className="bg-raised/90 backdrop-blur-xl border border-info rounded-xl shadow-lg min-w-[260px]">
      <div className="bg-info/10 px-3 py-2 border-b border-info/30 flex justify-between items-center">
        <span className="font-bold text-info text-xs uppercase tracking-wider">EXCHANGE ROUTING</span>
        {data.onDelete && <button onClick={() => data.onDelete(id)} className="text-muted hover:text-danger transition-colors" aria-label="Remove exchange routing block" title="Remove block">✕</button>}
      </div>
      <div className="p-4 bg-bg/80 rounded-b space-y-3">
        <div>
          <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Select API Credentials</label>
          <select className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag focus:border-info outline-none" value={selectedKey} onChange={(e) => data.onChange(id, 'apiKeyName', e.target.value)}>
            <option value="">No key (select exchange below)</option>
            {data.availableKeys?.map(k => (
              <option key={k.name} value={k.name}>{k.name} ({k.is_sandbox ? 'Sandbox' : 'Live'})</option>
            ))}
          </select>
        </div>
        {derivedExchange ? (
          <div className="flex items-center space-x-2 px-1">
            <div className="w-1.5 h-1.5 rounded-full bg-success shadow-[0_0_6px_var(--color-success)]" />
            <span className="text-2xs text-muted uppercase font-bold">Exchange: <span className="text-success">{derivedExchange.toUpperCase()}</span>{isSwap && <span className="text-warn"> · Perps</span>}</span>
          </div>
        ) : (
          <div>
            <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Data Exchange</label>
            <select className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag focus:border-info outline-none" value={data.dataExchange || 'okx'} onChange={(e) => data.onChange(id, 'dataExchange', e.target.value)}>
              {exchanges.map(ex => (
                <option key={ex.id} value={ex.id}>{ex.name}</option>
              ))}
            </select>
          </div>
        )}
        <div>
          <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Market</label>
          <select
            className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag focus:border-info outline-none disabled:opacity-60"
            value={marketType}
            disabled={!!keyMarket || !hasSwap}
            onChange={(e) => data.onChange(id, 'marketType', e.target.value)}
          >
            <option value="spot">Spot</option>
            <option value="swap" disabled={!hasSwap}>Perpetual swaps (USDT/USDC-settled)</option>
          </select>
          <span className="text-3xs text-muted block mt-1">
            {keyMarket
              ? `Follows the key — "${selectedKey}" is a ${keyMarket === 'swap' ? 'perpetuals' : 'spot'} key.`
              : hasSwap ? 'Perps trade the BASE/QUOTE:SETTLE form (e.g. BTC/USDT:USDT).' : `${exchangeId.toUpperCase()} has spot only in ApexAlgo.`}
          </span>
        </div>
        {isSwap && (
          <div className="flex space-x-2">
            <div className="w-1/2">
              <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Leverage</label>
              <input type="number" step="1" min="1" max={maxLev}
                className={`w-full bg-inset border text-xs rounded-md p-2 nodrag outline-none font-num text-center ${leverage > 3 ? 'border-warn text-warn focus:border-warn' : 'border-border text-accent focus:border-info'}`}
                value={data.leverage !== undefined && data.leverage !== '' ? data.leverage : 1}
                onChange={(e) => data.onChange(id, 'leverage', e.target.value === '' ? '' : Math.min(maxLev, Math.max(1, parseInt(e.target.value) || 1)))}
              />
            </div>
            <div className="w-1/2">
              <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Margin mode</label>
              <select className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag focus:border-info outline-none" value={data.marginMode || 'isolated'} onChange={(e) => data.onChange(id, 'marginMode', e.target.value)}>
                <option value="isolated">Isolated</option>
                <option value="cross">Cross</option>
              </select>
            </div>
          </div>
        )}
        {isSwap && (
          <span className="text-3xs text-muted block">
            Up to {maxLev}× on {exchangeId.toUpperCase()}. {leverage > 3 ? `${leverage}× liquidates after a ≈ −${Math.round(100 / leverage)}% move — ` : ''}Position size = margin × leverage; funding payments are not modelled.
          </span>
        )}
      </div>
    </div>
  );
};

// ==========================================
// 2. LOGIC AND DATA NODES
// ==========================================

export const IndicatorNode = ({ id, data }) => {
  const currentIndKey = data.indicator !== undefined ? data.indicator : "rsi";
  const { registry, error } = useIndicators();
  // Unknown = not in the backend registry (e.g. a hand-edited import). The
  // node still renders so the user can pick a supported indicator.
  const indDef = registry?.byMethod[currentIndKey] || null;
  const unknown = !!registry && !indDef;
  const outputs = indDef?.outputs || [];
  const showDropdown = outputs.length > 1;

  const currentParams = data.params || {};

  const handleParamChange = (paramId, value) => {
      const parsed = value === "" ? "" : parseFloat(value);
      const newParams = { ...currentParams, [paramId]: parsed };
      data.onChange(id, 'params', newParams);
  };

  return (
  <div className="bg-raised/90 backdrop-blur-xl border border-border rounded-xl shadow-lg min-w-[250px] hover:border-accent transition-all duration-200 relative">
    <div className="bg-overlay px-3 py-2 flex justify-between items-center">
      <span className="font-bold text-text text-xs uppercase tracking-wider">TECHNICAL INDICATOR</span>
      {data.onDelete && <button onClick={() => data.onDelete(id)} className="text-muted hover:text-danger transition-colors" aria-label="Remove indicator block" title="Remove block">✕</button>}
    </div>
    <div className="p-4 space-y-3 bg-bg/80 rounded-b">
      
      <select className={`w-full bg-inset border text-text text-xs rounded-md p-2 nodrag focus:border-accent outline-none font-semibold ${unknown ? 'border-danger' : 'border-border'}`} value={currentIndKey} onChange={(e) => data.onChange(id, 'indicator', e.target.value)}>
          {!registry && <option value={currentIndKey}>{error ? 'Failed to load indicators' : 'Loading indicators…'}</option>}
          {unknown && <option value={currentIndKey}>Unknown: {currentIndKey}</option>}
          {registry && registry.categories.map(groupName => (
              <optgroup key={groupName} label={groupName}>
                  {registry.groups[groupName].map(ind => (
                      <option key={ind.method} value={ind.method}>{ind.label}</option>
                  ))}
              </optgroup>
          ))}
      </select>

      {unknown && (
          <p className="text-2xs text-danger">Indicator &quot;{currentIndKey}&quot; is not supported by the backend. Pick another one.</p>
      )}

      {indDef && indDef.params.length > 0 && (
          <div className="border-t border-border pt-3 space-y-2">
              {indDef.params.map(p => (
                  <div key={p.id} className="flex items-center space-x-2">
                    <span className="text-2xs text-muted font-bold uppercase flex-1">{p.label}</span>
                    <input 
                        type="number" 
                        step="any"
                        className="w-16 bg-inset border border-border text-accent text-xs rounded-md p-1 nodrag text-right font-num focus:border-accent outline-none" 
                        value={currentParams[p.id] !== undefined ? currentParams[p.id] : p.default} 
                        onChange={(e) => handleParamChange(p.id, e.target.value)} 
                    />
                  </div>
              ))}
          </div>
      )}

      {showDropdown && (
        <div className="border-t border-border pt-3 mt-3 animate-fade-in">
          <label className="text-3xs text-info font-bold uppercase mb-1.5 block">Signal Output (Multi-Line)</label>
          <select className="w-full bg-inset border border-info/50 text-text text-2xs rounded-md p-1.5 focus:border-info outline-none" value={data.outputIdx !== undefined ? data.outputIdx : 0} onChange={(e) => data.onChange(id, 'outputIdx', parseInt(e.target.value))}>
            {outputs.map((lineName, idx) => (
                <option key={idx} value={idx} disabled={indDef.disabled_outputs.includes(idx)}>{lineName} (Idx: {idx}){indDef.disabled_outputs.includes(idx) ? ' — look-ahead, disabled' : ''}</option>
            ))}
          </select>
        </div>
      )}

    </div>
    <Handle type="source" position={Position.Right} style={{ top: '50%' }} className="w-10 h-10 bg-accent border-[4px] border-raised -right-[20px]" />
  </div>
  );
};

export const PriceDataNode = ({ id, data }) => (
  <div className="bg-raised/90 backdrop-blur-xl border border-border rounded-xl shadow-lg min-w-[220px] hover:border-accent transition-all duration-200 relative">
    <div className="bg-overlay/60 px-3 py-2 border-b border-border/50 flex justify-between items-center">
      <span className="font-bold text-text text-xs uppercase tracking-wider">PRICE DATA</span>
      {data.onDelete && <button onClick={() => data.onDelete(id)} className="text-muted hover:text-danger transition-colors" aria-label="Remove price data block" title="Remove block">✕</button>}
    </div>
    <div className="p-4 space-y-3 bg-bg/80 rounded-b">
      <div>
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Price Type</label>
        <select className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag focus:border-accent outline-none font-semibold" value={data.priceType !== undefined ? data.priceType : "close"} onChange={(e) => data.onChange(id, 'priceType', e.target.value)}>
          <option value="open">Open</option>
          <option value="high">High</option>
          <option value="low">Low</option>
          <option value="close">Close</option>
          <option value="volume">Volume</option>
        </select>
      </div>
      <div className="border-t border-border pt-3">
         <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Candle Offset</label>
         <select className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag focus:border-accent outline-none font-semibold" value={data.offset !== undefined ? data.offset : 0} onChange={(e) => data.onChange(id, 'offset', parseInt(e.target.value))}>
            <option value={0}>Current (Live)</option>
            <option value={1}>Previous (Closed)</option>
            <option value={2}>2 Candles Ago</option>
         </select>
      </div>
    </div>
    <Handle type="source" position={Position.Right} style={{ top: '50%' }} className="w-10 h-10 bg-accent border-[4px] border-raised -right-[20px]" />
  </div>
);

export const ConditionNode = ({ id, data }) => (
  <div className="bg-raised/90 backdrop-blur-xl border border-border rounded-xl shadow-lg min-w-[260px] relative">
    <Handle type="target" position={Position.Left} id="left" style={{ top: '38%' }} className="w-10 h-10 bg-info border-[4px] border-raised -left-[20px]" />
    <Handle type="target" position={Position.Left} id="right" style={{ top: '80%' }} className="w-10 h-10 bg-purple border-[4px] border-raised -left-[20px]" />
    
    <div className="bg-overlay/60 px-3 py-2 border-b border-border/50 flex justify-between items-center">
      <span className="font-bold text-text text-xs uppercase tracking-wider">DATA CONDITION</span>
      {data.onDelete && <button onClick={() => data.onDelete(id)} className="text-muted hover:text-danger transition-colors" aria-label="Remove condition block" title="Remove block">✕</button>}
    </div>
    
    <div className="p-4 bg-bg/80 rounded-b flex flex-col space-y-4">
      <div className="flex items-center">
         <span className="text-2xs text-info font-bold uppercase ml-1">Input A (Signal)</span>
      </div>
      <div className="flex justify-center border-y border-border py-2">
        <select className="w-full bg-inset border border-border text-accent text-xs rounded-md p-2 nodrag font-bold focus:border-accent outline-none text-center" value={data.operator !== undefined ? data.operator : ">"} onChange={(e) => data.onChange(id, 'operator', e.target.value)}>
          <option value=">">IS GREATER THAN (&gt;)</option>
          <option value="<">IS LESS THAN (&lt;)</option>
          <option value="==">IS EQUAL TO (==)</option>
          <option value="!=">IS NOT EQUAL (!=)</option>
          <option value=">=">GREATER OR EQUAL (&gt;=)</option>
          <option value="<=">LESS OR EQUAL (&lt;=)</option>
          <option value="cross_above">CROSSES ABOVE</option>
          <option value="cross_below">CROSSES BELOW</option>
          <option value="increasing">IS INCREASING (Up)</option>
          <option value="decreasing">IS DECREASING (Down)</option>
          <option value="increasing_for">INCREASING FOR N BARS</option>
          <option value="decreasing_for">DECREASING FOR N BARS</option>
        </select>
      </div>
      <div className={`flex items-center justify-between transition-opacity ${['increasing', 'decreasing'].includes(data.operator) ? 'opacity-30 pointer-events-none' : 'opacity-100'}`}>
         <span className="text-2xs text-purple font-bold uppercase ml-1">Input B</span>
         <div className="flex items-center space-x-2">
           <span className="text-3xs text-muted font-bold">OR</span>
           <input type="number" placeholder="Static Value" title="Connect a line to Input B or type a static number here." className="w-20 bg-inset border border-border text-text text-xs rounded-md p-1.5 nodrag font-num focus:border-accent outline-none text-center" value={data.rightValue !== undefined ? data.rightValue : ""} onChange={(e) => data.onChange(id, 'rightValue', e.target.value === "" ? "" : parseFloat(e.target.value))} disabled={['increasing', 'decreasing'].includes(data.operator)} />
         </div>
      </div>
    </div>
    
    <Handle type="source" position={Position.Right} style={{ top: '50%' }} className="w-10 h-10 bg-accent border-[4px] border-raised -right-[20px]" />
  </div>
);

export const LogicNode = ({ id, data }) => {
  const isSingleInput = data.logicType === "not";
  return (
    <div className="bg-raised/90 backdrop-blur-xl border border-success rounded-xl shadow-lg min-w-[200px] relative">
      <Handle type="target" position={Position.Left} id="in1" style={{ top: isSingleInput ? '50%' : '35%' }} className="w-10 h-10 bg-muted border-[4px] border-raised -left-[20px]" />
      {!isSingleInput && (
        <Handle type="target" position={Position.Left} id="in2" style={{ top: '65%' }} className="w-10 h-10 bg-muted border-[4px] border-raised -left-[20px]" />
      )}
      <div className="bg-success/10 px-3 py-2 border-b border-success/30 flex justify-between items-center">
        <span className="font-bold text-success text-xs uppercase tracking-wider">LOGIC GATE</span>
        {data.onDelete && <button onClick={() => data.onDelete(id)} className="text-muted hover:text-danger transition-colors" aria-label="Remove logic gate block" title="Remove block">✕</button>}
      </div>
      <div className="p-4 bg-bg/80 rounded-b">
        <select className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag font-bold text-center focus:border-success outline-none" value={data.logicType !== undefined ? data.logicType : "and"} onChange={(e) => data.onChange(id, 'logicType', e.target.value)}>
          <option value="and">AND (Require Both)</option>
          <option value="or">OR (Require Either)</option>
          <option value="xor">XOR (Exclusive OR)</option>
          <option value="nand">NAND (Not AND)</option>
          <option value="nor">NOR (Not OR)</option>
          <option value="not">NOT (Invert Input)</option>
        </select>
      </div>
      <Handle type="source" position={Position.Right} style={{ top: '50%' }} className="w-10 h-10 bg-accent border-[4px] border-raised -right-[20px]" />
    </div>
  );
};

// ==========================================
// 3. RISK MANAGEMENT NODES
// ==========================================

export const StopLossNode = ({ id, data }) => (
  <div className="bg-raised/90 backdrop-blur-xl border border-danger rounded-xl shadow-lg min-w-[280px] relative">
    
    <Handle type="target" position={Position.Left} style={{ top: '50%' }} className="w-10 h-10 bg-danger border-[4px] border-raised -left-[20px]" />
    
    <div className="bg-danger/10 px-3 py-2 border-b border-danger/30 flex justify-between items-center">
      <span className="font-bold text-danger text-xs uppercase tracking-wider">STOP LOSS (RISK)</span>
      <div className="flex space-x-3 items-center">
        <span className="text-3xs text-muted font-num">&larr; IN</span>
        {data.onDelete && <button onClick={() => data.onDelete(id)} className="text-muted hover:text-danger transition-colors" aria-label="Remove stop loss block" title="Remove block">✕</button>}
      </div>
    </div>
    <div className="p-4 bg-bg/80 rounded-b space-y-4">
      <div>
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Trigger Level (Loss)</label>
        <div className="flex space-x-2">
            <select className="w-1/2 bg-inset border border-border text-text text-2xs font-bold rounded-md p-2 nodrag focus:border-danger outline-none" value={data.triggerType !== undefined ? data.triggerType : "percentage"} onChange={(e) => data.onChange(id, 'triggerType', e.target.value)}>
                <option value="percentage">Percentage (%)</option>
                <option value="trailing">Trailing (%)</option>
                <option value="atr">ATR Trailing (x)</option>
                <option value="fixed">Fixed Price</option>
            </select>
            <input type="number" placeholder={data.triggerType === 'atr' ? "Multiplier (e.g. 2.5)" : "Value"} className="w-1/2 bg-inset border border-border text-danger text-xs rounded-md p-2 nodrag font-num focus:border-danger outline-none text-center" value={data.triggerValue !== undefined ? data.triggerValue : ""} onChange={(e) => data.onChange(id, 'triggerValue', e.target.value === "" ? "" : parseFloat(e.target.value))} />
        </div>
      </div>
      <div className="pt-3 border-t border-border">
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Amount to Close</label>
        <div className="flex space-x-2">
            <select className="w-1/2 bg-inset border border-border text-text text-xs rounded-md p-2 nodrag focus:border-danger outline-none" value={data.closeType !== undefined ? data.closeType : "percentage"} onChange={(e) => data.onChange(id, 'closeType', e.target.value)}>
                <option value="percentage">% of Position</option>
                <option value="fixed">Fixed Amount</option>
            </select>
            <input type="number" placeholder="100" className="w-1/2 bg-inset border border-border text-text text-xs rounded-md p-2 nodrag font-num focus:border-danger outline-none text-center" value={data.closeValue !== undefined ? data.closeValue : 100} onChange={(e) => data.onChange(id, 'closeValue', e.target.value === "" ? "" : parseFloat(e.target.value))} />
        </div>
      </div>
    </div>
  </div>
);

export const TakeProfitNode = ({ id, data }) => (
  <div className="bg-raised/90 backdrop-blur-xl border border-success rounded-xl shadow-lg min-w-[280px] relative">
    
    <Handle type="target" position={Position.Left} style={{ top: '50%' }} className="w-10 h-10 bg-success border-[4px] border-raised -left-[20px]" />

    <div className="bg-success/10 px-3 py-2 border-b border-success/30 flex justify-between items-center">
      <span className="font-bold text-success text-xs uppercase tracking-wider">TAKE PROFIT (TARGET)</span>
      <div className="flex space-x-3 items-center">
        <span className="text-3xs text-muted font-num">&larr; IN</span>
        {data.onDelete && <button onClick={() => data.onDelete(id)} className="text-muted hover:text-danger transition-colors" aria-label="Remove take profit block" title="Remove block">✕</button>}
      </div>
    </div>
    <div className="p-4 bg-bg/80 rounded-b space-y-4">
      <div>
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Trigger Level (Profit)</label>
        <div className="flex space-x-2">
            <select className="w-1/2 bg-inset border border-border text-text text-2xs font-bold rounded-md p-2 nodrag focus:border-success outline-none" value={data.triggerType !== undefined ? data.triggerType : "percentage"} onChange={(e) => data.onChange(id, 'triggerType', e.target.value)}>
                <option value="percentage">Percentage (%)</option>
                <option value="trailing">Trailing (%)</option>
                <option value="atr">ATR Trailing (x)</option>
                <option value="fixed">Fixed Price</option>
            </select>
            <input type="number" placeholder={data.triggerType === 'atr' ? "Multiplier (e.g. 2.5)" : "Value"} className="w-1/2 bg-inset border border-border text-success text-xs rounded-md p-2 nodrag font-num focus:border-success outline-none text-center" value={data.triggerValue !== undefined ? data.triggerValue : ""} onChange={(e) => data.onChange(id, 'triggerValue', e.target.value === "" ? "" : parseFloat(e.target.value))} />
        </div>
      </div>
      <div className="pt-3 border-t border-border">
        <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">Amount to Close</label>
        <div className="flex space-x-2">
            <select className="w-1/2 bg-inset border border-border text-text text-xs rounded-md p-2 nodrag focus:border-success outline-none" value={data.closeType !== undefined ? data.closeType : "percentage"} onChange={(e) => data.onChange(id, 'closeType', e.target.value)}>
                <option value="percentage">% of Position</option>
                <option value="fixed">Fixed Amount</option>
            </select>
            <input type="number" placeholder="100" className="w-1/2 bg-inset border border-border text-text text-xs rounded-md p-2 nodrag font-num focus:border-success outline-none text-center" value={data.closeValue !== undefined ? data.closeValue : 100} onChange={(e) => data.onChange(id, 'closeValue', e.target.value === "" ? "" : parseFloat(e.target.value))} />
        </div>
      </div>
    </div>
  </div>
);

// ==========================================
// 4. ACTION NODE
// ==========================================

export const ActionNode = ({ id, data }) => {
  const isBuy = data.actionType === 'buy';
  // CSS var resolves at paint time so the tint follows the active theme
  const color = isBuy ? 'var(--color-success)' : 'var(--color-danger)';
  
  return (
    <div className={`bg-raised/90 backdrop-blur-xl border-2 rounded-xl shadow-lg min-w-[320px]`} style={{ borderColor: color }}>
      
      <div className="px-3 py-2 font-bold text-xs uppercase tracking-wider border-b flex justify-between items-center" style={{ backgroundColor: `color-mix(in srgb, ${color} 6%, transparent)`, color: color, borderColor: `color-mix(in srgb, ${color} 19%, transparent)` }}>
        <span>{isBuy ? 'ORDER ROUTING: LONG ENTRY' : 'ORDER ROUTING: CLOSE POSITION'}</span>
        {data.onDelete && <button onClick={() => data.onDelete(id)} className="text-muted hover:text-danger transition-colors" aria-label="Remove action block" title="Remove block">✕</button>}
      </div>
      
      <div className="p-4 bg-bg/80 rounded-b space-y-4">
         
         <div className="relative border border-border rounded-md p-3">
             <Handle type="target" position={Position.Left} id="logic" className="w-10 h-10 bg-muted border-[4px] border-raised -left-[20px]" style={{ top: '50%' }} />
             <span className="absolute -left-14 top-1/2 -translate-y-1/2 text-3xs font-bold text-muted -rotate-90">LOGIC</span>
             
             <div className="flex space-x-2">
                <div className="w-1/2">
                    <label className="text-3xs text-muted font-bold uppercase mb-1 block">Direction</label>
                    <select className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag font-bold outline-none focus:border-info" style={{ color: color }} value={data.actionType !== undefined ? data.actionType : "buy"} onChange={(e) => data.onChange(id, 'actionType', e.target.value)}>
                        <option value="buy">BUY (Open)</option>
                        <option value="sell">SELL (Close)</option>
                    </select>
                </div>
                <div className="w-1/2">
                    <label className="text-3xs text-muted font-bold uppercase mb-1 block">Order Type</label>
                    <select className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag outline-none focus:border-info" value={data.orderType !== undefined ? data.orderType : "market"} onChange={(e) => data.onChange(id, 'orderType', e.target.value)}>
                        <option value="market">Market</option>
                        <option value="limit">Limit</option>
                    </select>
                </div>
             </div>
         </div>

         <div className="grid grid-cols-2 gap-2">
            <div>
                <label className="text-3xs text-muted font-bold uppercase mb-1 block">Slippage (%)</label>
                <input type="number" step="0.01" className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag outline-none focus:border-info font-num" value={data.slippage !== undefined ? data.slippage : 0.05} onChange={(e) => data.onChange(id, 'slippage', e.target.value === "" ? "" : parseFloat(e.target.value))} />
            </div>
            <div>
                <label className="text-3xs text-muted font-bold uppercase mb-1 block">Trading Fee (%)</label>
                <input type="number" step="0.01" className="w-full bg-inset border border-border text-text text-xs rounded-md p-2 nodrag outline-none focus:border-info font-num" value={data.fee !== undefined ? data.fee : 0.1} onChange={(e) => data.onChange(id, 'fee', e.target.value === "" ? "" : parseFloat(e.target.value))} />
            </div>
         </div>

         <div className="border border-border rounded-md p-3">
             <label className="text-2xs text-muted font-bold uppercase mb-1.5 block">{isBuy ? 'Entry Size' : 'Amount to Close'}</label>
             <div className="flex space-x-2">
                 <select className="w-1/2 bg-inset border border-border text-text text-xs rounded-md p-2 nodrag outline-none focus:border-info" value={data.amountType !== undefined ? data.amountType : "percentage"} onChange={(e) => data.onChange(id, 'amountType', e.target.value)}>
                     <option value="percentage">{isBuy ? '% of Capital' : '% of Position'}</option>
                     <option value="fixed">Fixed Amount</option>
                 </select>
                 <input type="number" placeholder="100" className="w-1/2 bg-inset border border-border text-info text-xs rounded-md p-2 nodrag text-center font-num focus:border-info outline-none" value={data.amountValue !== undefined ? data.amountValue : 100} onChange={(e) => data.onChange(id, 'amountValue', e.target.value === "" ? "" : parseFloat(e.target.value))} />
             </div>
         </div>

         {isBuy && (
             <div className="relative border border-border rounded-md p-3 pt-4 pb-4 mt-2">
                 
                 <Handle type="source" position={Position.Right} id="tp" className="w-10 h-10 bg-success border-[4px] border-raised -right-[20px]" style={{ top: '25%' }} />
                 <span className="absolute right-[13px] top-[25%] -translate-y-1/2 text-3xs font-bold text-success pointer-events-none">TP</span>

                 <Handle type="source" position={Position.Right} id="sl" className="w-10 h-10 bg-danger border-[4px] border-raised -right-[20px]" style={{ top: '75%' }} />
                 <span className="absolute right-[13px] top-[75%] -translate-y-1/2 text-3xs font-bold text-danger pointer-events-none">SL</span>
                 
                 <div className="text-3xs text-muted italic text-center leading-relaxed">
                     Connect Take Profit or Stop Loss blocks to the <span className="text-success font-bold">TP</span> and <span className="text-danger font-bold">SL</span> ports on the right.
                 </div>
             </div>
         )}
      </div>
    </div>
  );
};