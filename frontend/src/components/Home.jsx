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

const StatTile = ({ label, value, sub, accent, icon, onClick, delay }) => (
  <button
    onClick={onClick}
    className={`terminal-card relative text-left p-5 group transition-all duration-300 hover:border-border-strong hover:-translate-y-0.5 overflow-hidden fade-in-delay-${delay}`}
  >
    <div
      className="pointer-events-none absolute -top-10 -right-10 w-28 h-28 rounded-full blur-3xl opacity-[0.08] group-hover:opacity-[0.14] transition-opacity duration-300"
      style={{ background: accent }}
    />
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
    <p className="text-[10px] font-bold uppercase tracking-widest text-muted">{label}</p>
    {sub && <p className="text-[10px] text-faint mt-1">{sub}</p>}
  </button>
);

export default function Home({ setActiveView, bots = [], backendOk = true, refetchBots }) {
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
      {/* Ambient glows */}
      <div className="absolute top-0 left-1/3 w-[500px] h-[500px] bg-accent/[0.04] rounded-full blur-[130px] pointer-events-none" />
      <div className="absolute bottom-0 right-0 w-[400px] h-[400px] bg-info/[0.04] rounded-full blur-[130px] pointer-events-none" />

      <div className="relative z-10 max-w-5xl mx-auto px-5 md:px-8 pt-20 md:pt-14 pb-10">

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
          <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight text-text mb-3">
            Apex<span className="text-accent">Algo</span>
            <span className="ml-3 align-middle text-[10px] font-num font-medium text-faint tracking-[0.25em] uppercase">v1.0.0A</span>
          </h1>
          <p className="text-muted text-sm md:text-base max-w-2xl leading-relaxed">
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
            label="Analytics"
            value="P&L"
            sub="equity curve & drawdown"
            accent="var(--color-info)"
            onClick={() => setActiveView('trades')}
            icon={<svg className="w-4.5 h-4.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" /></svg>}
          />
        </div>

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
                        className="w-full flex items-center gap-4 px-5 py-3.5 text-left hover:bg-text/[0.03] transition-colors group"
                      >
                        <span className={`w-2 h-2 rounded-full shrink-0 ${bot.is_active ? 'bg-success shadow-[0_0_8px_var(--color-success)] animate-pulse' : 'bg-faint/40'}`} />
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-semibold text-text truncate group-hover:text-text transition-colors">{bot.name}</p>
                          <p className="text-[10px] text-faint font-num truncate mt-0.5">
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
        <div className="mt-10 flex items-center gap-6 text-[9px] font-num uppercase tracking-widest fade-in-delay-6">
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
