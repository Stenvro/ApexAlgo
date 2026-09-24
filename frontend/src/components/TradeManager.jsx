import { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { apiClient } from '../api/client';
import PageShell from './ui/PageShell';
import Button from './ui/Button';
import { Select } from './ui/Input';
import Badge from './ui/Badge';
import ModeBadge from './ui/ModeBadge';
import StatCard from './ui/StatCard';
import EmptyState from './ui/EmptyState';
import { Skeleton } from './ui/Skeleton';
import { toast } from './ui/Toast';
import { confirmDialog } from './ui/ConfirmDialog';

// ─── Formatters ──────────────────────────────────────────────────────────────

const safeNum = (val, decimals = 2) => {
    if (val === null || val === undefined || isNaN(Number(val))) return (0).toFixed(decimals);
    return Number(val).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
};

const formatCrypto = (val) => {
    if (val === null || val === undefined) return '0.00';
    return Number(val).toFixed(6).replace(/\.?0+$/, '');
};

const formatHoldTime = (ms) => {
    if (!ms || ms <= 0) return '—';
    const totalMins = Math.floor(ms / 60000);
    const totalHours = Math.floor(totalMins / 60);
    if (totalHours >= 48) return `${Math.floor(totalHours / 24)}d ${totalHours % 24}h`;
    if (totalHours >= 1) return `${totalHours}h ${totalMins % 60}m`;
    return `${totalMins}m`;
};

// Longest stretch with no position open at all — sample-size context for a low
// trade count (a selective strategy vs. one whose market never showed up).
// Overlapping positions (multi-pair) are merged first; the window edges only
// count when the user has set a date range, otherwise the true start/end of
// the data is unknown and only gaps between trades are considered.
const longestFlatGap = (intervals, windowFrom, windowTo) => {
    const sorted = intervals.filter(i => i.end > i.start).sort((a, b) => a.start - b.start);
    if (sorted.length === 0) return null;
    const merged = [];
    for (const i of sorted) {
        const last = merged[merged.length - 1];
        if (last && i.start <= last.end) last.end = Math.max(last.end, i.end);
        else merged.push({ start: i.start, end: i.end });
    }
    let best = null;
    const consider = (from, to) => {
        if (to - from > (best?.ms || 0)) best = { ms: to - from, from, to };
    };
    if (windowFrom !== null && windowFrom < merged[0].start) consider(windowFrom, merged[0].start);
    for (let k = 1; k < merged.length; k++) consider(merged[k - 1].end, merged[k].start);
    const lastEnd = merged[merged.length - 1].end;
    if (windowTo !== null && windowTo > lastEnd) consider(lastEnd, windowTo);
    return best;
};

// Entry timestamp — use entry order timestamps as fallback for backtest positions
// whose created_at may be the wall-clock run time, not the candle entry time
const entryTimeOf = (p, exitTs, entryTsByPos) => {
    if (p.created_at) {
        const createdTs = new Date(p.created_at);
        if (exitTs > createdTs) return createdTs;
    }
    const entryTs = entryTsByPos[p.id];
    if (entryTs && exitTs > entryTs) return entryTs;
    return null;
};

const pnlColor = (v) => (v >= 0 ? 'text-success' : 'text-danger');
const pnlSign = (v) => (v >= 0 ? '+' : '');

// Mode filter values → the label the collapsed (phone) filter summary shows.
const MODE_LABELS = {
    real: 'Paper + Live',
    live: 'Live',
    paper: 'Paper',
    forward_test: 'Forward test',
    backtest: 'Backtest',
    all: 'All modes',
};

// ─── Equity Curve SVG ────────────────────────────────────────────────────────

const EquityCurve = ({ data }) => {
    // Hovered point index (null = none). Hooks must run before the early return.
    const [hover, setHover] = useState(null);
    const wrapRef = useRef(null);
    if (data.length < 2) {
        return (
            <div className="flex flex-col items-center justify-center h-full space-y-2 text-center">
                <svg className="w-8 h-8 text-border" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
                </svg>
                <p className="text-2xs text-muted uppercase tracking-wider">Close at least 2 trades to render the curve</p>
            </div>
        );
    }

    const W = 600, H = 140;
    const PAD = { t: 12, r: 8, b: 24, l: 8 };
    const iW = W - PAD.l - PAD.r;
    const iH = H - PAD.t - PAD.b;

    const values = data.map(d => d.value);
    const minV = Math.min(0, ...values);
    const maxV = Math.max(0, ...values);
    const range = maxV - minV || 1;

    const xS = (i) => PAD.l + (i / (data.length - 1)) * iW;
    const yS = (v) => PAD.t + iH - ((v - minV) / range) * iH;

    const zeroY = yS(0);
    const points = data.map((d, i) => `${xS(i)},${yS(d.value)}`).join(' ');
    const areaPath = [
        `M${xS(0)},${zeroY}`,
        `L${xS(0)},${yS(data[0].value)}`,
        ...data.map((d, i) => `L${xS(i)},${yS(d.value)}`),
        `L${xS(data.length - 1)},${zeroY}`,
        'Z',
    ].join(' ');

    const lastVal = data[data.length - 1].value;
    // CSS vars work in SVG style props (not attributes), so paint via style
    const lineClr = lastVal >= 0 ? 'var(--color-success)' : 'var(--color-danger)';
    const gradId = lastVal >= 0 ? 'ecGreen' : 'ecRed';

    const firstDate = data[0].date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    const lastDate = data[data.length - 1].date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

    // Map mouse x (in CSS px) to the nearest data index; the SVG is stretched
    // with preserveAspectRatio="none", so scale by the wrapper width.
    const onMove = (e) => {
        const rect = wrapRef.current?.getBoundingClientRect();
        if (!rect || rect.width === 0) return;
        const vx = ((e.clientX - rect.left) / rect.width) * W;
        const idx = Math.round(((vx - PAD.l) / iW) * (data.length - 1));
        setHover(Math.max(0, Math.min(data.length - 1, idx)));
    };
    const hp = hover !== null ? data[hover] : null;
    const hoverPct = hp ? (xS(hover) / W) * 100 : 0;
    const flipTip = hoverPct > 60;

    return (
        <div ref={wrapRef} className="relative w-full h-full" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-full" preserveAspectRatio="none">
            <defs>
                <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" style={{ stopColor: lineClr, stopOpacity: 0.25 }} />
                    <stop offset="100%" style={{ stopColor: lineClr, stopOpacity: 0.01 }} />
                </linearGradient>
            </defs>
            {/* Zero baseline */}
            <line x1={PAD.l} y1={zeroY} x2={W - PAD.r} y2={zeroY}
                style={{ stroke: 'var(--color-border)' }} strokeWidth="1" strokeDasharray="3,4" />
            {/* Area fill */}
            <path d={areaPath} fill={`url(#${gradId})`} />
            {/* Line */}
            <polyline points={points} fill="none" style={{ stroke: lineClr }} strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />
            {/* End dot */}
            <circle cx={xS(data.length - 1)} cy={yS(lastVal)} r="2.5" style={{ fill: lineClr }} />
            {/* Date labels */}
            <text x={PAD.l} y={H - 4} style={{ fill: 'var(--color-muted)' }} fontSize="10" fontFamily="JetBrains Mono, monospace">{firstDate}</text>
            <text x={W - PAD.r} y={H - 4} style={{ fill: 'var(--color-muted)' }} fontSize="10" fontFamily="JetBrains Mono, monospace" textAnchor="end">{lastDate}</text>
            {/* Crosshair */}
            {hp && (
                <>
                    <line x1={xS(hover)} y1={PAD.t} x2={xS(hover)} y2={H - PAD.b}
                        style={{ stroke: 'var(--color-muted)' }} strokeWidth="1" strokeDasharray="2,3" vectorEffect="non-scaling-stroke" />
                    <circle cx={xS(hover)} cy={yS(hp.value)} r="3.5" style={{ fill: 'var(--color-bg)', stroke: lineClr }} strokeWidth="2" vectorEffect="non-scaling-stroke" />
                </>
            )}
        </svg>
        {hp && (
            <div className={`absolute top-1 pointer-events-none z-10 bg-surface border border-border rounded-md shadow-lg px-3 py-2 text-2xs font-num whitespace-nowrap ${flipTip ? '-translate-x-full' : ''}`}
                style={{ left: `calc(${hoverPct}% ${flipTip ? '- 10px' : '+ 10px'})` }}>
                <div className="text-muted mb-1">{hp.date.toLocaleString('en-US', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })} <span className="text-faint">· trade #{hp.index}</span></div>
                <div className="grid grid-cols-[auto_auto] gap-x-4 gap-y-0.5">
                    <span className="text-muted">Equity</span><span className="text-text font-bold text-right">${safeNum(hp.equity)}</span>
                    <span className="text-muted">Cumulative PNL</span><span className={`font-bold text-right ${pnlColor(hp.value)}`}>{pnlSign(hp.value)}${safeNum(Math.abs(hp.value))} <span className="text-faint font-normal">({pnlSign(hp.value)}{safeNum(hp.capital > 0 ? (hp.value / hp.capital) * 100 : 0, 1)}%)</span></span>
                    <span className="text-muted">Peak</span><span className="text-text text-right">${safeNum(hp.peak)}</span>
                    <span className="text-muted">Drawdown</span><span className={`text-right ${hp.drawdownPct > 0 ? 'text-danger' : 'text-faint'}`}>{hp.drawdownPct > 0 ? `-${safeNum(hp.drawdownPct, 1)}%` : '0%'}</span>
                </div>
                <div className="mt-1.5 pt-1.5 border-t border-border/60 text-muted">
                    <span className="text-text">{hp.trade.symbol}</span> · {hp.trade.bot}
                    <span className={`ml-2 font-bold ${pnlColor(hp.trade.pnl)}`}>{pnlSign(hp.trade.pnl)}${safeNum(Math.abs(hp.trade.pnl))}</span>
                    <span className={`ml-1 ${pnlColor(hp.trade.pct)}`}>({pnlSign(hp.trade.pct)}{safeNum(hp.trade.pct, 2)}%)</span>
                </div>
            </div>
        )}
        </div>
    );
};

// ─── Pagination ──────────────────────────────────────────────────────────────

const PaginationBar = ({ total, current, onPrev, onNext }) => total <= 1 ? null : (
    <div className="flex items-center bg-inset rounded-md border border-border overflow-hidden">
        <button disabled={current === 1} onClick={onPrev} className="px-2.5 py-1 hover:bg-overlay disabled:opacity-30 text-muted transition-colors" aria-label="Previous page">&#9664;</button>
        <span className="text-3xs font-bold text-text px-2 font-num">{current} / {total}</span>
        <button disabled={current === total} onClick={onNext} className="px-2.5 py-1 hover:bg-overlay disabled:opacity-30 text-muted transition-colors" aria-label="Next page">&#9654;</button>
    </div>
);

// ─── Analysis window (date range) ────────────────────────────────────────────

const DAY = 86400000;
const toDateInput = (ms) => new Date(ms).toISOString().slice(0, 10);
const fmtShortDate = (ms) => new Date(ms).toLocaleDateString(undefined, { day: '2-digit', month: 'short', year: 'numeric' });
const startOfUtcDay = (ms) => Math.floor(ms / DAY) * DAY;
const endOfUtcDay = (ms) => startOfUtcDay(ms) + DAY - 1;

const RANGE_PRESETS = [
    { key: 'all', label: 'All' },
    { key: 'ytd', label: 'YTD' },
    { key: '1y', label: '1Y' },
    { key: '6m', label: '6M' },
    { key: '3m', label: '3M' },
    { key: '1m', label: '1M' },
];

const presetStart = (key, max) => {
    const d = new Date(max);
    switch (key) {
        case 'ytd': return Date.UTC(d.getUTCFullYear(), 0, 1);
        case '1y': d.setUTCFullYear(d.getUTCFullYear() - 1); return startOfUtcDay(d.getTime());
        case '6m': d.setUTCMonth(d.getUTCMonth() - 6); return startOfUtcDay(d.getTime());
        case '3m': d.setUTCMonth(d.getUTCMonth() - 3); return startOfUtcDay(d.getTime());
        case '1m': d.setUTCMonth(d.getUTCMonth() - 1); return startOfUtcDay(d.getTime());
        default: return null;
    }
};

const rangeThumb = 'appearance-none bg-transparent pointer-events-none absolute inset-x-0 top-0 h-4 m-0 ' +
    '[&::-webkit-slider-runnable-track]:bg-transparent [&::-moz-range-track]:bg-transparent ' +
    '[&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:pointer-events-auto [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-accent [&::-webkit-slider-thumb]:border-2 [&::-webkit-slider-thumb]:border-bg [&::-webkit-slider-thumb]:shadow [&::-webkit-slider-thumb]:cursor-grab ' +
    '[&::-moz-range-thumb]:pointer-events-auto [&::-moz-range-thumb]:w-3.5 [&::-moz-range-thumb]:h-3.5 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:bg-accent [&::-moz-range-thumb]:border-2 [&::-moz-range-thumb]:border-bg [&::-moz-range-thumb]:cursor-grab';

/**
 * Dual-thumb slider + date inputs + presets. `from`/`to` are ms or null (unbounded).
 * The slider snaps to whole UTC days across the span of all closed trades.
 */
// Incremental poll merge: rows from the server replace same-id rows, new ids
// are appended; the API returns newest first and the tables sort themselves.
const mergeById = (prev, incoming) => {
    if (incoming.length === 0) return prev;
    const byId = new Map(prev.map(r => [r.id, r]));
    incoming.forEach(r => byId.set(r.id, r));
    return [...byId.values()];
};

const DateRangeControl = ({ bounds, from, to, onChange }) => {
    if (!bounds) return null;
    const minDay = startOfUtcDay(bounds.min);
    const maxDay = startOfUtcDay(bounds.max);
    const totalDays = Math.max(1, Math.round((maxDay - minDay) / DAY));
    const effFrom = from === null ? minDay : Math.min(Math.max(startOfUtcDay(from), minDay), maxDay);
    const effTo = to === null ? maxDay : Math.min(Math.max(startOfUtcDay(to), minDay), maxDay);
    const fromIdx = Math.round((effFrom - minDay) / DAY);
    const toIdx = Math.round((effTo - minDay) / DAY);
    const pct = (i) => (i / totalDays) * 100;
    const isAll = from === null && to === null;
    const activePreset = isAll ? 'all'
        : RANGE_PRESETS.find(p => p.key !== 'all' && presetStart(p.key, bounds.max) === from && to === null)?.key || null;

    const commit = (nextFrom, nextTo) => {
        // Snap back to "unbounded" when a thumb sits at the edge, so new trades keep flowing in.
        onChange(nextFrom <= minDay ? null : nextFrom, nextTo >= maxDay ? null : endOfUtcDay(nextTo));
    };
    const spanDays = toIdx - fromIdx + 1;

    return (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 pt-3 mt-3 border-t border-border">
            <span className="text-2xs font-bold uppercase tracking-wider text-muted shrink-0">Period</span>
            <div className="flex items-center gap-1 shrink-0">
                {RANGE_PRESETS.map(p => (
                    <button key={p.key} type="button"
                        onClick={() => onChange(presetStart(p.key, bounds.max), null)}
                        className={`px-2 py-0.5 rounded text-2xs font-bold font-num transition-colors ${activePreset === p.key ? 'bg-accent/15 text-accent' : 'text-muted hover:text-text hover:bg-overlay'}`}>
                        {p.label}
                    </button>
                ))}
            </div>
            <div className="flex items-center gap-2 shrink-0">
                <input type="date" value={toDateInput(effFrom)} min={toDateInput(minDay)} max={toDateInput(effTo)}
                    onChange={e => { const t = Date.parse(e.target.value); if (!Number.isNaN(t)) commit(Math.min(t, effTo), effTo); }}
                    className="bg-inset border border-border hover:border-border-strong focus:border-accent/70 rounded-md px-2 py-1 text-xs font-num text-text outline-none [color-scheme:dark] [html.light_&]:[color-scheme:light]" aria-label="Period start" />
                <span className="text-faint text-xs">→</span>
                <input type="date" value={toDateInput(effTo)} min={toDateInput(effFrom)} max={toDateInput(maxDay)}
                    onChange={e => { const t = Date.parse(e.target.value); if (!Number.isNaN(t)) commit(effFrom, Math.max(t, effFrom)); }}
                    className="bg-inset border border-border hover:border-border-strong focus:border-accent/70 rounded-md px-2 py-1 text-xs font-num text-text outline-none [color-scheme:dark] [html.light_&]:[color-scheme:light]" aria-label="Period end" />
            </div>
            <div className="flex-1 min-w-[220px] flex items-center gap-3">
                <span className="text-2xs font-num text-faint shrink-0 hidden md:inline">{fmtShortDate(minDay)}</span>
                <div className="relative flex-1 h-4">
                    <div className="absolute inset-x-0 top-1/2 -translate-y-1/2 h-1 rounded-full bg-inset border border-border" />
                    <div className="absolute top-1/2 -translate-y-1/2 h-1 rounded-full bg-accent/70"
                        style={{ left: `${pct(fromIdx)}%`, right: `${100 - pct(toIdx)}%` }} />
                    <input type="range" min={0} max={totalDays} value={fromIdx} aria-label="Period start slider"
                        onChange={e => { const i = Math.min(Number(e.target.value), toIdx); commit(minDay + i * DAY, effTo); }}
                        className={`${rangeThumb} w-full ${fromIdx === toIdx ? 'z-20' : 'z-10'}`} />
                    <input type="range" min={0} max={totalDays} value={toIdx} aria-label="Period end slider"
                        onChange={e => { const i = Math.max(Number(e.target.value), fromIdx); commit(effFrom, minDay + i * DAY); }}
                        className={`${rangeThumb} w-full z-10`} />
                </div>
                <span className="text-2xs font-num text-faint shrink-0 hidden md:inline">{fmtShortDate(maxDay)}</span>
            </div>
            <span className="text-2xs font-num text-muted shrink-0">
                {isAll ? 'Full history' : `${spanDays.toLocaleString()} days`}
                {!isAll && (
                    <button type="button" onClick={() => onChange(null, null)} className="ml-2 text-accent hover:underline font-bold">reset</button>
                )}
            </span>
        </div>
    );
};

// ─── Main Component ──────────────────────────────────────────────────────────

// Starting capital of a bot for a set of positions: paper/live trades run on the
// wallet snapshot taken at go-live (live_starting_capital), everything else on
// the backtest pool. Mixed views fall back to the backtest pool.
const botCapital = (bot, modes) => {
    const s = bot?.settings || {};
    const live = Number(s.live_starting_capital);
    const onlyReal = modes && modes.size > 0 && [...modes].every(m => m === 'paper' || m === 'live');
    if (onlyReal && live > 0) return live;
    return Number(s.backtest_capital) || 1000;
};
const modesOf = (positions) => new Set(positions.map(p => p.mode).filter(Boolean));

// Mode filter values: 'real' = paper + live (money or a sandbox that mimics
// it), 'all' = everything mixed together, otherwise a single engine mode.
const REAL_MODES = new Set(['paper', 'live']);
const modeMatches = (filter, mode) =>
    filter === 'all' || (filter === 'real' ? REAL_MODES.has(mode) : mode === filter);
// Default view: real money first, else the forward test, else the backtest —
// never a sum across simulated and real trades unless explicitly chosen.
const defaultModeFor = (positions) => {
    const modes = modesOf(positions);
    if (modes.has('live') || modes.has('paper')) return 'real';
    if (modes.has('forward_test')) return 'forward_test';
    return 'backtest';
};
const MODE_ORDER = ['live', 'paper', 'forward_test', 'backtest'];
const sortModes = (modes) => [...modes].sort((a, b) => MODE_ORDER.indexOf(a) - MODE_ORDER.indexOf(b));

export default function TradeManager({ setError, bots = [], request = null }) {
    const [positions, setPositions] = useState([]);
    const [orders, setOrders] = useState([]);
    const [loading, setLoading] = useState(true);
    const [hasLoadedOnce, setHasLoadedOnce] = useState(false);
    const [activeTab, setActiveTab] = useState('positions');
    const [currentPage, setCurrentPage] = useState(1);
    const itemsPerPage = 150;

    const [livePrices, setLivePrices] = useState({});
    const [priceSyncing, setPriceSyncing] = useState(false);
    const [busyAction, setBusyAction] = useState(false);
    const [closingId, setClosingId] = useState(null);

    const [filterBot, setFilterBot] = useState(request?.bot || 'all');
    // Phones start with the filter panel folded to a one-line summary; desktop always shows it.
    const [filtersOpen, setFiltersOpen] = useState(() => typeof window === 'undefined' || window.innerWidth >= 768);
    const [filterSymbol, setFilterSymbol] = useState('all');
    const [filterExchange, setFilterExchange] = useState('all');
    // null = auto (resolved from the loaded positions, see defaultModeFor)
    const [filterModeChoice, setFilterMode] = useState(request?.mode || null);
    // A bot card's "View in Analytics" while this view is already mounted
    const appliedRequestAt = useRef(request?.at || null);
    useEffect(() => {
        if (!request || request.at === appliedRequestAt.current) return;
        appliedRequestAt.current = request.at;
        setFilterBot(request.bot || 'all'); // eslint-disable-line react-hooks/set-state-in-effect -- external navigation request
        setFilterMode(request.mode || null);
        setCurrentPage(1);
    }, [request]);
    const [filterInterval, setFilterInterval] = useState('all');
    // Analysis window (ms epoch, null = unbounded). Applies to closed trades and orders.
    const [dateFrom, setDateFrom] = useState(null);
    const [dateTo, setDateTo] = useState(null);

    // ── Data fetching ─────────────────────────────────────────────────────────

    const fetchLivePrices = useCallback(async (currentPositions) => {
        const uniqueSymbols = [...new Set(currentPositions.map(p => p.symbol))];
        if (uniqueSymbols.length === 0) return;
        setPriceSyncing(true);
        const priceMap = {};
        const results = await Promise.allSettled(
            uniqueSymbols.map(sym =>
                apiClient.get(`/api/data/market-info/${sym.replace('/', '-')}`)
                    .then(res => ({ sym, price: res.data?.last }))
            )
        );
        results.forEach(r => {
            if (r.status === 'fulfilled' && r.value.price) priceMap[r.value.sym] = r.value.price;
        });
        setLivePrices(prev => ({ ...prev, ...priceMap }));
        setPriceSyncing(false);
    }, []);

    const positionsRef = useRef(positions);
    const ordersRef = useRef(orders);
    useEffect(() => {
        positionsRef.current = positions;
        ordersRef.current = orders;
    }, [positions, orders]);

    const [lastUpdated, setLastUpdated] = useState(null);
    // Server-side window: the earliest backtest window of the bots on screen.
    // Backtest rows dominate the volume, so this keeps the initial load small;
    // open positions are always returned by the API regardless of the window
    // and "Load full history" widens to everything.
    const botsDataFrom = useMemo(() => {
        let min = Infinity;
        bots.forEach(b => {
            const t = b.last_backtest_summary?.data_from ? new Date(b.last_backtest_summary.data_from).getTime() : NaN;
            if (!Number.isNaN(t) && t < min) min = t;
        });
        return min === Infinity ? null : min;
    }, [bots]);
    const [fullHistory, setFullHistory] = useState(false);
    const serverFrom = fullHistory ? null : botsDataFrom;
    // Incremental polling: newest ids + last poll time (see /api/trades/positions)
    const pollCursor = useRef({ posId: 0, ordId: 0, since: null, from: undefined });

    const fetchAllData = useCallback(async ({ silent = false, signal } = {}) => {
        if (!silent) setLoading(true);
        const cur = pollCursor.current;
        const incremental = silent && cur.from === serverFrom && (cur.posId > 0 || cur.ordId > 0);
        const base = serverFrom !== null ? { from: new Date(serverFrom).toISOString() } : {};
        const polledAt = new Date();
        try {
            const [posRes, ordRes] = await Promise.all([
                apiClient.get('/api/trades/positions', { signal, params: incremental
                    ? { ...base, limit: 0, since_id: cur.posId, since: cur.since }
                    : { ...base, limit: 0 } }),
                apiClient.get('/api/trades/orders', { signal, params: incremental
                    ? { ...base, limit: 0, since_id: cur.ordId }
                    : { ...base, limit: 0 } }),
            ]);
            const posNew = posRes.data || [];
            const ordNew = ordRes.data || [];
            let pos = posNew, ord = ordNew;
            if (incremental) {
                // Merge by id: changed/new rows replace, everything else stays
                pos = mergeById(positionsRef.current, posNew);
                ord = ordNew.length ? mergeById(ordersRef.current, ordNew) : ordersRef.current;
            }
            pollCursor.current = {
                from: serverFrom,
                posId: pos.reduce((m, p) => Math.max(m, p.id), 0),
                ordId: ord.reduce((m, o) => Math.max(m, o.id), 0),
                // 5 min margin against client/server clock skew — re-sent rows merge by id
                since: new Date(polledAt.getTime() - 5 * 60 * 1000).toISOString(),
            };
            setPositions(pos);
            setOrders(ord);
            setLastUpdated(polledAt);
            if (setError) setError(null);
            fetchLivePrices(pos);
        } catch (err) {
            if (signal?.aborted) return;
            if (!silent && setError) setError(err.response?.data?.detail || 'Failed to load analytics data.');
        }
        if (!silent) setLoading(false);
        setHasLoadedOnce(true);
    }, [setError, fetchLivePrices, serverFrom]);

    // While any bot runs, new fills can land at any candle close — keep the
    // tables honest without the user hammering Sync.
    const anyBotActive = bots.some(b => b.is_active);
    useEffect(() => {
        if (!anyBotActive) return undefined;
        const controller = new AbortController();
        const t = setInterval(() => fetchAllData({ silent: true, signal: controller.signal }), 30000);
        return () => { controller.abort(); clearInterval(t); };
    }, [anyBotActive, fetchAllData]);

    useEffect(() => {
        const controller = new AbortController();
        fetchAllData({ signal: controller.signal }); // eslint-disable-line react-hooks/set-state-in-effect -- initial data load on mount
        return () => controller.abort();
    }, [fetchAllData]);

    useEffect(() => {
        if (positions.length === 0) return;
        const t = setInterval(() => fetchLivePrices(positionsRef.current), 10000);
        return () => clearInterval(t);
    }, [positions.length, fetchLivePrices]);

    // ── Filters ───────────────────────────────────────────────────────────────

    // Positions don't carry a timeframe; resolve it through the owning bot
    const tfByBot = useMemo(() => {
        const map = {};
        bots.forEach(b => { map[b.name] = b.settings?.timeframe || null; });
        return map;
    }, [bots]);

    const filterMode = useMemo(
        () => filterModeChoice ?? defaultModeFor(positions),
        [filterModeChoice, positions]);

    const applyFilters = useCallback((arr) =>
        arr
            .filter(x => filterBot === 'all' || x.bot_name === filterBot)
            .filter(x => filterSymbol === 'all' || x.symbol === filterSymbol)
            .filter(x => filterExchange === 'all' || (x.exchange || 'okx') === filterExchange)
            .filter(x => modeMatches(filterMode, x.mode))
            .filter(x => filterInterval === 'all' || tfByBot[x.bot_name] === filterInterval),
    [filterBot, filterSymbol, filterExchange, filterMode, filterInterval, tfByBot]);

    const resetPage = () => setCurrentPage(1);

    const inWindow = useCallback((iso) => {
        if (dateFrom === null && dateTo === null) return true;
        const t = new Date(iso).getTime();
        if (Number.isNaN(t)) return true;
        return (dateFrom === null || t >= dateFrom) && (dateTo === null || t <= dateTo);
    }, [dateFrom, dateTo]);

    // Full span of closed trades (before the date window) — bounds for the slider.
    const dateBounds = useMemo(() => {
        let min = Infinity, max = -Infinity;
        positions.forEach(p => {
            if (p.status !== 'closed' || !p.closed_at) return;
            const t = new Date(p.closed_at).getTime();
            if (Number.isNaN(t)) return;
            if (t < min) min = t;
            if (t > max) max = t;
        });
        return min === Infinity ? null : { min, max };
    }, [positions]);

    const closedPositions = useMemo(() =>
        applyFilters(positions.filter(p => p.status === 'closed' && inWindow(p.closed_at)))
            .sort((a, b) => new Date(b.closed_at) - new Date(a.closed_at)),
    [positions, applyFilters, inWindow]);

    const activePositions = useMemo(() =>
        applyFilters(positions.filter(p => p.status === 'open')),
    [positions, applyFilters]);

    const filteredOrders = useMemo(() =>
        applyFilters(orders.filter(o => inWindow(o.timestamp))).sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp)),
    [orders, applyFilters, inWindow]);

    // ── Actions ───────────────────────────────────────────────────────────────

    const deleteHistoricalTrade = async (id) => {
        const ok = await confirmDialog({
            title: 'Delete Trade Record',
            message: 'Permanently delete this trade from the ledger? This will affect your statistics.',
            confirmText: 'Delete',
            type: 'danger',
        });
        if (!ok) return;
        setBusyAction(true);
        try {
            await apiClient.delete(`/api/trades/positions/${id}`);
            toast.success('Trade record deleted.');
            fetchAllData();
        } catch {
            toast.error('Failed to delete trade.');
        }
        setBusyAction(false);
    };

    const forceClosePosition = async (pos) => {
        const id = pos.id;
        const real = REAL_MODES.has(pos.mode);
        const exch = (pos.exchange || 'okx').toUpperCase();
        const cur = livePrices[pos.symbol];
        // The backend places a real market order for paper/live positions;
        // simulated modes are closed against the last local candle. Say which.
        const ok = await confirmDialog(real ? {
            title: pos.mode === 'live' ? 'Sell at market — real order' : 'Sell at market — sandbox order',
            message: `Place a market SELL of ${formatCrypto(pos.amount)} ${pos.symbol} on ${exch} now${cur ? ` (last ~$${safeNum(cur)})` : ''}? `
                + `Fills at whatever the book gives — even at a loss. This cannot be undone.`,
            confirmText: 'Place market sell',
            type: 'danger',
        } : {
            title: 'Close Simulated Position',
            message: `Close this ${pos.mode === 'forward_test' ? 'forward-test' : pos.mode} position at the last known local market price? Nothing is sent to the exchange. It will be added to your Historical Ledger.`,
            confirmText: 'Close',
            type: 'warning',
        });
        if (!ok) return;
        setBusyAction(true);
        setClosingId(id);
        try {
            const res = await apiClient.post(`/api/trades/positions/${id}/close`);
            toast.success(res.data?.message || 'Position closed.');
            fetchAllData();
        } catch (e) {
            toast.error(e.response?.data?.detail || 'Failed to close position.');
        }
        setClosingId(null);
        setBusyAction(false);
    };

    const bulkDelete = async () => {
        if (closedPositions.length === 0) return;
        const ok = await confirmDialog({
            title: 'Bulk Delete Trades',
            message: `WARNING: Permanently delete ALL ${closedPositions.length} historical trades matching your current filters?`,
            confirmText: 'Delete All Filtered',
            type: 'danger',
        });
        if (!ok) return;
        setBusyAction(true);
        try {
            const ids = closedPositions.map(p => p.id);
            await apiClient.post('/api/trades/positions/bulk-delete', ids);
            toast.success(`Deleted ${ids.length} historical trades.`);
            fetchAllData();
        } catch {
            toast.error('Failed to delete trades.');
        }
        setBusyAction(false);
    };

    // ── Unique filter options ─────────────────────────────────────────────────

    const uniqueBots = useMemo(() => [...new Set(positions.map(p => p.bot_name).filter(Boolean))], [positions]);
    const uniqueSymbols = useMemo(() => [...new Set([...positions, ...orders].map(x => x.symbol).filter(Boolean))], [positions, orders]);
    const uniqueExchanges = useMemo(() => [...new Set([...positions, ...orders].map(x => x.exchange || 'okx').filter(Boolean))], [positions, orders]);
    const uniqueIntervals = useMemo(() => [...new Set(positions.map(p => tfByBot[p.bot_name]).filter(Boolean))].sort(), [positions, tfByBot]);

    // When a single bot is selected, prefer the drawdown the engine measured
    // and enforces (mark-to-market over the backtest, incl. open-position dips)
    const engineDrawdown = useMemo(() => {
        if (filterBot === 'all') return null;
        const dd = bots.find(b => b.name === filterBot)?.settings?.last_backtest_max_drawdown;
        return (dd === null || dd === undefined) ? null : dd;
    }, [filterBot, bots]);

    // ── Pre-computed lookups (shared by stats + ledger rows) ───────────────

    const feesByPosId = useMemo(() => {
        const map = {};
        for (const o of orders) {
            if (o.position_id && o.fee) {
                map[o.position_id] = (map[o.position_id] || 0) + o.fee;
            }
        }
        return map;
    }, [orders]);

    const entryTsByPos = useMemo(() => {
        const map = {};
        for (const o of orders) {
            if (o.side === 'buy' && o.status === 'filled' && o.position_id && o.timestamp) {
                const t = new Date(o.timestamp);
                if (!map[o.position_id] || t < map[o.position_id]) map[o.position_id] = t;
            }
        }
        return map;
    }, [orders]);

    // ── Stats ─────────────────────────────────────────────────────────────────

    const stats = useMemo(() => {
        const wins = closedPositions.filter(p => (p.profit_abs || 0) > 0);
        const losses = closedPositions.filter(p => (p.profit_abs || 0) <= 0);
        const grossProfit = wins.reduce((s, p) => s + (p.profit_abs || 0), 0);
        const grossLoss = Math.abs(losses.reduce((s, p) => s + (p.profit_abs || 0), 0));
        const netPnl = grossProfit - grossLoss;
        const winRate = closedPositions.length > 0 ? (wins.length / closedPositions.length) * 100 : 0;
        const profitFactor = grossLoss > 0 ? grossProfit / grossLoss : (grossProfit > 0 ? 999 : 0);

        // Max drawdown — percentage of peak equity using the pools in view as starting equity
        const sorted = [...closedPositions].sort((a, b) => new Date(a.closed_at) - new Date(b.closed_at));
        // Look up backtest_capital from bot config (default $1000)
        const filteredBotNames = [...new Set(sorted.map(p => p.bot_name).filter(Boolean))];
        const viewModes = modesOf(sorted);
        const capitalPerBot = filteredBotNames.map(name => botCapital(bots.find(b => b.name === name), viewModes));
        // Per-bot capital is a separate pool, so the capital in view is the sum
        // across the bots in view — one base for both the return and the
        // drawdown below. No trades in view → no capital deployed; never fall
        // back to a phantom $1000.
        const totalCapital = capitalPerBot.reduce((a, b) => a + b, 0);

        // Simulated and real trades never sum to one number: when the view
        // mixes modes the tile shows one line per mode instead
        const pnlByMode = {};
        for (const p of closedPositions) {
            if (!p.mode) continue;
            pnlByMode[p.mode] = (pnlByMode[p.mode] || 0) + (p.profit_abs || 0);
        }
        const modes = sortModes(Object.keys(pnlByMode));
        const mixed = modes.length > 1;

        let equity = totalCapital, peakEq = totalCapital, maxDDpct = 0;
        for (const p of sorted) {
            equity += (p.profit_abs || 0);
            if (equity > peakEq) peakEq = equity;
            if (peakEq > 0) maxDDpct = Math.max(maxDDpct, ((peakEq - equity) / peakEq) * 100);
        }

        const entryOf = (p, exitTs) => entryTimeOf(p, exitTs, entryTsByPos);
        const spans = closedPositions.map(p => {
            if (!p.closed_at) return null;
            const exitTs = new Date(p.closed_at);
            const entryTs = entryOf(p, exitTs);
            return entryTs ? { start: entryTs.getTime(), end: exitTs.getTime() } : null;
        }).filter(Boolean);
        const avgHoldMs = spans.length > 0
            ? spans.reduce((s, i) => s + (i.end - i.start), 0) / spans.length
            : 0;

        // Return/Risk (simplified Sharpe)
        const returns = closedPositions.map(p => p.profit_pct || 0);
        const mean = returns.length > 0 ? returns.reduce((a, b) => a + b, 0) / returns.length : 0;
        const stddev = returns.length > 1
            ? Math.sqrt(returns.reduce((s, r) => s + Math.pow(r - mean, 2), 0) / (returns.length - 1))
            : 0;
        const sharpe = stddev > 0 ? mean / stddev : 0;

        // Total fees from orders linked to filtered positions
        const filteredPosIds = new Set(closedPositions.map(p => p.id));
        const totalFees = orders
            .filter(o => o.position_id && filteredPosIds.has(o.position_id))
            .reduce((s, o) => s + (o.fee || 0), 0);

        return {
            netPnl,
            winRate,
            wins: wins.length,
            losses: losses.length,
            total: closedPositions.length,
            openCount: activePositions.length,
            profitFactor,
            maxDDpct,
            avgHoldMs,
            avgTrade: closedPositions.length > 0 ? netPnl / closedPositions.length : 0,
            sharpe,
            totalFees,
            avgWin: wins.length > 0 ? grossProfit / wins.length : 0,
            avgLoss: losses.length > 0 ? grossLoss / losses.length : 0,
            totalCapital,
            botCount: filteredBotNames.length,
            returnPct: totalCapital > 0 ? (netPnl / totalCapital) * 100 : 0,
            modes,
            mixed,
            pnlByMode,
        };
    }, [closedPositions, activePositions, orders, entryTsByPos, bots]);

    // ── Breakdown tables (by algorithm / by pair) ─────────────────────────────

    const breakdownRows = useMemo(() => {
        const build = (keyFn, labelFn) => {
            const groups = new Map();
            for (const p of closedPositions) {
                const key = keyFn(p);
                if (!groups.has(key)) groups.set(key, { key, label: labelFn(p), trades: 0, wins: 0, gross: 0, loss: 0, net: 0, modes: new Set(), fees: 0, holdMs: 0, holdN: 0, best: -Infinity, worst: Infinity, spans: [], botNames: new Set(), returns: [] });
                const g = groups.get(key);
                const pnl = p.profit_abs || 0;
                g.trades += 1;
                if (pnl > 0) { g.wins += 1; g.gross += pnl; } else { g.loss += Math.abs(pnl); }
                g.net += pnl;
                g.modes.add(p.mode);
                g.fees += feesByPosId[p.id] || 0;
                g.best = Math.max(g.best, pnl);
                g.worst = Math.min(g.worst, pnl);
                g.returns.push(p.profit_pct || 0);
                if (p.closed_at && p.created_at) {
                    const h = new Date(p.closed_at) - new Date(p.created_at);
                    if (h > 0) { g.holdMs += h; g.holdN += 1; }
                }
                g.botNames.add(p.bot_name);
                if (p.closed_at) {
                    const exitTs = new Date(p.closed_at);
                    const entryTs = entryTimeOf(p, exitTs, entryTsByPos);
                    if (entryTs) g.spans.push({ start: entryTs.getTime(), end: exitTs.getTime() });
                }
            }
            // Open positions are "in the market" indefinitely for the flat-gap calc
            for (const p of activePositions) {
                const g = groups.get(keyFn(p));
                if (!g) continue;
                const entryTs = entryTimeOf(p, new Date(8.64e15), entryTsByPos);
                if (entryTs) g.spans.push({ start: entryTs.getTime(), end: Infinity });
            }
            return [...groups.values()].map(g => {
                const bot = bots.find(b => b.name === g.key);
                const capital = bot ? botCapital(bot, g.modes) : null;
                // Flat-gap window: the date filter, else the data range the engine
                // walked in the last backtest of the bots behind this row
                let dataFrom = null, dataTo = null;
                for (const name of g.botNames) {
                    const sm = bots.find(b => b.name === name)?.settings?.last_backtest_summary;
                    const f = sm?.data_from ? new Date(sm.data_from).getTime() : NaN;
                    const t = sm?.data_to ? new Date(sm.data_to).getTime() : NaN;
                    if (!Number.isNaN(f) && (dataFrom === null || f < dataFrom)) dataFrom = f;
                    if (!Number.isNaN(t) && (dataTo === null || t > dataTo)) dataTo = t;
                }
                const longestFlat = longestFlatGap(g.spans, dateFrom ?? dataFrom, dateTo ?? dataTo);
                // Same per-trade Sharpe as the Return / Risk tile: mean trade
                // return ÷ sample std dev, not annualised
                const n = g.returns.length;
                const mean = n ? g.returns.reduce((a, b) => a + b, 0) / n : 0;
                const sd = n > 1 ? Math.sqrt(g.returns.reduce((a, r) => a + (r - mean) ** 2, 0) / (n - 1)) : 0;
                const sharpe = n > 1 && sd > 0 ? mean / sd : null;
                return {
                    ...g,
                    spans: undefined,
                    botNames: undefined,
                    returns: undefined,
                    longestFlat,
                    sharpe,
                    modes: [...g.modes],
                    winRate: g.trades ? (g.wins / g.trades) * 100 : 0,
                    profitFactor: g.loss > 0 ? g.gross / g.loss : (g.gross > 0 ? Infinity : 0),
                    avgHoldMs: g.holdN ? g.holdMs / g.holdN : 0,
                    capital,
                    returnPct: capital ? (g.net / capital) * 100 : null,
                    engineDD: bot?.settings?.last_backtest_max_drawdown ?? null,
                    timeframe: bot?.settings?.timeframe || tfByBot[g.key] || null,
                    isActive: !!bot?.is_active,
                };
            }).sort((a, b) => b.net - a.net);
        };
        return {
            byBot: build(p => p.bot_name, p => p.bot_name),
            bySymbol: build(p => `${p.exchange || 'okx'}:${p.symbol}`, p => p.symbol),
        };
    }, [closedPositions, activePositions, entryTsByPos, feesByPosId, bots, tfByBot, dateFrom, dateTo]);

    const [breakdownView, setBreakdownView] = useState('bot');

    // ── Capital allocation (config-driven: pool, entry size, exposure) ────────
    // Each bot owns one capital pool shared by all its pairs; a pair never has a
    // fixed budget. Rows respect the bot / pair / exchange / timeframe filters.
    const allocation = useMemo(() => {
        const deployedByBot = new Map();
        const deployedByPair = new Map();
        for (const p of positions) {
            if (p.status !== 'open') continue;
            const v = (p.amount || 0) * (p.entry_price || 0);
            const b = deployedByBot.get(p.bot_name) || { value: 0, count: 0 };
            b.value += v; b.count += 1; deployedByBot.set(p.bot_name, b);
            const s = deployedByPair.get(p.symbol) || { value: 0, count: 0, bots: new Set() };
            s.value += v; s.count += 1; s.bots.add(p.bot_name); deployedByPair.set(p.symbol, s);
        }

        const byBot = bots
            .filter(b => filterBot === 'all' || b.name === filterBot)
            .filter(b => filterExchange === 'all' || (b.settings?.data_exchange || 'okx') === filterExchange)
            .filter(b => filterInterval === 'all' || b.settings?.timeframe === filterInterval)
            .map(b => {
                const s = b.settings || {};
                const symbols = Array.isArray(s.symbols) && s.symbols.length ? s.symbols : (s.symbol ? [s.symbol] : []);
                if (filterSymbol !== 'all' && !symbols.includes(filterSymbol)) return null;
                const pool = Number(s.backtest_capital) || 1000;
                const rawVal = Number(s.entry_amount_value);
                const isFixed = s.entry_amount_type === 'fixed';
                const entryPct = isFixed
                    ? (rawVal > 0 ? (rawVal / pool) * 100 : 100)
                    : (rawVal > 0 ? rawVal : 100);
                const entryUsd = isFixed ? (rawVal > 0 ? rawVal : pool) : pool * (entryPct / 100);
                const maxPositions = Math.max(1, Number(s.max_positions) || 1);
                const cap = Number(s.max_order_value) || 0;
                const exposurePct = Math.min(100, entryPct * maxPositions);
                const dep = deployedByBot.get(b.name) || { value: 0, count: 0 };
                return {
                    key: b.name, label: b.name, isActive: !!b.is_active, timeframe: s.timeframe,
                    mode: b.execution_mode || (s.api_execution ? 'live' : 'forward_test'),
                    exchange: s.data_exchange || 'okx',
                    pool, entryPct, entryUsd, isFixed, maxPositions, cap,
                    exposurePct, exposureUsd: pool * (exposurePct / 100),
                    symbols, deployed: dep.value, openCount: dep.count,
                    free: Math.max(0, pool - dep.value),
                    runtime: b.runtime?.phase || null,
                };
            })
            .filter(Boolean)
            .sort((a, b) => b.pool - a.pool);

        // Per pair: which bots can trade it and what a single entry may commit.
        const pairMap = new Map();
        for (const r of byBot) {
            for (const sym of r.symbols) {
                if (filterSymbol !== 'all' && sym !== filterSymbol) continue;
                const g = pairMap.get(sym) || { key: sym, label: sym, bots: [], maxEntry: 0, poolAccess: 0 };
                g.bots.push(r.label);
                g.maxEntry += r.entryUsd;
                g.poolAccess += r.pool;
                pairMap.set(sym, g);
            }
        }
        const byPair = [...pairMap.values()].map(g => {
            const dep = deployedByPair.get(g.key) || { value: 0, count: 0 };
            return { ...g, deployed: dep.value, openCount: dep.count };
        }).sort((a, b) => b.deployed - a.deployed || b.maxEntry - a.maxEntry);

        const totalPool = byBot.reduce((a, r) => a + r.pool, 0);
        const totalDeployed = byBot.reduce((a, r) => a + r.deployed, 0);
        const totalExposure = byBot.reduce((a, r) => a + r.exposureUsd, 0);
        return { byBot, byPair, totalPool, totalDeployed, totalExposure };
    }, [bots, positions, filterBot, filterSymbol, filterExchange, filterInterval]);

    const [allocationView, setAllocationView] = useState('bot');

    // ── Monthly net PNL (last 12 months with activity) ────────────────────────

    const monthlyReturns = useMemo(() => {
        const months = new Map();
        for (const p of closedPositions) {
            if (!p.closed_at) continue;
            const d = new Date(p.closed_at);
            const key = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
            const m = months.get(key) || { key, net: 0, trades: 0 };
            m.net += p.profit_abs || 0;
            m.trades += 1;
            months.set(key, m);
        }
        const rows = [...months.values()].sort((a, b) => a.key.localeCompare(b.key)).slice(-12);
        const maxAbs = rows.reduce((m, r) => Math.max(m, Math.abs(r.net)), 0) || 1;
        return rows.map(r => ({ ...r, share: Math.abs(r.net) / maxAbs, label: new Date(`${r.key}-01T00:00:00Z`).toLocaleDateString(undefined, { month: 'short', year: '2-digit', timeZone: 'UTC' }) }));
    }, [closedPositions]);

    // ── Equity curve data ─────────────────────────────────────────────────────

    const equityCurveData = useMemo(() => {
        const sorted = [...closedPositions].sort((a, b) => new Date(a.closed_at) - new Date(b.closed_at));
        // Starting equity = sum of the pools of the bots in view (same rule as the stats grid)
        const names = [...new Set(sorted.map(p => p.bot_name).filter(Boolean))];
        const viewModes = modesOf(sorted);
        const capital = names.reduce((a, n) => a + botCapital(bots.find(b => b.name === n), viewModes), 0);
        const result = [];
        let cum = 0, peak = capital;
        for (const p of sorted) {
            cum += (p.profit_abs || 0);
            const equity = capital + cum;
            if (equity > peak) peak = equity;
            result.push({
                date: new Date(p.closed_at), value: cum, equity, capital, peak,
                drawdownPct: peak > 0 ? ((peak - equity) / peak) * 100 : 0,
                trade: { bot: p.bot_name, symbol: p.symbol, pnl: p.profit_abs || 0, pct: p.profit_pct || 0, mode: p.mode },
                index: result.length + 1,
            });
        }
        return result;
    }, [closedPositions, bots]);

    // ── Buy & Hold comparison ─────────────────────────────────────────────────

    // Backtest trades are benchmarked against holding over the full range the
    // engine walked (`last_backtest_summary.buy_hold`, data_from → data_to):
    // a strategy that sat flat before its first entry still gets charged for
    // the move it missed. Real/forward trades have no such range — they are
    // benchmarked from the first entry to now (or to the last exit in a
    // bounded window). A mixed view is not comparable and says so.
    const buyAndHoldData = useMemo(() => {
        const viewModes = modesOf([...activePositions, ...closedPositions]);
        const onlyBacktest = viewModes.size > 0 && [...viewModes].every(m => m === 'backtest');
        const onlyLive = viewModes.size > 0 && [...viewModes].every(m => m !== 'backtest');
        const basis = onlyBacktest ? 'backtest' : (onlyLive ? 'live' : (viewModes.size === 0 ? 'none' : 'mixed'));

        const bySymbol = {};
        // Scan ALL filtered positions (open + closed) so the reference entry price
        // reflects the true first entry even when that position is still open
        for (const p of [...activePositions, ...closedPositions]) {
            if (!p.symbol || !p.created_at || !p.entry_price) continue;
            if (!bySymbol[p.symbol]) {
                bySymbol[p.symbol] = { firstDate: new Date(p.created_at), firstPrice: p.entry_price, positions: [], botNames: new Set() };
            }
            const s = bySymbol[p.symbol];
            if (p.bot_name) s.botNames.add(p.bot_name);
            if (new Date(p.created_at) < s.firstDate) {
                s.firstDate = new Date(p.created_at);
                s.firstPrice = p.entry_price;
            }
        }
        // Strategy P&L only from closed positions
        for (const p of closedPositions) {
            if (bySymbol[p.symbol]) bySymbol[p.symbol].positions.push(p);
        }
        // Engine buy & hold per symbol: the widest range across the bots that
        // traded it (identical when one bot is in view, the common case)
        const engineBh = (symbol, botNames) => {
            let best = null;
            for (const name of botNames) {
                const sm = bots.find(b => b.name === name)?.settings?.last_backtest_summary;
                const bh = sm?.buy_hold?.[symbol];
                if (!bh || typeof bh.pct !== 'number') continue;
                const from = sm.data_from ? new Date(sm.data_from).getTime() : NaN;
                if (!best || (!Number.isNaN(from) && from < best.from)) best = { pct: bh.pct, from, to: sm.data_to ? new Date(sm.data_to).getTime() : NaN };
            }
            return best;
        };
        const rows = Object.entries(bySymbol)
            .filter(([, d]) => d.positions.length > 0)
            .map(([symbol, d]) => {
                const strategyPnl = d.positions.reduce((s, p) => s + (p.profit_abs || 0), 0);
                // Strategy % = total PnL over the pools of the bots that traded
                // this symbol — the same base the stats grid uses
                const botNames = [...new Set(d.positions.map(p => p.bot_name).filter(Boolean))];
                const symModes = modesOf(d.positions);
                const capital = botNames.reduce((a, name) => a + botCapital(bots.find(b => b.name === name), symModes), 0) || 1000;
                const strategyPct = capital > 0 ? (strategyPnl / capital) * 100 : 0;

                let bhPct = null, range = null;
                if (basis === 'backtest' && dateFrom === null && dateTo === null) {
                    const bh = engineBh(symbol, d.botNames);
                    if (bh) { bhPct = bh.pct; range = { from: bh.from, to: bh.to }; }
                }
                if (bhPct === null && basis !== 'mixed') {
                    // With a bounded window, B&H ends at the last exit inside it instead of today's price
                    let curPrice = livePrices[symbol];
                    if (dateTo !== null) {
                        const last = d.positions.reduce((acc, p) => (!acc || new Date(p.closed_at) > new Date(acc.closed_at)) ? p : acc, null);
                        if (last?.entry_price > 0 && typeof last.profit_pct === 'number') curPrice = last.entry_price * (1 + last.profit_pct / 100);
                    }
                    if (curPrice && d.firstPrice > 0) bhPct = ((curPrice - d.firstPrice) / d.firstPrice) * 100;
                }
                const edge = bhPct !== null ? strategyPct - bhPct : null;
                return { symbol, strategyPct, bhPct, edge, strategyPnl, capital, range };
            });

        // Equal-weight portfolio: the strategy's total return on the capital in
        // view against holding an equal slice of every symbol it traded
        const withBh = rows.filter(r => r.bhPct !== null);
        let portfolio = null;
        if (rows.length > 1 && withBh.length === rows.length) {
            const capital = stats.totalCapital || rows.reduce((a, r) => a + r.capital, 0);
            const strategyPct = capital > 0 ? (rows.reduce((a, r) => a + r.strategyPnl, 0) / capital) * 100 : 0;
            const bhPct = rows.reduce((a, r) => a + r.bhPct, 0) / rows.length;
            portfolio = { strategyPct, bhPct, edge: strategyPct - bhPct, symbols: rows.length };
        }
        let from = null, to = null;
        for (const r of rows) {
            if (!r.range) continue;
            if (!Number.isNaN(r.range.from) && (from === null || r.range.from < from)) from = r.range.from;
            if (!Number.isNaN(r.range.to) && (to === null || r.range.to > to)) to = r.range.to;
        }
        return { rows, portfolio, basis, range: from !== null ? { from, to } : null };
    }, [closedPositions, activePositions, livePrices, bots, dateFrom, dateTo, stats.totalCapital]);

    // ── Helpers ───────────────────────────────────────────────────────────────

    const getLivePnl = (pos) => {
        const cur = livePrices[pos.symbol];
        if (!cur) return { abs: 0, pct: 0 };
        const isLong = pos.side !== 'short';
        const abs = isLong ? (cur - pos.entry_price) * pos.amount : (pos.entry_price - cur) * pos.amount;
        const pct = isLong ? ((cur - pos.entry_price) / pos.entry_price) * 100 : ((pos.entry_price - cur) / pos.entry_price) * 100;
        return { abs, pct };
    };

    const getExitPrice = (pos) => {
        if (!pos.profit_abs || !pos.entry_price || !pos.amount) return null;
        return pos.side === 'short'
            ? pos.entry_price - pos.profit_abs / pos.amount
            : pos.entry_price + pos.profit_abs / pos.amount;
    };

    // ── CSV Export ────────────────────────────────────────────────────────────

    const exportToCSV = () => {
        if (activeTab === 'positions') {
            if (closedPositions.length === 0) { toast.info('No trades to export for the current filters.'); return; }
            const headers = ['Date Closed', 'Bot', 'Exchange', 'Mode', 'Symbol', 'Side', 'Entry', 'Exit', 'Amount', 'Hold Time', 'Return %', 'Net PNL', 'Fees'];
            const rows = closedPositions.map(p => {
                const fees = feesByPosId[p.id] || 0;
                const holdMs = p.closed_at && p.created_at ? new Date(p.closed_at) - new Date(p.created_at) : 0;
                const exit = getExitPrice(p);
                return [
                    new Date(p.closed_at).toISOString(),
                    p.bot_name, p.exchange || 'okx', p.mode, p.symbol, p.side,
                    p.entry_price, exit?.toFixed(6) ?? '', p.amount,
                    formatHoldTime(holdMs), p.profit_pct, p.profit_abs, fees.toFixed(4),
                ].join(',');
            });
            triggerDownload([headers.join(','), ...rows].join('\n'), 'apex_positions_ledger');
            toast.success(`Exported ${closedPositions.length} trades to CSV.`);
        } else {
            if (filteredOrders.length === 0) { toast.info('No orders to export for the current filters.'); return; }
            const headers = ['Timestamp', 'Bot', 'Exchange', 'Mode', 'Symbol', 'Side', 'Type', 'Price', 'Amount', 'Fee', 'Status'];
            const rows = filteredOrders.map(o => [
                new Date(o.timestamp).toISOString(),
                o.bot_name, o.exchange || 'okx', o.mode, o.symbol,
                o.side, o.order_type, o.price, o.amount, o.fee ?? '', o.status,
            ].join(','));
            triggerDownload([headers.join(','), ...rows].join('\n'), 'apex_raw_orders');
            toast.success(`Exported ${filteredOrders.length} orders to CSV.`);
        }
    };

    const triggerDownload = (csv, prefix) => {
        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = `${prefix}_${new Date().toISOString().split('T')[0]}.csv`;
        link.style.visibility = 'hidden';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    };

    // ── Pagination ────────────────────────────────────────────────────────────

    const totalPagesPos = Math.ceil(closedPositions.length / itemsPerPage);
    const totalPagesOrd = Math.ceil(filteredOrders.length / itemsPerPage);
    const renderedPositions = closedPositions.slice((currentPage - 1) * itemsPerPage, currentPage * itemsPerPage);
    const renderedOrders = filteredOrders.slice((currentPage - 1) * itemsPerPage, currentPage * itemsPerPage);

    // ── Shared styles ─────────────────────────────────────────────────────────

    const tabClass = (active) => `pb-2.5 text-xs font-bold uppercase tracking-wider transition-all duration-200 border-b-2 ${active ? 'text-accent border-accent' : 'text-muted border-transparent hover:text-text'}`;
    const thClass = 'px-3 py-1.5 text-3xs font-bold uppercase tracking-wider text-muted whitespace-nowrap';

    const initialLoading = loading && !hasLoadedOnce;

    // ─────────────────────────────────────────────────────────────────────────
    // RENDER
    // ─────────────────────────────────────────────────────────────────────────

    return (
        <PageShell>

            {/* ── FILTER BAR ─────────────────────────────────────────────────── */}
            <div className="terminal-card px-3 py-2 md:sticky md:top-0 z-20">
                {/* Phone: one summary line that unfolds into the full panel (desktop shows it always). */}
                <div className="flex items-center justify-between gap-2 md:hidden">
                    <button type="button" onClick={() => setFiltersOpen(o => !o)} aria-expanded={filtersOpen}
                        className="flex items-center gap-2 min-w-0 flex-1 text-left py-1 text-muted hover:text-text transition-colors">
                        <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 5h18l-7 8v5l-4 2v-7L3 5z" /></svg>
                        <span className="text-2xs font-bold uppercase tracking-wider shrink-0">Filters</span>
                        <span className="text-xs text-text truncate">
                            {[
                                filterBot === 'all' ? 'All bots' : filterBot,
                                MODE_LABELS[filterMode] || filterMode,
                                filterSymbol === 'all' ? null : filterSymbol,
                                filterInterval === 'all' ? null : filterInterval,
                                filterExchange === 'all' ? null : filterExchange.toUpperCase(),
                            ].filter(Boolean).join(' · ')}
                        </span>
                        <svg className={`w-3.5 h-3.5 shrink-0 transition-transform ${filtersOpen ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                    </button>
                    <Button variant="secondary" size="sm" onClick={() => fetchAllData()} loading={loading}>Sync</Button>
                </div>
                <div className={`${filtersOpen ? 'block pt-3 mt-2 border-t border-border' : 'hidden'} md:block md:pt-0 md:mt-0 md:border-0`}>
                <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-3">
                    <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 flex-1 min-w-[280px] max-w-[880px]">
                        <Select label="Algorithm" value={filterBot} onChange={e => { setFilterBot(e.target.value); resetPage(); }} className="py-1.5! text-xs!">
                            <option value="all">All Bots</option>
                            {uniqueBots.map(b => <option key={b} value={b}>{b}</option>)}
                        </Select>
                        <Select label="Interval" value={filterInterval} onChange={e => { setFilterInterval(e.target.value); resetPage(); }} className="py-1.5! text-xs! font-num">
                            <option value="all">All Intervals</option>
                            {uniqueIntervals.map(tf => <option key={tf} value={tf}>{tf}</option>)}
                        </Select>
                        <Select label="Asset" value={filterSymbol} onChange={e => { setFilterSymbol(e.target.value); resetPage(); }} className="py-1.5! text-xs! font-num">
                            <option value="all">All Pairs</option>
                            {uniqueSymbols.map(s => <option key={s} value={s}>{s}</option>)}
                        </Select>
                        <Select label="Exchange" value={filterExchange} onChange={e => { setFilterExchange(e.target.value); resetPage(); }} className="py-1.5! text-xs!">
                            <option value="all">All Exchanges</option>
                            {uniqueExchanges.map(ex => <option key={ex} value={ex}>{ex.toUpperCase()}</option>)}
                        </Select>
                        <Select label="Mode" value={filterMode} onChange={e => { setFilterMode(e.target.value); resetPage(); }} className="py-1.5! text-xs!">
                            <option value="real">Paper + Live (real)</option>
                            <option value="live">Live</option>
                            <option value="paper">Paper</option>
                            <option value="forward_test">Forward Test</option>
                            <option value="backtest">Backtest</option>
                            <option value="all">All Modes (mixed)</option>
                        </Select>
                    </div>
                    <div className="flex items-center gap-2 pb-0.5">
                        <Button variant="ghost" size="sm" onClick={exportToCSV}
                            icon={<svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" /></svg>}>
                            Export
                        </Button>
                        {lastUpdated && (
                            <span className="text-2xs text-faint font-num whitespace-nowrap" title={anyBotActive ? 'Refreshes every 30 s while a bot is running' : 'Press Sync to refresh'}>
                                updated {lastUpdated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}{anyBotActive ? ' · auto' : ''}
                            </span>
                        )}
                        <span className="hidden md:inline-flex">
                            <Button variant="secondary" size="sm" onClick={() => fetchAllData()} loading={loading}>Sync</Button>
                        </span>
                    </div>
                </div>
                <DateRangeControl bounds={dateBounds} from={dateFrom} to={dateTo}
                    onChange={(f, t) => { setDateFrom(f); setDateTo(t); resetPage(); }} />
                {serverFrom !== null && (
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 pt-2 text-2xs text-faint font-num">
                        <span>Loaded trades since {new Date(serverFrom).toISOString().slice(0, 10)} (earliest backtest window of your algorithms; open positions always included)</span>
                        <button type="button" onClick={() => setFullHistory(true)}
                            className="text-accent hover:underline font-bold" disabled={loading}>Load full history</button>
                    </div>
                )}
                </div>
            </div>

            {/* ── STATS GRID ─────────────────────────────────────────────────── */}
            {/* Row 1: result · Row 2: trade quality · Row 3: per-trade behaviour */}
            {initialLoading ? (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                    {Array.from({ length: 12 }).map((_, i) => <Skeleton key={i} className="h-[88px] w-full rounded-lg" />)}
                </div>
            ) : (
                <>
                {stats.mixed && (
                    <div role="status" className="flex flex-wrap items-center gap-2 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-2xs text-text">
                        <span className="font-bold uppercase tracking-wider text-warn">Mixed modes</span>
                        <span className="text-muted">Net PnL is shown per mode — simulated and real trades are never added up.</span>
                        <span className="flex items-center gap-1 ml-auto">
                            {stats.modes.map(m => <ModeBadge key={m} mode={m} short className="text-3xs!" />)}
                        </span>
                    </div>
                )}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                    {stats.mixed ? (
                        <StatCard
                            label="Net PNL · mixed"
                            value={
                                <span className="flex flex-col gap-0.5 text-sm">
                                    {stats.modes.map(m => (
                                        <span key={m} className="flex items-center justify-between gap-2">
                                            <ModeBadge mode={m} short className="text-3xs!" />
                                            <span className={`font-num ${pnlColor(stats.pnlByMode[m])}`}>{pnlSign(stats.pnlByMode[m])}${safeNum(Math.abs(stats.pnlByMode[m]))}</span>
                                        </span>
                                    ))}
                                </span>
                            }
                            sub="pick one mode for a return %"
                            color="neutral"
                        />
                    ) : (
                        <StatCard
                            label={`Net PNL${stats.modes.length === 1 ? ` · ${stats.modes[0] === 'forward_test' ? 'forward test' : stats.modes[0]}` : ''}`}
                            value={`${stats.netPnl >= 0 ? '+' : '-'}$${safeNum(Math.abs(stats.netPnl))}`}
                            sub={stats.total > 0 ? `${stats.returnPct >= 0 ? '+' : ''}${safeNum(stats.returnPct, 1)}% on $${safeNum(stats.totalCapital, 0)}` : 'no closed trades'}
                            color={stats.netPnl >= 0 ? 'success' : 'danger'}
                        />
                    )}
                    <StatCard
                        label="Starting Capital"
                        value={stats.botCount > 0 ? `$${safeNum(stats.totalCapital, 0)}` : '—'}
                        sub={stats.botCount > 1 ? `total across ${stats.botCount} bots` : (stats.botCount === 1 ? 'allocated to this bot' : 'no bots in view')}
                        color="accent"
                    />
                    <StatCard
                        label="Max Drawdown"
                        value={engineDrawdown !== null
                            ? `-${safeNum(engineDrawdown, 1)}%`
                            : (stats.total > 0 ? `-${safeNum(stats.maxDDpct, 1)}%` : '—')}
                        sub={engineDrawdown !== null
                            ? 'engine: mark-to-market (backtest)'
                            : 'closed trades only — intra-trade dips not included'}
                        color="danger"
                    />
                    <StatCard
                        label="Return / Risk"
                        value={stats.total > 1 ? safeNum(stats.sharpe) : '—'}
                        sub="mean return ÷ std dev"
                        color={stats.sharpe > 1 ? 'success' : stats.sharpe > 0 ? 'accent' : 'danger'}
                    />

                    <StatCard
                        label="Trades"
                        value={stats.total > 0 ? stats.total : '—'}
                        sub={stats.openCount > 0 ? `closed · ${stats.openCount} open now` : 'closed'}
                        color="neutral"
                    />
                    <StatCard
                        label="Win Rate"
                        value={stats.total > 0 ? `${safeNum(stats.winRate, 1)}%` : '—'}
                        sub={`${stats.wins} wins / ${stats.losses} losses`}
                        color="info"
                    />
                    <StatCard
                        label="Profit Factor"
                        value={stats.total > 0 ? (stats.profitFactor >= 999 ? '∞' : safeNum(stats.profitFactor)) : '—'}
                        sub="gross profit / gross loss"
                        color="accent"
                    />
                    <StatCard
                        label="Total Fees Paid"
                        value={stats.total > 0 ? `-$${safeNum(stats.totalFees)}` : '—'}
                        sub="all linked orders"
                        color={stats.totalFees > 0 ? 'danger' : 'neutral'}
                    />

                    <StatCard
                        label="Avg Win"
                        value={stats.wins > 0 ? `+$${safeNum(stats.avgWin)}` : '—'}
                        sub="per winning trade"
                        color="success"
                    />
                    <StatCard
                        label="Avg Loss"
                        value={stats.losses > 0 ? `-$${safeNum(stats.avgLoss)}` : '—'}
                        sub="per losing trade"
                        color="danger"
                    />
                    <StatCard
                        label="Avg Hold Time"
                        value={stats.total > 0 ? formatHoldTime(stats.avgHoldMs) : '—'}
                        sub="per closed position"
                        color="neutral"
                    />
                    <StatCard
                        label="Avg Trade"
                        value={stats.total > 0 ? `${stats.avgTrade >= 0 ? '+' : '-'}$${safeNum(Math.abs(stats.avgTrade))}` : '—'}
                        sub="expectancy · net PnL per closed trade"
                        color={stats.total > 0 ? (stats.avgTrade >= 0 ? 'success' : 'danger') : 'neutral'}
                    />
                </div>
                </>
            )}

            {/* ── EQUITY CURVE + BUY & HOLD ──────────────────────────────────── */}
            <div className="flex flex-col lg:flex-row gap-4">

                {/* Equity Curve */}
                <div className="terminal-card glow-panel-cyan p-5 flex-1 min-w-0">
                    <div className="flex items-center justify-between mb-4">
                        <div>
                            <h2 className="text-xs font-bold uppercase tracking-wider text-text">Equity Curve</h2>
                            <p className="text-3xs text-muted mt-0.5 uppercase tracking-wider">Cumulative PNL — closed trades</p>
                        </div>
                        <div className="flex items-center gap-4">
                            {/* Legend */}
                            <div className="hidden sm:flex items-center gap-3 text-3xs text-muted uppercase tracking-wider">
                                <span className="flex items-center gap-1.5">
                                    <span className={`w-3 h-0.5 rounded-full ${equityCurveData.length >= 2 && equityCurveData[equityCurveData.length - 1].value < 0 ? 'bg-danger' : 'bg-success'}`} />
                                    Cumulative PNL
                                </span>
                                <span className="flex items-center gap-1.5">
                                    <span className="w-3 border-t border-dashed border-border-strong" />
                                    Break-even
                                </span>
                            </div>
                            {equityCurveData.length >= 2 && (() => {
                                const last = equityCurveData[equityCurveData.length - 1];
                                return (
                                    <span className="text-right leading-tight">
                                        <span className={`block text-sm font-num font-bold ${pnlColor(last.value)}`}>
                                            {pnlSign(last.value)}${safeNum(Math.abs(last.value))}
                                        </span>
                                        <span className="block text-3xs font-num text-muted">
                                            ${safeNum(last.capital, 0)} → <span className="text-text">${safeNum(last.equity, 0)}</span>
                                        </span>
                                    </span>
                                );
                            })()}
                        </div>
                    </div>
                    <div className="h-[160px] w-full">
                        {initialLoading ? <Skeleton className="w-full h-full rounded-md" /> : <EquityCurve data={equityCurveData} />}
                    </div>
                </div>

                {/* Buy & Hold Comparison */}
                <div className="terminal-card p-4 lg:w-[340px] shrink-0">
                    <div className="mb-4">
                        <h2 className="text-xs font-bold uppercase tracking-wider text-text">Strategy vs Buy & Hold</h2>
                        <p className="text-3xs text-muted mt-0.5 uppercase tracking-wider">
                            {buyAndHoldData.basis === 'backtest' && buyAndHoldData.range
                                ? `Backtest range ${fmtShortDate(buyAndHoldData.range.from)} → ${fmtShortDate(buyAndHoldData.range.to)}`
                                : buyAndHoldData.basis === 'backtest'
                                    ? 'Per symbol — first entry to last exit'
                                    : buyAndHoldData.basis === 'mixed'
                                        ? 'Mixed modes — pick one mode to compare'
                                        : `Per symbol — first entry to ${dateTo !== null ? 'last exit' : 'now'}`}
                        </p>
                    </div>

                    {initialLoading ? (
                        <div className="space-y-3">
                            <Skeleton className="h-20 w-full rounded-lg" />
                            <Skeleton className="h-20 w-full rounded-lg" />
                        </div>
                    ) : buyAndHoldData.rows.length === 0 ? (
                        <div className="flex items-center justify-center h-32 text-muted text-2xs text-center">
                            No closed trades to compare.<br />Close positions to see the comparison.
                        </div>
                    ) : (
                        <div className="space-y-3 max-h-[220px] overflow-y-auto custom-scrollbar pr-1">
                            {buyAndHoldData.portfolio && (() => {
                                const p = buyAndHoldData.portfolio;
                                return (
                                    <div className="bg-accent/5 border border-accent/30 rounded-lg p-3">
                                        <div className="flex items-center justify-between mb-1">
                                            <span className="text-2xs font-bold text-text">Portfolio <span className="text-muted font-normal">equal-weight · {p.symbols} symbols</span></span>
                                            <span className={`text-3xs font-bold font-num px-1.5 py-0.5 rounded ${p.edge >= 0 ? 'bg-success/10 text-success' : 'bg-danger/10 text-danger'}`}>
                                                {p.edge >= 0 ? '↑' : '↓'} Edge: {pnlSign(p.edge)}{safeNum(p.edge, 1)}%
                                            </span>
                                        </div>
                                        <div className="flex justify-between text-3xs font-num">
                                            <span className="text-muted uppercase font-bold">Strategy <span className={pnlColor(p.strategyPct)}>{pnlSign(p.strategyPct)}{safeNum(p.strategyPct, 1)}%</span></span>
                                            <span className="text-muted uppercase font-bold">Buy & Hold <span className={pnlColor(p.bhPct)}>{pnlSign(p.bhPct)}{safeNum(p.bhPct, 1)}%</span></span>
                                        </div>
                                    </div>
                                );
                            })()}
                            {buyAndHoldData.rows.map(d => (
                                <div key={d.symbol} className="bg-bg/50 border border-border rounded-lg p-3">
                                    <div className="flex items-center justify-between mb-2">
                                        <span className="text-2xs font-bold text-text font-num">{d.symbol}</span>
                                        {d.edge !== null && (
                                            <span className={`text-3xs font-bold font-num px-1.5 py-0.5 rounded ${d.edge >= 0 ? 'bg-success/10 text-success' : 'bg-danger/10 text-danger'}`}>
                                                {d.edge >= 0 ? '↑' : '↓'} Edge: {pnlSign(d.edge)}{safeNum(d.edge, 1)}%
                                            </span>
                                        )}
                                    </div>
                                    <div className="space-y-1.5">
                                        {/* Strategy bar */}
                                        <div>
                                            <div className="flex justify-between mb-0.5">
                                                <span className="text-3xs text-muted uppercase font-bold">Strategy</span>
                                                <span className={`text-3xs font-num font-bold ${pnlColor(d.strategyPct)}`}>
                                                    {pnlSign(d.strategyPct)}{safeNum(d.strategyPct, 1)}%
                                                </span>
                                            </div>
                                            <div className="h-1 bg-border rounded-full overflow-hidden">
                                                <div
                                                    className={`h-full rounded-full transition-all ${d.strategyPct >= 0 ? 'bg-success' : 'bg-danger'}`}
                                                    style={{ width: `${Math.min(100, Math.abs(d.strategyPct))}%` }}
                                                />
                                            </div>
                                        </div>
                                        {/* Buy & Hold bar */}
                                        <div>
                                            <div className="flex justify-between mb-0.5">
                                                <span className="text-3xs text-muted uppercase font-bold">Buy & Hold</span>
                                                <span className={`text-3xs font-num font-bold ${d.bhPct !== null ? pnlColor(d.bhPct) : 'text-muted'}`}>
                                                    {d.bhPct !== null ? `${pnlSign(d.bhPct)}${safeNum(d.bhPct, 1)}%` : (priceSyncing ? 'Loading…' : 'N/A')}
                                                </span>
                                            </div>
                                            <div className="h-1 bg-border rounded-full overflow-hidden">
                                                {d.bhPct !== null && (
                                                    <div
                                                        className={`h-full rounded-full transition-all opacity-50 ${d.bhPct >= 0 ? 'bg-info' : 'bg-danger'}`}
                                                        style={{ width: `${Math.min(100, Math.abs(d.bhPct))}%` }}
                                                    />
                                                )}
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                    {/* price refresh happens silently in the background */}
                </div>
            </div>

            {/* ── BREAKDOWN + MONTHLY ────────────────────────────────────────── */}
            {!initialLoading && closedPositions.length > 0 && (
                <div className="flex flex-col lg:flex-row gap-4">
                    <div className="terminal-card flex-1 min-w-0 overflow-hidden">
                        <div className="px-4 py-2.5 border-b border-border bg-bg/40 flex items-center justify-between gap-3 flex-wrap">
                            <div>
                                <h2 className="text-xs font-bold uppercase tracking-wider text-text">Performance Breakdown</h2>
                                <p className="text-3xs text-muted mt-0.5 uppercase tracking-wider">Closed trades in the current filter, best first</p>
                            </div>
                            <div className="flex bg-inset rounded-md border border-border overflow-hidden">
                                {[['bot', 'By algorithm'], ['symbol', 'By pair']].map(([v, l]) => (
                                    <button key={v} type="button" onClick={() => setBreakdownView(v)}
                                        className={`px-3 py-1.5 text-3xs font-bold uppercase tracking-wider transition-colors ${breakdownView === v ? 'bg-accent/10 text-accent' : 'text-muted hover:text-text'}`}>
                                        {l}
                                    </button>
                                ))}
                            </div>
                        </div>
                        <div className="overflow-x-auto max-h-[320px] overflow-y-auto custom-scrollbar">
                            <table className="w-full text-left whitespace-nowrap">
                                <thead className="bg-surface text-muted border-b border-border sticky top-0">
                                    <tr>
                                        <th className={thClass}>{breakdownView === 'bot' ? 'Algorithm' : 'Pair'}</th>
                                        <th className={`${thClass} text-right`}>Trades</th>
                                        <th className={`${thClass} text-right`}>Win rate</th>
                                        <th className={`${thClass} text-right`}>Net PNL</th>
                                        {breakdownView === 'bot' && <th className={`${thClass} text-right`}>Return</th>}
                                        <th className={`${thClass} text-right`}>PF</th>
                                        <th className={`${thClass} text-right`} title="Return / Risk: mean trade return ÷ std dev of trade returns (per-trade Sharpe, not annualised)">Sharpe</th>
                                        {breakdownView === 'bot' && <th className={`${thClass} text-right`}>Max DD</th>}
                                        <th className={`${thClass} text-right`}>Best / Worst</th>
                                        <th className={`${thClass} text-right`}>Avg hold</th>
                                        <th className={`${thClass} text-right`}>Longest flat</th>
                                        <th className={`${thClass} text-right`}>Fees</th>
                                    </tr>
                                </thead>
                                <tbody className="text-xs">
                                    {(breakdownView === 'bot' ? breakdownRows.byBot : breakdownRows.bySymbol).map(r => (
                                        <tr key={r.key} className="border-b border-border/40 hover:bg-overlay/50 transition-colors">
                                            <td className="px-3 py-1.5 font-bold text-text">
                                                <div className="flex items-center gap-2 min-w-0">
                                                    {breakdownView === 'bot' && <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${r.isActive ? 'bg-success animate-pulse' : 'bg-faint/40'}`} />}
                                                    <span className="truncate max-w-[220px]" title={r.label}>{r.label}</span>
                                                    {breakdownView === 'bot' && r.timeframe && <span className="text-3xs font-num text-accent">{r.timeframe}</span>}
                                                    {r.modes.map(m => <ModeBadge key={m} mode={m} short className="text-3xs!" />)}
                                                </div>
                                            </td>
                                            <td className="px-3 py-1.5 text-right font-num text-muted">{r.trades} <span className="text-faint">({r.wins}W)</span></td>
                                            <td className="px-3 py-1.5 text-right font-num text-info">{safeNum(r.winRate, 1)}%</td>
                                            <td className={`px-3 py-1.5 text-right font-num font-bold ${pnlColor(r.net)}`}>{pnlSign(r.net)}${safeNum(Math.abs(r.net))}</td>
                                            {breakdownView === 'bot' && (
                                                <td className={`px-3 py-1.5 text-right font-num ${r.returnPct === null ? 'text-faint' : pnlColor(r.returnPct)}`}>
                                                    {r.returnPct === null ? '—' : `${pnlSign(r.returnPct)}${safeNum(r.returnPct, 1)}%`}
                                                    {r.capital && <span className="text-faint ml-1">on ${safeNum(r.capital, 0)}</span>}
                                                </td>
                                            )}
                                            <td className="px-3 py-1.5 text-right font-num text-muted">{r.profitFactor === Infinity ? '∞' : safeNum(r.profitFactor)}</td>
                                            <td className={`px-3 py-1.5 text-right font-num ${r.sharpe === null ? 'text-faint' : r.sharpe > 1 ? 'text-success' : r.sharpe > 0 ? 'text-accent' : 'text-danger'}`}
                                                title={r.sharpe === null ? 'needs at least two closed trades with different returns' : undefined}>
                                                {r.sharpe === null ? '—' : safeNum(r.sharpe)}
                                            </td>
                                            {breakdownView === 'bot' && (
                                                <td className="px-3 py-1.5 text-right font-num text-danger">{r.engineDD !== null ? `-${safeNum(r.engineDD, 1)}%` : '—'}</td>
                                            )}
                                            <td className="px-3 py-1.5 text-right font-num"><span className="text-success">+${safeNum(Math.max(0, r.best))}</span> <span className="text-faint">/</span> <span className="text-danger">-${safeNum(Math.abs(Math.min(0, r.worst)))}</span></td>
                                            <td className="px-3 py-1.5 text-right font-num text-muted">{r.avgHoldMs ? formatHoldTime(r.avgHoldMs) : '—'}</td>
                                            <td className="px-3 py-1.5 text-right font-num text-muted"
                                                title={r.longestFlat ? `${fmtShortDate(r.longestFlat.from)} – ${fmtShortDate(r.longestFlat.to)}` : 'never flat in this range'}>
                                                {r.longestFlat ? formatHoldTime(r.longestFlat.ms) : '—'}
                                            </td>
                                            <td className="px-3 py-1.5 text-right font-num text-muted">${safeNum(r.fees)}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </div>

                    <div className="terminal-card p-4 lg:w-[340px] shrink-0">
                        <div className="mb-4">
                            <h2 className="text-xs font-bold uppercase tracking-wider text-text">Monthly Net PNL</h2>
                            <p className="text-3xs text-muted mt-0.5 uppercase tracking-wider">By close date (UTC) · last {monthlyReturns.length} months</p>
                        </div>
                        <div className="space-y-2 max-h-[280px] overflow-y-auto custom-scrollbar pr-1">
                            {monthlyReturns.map(m => (
                                <div key={m.key} className="flex items-center gap-3">
                                    <span className="text-3xs font-num text-muted w-14 shrink-0">{m.label}</span>
                                    <div className="flex-1 h-2 bg-border/60 rounded-full overflow-hidden flex">
                                        <div className={`h-full rounded-full ${m.net >= 0 ? 'bg-success' : 'bg-danger'}`} style={{ width: `${Math.max(2, m.share * 100)}%` }} />
                                    </div>
                                    <span className={`text-2xs font-num font-bold w-20 text-right shrink-0 ${pnlColor(m.net)}`} title={`${m.trades} trades`}>{pnlSign(m.net)}${safeNum(Math.abs(m.net), 0)}</span>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            )}

            {/* ── CAPITAL ALLOCATION ─────────────────────────────────────────── */}
            {!initialLoading && allocation.byBot.length > 0 && (
                <div className="terminal-card overflow-hidden">
                    <div className="px-4 py-2.5 border-b border-border bg-bg/40 flex items-center justify-between gap-3 flex-wrap">
                        <div>
                            <h2 className="text-xs font-bold uppercase tracking-wider text-text">Capital Allocation</h2>
                            <p className="text-3xs text-muted mt-0.5 uppercase tracking-wider">
                                Each algorithm owns one pool shared by all its pairs · entries take a % of free equity · pairs have no fixed budget
                            </p>
                        </div>
                        <div className="flex items-center gap-4 flex-wrap">
                            <div className="flex items-center gap-4 text-2xs font-num">
                                <span className="text-muted">Pools <span className="text-text font-bold">${safeNum(allocation.totalPool, 0)}</span></span>
                                <span className="text-muted">Deployed <span className={`font-bold ${allocation.totalDeployed > 0 ? 'text-accent' : 'text-text'}`}>${safeNum(allocation.totalDeployed, 0)}</span></span>
                                <span className="text-muted">Max exposure <span className="text-text font-bold">${safeNum(allocation.totalExposure, 0)}</span></span>
                            </div>
                            <div className="flex bg-inset rounded-md border border-border overflow-hidden">
                                {[['bot', 'By algorithm'], ['pair', 'By pair']].map(([v, l]) => (
                                    <button key={v} type="button" onClick={() => setAllocationView(v)}
                                        className={`px-3 py-1.5 text-3xs font-bold uppercase tracking-wider transition-colors ${allocationView === v ? 'bg-accent/10 text-accent' : 'text-muted hover:text-text'}`}>
                                        {l}
                                    </button>
                                ))}
                            </div>
                        </div>
                    </div>
                    <div className="overflow-x-auto max-h-[320px] overflow-y-auto custom-scrollbar">
                        {allocationView === 'bot' ? (
                            <table className="w-full text-left whitespace-nowrap">
                                <thead className="bg-surface text-muted border-b border-border sticky top-0">
                                    <tr>
                                        <th className={thClass}>Algorithm</th>
                                        <th className={`${thClass} text-right`}>Pool</th>
                                        <th className={`${thClass} text-right`}>Per entry</th>
                                        <th className={`${thClass} text-right`}>Max positions</th>
                                        <th className={`${thClass} text-right`}>Max exposure</th>
                                        <th className={`${thClass} text-right`}>Order cap</th>
                                        <th className={`${thClass} text-right`}>Deployed now</th>
                                        <th className={thClass}>Pairs</th>
                                    </tr>
                                </thead>
                                <tbody className="text-xs">
                                    {allocation.byBot.map(r => (
                                        <tr key={r.key} className="border-b border-border/40 hover:bg-overlay/50 transition-colors">
                                            <td className="px-3 py-1.5 font-bold text-text">
                                                <div className="flex items-center gap-2 min-w-0">
                                                    <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${r.isActive ? 'bg-success animate-pulse' : 'bg-faint/40'}`} />
                                                    <span className="truncate max-w-[220px]" title={r.label}>{r.label}</span>
                                                    {r.timeframe && <span className="text-3xs font-num text-accent">{r.timeframe}</span>}
                                                    <ModeBadge mode={r.mode} short className="text-3xs!" />
                                                </div>
                                            </td>
                                            <td className="px-3 py-1.5 text-right font-num font-bold text-text">${safeNum(r.pool, 0)}</td>
                                            <td className="px-3 py-1.5 text-right font-num text-muted">
                                                ${safeNum(r.entryUsd, 0)} <span className="text-faint">({r.isFixed ? 'fixed' : `${safeNum(r.entryPct, 0)}%`})</span>
                                            </td>
                                            <td className="px-3 py-1.5 text-right font-num text-muted">{r.maxPositions}</td>
                                            <td className="px-3 py-1.5 text-right font-num text-muted">
                                                ${safeNum(r.exposureUsd, 0)} <span className="text-faint">({safeNum(r.exposurePct, 0)}%)</span>
                                            </td>
                                            <td className={`px-3 py-1.5 text-right font-num ${r.cap > 0 ? 'text-muted' : 'text-faint'}`}>{r.cap > 0 ? `$${safeNum(r.cap, 0)}` : (r.mode === 'live' ? 'none!' : '—')}</td>
                                            <td className="px-3 py-1.5 text-right font-num">
                                                {r.openCount > 0 ? (
                                                    <span className="text-accent font-bold">${safeNum(r.deployed, 0)} <span className="text-faint font-normal">· {r.openCount} open · ${safeNum(r.free, 0)} free</span></span>
                                                ) : <span className="text-faint">idle · ${safeNum(r.pool, 0)} free</span>}
                                            </td>
                                            <td className="px-3 py-1.5 font-num text-muted">
                                                <span className="text-text font-bold">{r.symbols.length}</span>
                                                <span className="text-faint ml-1.5 truncate inline-block max-w-[260px] align-bottom" title={r.symbols.join(', ')}>{r.symbols.join(', ')}</span>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        ) : (
                            <table className="w-full text-left whitespace-nowrap">
                                <thead className="bg-surface text-muted border-b border-border sticky top-0">
                                    <tr>
                                        <th className={thClass}>Pair</th>
                                        <th className={`${thClass} text-right`}>Algorithms</th>
                                        <th className={`${thClass} text-right`}>Pool access</th>
                                        <th className={`${thClass} text-right`}>Max per entry</th>
                                        <th className={`${thClass} text-right`}>Deployed now</th>
                                        <th className={thClass}>Traded by</th>
                                    </tr>
                                </thead>
                                <tbody className="text-xs">
                                    {allocation.byPair.map(r => (
                                        <tr key={r.key} className="border-b border-border/40 hover:bg-overlay/50 transition-colors">
                                            <td className="px-3 py-1.5 font-bold text-text">{r.label}</td>
                                            <td className="px-3 py-1.5 text-right font-num text-muted">{r.bots.length}</td>
                                            <td className="px-3 py-1.5 text-right font-num text-muted" title="Sum of the pools this pair competes for — shared with the other pairs of each algorithm">${safeNum(r.poolAccess, 0)}</td>
                                            <td className="px-3 py-1.5 text-right font-num text-muted" title="What one entry signal on this pair may commit, summed over all algorithms">${safeNum(r.maxEntry, 0)}</td>
                                            <td className="px-3 py-1.5 text-right font-num">
                                                {r.openCount > 0
                                                    ? <span className="text-accent font-bold">${safeNum(r.deployed, 0)} <span className="text-faint font-normal">· {r.openCount} open</span></span>
                                                    : <span className="text-faint">idle</span>}
                                            </td>
                                            <td className="px-3 py-1.5 font-num text-faint"><span className="truncate inline-block max-w-[320px] align-bottom" title={r.bots.join(', ')}>{r.bots.join(', ')}</span></td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                    </div>
                </div>
            )}

            {/* ── ACTIVE POSITIONS ───────────────────────────────────────────── */}
            {activePositions.length > 0 && (
                <div className="terminal-card overflow-hidden">
                    <div className="px-4 py-2.5 border-b border-border bg-bg/40 flex items-center justify-between">
                        <div className="flex items-center gap-3">
                            <div className="w-1.5 h-1.5 rounded-full bg-success animate-pulse shadow-glow-success" />
                            <h3 className="text-xs font-bold uppercase tracking-wider text-text">
                                Open Positions <span className="text-muted font-normal ml-1 font-num">({activePositions.length})</span>
                            </h3>
                        </div>
                        {priceSyncing && <span className="text-3xs text-muted animate-pulse">Syncing prices…</span>}
                    </div>
                    <div className="overflow-x-auto">
                        <table className="w-full text-left whitespace-nowrap min-w-[700px]">
                            <thead className="bg-surface text-muted border-b border-border">
                                <tr>
                                    <th className={thClass}>Algorithm</th>
                                    <th className={thClass}>Exchange</th>
                                    <th className={thClass}>Symbol</th>
                                    <th className={thClass}>Side</th>
                                    <th className={`${thClass} text-right`}>Entry</th>
                                    <th className={`${thClass} text-right`}>Size</th>
                                    <th className={`${thClass} text-right`}>Unreal. PNL</th>
                                    <th className={`${thClass} text-right`}>Unreal. %</th>
                                    <th className={`${thClass} text-right`}>Actions</th>
                                </tr>
                            </thead>
                            <tbody className="text-xs">
                                {activePositions.map(pos => {
                                    const pnl = getLivePnl(pos);
                                    const hasPrice = !!livePrices[pos.symbol];
                                    return (
                                        <tr key={pos.id} className="border-b border-border/40 hover:bg-overlay/50 transition-colors">
                                            <td className="px-3 py-2 font-bold text-text">
                                                <span className="align-middle">{pos.bot_name}</span>
                                                <ModeBadge mode={pos.mode} short className="ml-2 text-3xs!" />
                                            </td>
                                            <td className="px-3 py-2 text-accent font-bold uppercase text-2xs">{pos.exchange || 'okx'}</td>
                                            <td className="px-3 py-2 font-bold text-text font-num">{pos.symbol}</td>
                                            <td className="px-4 py-3">
                                                <Badge variant={pos.side === 'long' ? 'success' : 'danger'} className="text-3xs!">{pos.side}</Badge>
                                                {(Number(pos.leverage) || 1) > 1 && (
                                                    <Badge variant="warn" className="ml-1 text-3xs!" title={`Perpetual swap at ${pos.leverage}x — size is the notional, margin ≈ ${formatCrypto(pos.amount / pos.leverage)}`}>{Number(pos.leverage)}×</Badge>
                                                )}
                                            </td>
                                            <td className="px-3 py-2 text-right font-num text-muted">${safeNum(pos.entry_price)}</td>
                                            <td className="px-3 py-2 text-right font-num text-muted">{formatCrypto(pos.amount)}</td>
                                            <td className={`px-3 py-2 text-right font-num font-bold ${hasPrice ? pnlColor(pnl.abs) : 'text-muted'}`}>
                                                {hasPrice ? `${pnl.abs >= 0 ? '+' : '-'}$${safeNum(Math.abs(pnl.abs))}` : '—'}
                                            </td>
                                            <td className={`px-3 py-2 text-right font-num font-bold ${hasPrice ? pnlColor(pnl.pct) : 'text-muted'}`}>
                                                {hasPrice ? `${pnlSign(pnl.pct)}${safeNum(pnl.pct, 2)}%` : '—'}
                                            </td>
                                            <td className="px-3 py-2 text-right">
                                                {/* No "Drop" on an open row: deleting the record of a live
                                                    position would orphan the coins on the exchange. Close it
                                                    first; the closed row keeps the delete action. */}
                                                <Button variant={REAL_MODES.has(pos.mode) ? 'danger' : 'secondary'} size="sm" loading={closingId === pos.id} disabled={busyAction && closingId !== pos.id} onClick={() => forceClosePosition(pos)}
                                                    title={REAL_MODES.has(pos.mode) ? 'Places a market sell on the exchange' : 'Closes the simulated position at the last local price'}>
                                                    {REAL_MODES.has(pos.mode) ? 'Sell at market' : 'Close'}
                                                </Button>
                                            </td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}

            {/* ── TABS ───────────────────────────────────────────────────────── */}
            <div className="flex space-x-8 border-b border-border">
                <button onClick={() => { setActiveTab('positions'); resetPage(); }} className={tabClass(activeTab === 'positions')}>
                    Historical Ledger
                    {closedPositions.length > 0 && <span className="ml-2 bg-raised border border-border text-muted px-1.5 py-0.5 rounded text-3xs font-num">{closedPositions.length}</span>}
                </button>
                <button onClick={() => { setActiveTab('orders'); resetPage(); }} className={tabClass(activeTab === 'orders')}>
                    Execution Log
                    {filteredOrders.length > 0 && <span className="ml-2 bg-raised border border-border text-muted px-1.5 py-0.5 rounded text-3xs font-num">{filteredOrders.length}</span>}
                </button>
            </div>

            {/* ── HISTORICAL LEDGER ──────────────────────────────────────────── */}
            {activeTab === 'positions' && (
                <div className="terminal-card overflow-hidden flex flex-col h-[560px]">
                    <div className="px-4 py-2.5 border-b border-border bg-bg/40 flex flex-wrap gap-y-2 items-center justify-between shrink-0">
                        <div className="flex items-center gap-3">
                            <h3 className="text-xs font-bold uppercase tracking-wider text-text">Historical Ledger</h3>
                            <PaginationBar
                                total={totalPagesPos}
                                current={currentPage}
                                onPrev={() => setCurrentPage(p => p - 1)}
                                onNext={() => setCurrentPage(p => p + 1)}
                            />
                        </div>
                        <Button variant="danger" size="sm" onClick={bulkDelete} disabled={closedPositions.length === 0 || busyAction}>
                            Wipe Filtered
                        </Button>
                    </div>
                    <div className="overflow-x-auto overflow-y-auto flex-1 custom-scrollbar">
                        {initialLoading ? (
                            <div className="p-5 space-y-2.5">
                                {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}
                            </div>
                        ) : closedPositions.length === 0 ? (
                            <EmptyState
                                title="No historical trades"
                                description="No closed trades match your current filters. Adjust the filters above or wait for a bot to close a position."
                            />
                        ) : (
                            <table className="w-full text-left whitespace-nowrap min-w-[860px] relative">
                                <thead className="bg-surface text-muted sticky top-0 z-10 border-b border-border">
                                    <tr>
                                        <th className={thClass}>Date Closed</th>
                                        <th className={thClass}>Algorithm</th>
                                        <th className={thClass}>Exchange</th>
                                        <th className={thClass}>Pair</th>
                                        <th className={`${thClass} text-right`}>Entry → Exit</th>
                                        <th className={`${thClass} text-right`}>Size</th>
                                        <th className={`${thClass} text-right`}>Hold</th>
                                        <th className={`${thClass} text-right`}>Yield</th>
                                        <th className={`${thClass} text-right`}>Net PNL</th>
                                        <th className={`${thClass} text-right`}>Fees</th>
                                        <th className={`${thClass} text-center`}></th>
                                    </tr>
                                </thead>
                                <tbody className="text-xs">
                                    {renderedPositions.map(pos => {
                                        const isWin = (pos.profit_abs || 0) >= 0;
                                        const exitPrice = getExitPrice(pos);
                                        const holdMs = (() => {
                                            if (!pos.closed_at) return 0;
                                            const closedTs = new Date(pos.closed_at);
                                            if (pos.created_at) {
                                                const d = closedTs - new Date(pos.created_at);
                                                if (d > 0) return d;
                                            }
                                            const entryTs = entryTsByPos[pos.id];
                                            return (entryTs && closedTs > entryTs) ? closedTs - entryTs : 0;
                                        })();
                                        const posFees = feesByPosId[pos.id] || 0;
                                        return (
                                            <tr key={pos.id} className="border-b border-border/40 hover:bg-overlay/50 transition-colors group">
                                                <td className="px-3 py-1.5 font-num text-muted text-2xs">
                                                    {pos.closed_at ? new Date(pos.closed_at).toLocaleString() : '—'}
                                                </td>
                                                <td className="px-3 py-1.5 font-bold text-text">
                                                    <span className="align-middle">{pos.bot_name}</span>
                                                    <ModeBadge mode={pos.mode} short className="ml-1.5 text-3xs!" />
                                                </td>
                                                <td className="px-3 py-1.5 text-accent font-bold uppercase text-2xs">{pos.exchange || 'okx'}</td>
                                                <td className="px-3 py-1.5 font-bold font-num text-text">{pos.symbol}{(Number(pos.leverage) || 1) > 1 && <span className="ml-1 text-3xs text-warn" title={`Perpetual swap at ${pos.leverage}x`}>{Number(pos.leverage)}×</span>}</td>
                                                <td className="px-3 py-1.5 text-right font-num text-2xs">
                                                    <span className="text-muted">${safeNum(pos.entry_price)}</span>
                                                    <span className="text-faint mx-1">→</span>
                                                    <span className={exitPrice ? pnlColor(pos.profit_abs) : 'text-muted'}>
                                                        {exitPrice ? `$${safeNum(exitPrice)}` : '—'}
                                                    </span>
                                                </td>
                                                <td className="px-3 py-1.5 text-right font-num text-muted text-2xs">{formatCrypto(pos.amount)}</td>
                                                <td className="px-3 py-1.5 text-right font-num text-muted text-2xs">{formatHoldTime(holdMs)}</td>
                                                <td className="px-3 py-1.5 text-right">
                                                    <span className={`px-1.5 py-0.5 rounded text-3xs font-bold font-num ${isWin ? 'bg-success/10 text-success' : 'bg-danger/10 text-danger'}`}>
                                                        {pnlSign(pos.profit_pct)}{safeNum(pos.profit_pct)}%
                                                    </span>
                                                </td>
                                                <td className={`px-3 py-1.5 text-right font-num font-bold ${pnlColor(pos.profit_abs)}`}>
                                                    {(pos.profit_abs || 0) >= 0 ? '+' : '-'}${safeNum(Math.abs(pos.profit_abs || 0))}
                                                </td>
                                                <td className="px-3 py-1.5 text-right font-num text-muted text-2xs">
                                                    {posFees > 0 ? `-$${safeNum(posFees, 4)}` : '—'}
                                                </td>
                                                <td className="px-3 py-1.5 text-center opacity-60 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
                                                    <button onClick={() => deleteHistoricalTrade(pos.id)} className="text-muted hover:text-danger transition-colors font-bold text-xs" aria-label="Delete trade">✕</button>
                                                </td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        )}
                    </div>
                </div>
            )}

            {/* ── EXECUTION LOG ──────────────────────────────────────────────── */}
            {activeTab === 'orders' && (
                <div className="terminal-card overflow-hidden flex flex-col h-[560px]">
                    <div className="px-4 py-2.5 border-b border-border bg-bg/40 flex flex-wrap gap-y-2 items-center justify-between shrink-0">
                        <div className="flex items-center gap-3">
                            <div>
                                <h3 className="text-xs font-bold uppercase tracking-wider text-text">Execution Log</h3>
                                <p className="text-3xs text-muted mt-0.5 tracking-wide">Every order dispatched to exchange or simulator</p>
                            </div>
                            <PaginationBar
                                total={totalPagesOrd}
                                current={currentPage}
                                onPrev={() => setCurrentPage(p => p - 1)}
                                onNext={() => setCurrentPage(p => p + 1)}
                            />
                        </div>
                    </div>
                    <div className="overflow-x-auto overflow-y-auto flex-1 custom-scrollbar">
                        {initialLoading ? (
                            <div className="p-5 space-y-2.5">
                                {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}
                            </div>
                        ) : filteredOrders.length === 0 ? (
                            <EmptyState
                                title="No orders logged"
                                description="No orders match your current filters. Orders appear here as soon as a bot dispatches one to the exchange or simulator."
                            />
                        ) : (
                            <table className="w-full text-left whitespace-nowrap min-w-[800px] relative">
                                <thead className="bg-surface text-muted sticky top-0 z-10 border-b border-border">
                                    <tr>
                                        <th className={thClass}>Timestamp</th>
                                        <th className={thClass}>Algorithm</th>
                                        <th className={thClass}>Exchange</th>
                                        <th className={thClass}>Pair</th>
                                        <th className={thClass}>Action</th>
                                        <th className={`${thClass} text-right`}>Fill Price</th>
                                        <th className={`${thClass} text-right`}>Size</th>
                                        <th className={`${thClass} text-right`}>Fee</th>
                                        <th className={`${thClass} text-right`}>Status</th>
                                    </tr>
                                </thead>
                                <tbody className="text-xs">
                                    {renderedOrders.map(order => (
                                        <tr key={order.id} className="border-b border-border/40 hover:bg-overlay/50 transition-colors">
                                            <td className="px-3 py-1.5 font-num text-muted text-2xs">{new Date(order.timestamp).toLocaleString()}</td>
                                            <td className="px-3 py-1.5 font-bold text-text">
                                                <span className="align-middle">{order.bot_name}</span>
                                                <ModeBadge mode={order.mode} short className="ml-1.5 text-3xs!" />
                                            </td>
                                            <td className="px-3 py-1.5 text-accent font-bold uppercase text-2xs">{order.exchange || 'okx'}</td>
                                            <td className="px-3 py-1.5 font-bold font-num text-text">{order.symbol}</td>
                                            <td className="px-3 py-1.5">
                                                <span className={`font-bold uppercase text-2xs ${order.side === 'buy' ? 'text-success' : 'text-danger'}`}>
                                                    {order.side}
                                                </span>
                                                <span className="ml-1.5 text-muted text-3xs uppercase">{order.order_type}</span>
                                            </td>
                                            <td className="px-3 py-1.5 text-right font-num text-text">${safeNum(order.price)}</td>
                                            <td className="px-3 py-1.5 text-right font-num text-muted">{formatCrypto(order.amount)}</td>
                                            <td className="px-3 py-1.5 text-right font-num text-muted text-2xs">
                                                {order.fee > 0 ? `-$${safeNum(order.fee, 4)}` : '—'}
                                            </td>
                                            <td className="px-3 py-1.5 text-right">
                                                <Badge
                                                    variant={order.status === 'filled' ? 'success' : (order.status === 'rejected' || order.status === 'canceled') ? 'danger' : 'accent'}
                                                    className="text-3xs!">
                                                    {order.status}
                                                </Badge>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                    </div>
                </div>
            )}
        </PageShell>
    );
}
