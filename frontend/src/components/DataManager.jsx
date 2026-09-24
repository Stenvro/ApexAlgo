import { useState, useEffect, useMemo, useCallback } from 'react';
import { apiClient } from '../api/client';
import { humanizeApiError } from '../api/errors';
import PageShell from './ui/PageShell';
import GlowPanel from './ui/GlowPanel';
import SectionHeader from './ui/SectionHeader';
import Modal from './ui/Modal';
import Button from './ui/Button';
import Badge from './ui/Badge';
import StatCard from './ui/StatCard';
import DataTable from './ui/DataTable';
import EmptyState from './ui/EmptyState';
import { Input, Select } from './ui/Input';
import { Skeleton } from './ui/Skeleton';
import { toast } from './ui/Toast';
import { confirmDialog } from './ui/ConfirmDialog';
import { useExchanges } from '../api/exchanges';

/* ── Inline icons (stroke 1.8) ── */
const IconSync = (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h5M20 20v-5h-5M5.5 9a7.5 7.5 0 0113-2.2M18.5 15a7.5 7.5 0 01-13 2.2" />
  </svg>
);
const IconChart = (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" d="M4 20V4m0 16h16M8 16v-5m4 5V8m4 8v-3" />
  </svg>
);
const IconTrash = (
  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" d="M4 7h16M9 7V5a1 1 0 011-1h4a1 1 0 011 1v2m3 0l-.8 12.1A2 2 0 0115.2 21H8.8a2 2 0 01-2-1.9L6 7m4 4v6m4-6v6" />
  </svg>
);
const IconDatabase = (
  <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
    <ellipse cx="12" cy="5.5" rx="8" ry="2.8" />
    <path strokeLinecap="round" d="M4 5.5v13c0 1.55 3.58 2.8 8 2.8s8-1.25 8-2.8v-13M4 12c0 1.55 3.58 2.8 8 2.8s8-1.25 8-2.8" />
  </svg>
);

const Spinner = (
  <svg className="spin w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
    <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
  </svg>
);

function RowAction({ title, onClick, disabled, tone, children }) {
  const tones = {
    warn:   'text-warn hover:bg-warn/10',
    info:   'text-info hover:bg-info/10',
    danger: 'text-danger hover:bg-danger/10',
  };
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      onClick={onClick}
      disabled={disabled}
      className={`p-1.5 rounded-md transition-colors duration-150 disabled:opacity-40 disabled:pointer-events-none ${tones[tone] || tones.info}`}
    >
      {children}
    </button>
  );
}

export default function DataManager({ openChart }) {
  const [summary, setSummary] = useState([]);
  const [liveKeys, setLiveKeys] = useState(() => new Set());
  const [initialLoading, setInitialLoading] = useState(true);
  const [loading, setLoading] = useState(false);
  const [syncingSymbol, setSyncingSymbol] = useState(null);

  const [symbol, setSymbol] = useState('BTC-USDC');
  const exchanges = useExchanges();
  const [exchange, setExchange] = useState('okx');
  const [timeframe, setTimeframe] = useState('1d');
  const [startDate, setStartDate] = useState('2024-01-01T00:00');
  const [endDate, setEndDate] = useState(new Date().toISOString().slice(0, 16));
  const [exchangeTimeframes, setExchangeTimeframes] = useState(null);

  const [pruneModalConfig, setPruneModalConfig] = useState(null);
  const [pruneDate, setPruneDate] = useState('');

  const [filterSymbol, setFilterSymbol] = useState('ALL');
  const [filterTf, setFilterTimeframe] = useState('ALL');
  const [currentPage, setCurrentPage] = useState(1);
  const itemsPerPage = 50;

  const fetchSummary = useCallback(async () => {
    try {
      const [summaryRes, botsRes] = await Promise.all([
        apiClient.get('/api/data/summary'),
        apiClient.get('/api/bots/summary').catch(() => ({ data: [] })),
      ]);
      setSummary(summaryRes.data);
      // Datasets that a running bot polls live must not be deleted
      const keys = new Set();
      botsRes.data.filter(b => b.is_active).forEach(b => {
        const s = b.settings || {};
        const ex = (b.exchange || s.data_exchange || 'okx').toLowerCase();
        const tf = s.timeframe;
        const syms = s.symbols?.length ? s.symbols : (s.symbol ? [s.symbol] : []);
        syms.forEach(sym => keys.add(`${ex}|${sym}|${tf}`));
      });
      setLiveKeys(keys);
    } catch (err) {
      toast.error(humanizeApiError(err, 'Failed to load data summary.'));
    }
    setInitialLoading(false);
  }, []);

  const isLiveRow = useCallback((row) =>
    liveKeys.has(`${(row.exchange || 'okx').toLowerCase()}|${row.symbol}|${row.timeframe}`),
  [liveKeys]);

  useEffect(() => {
    fetchSummary(); // eslint-disable-line react-hooks/set-state-in-effect -- initial data fetch on mount
    // Background backfills (running bots) fill the vault while this view is
    // open — refresh silently so new datasets appear without a remount
    const t = setInterval(fetchSummary, 20000);
    return () => clearInterval(t);
  }, [fetchSummary]);

  // Fetch supported timeframes when exchange changes
  useEffect(() => {
    apiClient.get(`/api/data/timeframes/${exchange}`).then(res => {
      setExchangeTimeframes(res.data.timeframes);
      // Reset timeframe if current one isn't supported
      if (res.data.timeframes && !res.data.timeframes.includes(timeframe)) {
        setTimeframe(res.data.timeframes.includes('1d') ? '1d' : res.data.timeframes[0] || '1d');
      }
    }).catch(() => setExchangeTimeframes(null));
  }, [exchange]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleFilterSymbol = useCallback((val) => {
      setFilterSymbol(val);
      setCurrentPage(1);
  }, []);
  const handleFilterTimeframe = useCallback((val) => {
      setFilterTimeframe(val);
      setCurrentPage(1);
  }, []);

  const handleDownload = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      const payload = {
        exchange: exchange,
        timeframe: timeframe,
        start_date: new Date(startDate).toISOString(),
        end_date: new Date(endDate).toISOString()
      };

      // Accept both BTC/USDC and BTC-USDC — the API path expects the dash form.
      const normalizedSymbol = symbol.trim().toUpperCase().replace(/\//g, '-');
      const response = await apiClient.post(`/api/data/fetch/${normalizedSymbol}`, payload);
      toast.success(response.data.new_saved != null
        ? `${response.data.message} ${response.data.new_saved} new candles added.`
        : response.data.message);
      fetchSummary();
    } catch (err) {
      toast.error(humanizeApiError(err, 'Failed to download candle data.'));
    }
    setLoading(false);
  };

  const handleSync = async (row) => {
    setSyncingSymbol(`${row.symbol}_${row.timeframe}`);
    try {
      const payload = {
        exchange: row.exchange || 'okx',
        timeframe: row.timeframe,
        start_date: new Date(row.newest_candle).toISOString(),
        end_date: new Date().toISOString()
      };

      const safeSymbol = row.symbol.replace('/', '-');
      const response = await apiClient.post(`/api/data/fetch/${safeSymbol}`, payload);
      toast.success(`${row.symbol} synced. ${response.data.new_saved} new candles fetched.`);
      fetchSummary();
    } catch (err) {
      toast.error(humanizeApiError(err, 'Failed to sync candles.'));
    }
    setSyncingSymbol(null);
  };

  const executeDelete = async (delSymbol, delTimeframe, beforeDateStr, delExchange) => {
    setLoading(true);
    try {
      let endpoint = `/api/data?symbol=${encodeURIComponent(delSymbol)}&timeframe=${delTimeframe}&exchange=${encodeURIComponent(delExchange || 'okx')}`;
      if (beforeDateStr && beforeDateStr.trim() !== "") {
          const isoDate = new Date(beforeDateStr).toISOString();
          endpoint += `&before_date=${isoDate}`;
      }

      const res = await apiClient.delete(endpoint);
      setPruneModalConfig(null);
      toast.success(res.data.message);
      fetchSummary();
      window.dispatchEvent(new CustomEvent('data-changed'));
    } catch (err) {
      toast.error(humanizeApiError(err, 'Failed to delete candle data.'));
      setPruneModalConfig(null);
    }
    setLoading(false);
  };

  const handleDeleteClick = (row) => {
    if (isLiveRow(row)) {
      toast.warn('This dataset is in use by a running bot. Stop the bot first.');
      return;
    }
    setPruneDate('');
    setPruneModalConfig({ symbol: row.symbol, timeframe: row.timeframe, exchange: row.exchange || 'okx' });
  };

  const uniqueSymbols = useMemo(() => [...new Set(summary.map(r => r.symbol))], [summary]);
  const uniqueTimeframes = useMemo(() => [...new Set(summary.map(r => r.timeframe))], [summary]);
  const uniqueExchanges = useMemo(() => [...new Set(summary.map(r => r.exchange || 'okx'))], [summary]);
  const totalCandles = useMemo(() => summary.reduce((acc, r) => acc + (r.count || 0), 0), [summary]);

  const filteredData = useMemo(() => {
      return summary.filter(row => {
          if (filterSymbol !== 'ALL' && row.symbol !== filterSymbol) return false;
          if (filterTf !== 'ALL' && row.timeframe !== filterTf) return false;
          return true;
      });
  }, [summary, filterSymbol, filterTf]);

  const totalPages = Math.ceil(filteredData.length / itemsPerPage);
  const renderedData = filteredData.slice((currentPage - 1) * itemsPerPage, currentPage * itemsPerPage);

  const bulkDeleteFiltered = async () => {
      // Datasets a running bot depends on are excluded from the wipe
      const deletable = filteredData.filter(row => !isLiveRow(row));
      const liveCount = filteredData.length - deletable.length;
      if (deletable.length === 0) {
          toast.warn(liveCount > 0
            ? 'All matching datasets are in use by running bots. Stop the bots first.'
            : 'No datasets match your filters.');
          return;
      }
      const ok = await confirmDialog({
        title: 'Bulk Wipe Data',
        message: `You are about to permanently delete all candle history for ${deletable.length} dataset${deletable.length === 1 ? '' : 's'} matching your filters.`
          + (liveCount > 0 ? ` ${liveCount} live dataset${liveCount === 1 ? ' is' : 's are'} in use by running bots and will be kept.` : '')
          + ' This cannot be undone.',
        confirmText: 'Wipe All Filtered',
        type: 'danger',
      });
      if (!ok) return;
      setLoading(true);
      try {
          await Promise.all(deletable.map(row =>
              apiClient.delete(`/api/data?symbol=${encodeURIComponent(row.symbol)}&timeframe=${row.timeframe}&exchange=${encodeURIComponent(row.exchange || 'okx')}`)
          ));
          fetchSummary();
          window.dispatchEvent(new CustomEvent('data-changed'));
          toast.success('Successfully deleted all data matching your filters.');
      } catch {
          toast.error('Failed to delete some data.');
      }
      setLoading(false);
  };

  const columns = [
    {
      key: 'exchange', label: 'Exchange',
      render: (v) => <span className="text-accent font-bold uppercase text-2xs">{v || 'okx'}</span>,
    },
    {
      key: 'symbol', label: 'Symbol',
      render: (v, row) => (
        <span className="inline-flex items-center gap-2">
          <span className="text-text font-bold">{v}</span>
          {isLiveRow(row) && <Badge variant="success" dot pulse>Live</Badge>}
        </span>
      ),
    },
    {
      key: 'timeframe', label: 'Interval',
      render: (v) => <Badge variant="info">{v}</Badge>,
    },
    {
      key: 'count', label: 'Data Points', align: 'right',
      render: (v) => <span className="text-text-secondary">{(v ?? 0).toLocaleString()}</span>,
    },
    {
      key: 'oldest_candle', label: 'Oldest Record',
      render: (v) => <span className="text-muted text-2xs">{new Date(v).toLocaleString()}</span>,
    },
    {
      key: 'newest_candle', label: 'Newest Record',
      render: (v) => <span className="text-text text-2xs font-bold">{new Date(v).toLocaleString()}</span>,
    },
    {
      key: 'actions', label: 'Actions', align: 'right',
      render: (_, row) => {
        const isSyncing = syncingSymbol === `${row.symbol}_${row.timeframe}`;
        return (
          <span className="inline-flex items-center gap-0.5">
            <RowAction
              title="Sync missing candles up to right now"
              tone="warn"
              disabled={isSyncing || loading}
              onClick={() => handleSync(row)}
            >
              {isSyncing ? Spinner : IconSync}
            </RowAction>
            <RowAction
              title="Open in chart"
              tone="info"
              disabled={isSyncing || loading}
              onClick={() => openChart(row)}
            >
              {IconChart}
            </RowAction>
            <RowAction
              title={isLiveRow(row)
                ? 'In use by a running bot — stop the bot first to delete this data'
                : 'Delete or prune this dataset'}
              tone="danger"
              disabled={isSyncing || loading || isLiveRow(row)}
              onClick={() => handleDeleteClick(row)}
            >
              {IconTrash}
            </RowAction>
          </span>
        );
      },
    },
  ];

  const filterSelectClass = '!py-1.5 !text-xs !font-bold uppercase';

  return (
    <PageShell>
      {/* Prune modal — custom body (date input, so richer than confirmDialog) */}
      {pruneModalConfig && (
        <Modal
          config={{
            type: 'danger',
            title: 'Prune Market Data',
            onCancel: () => setPruneModalConfig(null),
          }}
          customBody={
            <div className="space-y-4">
              <p className="text-xs text-text-secondary leading-relaxed">
                Manage local data for{' '}
                <strong className="text-accent font-num">{pruneModalConfig.symbol} ({pruneModalConfig.timeframe})</strong>.
                Select a date to delete all history before that date, or click Delete All to wipe the entire pair.
              </p>
              <Input
                type="date"
                label="Prune Before Date (Optional)"
                value={pruneDate}
                onChange={e => setPruneDate(e.target.value)}
                className="color-scheme-dark"
              />
              <div className="flex justify-end gap-3 pt-2">
                <Button
                  variant="danger"
                  size="sm"
                  loading={loading}
                  onClick={() => executeDelete(pruneModalConfig.symbol, pruneModalConfig.timeframe, '', pruneModalConfig.exchange)}
                >
                  Delete All
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  loading={loading}
                  disabled={!pruneDate}
                  className="!bg-danger !text-danger-ink hover:!bg-danger-hover"
                  onClick={() => executeDelete(pruneModalConfig.symbol, pruneModalConfig.timeframe, pruneDate, pruneModalConfig.exchange)}
                >
                  Prune Date
                </Button>
              </div>
            </div>
          }
        />
      )}

      <SectionHeader
        title="Market Data"
        subtitle="Download, sync, and manage historical candle datasets"
        accentColor="accent"
      />

      {/* Summary stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {initialLoading ? (
          <>
            <Skeleton className="h-[86px]" />
            <Skeleton className="h-[86px]" />
            <Skeleton className="h-[86px]" />
            <Skeleton className="h-[86px]" />
          </>
        ) : (
          <>
            <StatCard label="Datasets" value={summary.length.toLocaleString()} color="accent" sub="pair / interval combos" />
            <StatCard label="Total Candles" value={totalCandles.toLocaleString()} color="info" />
            <StatCard label="Unique Pairs" value={uniqueSymbols.length.toLocaleString()} color="success" />
            <StatCard label="Exchanges" value={uniqueExchanges.length.toLocaleString()} color="purple" />
          </>
        )}
      </div>

      {/* Download form */}
      <GlowPanel glowColor="accent">
        <form onSubmit={handleDownload} className="space-y-5">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-5">
            {/* Step 1 — Source */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <span className="w-5 h-5 rounded-full bg-accent/10 border border-accent/30 text-accent text-2xs font-num font-bold flex items-center justify-center">1</span>
                <span className="text-2xs font-bold uppercase tracking-wider text-muted">Source</span>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <Select label="Exchange" value={exchange} onChange={e => setExchange(e.target.value)}>
                  {exchanges.map(ex => (
                    <option key={ex.id} value={ex.id}>{ex.name}</option>
                  ))}
                </Select>
                <Input
                  label="Asset Pair"
                  mono
                  required
                  value={symbol}
                  onChange={e => setSymbol(e.target.value.toUpperCase())}
                  placeholder="BTC-USDC or BTC/USDC"
                  hint="Both BTC-USDC and BTC/USDC work"
                />
              </div>
            </div>

            {/* Step 2 — Range */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <span className="w-5 h-5 rounded-full bg-accent/10 border border-accent/30 text-accent text-2xs font-num font-bold flex items-center justify-center">2</span>
                <span className="text-2xs font-bold uppercase tracking-wider text-muted">Interval & Range</span>
              </div>
              <div className="grid grid-cols-3 gap-3">
                <Select label="Interval" required value={timeframe} onChange={e => setTimeframe(e.target.value)} className="font-num">
                  {(exchangeTimeframes || ['1m','5m','15m','1h','4h','1d']).map(tf => (
                    <option key={tf} value={tf}>{tf}</option>
                  ))}
                </Select>
                <Input
                  type="datetime-local"
                  label="Start"
                  mono
                  required
                  value={startDate}
                  onChange={e => setStartDate(e.target.value)}
                  className="color-scheme-dark"
                />
                <Input
                  type="datetime-local"
                  label="End"
                  mono
                  required
                  value={endDate}
                  onChange={e => setEndDate(e.target.value)}
                  className="color-scheme-dark"
                />
              </div>
            </div>
          </div>

          <div className="flex items-center justify-between gap-4 pt-4 border-t border-border">
            <p className="text-2xs text-faint">
              {loading
                ? 'Fetching candles from the exchange — large ranges can take a while.'
                : 'Candles are stored locally, deduplicated per exchange, symbol and interval.'}
            </p>
            <Button type="submit" loading={loading} disabled={syncingSymbol !== null}>
              {loading ? 'Fetching…' : 'Download Data'}
            </Button>
          </div>
        </form>
      </GlowPanel>

      {/* Data overview */}
      <div className="space-y-3">
        <div className="flex flex-wrap gap-y-3 justify-between items-center">
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-2xs text-muted font-bold uppercase tracking-wider hidden md:inline">Filter</span>
            <div className="w-36">
              <Select value={filterSymbol} onChange={(e) => handleFilterSymbol(e.target.value)} className={filterSelectClass}>
                <option value="ALL">All Pairs</option>
                {uniqueSymbols.map(sym => <option key={sym} value={sym}>{sym}</option>)}
              </Select>
            </div>
            <div className="w-36">
              <Select value={filterTf} onChange={(e) => handleFilterTimeframe(e.target.value)} className={filterSelectClass}>
                <option value="ALL">All Intervals</option>
                {uniqueTimeframes.map(tf => <option key={tf} value={tf}>{tf}</option>)}
              </Select>
            </div>

            {totalPages > 1 && (
              <div className="flex items-center bg-inset rounded-md border border-border overflow-hidden">
                <button
                  disabled={currentPage === 1}
                  onClick={() => setCurrentPage(p => p - 1)}
                  title="Previous page"
                  className="px-2.5 py-1.5 hover:bg-overlay disabled:opacity-30 text-muted transition-colors"
                >
                  &#9664;
                </button>
                <span className="text-3xs font-bold text-text px-2 font-num">PG {currentPage} / {totalPages}</span>
                <button
                  disabled={currentPage === totalPages}
                  onClick={() => setCurrentPage(p => p + 1)}
                  title="Next page"
                  className="px-2.5 py-1.5 hover:bg-overlay disabled:opacity-30 text-muted transition-colors"
                >
                  &#9654;
                </button>
              </div>
            )}
          </div>

          <Button
            variant="danger"
            size="sm"
            onClick={bulkDeleteFiltered}
            disabled={filteredData.length === 0 || loading}
          >
            Wipe Filtered History
          </Button>
        </div>

        {initialLoading ? (
          <div className="terminal-card p-5 space-y-3">
            <Skeleton className="h-8" />
            <Skeleton className="h-8" />
            <Skeleton className="h-8" />
            <Skeleton className="h-8" />
          </div>
        ) : (
          <DataTable
            columns={columns}
            data={renderedData}
            maxHeight="540px"
            emptyState={
              <EmptyState
                icon={IconDatabase}
                title={summary.length === 0 ? 'No market data yet' : 'No datasets match your filters'}
                description={summary.length === 0
                  ? 'Download historical candles above to start backtesting your strategies.'
                  : 'Adjust the pair or interval filters to see stored datasets.'}
                action={summary.length > 0 ? (
                  <Button size="sm" variant="secondary" onClick={() => { handleFilterSymbol('ALL'); handleFilterTimeframe('ALL'); }}>
                    Clear Filters
                  </Button>
                ) : null}
              />
            }
          />
        )}
      </div>
    </PageShell>
  );
}
