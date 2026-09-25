import { useState, useRef, memo, useCallback } from 'react';
import { apiClient } from '../api/client';
import { humanizeApiError } from '../api/errors';
import ExampleLoader from './ExampleLoader';
import PageShell from './ui/PageShell';
import SectionHeader from './ui/SectionHeader';
import Button from './ui/Button';
import Badge from './ui/Badge';
import ModeBadge from './ui/ModeBadge';
import EmptyState from './ui/EmptyState';
import { toast } from './ui/Toast';
import { confirmDialog } from './ui/ConfirmDialog';
import BotConsole from './BotConsole';
import { contractKindOf, symbolParts } from '../utils/money';

/* ── Inline icons (stroke 1.8) ── */
const IconEdit = (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" d="M16.86 4.49a1.9 1.9 0 112.69 2.69L7.5 19.23 4 20l.77-3.5L16.86 4.49z" />
  </svg>
);
const IconExport = (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" d="M12 15V3m0 0L7.5 7.5M12 3l4.5 4.5M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2" />
  </svg>
);
const IconDuplicate = (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
    <rect x="9" y="9" width="11" height="11" rx="2" strokeLinecap="round" strokeLinejoin="round" />
    <path strokeLinecap="round" strokeLinejoin="round" d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
  </svg>
);
const IconBroom = (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" d="M4 4l7.5 7.5M9 13l-5 7h16l-3.5-9.5L9 13z" />
  </svg>
);
const IconTrash = (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" d="M4 7h16M9 7V5a1 1 0 011-1h4a1 1 0 011 1v2m3 0l-.8 12.1A2 2 0 0115.2 21H8.8a2 2 0 01-2-1.9L6 7m4 4v6m4-6v6" />
  </svg>
);
const IconPlay = (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" d="M7 5.5v13l11-6.5-11-6.5z" />
  </svg>
);
const IconStop = (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
    <rect x="6.5" y="6.5" width="11" height="11" rx="1.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);
const IconRestart = (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h5M20 20v-5h-5M5.5 9A7.5 7.5 0 0119 7.5M18.5 15A7.5 7.5 0 015 16.5" />
  </svg>
);
const IconChart = (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" d="M4 19V5m0 14h16M8 15l3-4 3 2 4-6" />
  </svg>
);
const IconBotEmpty = (
  <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
    <rect x="5" y="8" width="14" height="11" rx="2" strokeLinecap="round" strokeLinejoin="round" />
    <path strokeLinecap="round" strokeLinejoin="round" d="M12 8V4m0 0h3M9 13h.01M15 13h.01M9.5 16.5h5" />
  </svg>
);

/* Small icon-button used in the card footer */
function IconButton({ title, onClick, disabled, tone = 'muted', children }) {
  const tones = {
    muted:  'text-muted hover:text-text hover:bg-overlay',
    info:   'text-info hover:bg-info/10',
    warn:   'text-warn hover:bg-warn/10',
    danger: 'text-danger hover:bg-danger/10',
  };
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      onClick={onClick}
      disabled={disabled}
      className={`p-1.5 rounded-md border border-transparent transition-colors duration-150 disabled:opacity-40 disabled:pointer-events-none ${tones[tone] || tones.muted}`}
    >
      {children}
    </button>
  );
}

const PHASE_META = {
  starting:    { label: 'Starting',      cls: 'text-accent bg-accent/10 border-accent/30',    pulse: true },
  fetching:    { label: 'Fetching data', cls: 'text-info bg-info/10 border-info/30',          pulse: true },
  backtesting: { label: 'Backtesting',   cls: 'text-purple bg-purple/10 border-purple/30',    pulse: true },
  live:        { label: 'Monitoring',    cls: 'text-success bg-success/10 border-success/30', pulse: false },
};
const MODE_LABEL = { live: 'live orders', paper: 'paper (sandbox)', forward_test: 'forward test' };

const fmtClock = (iso) => {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
};

/* Engine phase strip: what the bot is doing right now, with progress */
function RuntimeStrip({ bot }) {
  const rt = bot.runtime;
  if (!bot.is_active) return null;
  const meta = PHASE_META[rt?.phase] || { label: 'Running', cls: 'text-success bg-success/10 border-success/30' };
  const pct = rt?.progress?.total ? Math.min(100, Math.round((rt.progress.done / rt.progress.total) * 100)) : null;
  const nextClose = rt?.phase === 'live' ? fmtClock(rt.next_close) : null;
  const symbolPos = rt?.symbol_count > 1 && rt?.symbol_index ? ` (${rt.symbol_index}/${rt.symbol_count})` : '';
  return (
    <div className="px-4 py-2 border-b border-border bg-inset/40">
      <div className="flex items-center gap-2.5 min-w-0">
        <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-sm border text-3xs font-bold uppercase tracking-wider shrink-0 ${meta.cls}`}>
          {meta.pulse && <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" />}
          {meta.label}
          {rt?.phase === 'live' && rt.mode && <span className="font-medium normal-case tracking-normal opacity-80">· {MODE_LABEL[rt.mode] || rt.mode}</span>}
        </span>
        <span className="text-2xs text-text-secondary font-num truncate flex-1" title={rt?.detail}>
          {rt?.detail || 'Engine active'}{symbolPos}
        </span>
        {nextClose && <span className="text-3xs text-faint font-num shrink-0">next candle {nextClose}</span>}
        {pct !== null && <span className="text-3xs text-muted font-num shrink-0">{pct}%</span>}
      </div>
      {pct !== null && (
        <div className="mt-2 h-1 rounded-full bg-border overflow-hidden">
          <div
            className={`h-full rounded-full transition-[width] duration-500 ${rt.phase === 'backtesting' ? 'bg-purple' : 'bg-info'}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
    </div>
  );
}

/* Why the engine stopped this bot on its own (cleared on next start) */
function StopReason({ bot }) {
  const reason = bot.settings?.last_stop_reason;
  if (bot.is_active || !reason) return null;
  return (
    <div className="px-4 py-2 border-b border-warn/30 bg-warn/[0.06] flex items-start gap-2">
      <svg className="w-3.5 h-3.5 text-warn shrink-0 mt-px" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M12 3l9 16H3l9-16z" />
      </svg>
      <p className="text-2xs text-warn leading-snug"><span className="font-bold uppercase tracking-wider mr-1">Stopped by engine</span>{reason}</p>
    </div>
  );
}

const fmtDay = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '?' : d.toLocaleDateString(undefined, { day: '2-digit', month: 'short', year: '2-digit' });
};

/* Two slim rows: (1) how many configurations of this strategy have been
   backtested (on this slice of data / ever), the range the last one walked,
   whether it is locked, and a jump to Analytics — the performance numbers
   themselves live there, not on the card; (2) whether that range has been
   verified against the exchange, with the verify/lock actions */
function BacktestResult({ bot, updateBotConfig, verifyData, verifying }) {
  const sm = bot.last_backtest_summary ?? bot.settings?.last_backtest_summary;
  if (!sm || typeof sm.trades !== 'number') return null;
  const total = Number(sm.variants) || 0;
  const onSlice = Number(sm.variants_on_slice) || 0;
  const locked = !!(bot.settings?.backtest_from && bot.settings?.backtest_to);
  const verified = !!sm.verified_at;
  const restated = Number(sm.restated_candles) || 0;
  const hasRange = !!sm.data_from && !!sm.data_to;
  // Lock only after a verify: the snapshot you freeze should be one you have
  // compared with the exchange. Unlocking is always allowed.
  const canLock = !bot.is_active && hasRange && (locked || verified);
  const counterTitle = onSlice > 0
    ? `Variant #${onSlice} on this slice of data (same pairs, timeframe and range) — ${total} distinct configuration${total === 1 ? '' : 's'} of this strategy backtested in total. Switching pairs or range starts a new slice; the total keeps counting. Reset the bot to start over.`
    : (total > 0 ? `${total} distinct configuration${total === 1 ? '' : 's'} of this strategy have been backtested. Reset the bot to start counting again.` : undefined);
  const lock = () => updateBotConfig(bot.id, bot, { settings: { backtest_from: sm.data_from, backtest_to: sm.data_to } });
  const unlock = () => updateBotConfig(bot.id, bot, { settings: { backtest_from: null, backtest_to: null } });
  const action = "text-3xs font-bold uppercase tracking-wider transition-colors shrink-0";
  const exchange = (bot.settings?.data_exchange || 'exchange').toString();
  const verifiedTime = verified ? new Date(sm.verified_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
  return (
    <div className="px-4 py-2 border-b border-border bg-bg/40 flex flex-col gap-1 text-3xs">
      {/* Row 1: what ran */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2 min-w-0 font-num text-muted" title={counterTitle}>
          <span className="font-bold uppercase tracking-widest shrink-0">
            Backtest{onSlice > 0 && <span className="text-faint"> #{onSlice}</span>}
          </span>
          {total > onSlice && <span className="text-faint shrink-0">· {total} total</span>}
          {hasRange && <span className="truncate">{fmtDay(sm.data_from)} → {fmtDay(sm.data_to)}</span>}
          {locked && (
            <span className="shrink-0 rounded-sm border border-info/40 bg-info/10 px-1 py-px font-bold uppercase tracking-wider text-info"
              title="Every start replays exactly this range of candles, so the result stays reproducible. Unlock to slide the window to the newest data.">
              Locked
            </span>
          )}
        </div>
        <button type="button"
          onClick={() => window.dispatchEvent(new CustomEvent('open-analytics', { detail: { bot: bot.name, mode: 'backtest' } }))}
          className={`${action} text-info hover:text-text ml-auto`}>
          View in Analytics →
        </button>
      </div>
      {/* Row 2: is the data trustworthy, and freeze it */}
      {hasRange && (
        <div className="flex items-center gap-3">
          <span className={`min-w-0 truncate ${restated > 0 ? 'text-warn' : 'text-faint'}`}
            title={!verified
              ? 'The stored candles have not been compared with the exchange yet. Exchanges (Binance most of all) silently restate history.'
              : restated > 0
                ? 'The exchange now reports different values for these candles. The backtest keeps using the local snapshot (reproducible); verify again and accept the exchange data to overwrite it.'
                : 'The stored candles in this range match what the exchange reports.'}>
            {verifying ? 'Verifying against exchange…'
              : !verified ? 'Not verified against exchange'
              : restated > 0 ? `${exchange} restated ${restated} candle${restated === 1 ? '' : 's'} · local snapshot kept · ${verifiedTime}`
              : `Matches ${exchange} · verified ${verifiedTime}`}
          </span>
          <div className="flex items-center gap-3 ml-auto shrink-0">
            {!verifying && (
              <button type="button" onClick={() => verifyData(bot)}
                title="Re-fetch this range from the exchange and compare it with the stored candles. Nothing is changed unless you accept the exchange data."
                className={`${action} text-muted hover:text-text`}>Verify data</button>
            )}
            {canLock && (locked
              ? <button type="button" onClick={unlock} title="Unlock: the next start walks the newest candles again"
                  className={`${action} text-muted hover:text-text`}>Unlock</button>
              : <button type="button" onClick={lock} title="Lock this range: every next start replays exactly these candles (reproducible result)"
                  className={`${action} text-muted hover:text-text`}>Lock range</button>)}
          </div>
        </div>
      )}
      {sm.data_changed === true && (
        <p className="text-warn leading-snug"
          title="The raw candles in this range hash differently than in the previous run on the same slice — a re-download, gap repair or an accepted exchange restatement changed them. The two results are not directly comparable.">
          <span className="font-bold uppercase tracking-wider mr-1">Data changed</span>
          historical candles differ from the previous run on this slice
        </p>
      )}
    </div>
  );
}

const runtimeKey = (rt) => {
  if (!rt) return '';
  const { updated_at: _u, last_tick_at: _t, ...rest } = rt;
  return JSON.stringify(rest);
};

const BotCard = memo(function BotCard({ bot, index, busyAction, togglingBot, openConsoles, clearSignals, toggleBotState, restartBot, handleExport, handleDuplicate, handleClearCacheClick, handleDeleteClick, updateBotConfig, toggleConsole, verifyData, verifyingBot }) {
  const isBacktestOn     = bot.settings?.backtest_on_start === true;
  const isApiExecutionOn = bot.settings?.api_execution === true;
  const hasApiKey        = !!bot.settings?.api_key_name;
  const consoleOpen      = openConsoles[bot.id] ?? false;
  const isToggling       = togglingBot === bot.id;

  const assignedPairs = Array.isArray(bot.settings?.symbols)
    ? bot.settings.symbols
    : (bot.settings?.symbol ? [bot.settings.symbol] : []);
  const visiblePairs = assignedPairs.slice(0, 3);
  const extraPairs   = assignedPairs.length - visiblePairs.length;
  return (
    <div
      className={`terminal-card flex flex-col overflow-hidden transition-all duration-300 hover:border-border-strong ${
        bot.is_active ? 'border-success/30' : ''
      } fade-in-delay-${Math.min(index + 1, 6)}`}
    >
      {/* ── Card Header: status hierarchy + primary action ── */}
      <div className="px-4 py-3 border-b border-border flex justify-between items-start gap-3">
        <div className="flex flex-col min-w-0">
          <div className="flex items-center gap-2.5 flex-wrap">
            <h3 className="text-text font-bold text-sm tracking-wide truncate">{bot.name}</h3>
            {bot.is_active
              ? <Badge variant="success" dot pulse>Running</Badge>
              : <Badge variant="neutral" dot>Stopped</Badge>}
            <ModeBadge mode={bot.execution_mode || (isApiExecutionOn ? 'live' : 'forward_test')} />
            {isBacktestOn && <Badge variant="neutral">+ Backtest</Badge>}
            {bot.settings?.market_type === 'swap' && (() => {
              // Inverse pairs (settle = base) keep margin and PnL in the base coin
              const inverse = assignedPairs.filter(p => contractKindOf(p) === 'inverse');
              const bases = [...new Set(inverse.map(p => symbolParts(p).base).filter(Boolean))];
              return (
                <Badge variant="warn" title={`Perpetual swaps at ${Number(bot.settings?.leverage) || 1}× ${bot.settings?.margin_mode || 'isolated'} margin — liquidation modelled in backtest and forward; funding not modelled${inverse.length ? `. Inverse: margin and PnL in ${bases.join('/')}` : ''}`}>
                  Perps {Number(bot.settings?.leverage) || 1}×{inverse.length ? ' · inverse' : ''}
                </Badge>
              );
            })()}
          </div>

          {/* Metrics row */}
          <div className="flex items-center gap-4 mt-2.5 flex-wrap">
            <div className="flex flex-col">
              <span className="text-3xs font-bold uppercase tracking-wider text-faint">Timeframe</span>
              <span className="text-xs font-num font-bold text-accent">{bot.settings?.timeframe || 'N/A'}</span>
            </div>
            <div className="flex flex-col">
              <span className="text-3xs font-bold uppercase tracking-wider text-faint">Pairs</span>
              <span className="text-xs font-num font-bold text-text">{assignedPairs.length}</span>
            </div>
            <div className="flex flex-col min-w-0" title={assignedPairs.join(', ')}>
              <span className="text-3xs font-bold uppercase tracking-wider text-faint">Whitelist</span>
              <span className="flex items-center gap-1 flex-wrap">
                {visiblePairs.length === 0 && <span className="text-2xs text-faint">—</span>}
                {visiblePairs.map(pair => (
                  <span key={pair} className="text-3xs font-num font-bold text-text-secondary bg-inset border border-border rounded-sm px-1.5 py-0.5">
                    {pair}
                  </span>
                ))}
                {extraPairs > 0 && (
                  <span className="text-3xs font-num text-muted">+{extraPairs}</span>
                )}
              </span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {bot.is_active && (
            <IconButton title="Restart (stop + fresh start)" onClick={() => restartBot(bot.id)} disabled={isToggling}>
              {IconRestart}
            </IconButton>
          )}
          <Button
            variant={bot.is_active ? 'danger' : 'success'}
            size="md"
            loading={isToggling}
            icon={bot.is_active ? IconStop : IconPlay}
            onClick={() => toggleBotState(bot.id, bot.is_active)}
            title={bot.is_active ? 'Stop this bot (safe at any point — a startup in progress is aborted)' : 'Start the trading engine'}
          >
            {bot.is_active ? 'Stop' : 'Start'}
          </Button>
        </div>
      </div>

      <RuntimeStrip bot={bot} />
      <StopReason bot={bot} />
      <BacktestResult bot={bot} updateBotConfig={updateBotConfig} verifyData={verifyData} verifying={verifyingBot === bot.id} />

      {/* ── Card Body ── */}
      <div className="px-4 py-3 flex-1 flex flex-col space-y-4">

        {/* Environment Routing */}
        <div className="flex flex-col space-y-2">
          <div className="flex justify-between items-end">
            <span className="text-3xs font-bold text-muted uppercase tracking-wider">Environment Routing</span>
            {!hasApiKey && <span className="text-3xs font-bold uppercase text-danger">No API Key Linked</span>}
          </div>
          <div className="flex bg-inset rounded-md border border-border overflow-hidden">
            <button
              disabled={bot.is_active}
              onClick={() => updateBotConfig(bot.id, bot, { settings: { api_execution: false } })}
              title="Forward test: simulate fills locally on live candles without touching the exchange."
              className={`flex-1 py-2 text-3xs font-bold uppercase transition-all duration-200 disabled:opacity-50 ${!isApiExecutionOn ? 'bg-purple/10 text-purple' : 'text-muted hover:text-text hover:bg-raised'}`}
            >
              Forward test
            </button>
            <button
              disabled={bot.is_active || !hasApiKey}
              onClick={() => updateBotConfig(bot.id, bot, { settings: { api_execution: true } })}
              title={!hasApiKey ? 'Assign an API key to route orders (paper on a sandbox key, live otherwise).' : 'Route orders through the API key: paper on a sandbox key, live (real money) otherwise.'}
              className={`flex-1 py-2 text-3xs font-bold uppercase transition-all duration-200 border-l border-border disabled:opacity-50 ${isApiExecutionOn ? 'bg-accent/10 text-accent' : 'text-muted hover:text-text hover:bg-raised'}`}
            >
              Exchange orders
            </button>
          </div>
        </div>

        {/* Initialization Protocol */}
        <div className="flex flex-col space-y-2 border-t border-border pt-4">
          <span className="text-3xs font-bold text-muted uppercase tracking-wider">Initialization Protocol</span>
          <label className={`flex items-center p-3 rounded-md border transition-all duration-200 ${bot.is_active ? 'opacity-50 pointer-events-none cursor-not-allowed' : 'cursor-pointer hover:border-border-strong'} ${isBacktestOn ? 'bg-success/5 border-success/30' : 'bg-inset border-border'}`}>
            <input
              type="checkbox"
              disabled={bot.is_active}
              checked={isBacktestOn}
              onChange={(e) => updateBotConfig(bot.id, bot, { settings: { backtest_on_start: e.target.checked } })}
              className="form-checkbox h-3.5 w-3.5 accent-success rounded-sm cursor-pointer"
            />
            <div className="ml-3 flex flex-col">
              <span className={`text-xs font-bold uppercase tracking-wider ${isBacktestOn ? 'text-success' : 'text-text'}`}>Run Historical Backtest</span>
              <span className="text-3xs text-muted mt-0.5">Process past data before executing live. Previous backtest results are cleared automatically on every run.</span>
            </div>
          </label>
        </div>
      </div>

      {/* ── Console Toggle Bar ── */}
      <button
        onClick={() => toggleConsole(bot.id)}
        title={consoleOpen ? 'Hide console output' : 'Show console output'}
        className="w-full px-4 py-2 border-t border-border bg-bg/60 flex justify-between items-center hover:bg-bg transition-colors group"
      >
        <span className="text-3xs font-bold uppercase tracking-widest text-muted group-hover:text-text transition-colors">
          Console
        </span>
        <ChevronIcon open={consoleOpen} />
      </button>

      {/* ── Console Panel ── */}
      <div
        className={`overflow-hidden transition-all duration-300 ease-in-out ${
          consoleOpen ? 'max-h-56 opacity-100' : 'max-h-0 opacity-0'
        }`}
      >
        <BotConsole botName={bot.name} isOpen={consoleOpen} clearSignal={clearSignals[bot.name] || 0} />
      </div>

      {/* ── Card Footer: icon actions ── */}
      <div className="px-4 py-2 bg-bg/50 border-t border-border flex justify-between items-center">
        <span className="text-3xs font-bold text-faint uppercase tracking-wider font-num">ID {bot.id}</span>
        <div className="flex items-center gap-0.5">
          <IconButton
            title="Edit strategy in the builder"
            tone="info"
            disabled={bot.is_active}
            onClick={() => window.dispatchEvent(new CustomEvent('open-builder', { detail: bot }))}
          >
            {IconEdit}
          </IconButton>
          <IconButton title="Open charts for this bot's pairs" tone="info" onClick={() => window.dispatchEvent(new CustomEvent('open-bot-chart', { detail: bot }))}>
            {IconChart}
          </IconButton>
          <IconButton title="Export bot as .apex.json" onClick={() => handleExport(bot)}>
            {IconExport}
          </IconButton>
          <IconButton title="Duplicate bot" disabled={bot.is_active} onClick={() => handleDuplicate(bot)}>
            {IconDuplicate}
          </IconButton>
          <div className="w-px h-3.5 bg-border mx-1" />
          <IconButton
            title="Reset bot: clear signals, console, backtest/forward-test trades and drawdown state (paper/live trades are kept)"
            tone="warn"
            disabled={bot.is_active || !!busyAction}
            onClick={() => handleClearCacheClick(bot)}
          >
            {IconBroom}
          </IconButton>
          <IconButton
            title="Delete bot permanently"
            tone="danger"
            disabled={bot.is_active || !!busyAction}
            onClick={() => handleDeleteClick(bot.id, bot.name)}
          >
            {IconTrash}
          </IconButton>
        </div>
      </div>

    </div>
  );
}, (prev, next) =>
  prev.bot.id === next.bot.id &&
  prev.bot.is_active === next.bot.is_active &&
  prev.bot.name === next.bot.name &&
  prev.bot.execution_mode === next.bot.execution_mode &&
  prev.busyAction === next.busyAction &&
  (prev.togglingBot === prev.bot.id) === (next.togglingBot === next.bot.id) &&
  (prev.verifyingBot === prev.bot.id) === (next.verifyingBot === next.bot.id) &&
  prev.openConsoles[prev.bot.id] === next.openConsoles[next.bot.id] &&
  prev.clearSignals[prev.bot.name] === next.clearSignals[next.bot.name] &&
  // Runtime timestamps tick on every poll but are never rendered — strip
  // them so an idle live bot does not re-render its card every 15 s
  runtimeKey(prev.bot.runtime) === runtimeKey(next.bot.runtime) &&
  JSON.stringify(prev.bot.settings) === JSON.stringify(next.bot.settings) &&
  JSON.stringify(prev.bot.last_backtest_summary) === JSON.stringify(next.bot.last_backtest_summary)
);

function ChevronIcon({ open }) {
  return (
    <svg
      className={`w-3 h-3 text-muted transition-transform duration-300 ${open ? 'rotate-180' : ''}`}
      fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
    </svg>
  );
}

const MODE_WORD = { live: 'Live', paper: 'Paper', forward_test: 'Forward test' };

/**
 * The stop endpoints refuse (409) while a bot holds open forward/paper/live
 * positions. Turn that refusal into an explicit choice; resolves to the
 * `close_positions` value to retry with, or null when the user cancels.
 */
async function askAboutOpenPositions(detail, { bulk = false } = {}) {
  const positions = detail?.open_positions || [];
  const real = positions.filter((p) => p.mode === 'live' || p.mode === 'paper').length;
  const lines = positions.map((p) =>
    `• ${bulk && p.bot_name ? `${p.bot_name} — ` : ''}${MODE_WORD[p.mode] || p.mode} ${p.symbol}: ${p.amount} @ ${p.entry_price}`,
  );
  const choice = await confirmDialog({
    title: bulk ? 'Bots hold open positions' : 'Bot holds open positions',
    message:
      `${lines.join('\n')}\n\n` +
      (real
        ? `${real} of these ${real === 1 ? 'is a real position' : 'are real positions'} on the exchange. `
        : '') +
      'Close them at market now (even at a loss), or stop and leave them open? Open positions of a stopped bot are unmanaged: no stop-loss or take-profit will fire.',
    confirmText: 'Close at market & stop',
    secondaryText: 'Stop, leave open',
    cancelText: 'Cancel',
    type: real ? 'danger' : 'warning',
  });
  if (choice === true) return true;
  if (choice === 'secondary') return false;
  return null;
}

/** Retry a stop request with the user's choice after a 409; rethrows anything else. */
async function postStop(url, { bulk = false } = {}) {
  try {
    return await apiClient.post(url, null);
  } catch (err) {
    if (err.response?.status !== 409 || !Array.isArray(err.response.data?.detail?.open_positions)) throw err;
    const close = await askAboutOpenPositions(err.response.data.detail, { bulk });
    if (close === null) return null;
    return apiClient.post(url, null, { params: { close_positions: close } });
  }
}

function describeStop(data) {
  const closed = data?.closed_positions?.length || 0;
  const open = data?.unmanaged_positions?.length || 0;
  if (closed) return `${closed} position${closed === 1 ? '' : 's'} closed at market`;
  if (open) return `${open} position${open === 1 ? '' : 's'} left open — unmanaged`;
  return '';
}

export default function BotManagerUI({ bots = [], refetchBots, backendOk = true }) {
  const [openConsoles, setOpenConsoles] = useState({});
  const [busyAction, setBusyAction]     = useState(null);  // 'delete:ID' or 'wipe:name'
  const [togglingBot, setTogglingBot]   = useState(null);  // bot id being started/stopped
  const [verifyingBot, setVerifyingBot] = useState(null);  // bot id whose candles are being verified
  const [clearSignals, setClearSignals] = useState({});
  const fileInputRef                    = useRef(null);

  const toggleBotState = useCallback(async (botId, isCurrentlyActive) => {
    setTogglingBot(botId);
    try {
      if (isCurrentlyActive) {
        const res = await postStop(`/api/bots/${botId}/stop`);
        if (res) {
          refetchBots();
          const extra = describeStop(res.data);
          (extra.includes('unmanaged') ? toast.warn : toast.success)(extra ? `Bot stopped — ${extra}` : 'Bot stopped');
        }
      } else {
        await apiClient.post(`/api/bots/${botId}/start`);
        refetchBots();
        toast.success('Engine started');
      }
    } catch (err) {
      // A failed market close still stops the bot; make sure the card reflects that
      refetchBots();
      toast.error(humanizeApiError(err, 'Failed to toggle bot state.'));
    }
    setTogglingBot(null);
  }, [refetchBots]);

  const restartBot = useCallback(async (botId) => {
    setTogglingBot(botId);
    try {
      await apiClient.post(`/api/bots/${botId}/restart`);
      refetchBots();
      toast.success('Restarting — previous run aborted, fresh backfill queued');
    } catch (err) {
      toast.error(humanizeApiError(err, 'Failed to restart bot.'));
    }
    setTogglingBot(null);
  }, [refetchBots]);

  const [bulkBusy, setBulkBusy] = useState(null); // 'start' | 'stop'
  const startAll = useCallback(async () => {
    setBulkBusy('start');
    try {
      const res = await apiClient.post('/api/bots/bulk/start', null);
      refetchBots();
      const n = res.data?.started?.length || 0;
      toast.success(n ? `${n} bot${n === 1 ? '' : 's'} started — they backfill in parallel` : 'All bots are already running');
    } catch (err) {
      toast.error(humanizeApiError(err, 'Failed to start bots.'));
    }
    setBulkBusy(null);
  }, [refetchBots]);

  const stopAll = useCallback(async () => {
    // Open positions are handled by the server's 409 → explicit-choice flow in postStop
    const ok = await confirmDialog({
      title: 'Stop all bots',
      message: 'Stop every running bot? Startups in progress are aborted.',
      confirmText: 'Stop all',
      type: 'warning',
    });
    if (!ok) return;
    setBulkBusy('stop');
    try {
      const res = await postStop('/api/bots/bulk/stop', { bulk: true });
      if (res) {
        refetchBots();
        const n = res.data?.stopped?.length || 0;
        const extra = describeStop(res.data);
        (extra.includes('unmanaged') ? toast.warn : toast.success)(
          n ? `${n} bot${n === 1 ? '' : 's'} stopped${extra ? ` — ${extra}` : ''}` : 'No running bots',
        );
      }
    } catch (err) {
      refetchBots();
      toast.error(humanizeApiError(err, 'Failed to stop bots.'));
    }
    setBulkBusy(null);
  }, [refetchBots]);

  const handleDeleteClick = useCallback(async (botId, botName) => {
    if (busyAction) return;
    const ok = await confirmDialog({
      title: 'Delete Algorithm',
      message: `Deleting '${botName}' permanently removes its configuration, signals, trades and orders. Any open paper/live positions are market-closed on the exchange first — even at a loss. This cannot be undone.`,
      confirmText: 'Delete',
      type: 'danger',
    });
    if (!ok) return;
    setBusyAction(`delete:${botId}`);
    try {
      await apiClient.delete(`/api/bots/${botId}`);
      refetchBots();
      toast.success(`'${botName}' deleted`);
    } catch (err) {
      toast.error(humanizeApiError(err, 'Failed to delete bot.'));
    }
    setBusyAction(null);
  }, [busyAction, refetchBots]);

  const handleClearCacheClick = useCallback(async (bot) => {
    if (busyAction) return;
    const ok = await confirmDialog({
      title: 'Reset Bot',
      message: `Reset '${bot.name}' to a clean slate? This clears its signals, console logs, backtest and forward-test trades, drawdown state and the live-capital snapshot. Paper/live trades are kept. The next start runs a fresh backtest.`,
      confirmText: 'Reset',
      type: 'warning',
    });
    if (!ok) return;
    setBusyAction(`wipe:${bot.name}`);
    try {
      await apiClient.delete(`/api/bots/console/cache?bot_name=${encodeURIComponent(bot.name)}`);
      refetchBots();
      setClearSignals(prev => ({ ...prev, [bot.name]: (prev[bot.name] || 0) + 1 }));
      toast.success(`'${bot.name}' reset — next start runs a fresh backtest`);
    } catch {
      toast.error('Failed to clear cache.');
    }
    setBusyAction(null);
  }, [busyAction, refetchBots]);

  const updateBotConfig = useCallback(async (botId, currentBot, updates) => {
    try {
      await apiClient.put(`/api/bots/${botId}`, updates);
      refetchBots();
    } catch {
      toast.error('Failed to update bot configuration.');
      refetchBots();
    }
  }, [refetchBots]);

  /* Verify the last backtest range against the exchange. The local snapshot
     is never touched unless the user explicitly accepts the exchange data */
  const verifyData = useCallback(async (bot) => {
    setVerifyingBot(bot.id);
    try {
      const { data } = await apiClient.post(`/api/bots/${bot.id}/verify-data`);
      const n = data.restated || 0;
      if (n === 0) {
        toast.success(`'${bot.name}': stored candles match ${data.exchange}${data.missing_local ? ` (${data.missing_local} candles missing locally)` : ''}`);
        refetchBots();
        return;
      }
      refetchBots();
      const accept = await confirmDialog({
        title: 'Exchange restated candles',
        message: `${data.exchange} now reports different values for ${n} candle${n === 1 ? '' : 's'} in ${data.from?.slice(0, 10)} → ${data.to?.slice(0, 10)}. Keep the local snapshot (backtest stays reproducible) or overwrite it with the exchange data (the next run on this slice will be flagged as "data changed")?`,
        confirmText: 'Accept exchange data',
        cancelText: 'Keep local snapshot',
        type: 'warning',
      });
      if (!accept) return;
      setVerifyingBot(bot.id);
      const res = await apiClient.post(`/api/bots/${bot.id}/verify-data`, null, { params: { accept: true } });
      toast.success(`'${bot.name}': ${res.data.accepted} candle${res.data.accepted === 1 ? '' : 's'} overwritten with ${res.data.exchange} data`);
      refetchBots();
    } catch (err) {
      toast.error(humanizeApiError(err, 'Failed to verify candles against the exchange.'));
    } finally {
      setVerifyingBot(null);
    }
  }, [refetchBots]);

  const handleExport = useCallback(async (bot) => {
    try {
      const res = await apiClient.get(`/api/bots/${bot.id}/export`, { responseType: 'blob' });
      const url = URL.createObjectURL(res.data);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${bot.name.replace(/\s+/g, '_')}.apex.json`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success(`'${bot.name}' exported`);
    } catch {
      toast.error('Failed to export bot.');
    }
  }, []);

  const handleImportFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    let payload;
    try {
      payload = JSON.parse(await file.text());
    } catch {
      toast.error('Invalid bot file. The file may be corrupted or from an incompatible version.');
      if (fileInputRef.current) fileInputRef.current.value = '';
      return;
    }
    try {
      await apiClient.post('/api/bots/import', payload);
      refetchBots();
      toast.success(`'${payload?.bot?.name || 'Bot'}' imported successfully`);
    } catch (err) {
      toast.error(humanizeApiError(err, 'Invalid bot file. The file may be corrupted or from an incompatible version.'));
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleDuplicate = useCallback(async (bot) => {
    try {
      await apiClient.post(`/api/bots/${bot.id}/duplicate`);
      refetchBots();
      toast.success(`'${bot.name}' duplicated`);
    } catch (err) {
      toast.error(humanizeApiError(err, 'Failed to duplicate bot.'));
    }
  }, [refetchBots]);

  const toggleConsole = useCallback((botId) => {
    setOpenConsoles(prev => ({ ...prev, [botId]: !prev[botId] }));
  }, []);

  const runningCount  = bots.filter(b => b.is_active).length;
  const startingCount = bots.filter(b => b.is_active && ['starting', 'fetching', 'backtesting'].includes(b.runtime?.phase)).length;
  const liveCount     = bots.filter(b => b.is_active && b.settings?.api_execution).length;

  return (
    <PageShell>
      <input
        type="file"
        ref={fileInputRef}
        className="hidden"
        accept=".json,.apex.json"
        onChange={handleImportFile}
      />

      <SectionHeader
        title="Trading Algorithms"
        subtitle={runningCount
          ? `${runningCount} of ${bots.length} running${startingCount ? ` · ${startingCount} starting up` : ''}${liveCount ? ` · ${liveCount} on live orders` : ''}`
          : 'Manage, configure, and deploy automated strategies'}
        accentColor="neutral"
        action={
          <div className="flex flex-wrap items-center gap-2.5">
            {bots.length > 1 && (
              <div className="flex items-center rounded-md border border-border overflow-hidden">
                <Button variant="ghost" size="md" icon={IconPlay} loading={bulkBusy === 'start'} disabled={!!bulkBusy || runningCount === bots.length} onClick={startAll} title="Start every stopped bot — they backfill in parallel, no waiting">
                  Start all
                </Button>
                <div className="w-px h-5 bg-border" />
                <Button variant="ghost" size="md" icon={IconStop} loading={bulkBusy === 'stop'} disabled={!!bulkBusy || runningCount === 0} onClick={stopAll} title="Stop every running bot">
                  Stop all
                </Button>
              </div>
            )}
            {bots.length > 0 && (
              <ExampleLoader onImported={refetchBots} size="md" label="Examples" align="right"
                existingNames={bots.map(b => b.name)} />
            )}
            <Button
              variant="secondary"
              size="md"
              icon={IconExport}
              title="Import a bot from an .apex.json file"
              onClick={() => fileInputRef.current?.click()}
            >
              Import
            </Button>
            <Button
              variant="primary"
              size="md"
              onClick={() => window.dispatchEvent(new CustomEvent('open-builder'))}
            >
              + New Algorithm
            </Button>
          </div>
        }
      />

      {!backendOk && (
        <div className="p-3 bg-warn/10 border border-warn/40 text-warn text-xs rounded-md flex items-center gap-2.5 fade-in">
          <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M12 3l9 16H3l9-16z" />
          </svg>
          <span>Backend unreachable — retrying… {bots.length > 0 ? 'Showing the last known bot list.' : ''}</span>
        </div>
      )}

      {bots.length === 0 ? (
        <div className="terminal-card border-dashed">
          {!backendOk ? (
            <EmptyState
              icon={IconBotEmpty}
              title="Backend unreachable"
              description="Your strategies cannot be loaded right now. The list will reappear automatically once the connection is restored."
            />
          ) : (
            <EmptyState
              icon={IconBotEmpty}
              title="No trading bots yet"
              description="Design a strategy visually in the builder, load a working example, or import an existing .apex.json bot file. More examples live in the repo's examples/ directory."
              action={
                <div className="flex items-center gap-2.5 flex-wrap justify-center">
                  <Button size="sm" onClick={() => window.dispatchEvent(new CustomEvent('open-builder'))}>
                    Open Builder
                  </Button>
                  <ExampleLoader onImported={refetchBots} />
                  <Button size="sm" variant="secondary" onClick={() => fileInputRef.current?.click()}>
                    Import File
                  </Button>
                </div>
              }
            />
          )}
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {bots.map((bot, index) => (
            <BotCard
              key={bot.id}
              bot={bot}
              index={index}
              busyAction={busyAction}
              togglingBot={togglingBot}
              verifyingBot={verifyingBot}
              verifyData={verifyData}
              openConsoles={openConsoles}
              clearSignals={clearSignals}
              toggleBotState={toggleBotState}
              restartBot={restartBot}
              handleExport={handleExport}
              handleDuplicate={handleDuplicate}
              handleClearCacheClick={handleClearCacheClick}
              handleDeleteClick={handleDeleteClick}
              updateBotConfig={updateBotConfig}
              toggleConsole={toggleConsole}
            />
          ))}
        </div>
      )}
    </PageShell>
  );
}
