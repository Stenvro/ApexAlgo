import { useEffect, useMemo, useState } from 'react';
import { apiClient } from '../api/client';
import Badge from './ui/Badge';
import ModeBadge from './ui/ModeBadge';
import Button from './ui/Button';
import EmptyState from './ui/EmptyState';
import ExampleLoader from './ExampleLoader';

/**
 * Home — dashboard landing screen.
 * Receives the already-polled bots summary from App (no extra fetching).
 *
 * @param {Function} setActiveView  Navigate to a view key.
 * @param {Array} bots              /api/bots/summary payload.
 */

const openBuilder = (bot) =>
  window.dispatchEvent(new CustomEvent('open-builder', bot ? { detail: bot } : undefined));
const openAnalytics = (mode) =>
  window.dispatchEvent(new CustomEvent('open-analytics', { detail: { bot: 'all', mode } }));

const REAL_MODES = new Set(['paper', 'live']);
const fmtUsd = (n) => `$${Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

/* Everything the operator should look at before walking away: broken keys,
   engine auto-stops, and real positions nobody is managing any more. */
function AttentionStrip({ items }) {
  if (items.length === 0) return null;
  return (
    <section className="mb-8 fade-in-delay-4" aria-label="Needs attention">
      <div className="terminal-card overflow-hidden border-warn/40">
        <div className="px-5 py-2.5 border-b border-warn/30 bg-warn/[0.06] flex items-center gap-2">
          <svg className="w-3.5 h-3.5 text-warn shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M12 3l9 16H3l9-16z" />
          </svg>
          <h2 className="text-2xs font-bold uppercase tracking-[0.2em] text-warn">Needs attention · {items.length}</h2>
        </div>
        <ul className="divide-y divide-border/50">
          {items.map((it) => (
            <li key={it.key}>
              <button
                type="button"
                onClick={it.onClick}
                className="w-full flex items-center gap-3 px-5 py-2.5 text-left hover:bg-overlay/50 transition-colors group"
              >
                <span className={`text-3xs font-bold uppercase tracking-wider shrink-0 w-24 ${it.tone === 'danger' ? 'text-danger' : 'text-warn'}`}>{it.kind}</span>
                <span className="text-xs text-text flex-1 min-w-0 truncate">{it.text}</span>
                <span className="text-2xs text-faint shrink-0 group-hover:text-muted transition-colors">{it.action} →</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

const StatTile = ({ label, value, sub, accent, icon, onClick, delay }) => (
  <button
    onClick={onClick}
    className={`terminal-card relative text-left p-4 group transition-all duration-300 hover:border-border-strong hover:-translate-y-0.5 overflow-hidden fade-in-delay-${delay}`}
  >
    <div className="flex items-start justify-between mb-4">
      <span
        className="w-9 h-9 rounded-md border flex items-center justify-center transition-transform duration-300 group-hover:scale-105"
        style={{
          color: accent,
          borderColor: `color-mix(in srgb, ${accent} 25%, transparent)`,
          background: `color-mix(in srgb, ${accent} 6%, transparent)`,
        }}
      >
        {icon}
      </span>
      <svg className="w-3.5 h-3.5 text-faint opacity-0 group-hover:opacity-100 transition-opacity" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
      </svg>
    </div>
    <p className="text-2xl font-num font-bold text-text leading-none mb-1.5">{value}</p>
    <p className="text-2xs font-bold uppercase tracking-widest text-muted">{label}</p>
    {sub && <p className="text-2xs text-faint mt-1">{sub}</p>}
  </button>
);

export default function Home({ setActiveView, bots = [], backendOk = true, refetchBots }) {
  // Real-money state lives in positions and keys, not in the bots summary.
  const [openPositions, setOpenPositions] = useState([]);
  const [keys, setKeys] = useState(null);
  const activeCount = bots.filter((b) => b.is_active).length;
  useEffect(() => {
    let cancelled = false;
    apiClient.get('/api/trades/positions', { params: { status: 'open', limit: 5000 } })
      .then((r) => { if (!cancelled) setOpenPositions(Array.isArray(r.data) ? r.data : []); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [activeCount, backendOk]);
  useEffect(() => {
    let cancelled = false;
    apiClient.get('/api/keys')
      .then((r) => { if (!cancelled) setKeys(Array.isArray(r.data) ? r.data : []); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  const realOpen = useMemo(() => openPositions.filter((p) => REAL_MODES.has(p.mode)), [openPositions]);
  const exposure = useMemo(() => ({
    live: realOpen.filter((p) => p.mode === 'live').reduce((s, p) => s + (p.entry_price || 0) * (p.amount || 0), 0),
    paper: realOpen.filter((p) => p.mode === 'paper').reduce((s, p) => s + (p.entry_price || 0) * (p.amount || 0), 0),
  }), [realOpen]);

  const attention = useMemo(() => {
    const items = [];
    const byName = new Map(bots.map((b) => [b.name, b]));
    for (const k of keys || []) {
      if (!k.is_active) {
        items.push({
          key: `key:${k.name}`, kind: 'Key broken', tone: 'danger',
          text: `${k.name} (${String(k.exchange).toUpperCase()}) — ${k.error_msg || 'exchange rejected the credentials'}${k.bots?.length ? ` · used by ${k.bots.map((b) => b.name).join(', ')}` : ''}`,
          action: 'Settings', onClick: () => setActiveView('settings'),
        });
      }
    }
    const unmanaged = new Map();
    for (const p of realOpen) {
      const bot = byName.get(p.bot_name);
      if (bot && bot.is_active) continue;
      const cur = unmanaged.get(p.bot_name) || { count: 0, notional: 0, mode: p.mode, missing: !bot };
      cur.count += 1;
      cur.notional += (p.entry_price || 0) * (p.amount || 0);
      unmanaged.set(p.bot_name, cur);
    }
    for (const [name, u] of unmanaged) {
      items.push({
        key: `unmanaged:${name}`, kind: 'Unmanaged', tone: 'danger',
        text: `${u.count} open ${u.mode} position${u.count === 1 ? '' : 's'} (~${fmtUsd(u.notional)} at entry) on ${u.missing ? 'deleted bot' : 'stopped bot'} ${name} — no stop-loss or take-profit is being evaluated`,
        action: 'Analytics', onClick: () => openAnalytics(u.mode),
      });
    }
    for (const b of bots) {
      if (!b.is_active && b.settings?.last_stop_reason) {
        items.push({
          key: `stop:${b.name}`, kind: 'Engine stop', tone: 'warn',
          text: `${b.name} — ${b.settings.last_stop_reason}`,
          action: 'Bots', onClick: () => setActiveView('bots'),
        });
      }
    }
    return items;
  }, [bots, keys, realOpen, setActiveView]);

  const activeBots = bots.filter((b) => b.is_active);
  const startingBots = activeBots.filter((b) => ['starting', 'fetching', 'backtesting'].includes(b.runtime?.phase)).length;
  const haltedBots = bots.filter((b) => !b.is_active && b.settings?.last_stop_reason).length;
  const liveBots = bots.filter((b) => b.execution_mode === 'live');
  const runningLive = bots.filter((b) => b.is_active && b.execution_mode === 'live').length;
  const runningPaper = bots.filter((b) => b.is_active && b.execution_mode === 'paper').length;
  const runningForward = bots.filter((b) => b.is_active && b.execution_mode === 'forward_test').length;
  const executionSub = (runningLive || runningPaper || runningForward)
    ? [runningLive ? `${runningLive} live` : null, runningPaper ? `${runningPaper} paper` : null, runningForward ? `${runningForward} forward test` : null].filter(Boolean).join(' · ') + ' running'
    : (liveBots.length ? 'live configured, none running' : 'no live execution configured');
  const recentBots = [...bots]
    .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
    .slice(0, 5);

  return (
    <div className="w-full min-h-full relative overflow-y-auto overflow-x-hidden bg-bg grid-background">
      <div className="relative z-10 max-w-5xl mx-auto px-4 md:px-8 pt-8 md:pt-12 pb-10">

        {/* Hero */}
        <header className="mb-10 fade-in">
          <div className="flex items-center gap-2 mb-4">
            {backendOk
              ? <Badge variant="success" dot pulse>Engine online</Badge>
              : <Badge variant="warn" dot pulse>Reconnecting…</Badge>}
            {activeBots.length > 0 && (
              <Badge variant="accent">{activeBots.length} running</Badge>
            )}
          </div>
          <h1 className="text-2xl md:text-3xl font-extrabold tracking-tight text-text mb-3">
            Apex<span className="text-accent">Algo</span>
            <span className="ml-3 align-middle text-2xs font-num font-medium text-faint tracking-[0.25em] uppercase">v{__APP_VERSION__}</span>
          </h1>
          <p className="text-muted text-sm max-w-2xl leading-relaxed">
            Self-hosted quantitative trading terminal. Design strategies visually,
            backtest locally against real market data, and deploy to live exchanges.
          </p>
          <div className="flex flex-wrap gap-3 mt-6">
            <Button size="lg" onClick={() => openBuilder()}>
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
              </svg>
              New Strategy
            </Button>
            <Button size="lg" variant="secondary" onClick={() => setActiveView('manager')}>
              Data Vault
            </Button>
            <Button size="lg" variant="ghost" onClick={() => setActiveView('settings')}>
              Connect Exchange
            </Button>
          </div>
        </header>

        {/* Stat tiles */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-10">
          <StatTile
            delay={1}
            label="Total algorithms"
            value={bots.length}
            accent="var(--color-accent)"
            onClick={() => setActiveView('bots')}
            icon={<svg className="w-4.5 h-4.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" /></svg>}
          />
          <StatTile
            delay={2}
            label="Running now"
            value={activeBots.length}
            sub={startingBots
              ? `${startingBots} starting up · ${activeBots.length - startingBots} monitoring`
              : haltedBots
                ? `${haltedBots} stopped by engine — see bots`
                : activeBots.length ? 'evaluating on candle close' : 'all engines idle'}
            accent="var(--color-success)"
            onClick={() => setActiveView('bots')}
            icon={<svg className="w-4.5 h-4.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>}
          />
          <StatTile
            delay={3}
            label="Live execution"
            value={runningLive}
            sub={executionSub}
            accent="var(--color-danger)"
            onClick={() => setActiveView('bots')}
            icon={<svg className="w-4.5 h-4.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" /></svg>}
          />
          <StatTile
            delay={4}
            label="Open exposure"
            value={fmtUsd(exposure.live)}
            sub={realOpen.length
              ? `${realOpen.filter((p) => p.mode === 'live').length} live · ${realOpen.filter((p) => p.mode === 'paper').length} paper (${fmtUsd(exposure.paper)}) at entry`
              : 'no real positions open'}
            accent="var(--color-info)"
            onClick={() => openAnalytics(realOpen.length ? 'real' : undefined)}
            icon={<svg className="w-4.5 h-4.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" /></svg>}
          />
        </div>

        <AttentionStrip items={attention} />

        {/* Recent strategies */}
        <section className="fade-in-delay-5">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs font-bold uppercase tracking-[0.2em] text-muted">Recent strategies</h2>
            {bots.length > 0 && (
              <Button variant="ghost" size="sm" onClick={() => setActiveView('bots')}>
                View all
              </Button>
            )}
          </div>

          <div className="terminal-card overflow-hidden">
            {recentBots.length === 0 ? (
              <EmptyState
                title="No strategies yet"
                description="Build your first algorithm in the visual editor — drag indicators, conditions and actions onto the canvas. Or start from a working example (more live in the repo's examples/ directory)."
                action={
                  <div className="flex items-center gap-2.5 flex-wrap justify-center">
                    <Button size="sm" onClick={() => openBuilder()}>Open builder</Button>
                    <ExampleLoader onImported={refetchBots} />
                  </div>
                }
              />
            ) : (
              <ul className="divide-y divide-border/50">
                {recentBots.map((bot) => {
                  const symbols = bot.settings?.symbols?.length
                    ? bot.settings.symbols
                    : bot.settings?.symbol ? [bot.settings.symbol] : [];
                  return (
                    <li key={bot.id}>
                      <button
                        onClick={() => openBuilder(bot)}
                        className="w-full flex items-center gap-4 px-5 py-3.5 text-left hover:bg-overlay/50 transition-colors group"
                      >
                        <span className={`w-2 h-2 rounded-full shrink-0 ${bot.is_active ? 'bg-success shadow-[0_0_8px_var(--color-success)] animate-pulse' : 'bg-faint/40'}`} />
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-semibold text-text truncate group-hover:text-text transition-colors">{bot.name}</p>
                          <p className="text-2xs text-faint font-num truncate mt-0.5">
                            {symbols.slice(0, 3).join(' · ') || 'no pairs'}
                            {symbols.length > 3 && ` +${symbols.length - 3}`}
                            {bot.settings?.timeframe && `  ·  ${bot.settings.timeframe}`}
                          </p>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                          <ModeBadge mode={bot.execution_mode} />
                          {bot.is_active
                            ? <Badge variant="success" dot pulse>Running</Badge>
                            : <Badge variant="neutral">Stopped</Badge>}
                        </div>
                        <svg className="w-3.5 h-3.5 text-faint opacity-0 group-hover:opacity-100 transition-opacity shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                        </svg>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </section>

        {/* Footer status line */}
        <div className="mt-10 flex items-center gap-6 text-3xs font-num uppercase tracking-widest fade-in-delay-6">
          {backendOk ? (
            <>
              <span className="flex items-center gap-1.5 text-faint"><span className="w-1 h-1 rounded-full bg-success" /> Engine core</span>
              <span className="flex items-center gap-1.5 text-faint"><span className="w-1 h-1 rounded-full bg-success" /> Database</span>
              <span className="flex items-center gap-1.5 text-faint"><span className="w-1 h-1 rounded-full bg-info" /> Candle poller</span>
            </>
          ) : (
            <span className="flex items-center gap-1.5 text-warn"><span className="w-1 h-1 rounded-full bg-warn animate-pulse" /> Backend unreachable — reconnecting…</span>
          )}
        </div>
      </div>
    </div>
  );
}
