import { useState, useEffect, useCallback, useRef, lazy, Suspense } from 'react';
import Sidebar from './components/Sidebar';
import DataManager from './components/DataManager';
import Settings from './components/Settings';
import BotManagerUI from './components/BotManagerUI';
import TradeManager from './components/TradeManager';
import Home from './components/Home';
import ApiKeyGate from './components/ApiKeyGate';
import Toaster from './components/ui/Toast';
import ConfirmDialogHost from './components/ui/ConfirmDialog';
import { Skeleton } from './components/ui/Skeleton';
import { apiClient, checkSession, logout, migrateLegacyKey } from './api/client';

const ChartEngine = lazy(() => import('./components/ChartEngine'));
const BotBuilder = lazy(() => import('./components/Builder/BotBuilder'));

const LazyFallback = ({ label }) => (
  <div className="flex-1 flex flex-col items-center justify-center h-full gap-4 p-8">
    <div className="w-full max-w-lg space-y-3">
      <Skeleton className="h-8 w-1/3" />
      <Skeleton className="h-64 w-full" />
      <div className="flex gap-3">
        <Skeleton className="h-6 w-24" />
        <Skeleton className="h-6 w-24" />
      </div>
    </div>
    <span className="text-muted text-2xs uppercase tracking-[0.2em]">{label}</span>
  </div>
);

export default function App() {
  const [activeView, setActiveView] = useState(() => {
      return localStorage.getItem('apex_activeView') || 'home';
  });

  const [openCharts, setOpenCharts] = useState(() => {
      try {
          const savedCharts = localStorage.getItem('apex_openCharts');
          const parsed = savedCharts ? JSON.parse(savedCharts) : [];
          return Array.isArray(parsed) ? parsed : [];
      } catch {
          return [];
      }
  });

  const [allBots, setAllBots] = useState([]);
  const [error, setError] = useState(null);
  const [backendOk, setBackendOk] = useState(true);

  const [showBuilder, setShowBuilder] = useState(false);
  const [editingBot, setEditingBot] = useState(null);

  const [sidebarOpen, setSidebarOpen] = useState(window.innerWidth > 768);
  // null = session probe in flight, true/false = logged in / show gate
  const [hasApiKey, setHasApiKey] = useState(null);
  const [signedOutReason, setSignedOutReason] = useState(null);
  const pollIntervalRef = useRef(15000);
  const userClosedSidebarRef = useRef(false);
  const prevWidthRef = useRef(window.innerWidth);

  useEffect(() => {
      const handleKeyInvalid = (e) => {
          setSignedOutReason(e.detail?.reason || 'expired');
          setHasApiKey(false);
      };
      window.addEventListener('api-key-invalid', handleKeyInvalid);
      return () => window.removeEventListener('api-key-invalid', handleKeyInvalid);
  }, []);

  // Session probe on load; a key left behind by the localStorage-era UI is
  // exchanged for a cookie first. Network errors fall through to the gate,
  // which has its own "backend unreachable" handling.
  useEffect(() => {
      let cancelled = false;
      (async () => {
          await migrateLegacyKey();
          let ok = false;
          try { ok = await checkSession(); } catch { ok = false; }
          if (!cancelled) setHasApiKey(ok);
      })();
      return () => { cancelled = true; };
  }, []);

  const handleLogout = useCallback(async () => {
      await logout();
      setSignedOutReason(null);
      setHasApiKey(false);
  }, []);

  useEffect(() => {
      localStorage.setItem('apex_activeView', activeView);
  }, [activeView]);

  useEffect(() => {
      localStorage.setItem('apex_openCharts', JSON.stringify(openCharts));
  }, [openCharts]);

  useEffect(() => {
    const handleResize = () => {
      const prev = prevWidthRef.current;
      const now = window.innerWidth;
      prevWidthRef.current = now;
      if (now < 768 && prev >= 768) {
        // Shrinking below the breakpoint: always collapse (overlay mode).
        setSidebarOpen(false);
      } else if (now >= 768 && prev < 768 && !userClosedSidebarRef.current) {
        // Growing past the breakpoint: reopen only if the user didn't close it themselves.
        setSidebarOpen(true);
      }
    };
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // Sidebar open/close initiated by the user — remember the choice on desktop.
  const setSidebarOpenUser = useCallback((open) => {
    if (window.innerWidth >= 768) userClosedSidebarRef.current = !open;
    setSidebarOpen(open);
  }, []);

  const runningBots = allBots.filter(b => b.is_active);

  const refetchBots = useCallback(async () => {
    try {
      const res = await apiClient.get('/api/bots/summary');
      const list = Array.isArray(res.data) ? res.data : [];
      setAllBots(list);
      setBackendOk(true);
      // Poll fast while any bot is still starting up (fetching data /
      // backtesting) so the cards show live progress; relax once all are idle
      const transitional = list.some(b => b.is_active && ['starting', 'fetching', 'backtesting'].includes(b.runtime?.phase));
      pollIntervalRef.current = transitional ? 2500 : 15000;
    } catch {
      setBackendOk(false);
      pollIntervalRef.current = Math.min(pollIntervalRef.current * 2, 60000);
    }
  }, []);

  useEffect(() => {
    let botTimer;
    let cancelled = false;
    if (hasApiKey) {
      refetchBots(); // eslint-disable-line react-hooks/set-state-in-effect -- initial data fetch on mount
      const schedulePoll = () => {
        if (cancelled) return;
        botTimer = setTimeout(() => {
          refetchBots().finally(schedulePoll);
        }, pollIntervalRef.current);
      };
      schedulePoll();
    }

    const handleOpenBuilder = async (e) => {
        const botSummary = e.detail || null;
        if (botSummary && botSummary.id) {
            // Fetch full bot config (summary endpoint doesn't include nodes/edges)
            try {
                const res = await apiClient.get(`/api/bots/by-id/${botSummary.id}`);
                setEditingBot(res.data);
            } catch {
                setEditingBot(botSummary);
            }
        } else {
            setEditingBot(null);
        }
        setShowBuilder(true);
        if (window.innerWidth < 768) setSidebarOpen(false);
    };

    window.addEventListener('open-builder', handleOpenBuilder);
    // Bot cards ask for an immediate refresh right after start/stop so the
    // new phase shows up without waiting for the next scheduled poll
    const handleRefresh = () => { refetchBots(); };
    window.addEventListener('refresh-bots', handleRefresh);

    return () => {
      cancelled = true;
      clearTimeout(botTimer);
      window.removeEventListener('open-builder', handleOpenBuilder);
      window.removeEventListener('refresh-bots', handleRefresh);
    };
  }, [refetchBots, hasApiKey]);

  const handleOpenChart = (dataset) => {
    // Exchange is part of the identity: the same pair can be open
    // for two exchanges side by side
    const exchange = (dataset.exchange || 'okx').toLowerCase();
    const chartId = `${exchange}_${dataset.symbol}_${dataset.timeframe}`;
    if (!openCharts.find(c => c.id === chartId)) {
      setOpenCharts(prev => [...prev, { ...dataset, exchange, id: chartId }]);
    }
    setActiveView(chartId);
    if (window.innerWidth < 768) setSidebarOpen(false);
  };

  const openBotChart = (bot) => {
    const symbolsToOpen = (bot.settings?.symbols && bot.settings.symbols.length > 0)
      ? bot.settings.symbols
      : (bot.settings?.symbol ? [bot.settings.symbol] : []);

    const timeframe = bot.settings?.timeframe || "15m";
    const exchange = (bot.settings?.data_exchange || 'okx').toLowerCase();
    let updatedCharts = [...openCharts];
    let lastOpenedChartId = "";

    symbolsToOpen.forEach(sym => {
      const chartId = `${exchange}_${sym}_${timeframe}`;
      lastOpenedChartId = chartId;
      if (!updatedCharts.find(c => c.id === chartId)) {
        updatedCharts.push({ id: chartId, symbol: sym, timeframe, exchange });
      }
    });

    setOpenCharts(updatedCharts);
    if (lastOpenedChartId) {
      setActiveView(lastOpenedChartId);
    }
    if (window.innerWidth < 768) setSidebarOpen(false);
  };

  // Bot cards open their pair charts via a window event (same pattern as open-builder)
  useEffect(() => {
    const handler = (e) => { if (e.detail) openBotChart(e.detail); };
    window.addEventListener('open-bot-chart', handler);
    return () => window.removeEventListener('open-bot-chart', handler);
  });

  // "View in Analytics" on a bot card: jump to the trades view filtered on
  // that bot (+ mode). TradeManager reads the request once when it mounts or
  // when a new one arrives.
  const [analyticsRequest, setAnalyticsRequest] = useState(null);
  useEffect(() => {
    const handler = (e) => {
      setAnalyticsRequest({ bot: e.detail?.bot || 'all', mode: e.detail?.mode || null, at: Date.now() });
      setActiveView('trades');
      if (window.innerWidth < 768) setSidebarOpen(false);
    };
    window.addEventListener('open-analytics', handler);
    return () => window.removeEventListener('open-analytics', handler);
  }, []);

  const closeChart = (chartId, e) => {
    e.stopPropagation();
    setOpenCharts(prev => prev.filter(c => c.id !== chartId));
    if (activeView === chartId) {
      setActiveView('home');
    }
    // Intentionally not closing the sidebar here to preserve mobile UX state
  };

  const navigateTo = (view) => {
      setActiveView(view);
      if (window.innerWidth < 768) {
          setSidebarOpen(false);
      }
  };

  // Stable callback so the memoized ChartEngine doesn't re-render every poll.
  const openDataVault = useCallback(() => {
      setActiveView('manager');
      if (window.innerWidth < 768) setSidebarOpen(false);
  }, []);

  if (hasApiKey === null) {
    return <div className="h-[100dvh] bg-bg" aria-busy="true" />;
  }

  if (!hasApiKey) {
    return (
      <>
        <ApiKeyGate
          signedOutReason={signedOutReason}
          onUnlock={() => { setSignedOutReason(null); setHasApiKey(true); }}
        />
        <Toaster />
      </>
    );
  }

  const HEADER_TITLES = {
    manager: 'Market Data Vault',
    bots: 'Trading Algorithms',
    trades: 'Trade Analytics',
    settings: 'Exchange Configuration',
  };

  return (
    <div className="flex h-[100dvh] bg-bg text-text font-sans overflow-hidden relative">

      <button
        aria-label="Open sidebar"
        className={`fixed top-3 left-4 z-[90] p-2 bg-raised border border-border hover:border-accent rounded-md shadow-card text-muted hover:text-accent transition-all duration-300 ${sidebarOpen ? 'opacity-0 pointer-events-none -translate-x-10' : 'opacity-100 translate-x-0'}`}
        onClick={() => setSidebarOpenUser(true)}
      >
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" /></svg>
      </button>

      {sidebarOpen && (
         <div className="fixed inset-0 backdrop z-[70] md:hidden fade-in" onClick={() => setSidebarOpen(false)}></div>
      )}

      <Sidebar
        activeView={activeView}
        setActiveView={navigateTo}
        openCharts={openCharts}
        closeChart={closeChart}
        runningBots={runningBots}
        openBotChart={openBotChart}
        sidebarOpen={sidebarOpen}
        setSidebarOpen={setSidebarOpenUser}
        backendOk={backendOk}
        onLogout={handleLogout}
      />

      <div className={`flex-1 flex flex-col h-full overflow-hidden relative transition-all duration-300 ease-in-out ${sidebarOpen ? 'md:ml-64' : 'ml-0'}`}>

        {HEADER_TITLES[activeView] && (
          <header className="h-12 bg-raised border-b border-border flex items-center justify-between px-4 md:px-6 shrink-0 relative">
            <div className="absolute bottom-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-accent/20 to-transparent" />
            <div className={`transition-all duration-300 ${!sidebarOpen ? 'ml-12' : 'ml-0'}`}>
              <h2 className="text-xs md:text-sm font-semibold text-text tracking-[0.15em] uppercase">
                {HEADER_TITLES[activeView]}
              </h2>
            </div>
          </header>
        )}

        {error && (
          <div className="absolute top-16 left-1/2 transform -translate-x-1/2 p-3 bg-danger/10 backdrop-blur-xl border border-danger/50 text-danger text-xs md:text-sm rounded-md shadow-pop flex justify-between items-center z-[100] min-w-[300px] fade-in">
            <span>{error}</span>
            <button aria-label="Dismiss error" className="text-danger hover:text-text ml-4 font-bold" onClick={() => setError(null)}>✕</button>
          </div>
        )}

        <main className="flex-1 overflow-x-hidden overflow-y-auto flex flex-col relative w-full custom-scrollbar bg-bg">

          {activeView === 'home' && (
             <Home setActiveView={navigateTo} bots={allBots} backendOk={backendOk} refetchBots={refetchBots} />
          )}

          {activeView === 'manager' && <DataManager openChart={handleOpenChart} />}

          {activeView === 'settings' && <Settings />}

          {activeView === 'bots' && <BotManagerUI bots={allBots} refetchBots={refetchBots} backendOk={backendOk} />}

          {activeView === 'trades' && <TradeManager setError={setError} bots={allBots} request={analyticsRequest} />}

          {openCharts.map(chart => (
            activeView === chart.id && (
              <div key={chart.id} className="flex-1 w-full h-full relative border-t-0 border border-border fade-in">
                 <Suspense fallback={<LazyFallback label="Loading chart" />}>
                   <ChartEngine dataset={chart} openDataVault={openDataVault} />
                 </Suspense>
              </div>
            )
          ))}

        </main>
      </div>

      {showBuilder && (
        <div className="absolute inset-0 z-[100] bg-bg fade-in">
           <Suspense fallback={<LazyFallback label="Loading builder" />}>
             <BotBuilder closeBuilder={() => { setShowBuilder(false); refetchBots(); }} editingBot={editingBot} />
           </Suspense>
        </div>
      )}

      <Toaster />
      <ConfirmDialogHost />

    </div>
  );
}
