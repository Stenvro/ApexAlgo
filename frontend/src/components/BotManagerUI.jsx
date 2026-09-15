import { useState, useRef, memo, useCallback } from 'react';
import { apiClient } from '../api/client';
import { humanizeApiError } from '../api/errors';
import ExampleLoader from './ExampleLoader';
import PageShell from './ui/PageShell';
import SectionHeader from './ui/SectionHeader';
import Button from './ui/Button';
import Badge from './ui/Badge';
import EmptyState from './ui/EmptyState';
import { toast } from './ui/Toast';
import { confirmDialog } from './ui/ConfirmDialog';
import BotConsole from './BotConsole';

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
    <div className="px-5 py-2.5 border-b border-border bg-inset/40">
      <div className="flex items-center gap-2.5 min-w-0">
        <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-sm border text-[9px] font-bold uppercase tracking-wider shrink-0 ${meta.cls}`}>
          {meta.pulse && <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" />}
          {meta.label}
          {rt?.phase === 'live' && rt.mode && <span className="font-medium normal-case tracking-normal opacity-80">· {MODE_LABEL[rt.mode] || rt.mode}</span>}
        </span>
        <span className="text-[10px] text-text-secondary font-num truncate flex-1" title={rt?.detail}>
          {rt?.detail || 'Engine active'}{symbolPos}
        </span>
        {nextClose && <span className="text-[9px] text-faint font-num shrink-0">next candle {nextClose}</span>}
        {pct !== null && <span className="text-[9px] text-muted font-num shrink-0">{pct}%</span>}
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
    <div className="px-5 py-2.5 border-b border-warn/30 bg-warn/[0.06] flex items-start gap-2">
      <svg className="w-3.5 h-3.5 text-warn shrink-0 mt-px" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M12 3l9 16H3l9-16z" />
      </svg>
      <p className="text-[10px] text-warn leading-snug"><span className="font-bold uppercase tracking-wider mr-1">Stopped by engine</span>{reason}</p>
    </div>
  );
}

const BotCard = memo(function BotCard({ bot, index, busyAction, togglingBot, openConsoles, clearSignals, toggleBotState, restartBot, handleExport, handleDuplicate, handleClearCacheClick, handleDeleteClick, updateBotConfig, toggleConsole }) {
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
  // Distinct configs backtested so far (from the last backtest summary)
  const variants     = Number((bot.last_backtest_summary ?? bot.settings?.last_backtest_summary)?.variants) || 0;

  return (
    <div
      className={`terminal-card flex flex-col overflow-hidden transition-all duration-300 hover:border-border-strong ${
        bot.is_active ? 'border-success/30' : ''
      } fade-in-delay-${Math.min(index + 1, 6)}`}
    >
      {/* ── Card Header: status hierarchy + primary action ── */}
      <div className="px-5 py-4 border-b border-border flex justify-between items-start gap-3 bg-gradient-to-r from-bg/60 to-raised/40">
        <div className="flex flex-col min-w-0">
          <div className="flex items-center gap-2.5 flex-wrap">
            <h3 className="text-text font-bold text-sm tracking-wide truncate">{bot.name}</h3>
            {bot.is_active
              ? <Badge variant="success" dot pulse>Running</Badge>
              : <Badge variant="neutral" dot>Stopped</Badge>}
            {isApiExecutionOn
              ? <Badge variant="accent">Live</Badge>
              : <Badge variant="info">Paper</Badge>}
            {isBacktestOn && <Badge variant="purple">Backtest</Badge>}
          </div>

          {/* Metrics row */}
          <div className="flex items-center gap-4 mt-2.5 flex-wrap">
            <div className="flex flex-col">
              <span className="text-[9px] font-bold uppercase tracking-wider text-faint">Timeframe</span>
              <span className="text-[11px] font-num font-bold text-accent">{bot.settings?.timeframe || 'N/A'}</span>
            </div>
            <div className="flex flex-col">
              <span className="text-[9px] font-bold uppercase tracking-wider text-faint">Pairs</span>
              <span className="text-[11px] font-num font-bold text-text">{assignedPairs.length}</span>
            </div>
            {variants > 0 && (
              <div className="flex flex-col" title={`${variants} distinct configuration${variants === 1 ? '' : 's'} of this strategy have been backtested. Reset the bot to start counting again.`}>
                <span className="text-[9px] font-bold uppercase tracking-wider text-faint">Variant</span>
                <span className="text-[11px] font-num font-bold text-text">#{variants}</span>
              </div>
            )}
            <div className="flex flex-col min-w-0" title={assignedPairs.join(', ')}>
              <span className="text-[9px] font-bold uppercase tracking-wider text-faint">Whitelist</span>
              <span className="flex items-center gap-1 flex-wrap">
                {visiblePairs.length === 0 && <span className="text-[10px] text-faint">—</span>}
                {visiblePairs.map(pair => (
                  <span key={pair} className="text-[9px] font-num font-bold text-text-secondary bg-inset border border-border rounded-sm px-1.5 py-0.5">
                    {pair}
                  </span>
                ))}
                {extraPairs > 0 && (
                  <span className="text-[9px] font-num text-muted">+{extraPairs}</span>
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

      {/* ── Card Body ── */}
      <div className="px-5 py-4 flex-1 flex flex-col space-y-5">

        {/* Environment Routing */}
        <div className="flex flex-col space-y-2">
          <div className="flex justify-between items-end">
            <span className="text-[9px] font-bold text-muted uppercase tracking-wider">Environment Routing</span>
            {!hasApiKey && <span className="text-[8px] font-bold uppercase text-danger">No API Key Linked</span>}
          </div>
          <div className="flex bg-inset rounded-md border border-border overflow-hidden">
            <button
              disabled={bot.is_active}
              onClick={() => updateBotConfig(bot.id, bot, { settings: { api_execution: false } })}
              title="Simulate orders locally without touching the exchange."
              className={`flex-1 py-2 text-[9px] font-bold uppercase transition-all duration-200 disabled:opacity-50 ${!isApiExecutionOn ? 'bg-info/10 text-info' : 'text-muted hover:text-text hover:bg-raised'}`}
            >
              Paper Trade
            </button>
            <button
              disabled={bot.is_active || !hasApiKey}
              onClick={() => updateBotConfig(bot.id, bot, { settings: { api_execution: true } })}
              title={!hasApiKey ? 'Assign an API key to enable live/paper routing.' : 'Route orders through API key.'}
              className={`flex-1 py-2 text-[9px] font-bold uppercase transition-all duration-200 border-l border-border disabled:opacity-50 ${isApiExecutionOn ? 'bg-accent/10 text-accent' : 'text-muted hover:text-text hover:bg-raised'}`}
            >
              Live Exchange
            </button>
          </div>
        </div>

        {/* Initialization Protocol */}
        <div className="flex flex-col space-y-2 border-t border-border pt-4">
          <span className="text-[9px] font-bold text-muted uppercase tracking-wider">Initialization Protocol</span>
          <label className={`flex items-center p-3 rounded-md border transition-all duration-200 ${bot.is_active ? 'opacity-50 pointer-events-none cursor-not-allowed' : 'cursor-pointer hover:border-border-strong'} ${isBacktestOn ? 'bg-success/5 border-success/30' : 'bg-inset border-border'}`}>
            <input
              type="checkbox"
              disabled={bot.is_active}
              checked={isBacktestOn}
              onChange={(e) => updateBotConfig(bot.id, bot, { settings: { backtest_on_start: e.target.checked } })}
              className="form-checkbox h-3.5 w-3.5 accent-success rounded-sm cursor-pointer"
            />
            <div className="ml-3 flex flex-col">
              <span className={`text-[11px] font-bold uppercase tracking-wider ${isBacktestOn ? 'text-success' : 'text-text'}`}>Run Historical Backtest</span>
              <span className="text-[9px] text-muted mt-0.5">Process past data before executing live. Previous backtest results are cleared automatically on every run.</span>
            </div>
          </label>
        </div>
      </div>

      {/* ── Console Toggle Bar ── */}
      <button
        onClick={() => toggleConsole(bot.id)}
        title={consoleOpen ? 'Hide console output' : 'Show console output'}
        className="w-full px-5 py-2.5 border-t border-border bg-bg/60 flex justify-between items-center hover:bg-bg transition-colors group"
      >
        <span className="text-[8px] font-bold uppercase tracking-widest text-muted group-hover:text-text transition-colors">
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
        <span className="text-[9px] font-bold text-faint uppercase tracking-wider font-num">ID {bot.id}</span>
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
  prev.busyAction === next.busyAction &&
  (prev.togglingBot === prev.bot.id) === (next.togglingBot === next.bot.id) &&
  prev.openConsoles[prev.bot.id] === next.openConsoles[next.bot.id] &&
  prev.clearSignals[prev.bot.name] === next.clearSignals[next.bot.name] &&
  JSON.stringify(prev.bot.runtime) === JSON.stringify(next.bot.runtime) &&
  JSON.stringify(prev.bot.settings) === JSON.stringify(next.bot.settings) &&
  prev.bot.last_backtest_summary?.variants === next.bot.last_backtest_summary?.variants
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

export default function BotManagerUI({ bots = [], refetchBots, backendOk = true }) {
  const [openConsoles, setOpenConsoles] = useState({});
  const [busyAction, setBusyAction]     = useState(null);  // 'delete:ID' or 'wipe:name'
  const [togglingBot, setTogglingBot]   = useState(null);  // bot id being started/stopped
  const [clearSignals, setClearSignals] = useState({});
  const fileInputRef                    = useRef(null);

  const toggleBotState = useCallback(async (botId, isCurrentlyActive) => {
    setTogglingBot(botId);
    try {
      const endpoint = isCurrentlyActive ? `/api/bots/${botId}/stop` : `/api/bots/${botId}/start`;
      await apiClient.post(endpoint);
      refetchBots();
      toast.success(isCurrentlyActive ? 'Bot stopped' : 'Engine started');
    } catch (err) {
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
    const liveCount = bots.filter(b => b.is_active && b.settings?.api_execution).length;
    const ok = await confirmDialog({
      title: 'Stop all bots',
      message: liveCount
        ? `${liveCount} bot${liveCount === 1 ? ' is' : 's are'} routing live orders. Stopping leaves any open positions unmanaged (no SL/TP) until restarted. Continue?`
        : 'Stop every running bot? Startups in progress are aborted.',
      confirmText: 'Stop all',
      type: liveCount ? 'danger' : 'warning',
    });
    if (!ok) return;
    setBulkBusy('stop');
    try {
      const res = await apiClient.post('/api/bots/bulk/stop', null);
      refetchBots();
      const n = res.data?.stopped?.length || 0;
      toast.success(n ? `${n} bot${n === 1 ? '' : 's'} stopped` : 'No running bots');
    } catch (err) {
      toast.error(humanizeApiError(err, 'Failed to stop bots.'));
    }
    setBulkBusy(null);
  }, [bots, refetchBots]);

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
    <PageShell glowColor="green">
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
        accentColor="white"
        action={
          <div className="flex items-center gap-2.5">
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
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
          {bots.map((bot, index) => (
            <BotCard
              key={bot.id}
              bot={bot}
              index={index}
              busyAction={busyAction}
              togglingBot={togglingBot}
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
