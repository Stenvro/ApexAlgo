import { useEffect, useState, useRef, useMemo, useCallback, memo } from 'react';
import { createChart, CandlestickSeries, HistogramSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts';
import { apiClient } from '../api/client';
import { humanizeApiError } from '../api/errors';
import { useIndicators, getIndicatorPane } from './Builder/indicatorConfig';
import { getToken } from '../theme';
import Button from './ui/Button';
import Badge from './ui/Badge';
import { isShortOpen, isOpeningOrder, modeTag } from '../utils/orders';
import { fmtMoney, fmtMoneyTitle, rowCurrency, rowKind, symbolParts, positionPnl } from '../utils/money';

const safeParseTime = (ts) => { 
  if (!ts) return null; 
  if (typeof ts === 'number') return ts;  
  let cleanTs = ts; 
  if (typeof cleanTs === 'string') { 
    if (cleanTs.includes(' ')) cleanTs = cleanTs.replace(' ', 'T'); 
    if (!cleanTs.endsWith('Z') && !cleanTs.includes('+')) cleanTs += 'Z'; 
  } 
  const parsed = Math.floor(new Date(cleanTs).getTime() / 1000); 
  return isNaN(parsed) ? null : parsed; 
}; 

const getTimeframeSeconds = (tf) => { 
    if (!tf) return 60; 
    const val = parseInt(tf); 
    if (tf.endsWith('m')) return val * 60; 
    if (tf.endsWith('h')) return val * 3600; 
    if (tf.endsWith('d')) return val * 86400; 
    return 60; 
}; 

// Series palette resolved from the live CSS tokens at call time so indicator
// lines re-tint when the theme switches (chart is re-created on theme change).
const getChartPalette = () => [
    getToken('info'), getToken('accent'), getToken('success'), getToken('danger'),
    getToken('purple'), getToken('chart-1'), getToken('chart-2'), getToken('chart-3'),
];
const colorCache = {}; // seriesId -> stable palette index
let colorIdx = 0;
const getColor = (str) => {
    if (colorCache[str] === undefined) colorCache[str] = colorIdx++;
    const palette = getChartPalette();
    return palette[colorCache[str] % palette.length];
};

const formatNum = (num, decimals = 2) => { 
    if (num === undefined || num === null || isNaN(Number(num))) return '0.00'; 
    return Number(num).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals }); 
}; 

const formatCrypto = (val) => { 
    if (!val) return "0.00"; 
    return Number(val).toFixed(6).replace(/\.?0+$/, '');  
}; 

function ChartEngine({ dataset, openDataVault }) {
  const chartContainerRef = useRef(); 
  const chartRef = useRef(null); 
  const candleSeriesRef = useRef(null); 
  const volumeSeriesRef = useRef(null); 
  const indicatorSeriesRef = useRef({});  
  const markersPluginRef = useRef(null);  
  const priceLinesRef = useRef([]);  
  const lastCandleRef = useRef(null);  
  const isCrosshairActive = useRef(false);
  const lastSignalIdRef = useRef(0);
  // Incremental order/position polling (see /api/trades/positions `since_id`)
  const tradeCursorRef = useRef({ ordId: 0, posId: 0, since: null });
  const lastDbTimeRef = useRef(null);   // newest CLOSED candle time from the DB
  const formingCandleRef = useRef(null); // synthetic in-progress bar (ticker-fed)
   
  const { registry: indicatorRegistry } = useIndicators();
  const [candleTimes, setCandleTimes] = useState([]); 
  const [loading, setLoading] = useState(true); 
  const [errorMsg, setErrorMsg] = useState(null); 
  const [hoverData, setHoverData] = useState(null); 
  const [marketInfo, setMarketInfo] = useState(null); 
  const [isLiveStreamActive, setIsLiveStreamActive] = useState(false); 

  const [signals, setSignals] = useState([]); 
  const [orders, setOrders] = useState([]);  
  const [positions, setPositions] = useState([]);  
   
  const [retryTick, setRetryTick] = useState(0);
  const [themeTick, setThemeTick] = useState(0); // bumps on apex-theme-changed -> chart re-init with new tokens
  const [showMenu, setShowMenu] = useState(false);
  const [expandedMenuBot, setExpandedMenuBot] = useState(null);  
  const menuRef = useRef(null);
  // Overlay menu closes on outside click and Esc
  useEffect(() => {
    if (!showMenu) return undefined;
    const onDown = (e) => { if (menuRef.current && !menuRef.current.contains(e.target)) setShowMenu(false); };
    const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); setShowMenu(false); } };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey); };
  }, [showMenu]);
  // Bots on this pair + interval whose candles come from another exchange —
  // listed in the menu so "why isn't my bot here?" answers itself
  const [otherExchangeBots, setOtherExchangeBots] = useState([]);
  const [botConfigs, setBotConfigs] = useState({}); 

  const getSnappedTime = useCallback((rawTime) => { 
      if (!candleTimes || candleTimes.length === 0) return null; 
      
      let left = 0;
      let right = candleTimes.length - 1;
      let closest = candleTimes[0];
      let minDiff = Math.abs(rawTime - closest);

      while (left <= right) {
          const mid = Math.floor((left + right) / 2);
          const midTime = candleTimes[mid];
          const diff = Math.abs(rawTime - midTime);

          if (diff < minDiff) {
              minDiff = diff;
              closest = midTime;
          }

          if (midTime === rawTime) {
              return midTime; 
          } else if (midTime < rawTime) {
              left = mid + 1;
          } else {
              right = mid - 1;
          }
      }
      return minDiff <= 3600 ? closest : null; 
  }, [candleTimes]); 

  const fetchMarketInfo = async () => {
    try {
      const response = await apiClient.get(`/api/data/market-info/${dataset.symbol.replace('/', '-')}`, { params: { exchange: dataset.exchange || 'okx' } });
      setMarketInfo(response.data);
      updateFormingCandle(response.data?.last);
      setIsLiveStreamActive(true);
    } catch { setIsLiveStreamActive(false); }
  };

  // Paint the in-progress candle from the live ticker price. Closed candles
  // come from the database; the forming one exists only in the chart, so the
  // engine never sees half-finished data. Only drawn once the previous
  // candle has landed in the DB — lightweight-charts can't update backwards.
  const updateFormingCandle = (price) => {
    if (!price || !candleSeriesRef.current) return;
    const tfSeconds = getTimeframeSeconds(dataset.timeframe);
    if (!tfSeconds || !lastDbTimeRef.current) return;
    const bucket = Math.floor(Date.now() / 1000 / tfSeconds) * tfSeconds;
    if (bucket !== lastDbTimeRef.current + tfSeconds) return;

    const prev = formingCandleRef.current;
    const bar = (prev && prev.time === bucket)
      ? { ...prev, high: Math.max(prev.high, price), low: Math.min(prev.low, price), close: price }
      : { time: bucket, open: lastCandleRef.current?.close ?? price, high: price, low: price, close: price };
    formingCandleRef.current = bar;
    try {
      candleSeriesRef.current.update(bar);
      if (!isCrosshairActive.current) setHoverData({ ...bar, value: 0 });
    } catch { /* series may briefly lag behind DB updates */ }
  };

  const initBotConfigs = async () => { 
    try { 
      const botRes = await apiClient.get('/api/bots/'); 
       
      // A bot only belongs on this chart when exchange, pair AND interval all
      // match — its candles (and therefore its signals) come from that exact dataset.
      const chartExchange = (dataset.exchange || 'okx').toLowerCase();
      const botExchangeOf = (b) => (b.exchange || b.settings?.data_exchange || 'okx').toLowerCase();
      const samePairAndInterval = botRes.data.filter(b => {
          const hasSymbol = (b.settings?.symbols && b.settings.symbols.includes(dataset.symbol)) || b.settings?.symbol === dataset.symbol;
          return hasSymbol && b.settings?.timeframe === dataset.timeframe;
      });
      const validBots = samePairAndInterval.filter(b => botExchangeOf(b) === chartExchange);
      setOtherExchangeBots(samePairAndInterval.filter(b => botExchangeOf(b) !== chartExchange)
          .map(b => ({ name: b.name, exchange: botExchangeOf(b) })));
       
      setBotConfigs(prev => { 
         const newConfigs = { ...prev }; 
         validBots.forEach(bot => { 
             if (!newConfigs[bot.name]) { 
                 newConfigs[bot.name] = {  
                     showSignals: false,
                     showBacktestTrades: false,
                     showRealTrades: false,
                     showBacktestPositions: false,  
                     showRealPositions: false,  
                     indicators: {},  
                     nodeMap: {}  
                 }; 
             } 
             newConfigs[bot.name].nodeMap = {}; 
             if (bot.settings && bot.settings.nodes) { 
                 Object.entries(bot.settings.nodes).forEach(([nodeId, node]) => { 
                     if (node.class === 'indicator') { 
                         // CRUCIAL FIX: Handle both Array and Object params safely
                         let suffix = '';
                         if (node.params) {
                             if (Array.isArray(node.params) && node.params.length > 0) {
                                 suffix = `_${node.params.join('_')}`;
                             } else if (typeof node.params === 'object' && Object.keys(node.params).length > 0) {
                                 suffix = `_${Object.values(node.params).join('_')}`;
                             }
                         }
                         const indName = `${node.method.toUpperCase()}${suffix}`; 
                         newConfigs[bot.name].nodeMap[nodeId] = indName;  
                     } 
                 }); 
             } 
         }); 
         return newConfigs; 
      }); 
    } catch (e) { console.error("Error setting up configs", e); } 
  }; 

  const pollData = async (signal) => {
    try {
      const safeSymbol = dataset.symbol.replace('/', '-');
      const sigParams = { symbol: dataset.symbol, timeframe: dataset.timeframe, exchange: dataset.exchange || 'okx', limit: 200000 };
      if (lastSignalIdRef.current > 0) sigParams.since_id = lastSignalIdRef.current;
      const cur = tradeCursorRef.current;
      const incremental = cur.since !== null;
      const ordParams = { symbol: safeSymbol, limit: 50000, ...(incremental ? { since_id: cur.ordId } : {}) };
      const posParams = { symbol: safeSymbol, limit: 50000, ...(incremental ? { since_id: cur.posId, since: cur.since } : {}) };
      // 5 min margin against client/server clock skew — re-sent rows merge by id
      const polledAt = new Date(Date.now() - 5 * 60 * 1000).toISOString();

      const [sigRes, ordRes, posRes] = await Promise.all([
          apiClient.get(`/api/bots/signals`, { params: sigParams, signal }),
          apiClient.get(`/api/trades/orders`, { params: ordParams, signal }),
          apiClient.get(`/api/trades/positions`, { params: posParams, signal })
      ]);
      if (signal?.aborted) return;

      const newSigs = sigRes.data || [];
      if (newSigs.length > 0) {
          const maxId = Math.max(...newSigs.map(s => s.id));
          if (lastSignalIdRef.current > 0) {
              // Incremental: merge new signals into existing, never the same id twice
              setSignals(prev => {
                  const seen = new Set(prev.map(s => s.id));
                  return [...prev, ...newSigs.filter(s => !seen.has(s.id))];
              });
          } else {
              // Initial full load
              setSignals(newSigs);
          }
          lastSignalIdRef.current = maxId;
      }
      const mergeById = (prev, incoming) => {
          if (incoming.length === 0) return prev;
          const byId = new Map(prev.map(r => [r.id, r]));
          incoming.forEach(r => byId.set(r.id, r));
          return [...byId.values()];
      };
      // /api/trades has no exchange filter: keep only rows from this
      // chart's exchange (the same pair on another exchange is a different dataset)
      const chartEx = (dataset.exchange || 'okx').toLowerCase();
      const sameExchange = (r) => !r.exchange || String(r.exchange).toLowerCase() === chartEx;
      const ordNew = (ordRes.data || []).filter(sameExchange);
      const posNew = (posRes.data || []).filter(sameExchange);
      let maxOrd = cur.ordId, maxPos = cur.posId;
      ordNew.forEach(o => { if (o.id > maxOrd) maxOrd = o.id; });
      posNew.forEach(p => { if (p.id > maxPos) maxPos = p.id; });
      setOrders(prev => incremental ? mergeById(prev, ordNew) : ordNew);
      setPositions(prev => incremental ? mergeById(prev, posNew) : posNew);
      tradeCursorRef.current = { ordId: maxOrd, posId: maxPos, since: polledAt };
    } catch (e) { if (!signal?.aborted) console.error("Data Fetch Error:", e); }
  }; 

  const applyInitialDataToChart = (rawData) => { 
    if (!rawData || rawData.length === 0) return; 
    const uniqueData = []; 
    const seenTimes = new Set(); 
    const extractedTimes = []; 
     
    rawData.forEach(item => { 
        const safeTime = safeParseTime(item.time || item.timestamp); 
        if (safeTime && !seenTimes.has(safeTime)) { 
            seenTimes.add(safeTime); 
            extractedTimes.push(safeTime); 
            uniqueData.push({ time: safeTime, open: item.open, high: item.high, low: item.low, close: item.close, value: item.volume || item.value }); 
        } 
    }); 
     
    uniqueData.sort((a, b) => a.time - b.time); 
    extractedTimes.sort((a, b) => a - b); 
    setCandleTimes(extractedTimes); 
     
    try { 
      candleSeriesRef.current.setData(uniqueData); 
      const volumeData = uniqueData.map(d => ({ 
        time: d.time, value: d.value, color: (d.close >= d.open ? getToken('success') : getToken('danger')) + '80'
      })); 
      volumeSeriesRef.current.setData(volumeData); 
       
      if (uniqueData.length > 0) {
        lastCandleRef.current = { ...uniqueData[uniqueData.length - 1], value: volumeData[volumeData.length - 1].value };
        lastDbTimeRef.current = lastCandleRef.current.time;
        formingCandleRef.current = null;
        if (!isCrosshairActive.current) setHoverData({ ...lastCandleRef.current, time: lastCandleRef.current.time });
      }
    } catch (e) { console.error("Data Load Crash Prevented:", e); } 
  }; 

  const updateLatestCandles = async () => { 
    if (!candleSeriesRef.current || !volumeSeriesRef.current) return; 
    try { 
      const response = await apiClient.get(`/api/data/candles/${dataset.symbol.replace('/', '-')}`, { 
        headers: { 'x-timeframe': dataset.timeframe }, params: { limit: 10, exchange: dataset.exchange || 'okx' } 
      }); 
      if (response.data && response.data.length > 0) { 
        const rawLatest = response.data[response.data.length - 1]; 
        const latestTime = safeParseTime(rawLatest.time || rawLatest.timestamp); 
        if (!latestTime) return; 

        const latestDbCandle = { ...rawLatest, time: latestTime };

        // Compare against the last DB candle, not the synthetic forming bar,
        // so a freshly closed candle always replaces its ticker-fed preview
        if (!lastDbTimeRef.current || latestDbCandle.time >= lastDbTimeRef.current) {
            candleSeriesRef.current.update(latestDbCandle);
            volumeSeriesRef.current.update({
                time: latestDbCandle.time, value: latestDbCandle.volume || latestDbCandle.value,
                color: (latestDbCandle.close >= latestDbCandle.open ? getToken('success') : getToken('danger')) + '80'
            });

            setCandleTimes(prev => prev.includes(latestDbCandle.time) ? prev : [...prev, latestDbCandle.time].sort((a,b) => a-b));

            const newHoverState = { ...latestDbCandle, value: latestDbCandle.volume || latestDbCandle.value, time: latestDbCandle.time };
            if (!isCrosshairActive.current) setHoverData(newHoverState);

            lastDbTimeRef.current = latestDbCandle.time;
            lastCandleRef.current = newHoverState;
            if (formingCandleRef.current && formingCandleRef.current.time <= latestDbCandle.time) {
                formingCandleRef.current = null;
            }
        }
      } 
    } catch { /* silent */ }
  };

  useEffect(() => {
    const abortController = new AbortController();
    const signal = abortController.signal;

    fetchMarketInfo();
    initBotConfigs();
    tradeCursorRef.current = { ordId: 0, posId: 0, since: null };
    pollData(signal);

    // 10s matches the server-side market-info TTL cache and drives the
    // ticker-fed forming candle
    const infoInterval = setInterval(fetchMarketInfo, 10000);

    const initChart = async () => {
      try {
        setLoading(true);
        setErrorMsg(null);
        lastSignalIdRef.current = 0;
        // lightweight-charts needs raw color values — resolve the live CSS
        // tokens at init time; the chart is re-created on apex-theme-changed.
        const tBg = getToken('bg'), tBorder = getToken('border'), tMuted = getToken('muted');
        const tUp = getToken('success'), tDown = getToken('danger');
        const chart = createChart(chartContainerRef.current, {
          layout: { background: { type: 'solid', color: tBg }, textColor: tMuted },
          grid: { vertLines: { color: tBorder }, horzLines: { color: tBorder } },
          crosshair: { mode: 0 },

          rightPriceScale: { borderColor: tBorder, autoScale: true, scaleMargins: { top: 0.1, bottom: 0.25 } },

          leftPriceScale: { visible: true, borderColor: tBorder, autoScale: true, scaleMargins: { top: 0.8, bottom: 0 } },

          timeScale: { borderColor: tBorder, timeVisible: true },
          autoSize: true,
        });
        chartRef.current = chart;

        candleSeriesRef.current = chart.addSeries(CandlestickSeries, {
          upColor: tUp, downColor: tDown, borderVisible: false, wickUpColor: tUp, wickDownColor: tDown
        });

        markersPluginRef.current = createSeriesMarkers(candleSeriesRef.current, []);

        volumeSeriesRef.current = chart.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: '' });
        volumeSeriesRef.current.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });

        const response = await apiClient.get(`/api/data/candles/${dataset.symbol.replace('/', '-')}`, {
            headers: { 'x-timeframe': dataset.timeframe },
            params: { exchange: dataset.exchange || 'okx' },
            signal
        });

        if (signal.aborted) return;

        if (!response.data || response.data.length === 0) {
           setErrorMsg("No data found in local database. Download historical data first.");
           setLoading(false); return;
        }

        applyInitialDataToChart(response.data);
        chart.timeScale().fitContent();

        chart.subscribeCrosshairMove((param) => {
          if (!param.point || !param.time || param.point.x < 0 || param.point.y < 0) {
            isCrosshairActive.current = false;
            if (lastCandleRef.current) setHoverData({ ...lastCandleRef.current, time: lastCandleRef.current.time });
            return;
          }
          isCrosshairActive.current = true;
          const dCandle = param.seriesData.get(candleSeriesRef.current);
          const dVol = param.seriesData.get(volumeSeriesRef.current);
          if (dCandle) setHoverData({ ...dCandle, value: dVol ? dVol.value : 0, time: param.time });
        });
      } catch (error) {
        if (signal.aborted) return;
        setErrorMsg(humanizeApiError(error, 'Failed to load chart data.'));
      } finally { if (!signal.aborted) setLoading(false); }
    };

    initChart();
    const pollInterval = setInterval(updateLatestCandles, 5000);
    const signalInterval = setInterval(() => pollData(signal), 15000);

    return () => {
      abortController.abort();
      clearInterval(infoInterval);
      clearInterval(pollInterval);
      clearInterval(signalInterval);
      if (chartRef.current) { chartRef.current.remove(); chartRef.current = null; }
      // Series belonged to the removed chart — drop refs so they're recreated
      indicatorSeriesRef.current = {};
      markersPluginRef.current = null;
      priceLinesRef.current = [];
    };
  }, [dataset.exchange, dataset.symbol, dataset.timeframe, retryTick, themeTick]); // eslint-disable-line react-hooks/exhaustive-deps -- chart init must only re-run on exchange/symbol/timeframe change, theme switch, or manual retry

  // Rebuild the chart with the new token values when the theme flips
  useEffect(() => {
    const onTheme = () => setThemeTick(t => t + 1);
    window.addEventListener('apex-theme-changed', onTheme);
    return () => window.removeEventListener('apex-theme-changed', onTheme);
  }, []);

  useEffect(() => {
    if (signals.length === 0) return; 
    setBotConfigs(prev => { 
      let changed = false; 
      const next = { ...prev }; 
      signals.forEach(sig => { 
        if (!next[sig.bot_name]) return; 
        let parsedExtra = {}; 

        try { parsedExtra = typeof sig.extra_data === 'string' ? JSON.parse(sig.extra_data) : (sig.extra_data || {}); } catch { /* silent */ } 
        Object.keys(parsedExtra).forEach(key => { 
           const readableKey = next[sig.bot_name].nodeMap?.[key] || key; 
           if (next[sig.bot_name].indicators[readableKey] === undefined) { 
               if (!changed) changed = true; 
               next[sig.bot_name] = { ...next[sig.bot_name], indicators: { ...next[sig.bot_name].indicators, [readableKey]: false } }; 
           } 
        }); 
      }); 
      return changed ? next : prev; 
    }); 
  }, [signals]); 

  const snappedSignalMap = useMemo(() => { 
    const map = {}; 
    signals.forEach(sig => { 
      const rawTime = safeParseTime(sig.timestamp); 
      if (!rawTime) return; 
      const snappedTime = getSnappedTime(rawTime); 
      if (!snappedTime) return; 
      if (!map[snappedTime]) map[snappedTime] = {}; 
      let parsedExtra = {}; 

      try { parsedExtra = typeof sig.extra_data === 'string' ? JSON.parse(sig.extra_data) : (sig.extra_data || {}); } catch { /* silent */ } 
      const config = botConfigs[sig.bot_name]; 
      const mappedExtra = {}; 
      Object.keys(parsedExtra).forEach(k => { 
          const readableKey = (config && config.nodeMap && config.nodeMap[k]) ? config.nodeMap[k] : k; 
          mappedExtra[readableKey] = parsedExtra[k]; 
      }); 
      map[snappedTime][sig.bot_name] = { ...sig, extra_data: mappedExtra }; 
    }); 
    return map; 
  }, [signals, getSnappedTime, botConfigs]); 

  const snappedTradeMap = useMemo(() => { 
    const map = {}; 
    orders.forEach(order => { 
        const rawTime = safeParseTime(order.timestamp); 
        if (!rawTime) return; 
        const snappedTime = getSnappedTime(rawTime); 
        if (!snappedTime) return; 
         
        const config = botConfigs[order.bot_name]; 
        const isBacktest = order.mode === 'backtest'; 
        if (isBacktest && !config?.showBacktestTrades) return; 
        if (!isBacktest && !config?.showRealTrades) return; 

        const relatedPosition = positions.find(p => p.id === order.position_id); 

        if (!map[snappedTime]) map[snappedTime] = []; 
        map[snappedTime].push({ ...order, position: relatedPosition }); 
    }); 
    return map; 
  }, [orders, positions, getSnappedTime, botConfigs]); 

  useEffect(() => { 
    if (!chartRef.current || !candleSeriesRef.current || candleTimes.length === 0) return; 
     
    const markersByTime = {}; 
    signals.forEach(sig => { 
      if (botConfigs[sig.bot_name]?.showSignals && (sig.action === 'buy' || sig.action === 'sell' || sig.action === 'short' || sig.action === 'cover')) { 
        const rawTime = safeParseTime(sig.timestamp); 
        if (!rawTime) return;  
        const snappedTime = getSnappedTime(rawTime); 
        if (!snappedTime) return;  
        if (!markersByTime[snappedTime]) markersByTime[snappedTime] = []; 
        markersByTime[snappedTime].push({ type: 'signal', data: sig }); 
      } 
    }); 

    Object.entries(snappedTradeMap).forEach(([timeStr, tradesAtTime]) => { 
        const snappedTime = parseInt(timeStr); 
        if (!markersByTime[snappedTime]) markersByTime[snappedTime] = []; 
        tradesAtTime.forEach(trade => markersByTime[snappedTime].push({ type: 'trade', data: trade })); 
    }); 

    const finalMarkers = []; 
    Object.keys(markersByTime).forEach(timeStr => { 
        const time = parseInt(timeStr); 
        const itemsAtTime = markersByTime[time]; 
         
        const buySigs = itemsAtTime.filter(i => i.type === 'signal' && i.data.action === 'buy'); 
        const sellSigs = itemsAtTime.filter(i => i.type === 'signal' && i.data.action === 'sell'); 
        const shortSigs = itemsAtTime.filter(i => i.type === 'signal' && i.data.action === 'short'); 
        const coverSigs = itemsAtTime.filter(i => i.type === 'signal' && i.data.action === 'cover'); 
        // Opening vs closing fill, side-aware: a non-reduce-only sell on a
        // perp opens a short, a reduce-only buy covers one
        const buyTrades = itemsAtTime.filter(i => i.type === 'trade' && i.data.side === 'buy' && !i.data.reduce_only); 
        const sellTrades = itemsAtTime.filter(i => i.type === 'trade' && i.data.side === 'sell' && !isShortOpen(i.data)); 
        const shortTrades = itemsAtTime.filter(i => i.type === 'trade' && isShortOpen(i.data)); 
        const coverTrades = itemsAtTime.filter(i => i.type === 'trade' && i.data.side === 'buy' && i.data.reduce_only); 
         
        // Marker colors resolved from the live CSS tokens (raw values required)
        if (buySigs.length > 0) finalMarkers.push({ time: time, position: 'belowBar', color: getToken('success'), shape: 'arrowUp', text: 'S-B' });
        if (sellSigs.length > 0) finalMarkers.push({ time: time, position: 'aboveBar', color: getToken('danger'), shape: 'arrowDown', text: 'S-S' });
        if (shortSigs.length > 0) finalMarkers.push({ time: time, position: 'aboveBar', color: getToken('danger'), shape: 'arrowDown', text: 'S-SH' });
        if (coverSigs.length > 0) finalMarkers.push({ time: time, position: 'belowBar', color: getToken('success'), shape: 'arrowUp', text: 'S-CV' });

        if (buyTrades.length > 0) finalMarkers.push({ time: time, position: 'belowBar', color: getToken('info'), shape: 'circle', text: 'T-BUY' });
        if (sellTrades.length > 0) finalMarkers.push({ time: time, position: 'aboveBar', color: getToken('purple'), shape: 'circle', text: 'T-SELL' });
        if (shortTrades.length > 0) finalMarkers.push({ time: time, position: 'aboveBar', color: getToken('info'), shape: 'circle', text: 'T-SHORT' });
        if (coverTrades.length > 0) finalMarkers.push({ time: time, position: 'belowBar', color: getToken('purple'), shape: 'circle', text: 'T-COVER' });
    }); 

    finalMarkers.sort((a, b) => a.time - b.time); 
    try { if (markersPluginRef.current) markersPluginRef.current.setMarkers(finalMarkers); } catch { /* silent */ } 

    priceLinesRef.current.forEach(line => { try { candleSeriesRef.current.removePriceLine(line); } catch { /* silent */ } }); 
    priceLinesRef.current = []; 

    positions.forEach(pos => { 
        if (pos.status === 'open') { 
            const isBacktest = pos.mode === 'backtest'; 
            const config = botConfigs[pos.bot_name]; 

            if (isBacktest && !config?.showBacktestPositions) return; 
            if (!isBacktest && !config?.showRealPositions) return; 

            const priceLine = { 
                price: pos.entry_price, 
                color: isBacktest ? getToken('muted') : (pos.side === 'long' ? getToken('success') : getToken('danger')),
                lineWidth: 2, 
                lineStyle: 2,  
                axisLabelVisible: true, 
                title: `ENTRY (${modeTag(pos.mode)})`, 
            }; 
            try { priceLinesRef.current.push(candleSeriesRef.current.createPriceLine(priceLine)); } catch { /* silent */ } 
        } 
    }); 

    Object.keys(botConfigs).forEach(botName => { 
        const config = botConfigs[botName]; 
        Object.keys(config.indicators).forEach(indKey => { 
            const seriesId = `${botName}_${indKey}`; 
            const isActive = config.indicators[indKey]; 

            if (isActive) {
                if (!indicatorSeriesRef.current[seriesId]) {
                    // Pane comes from the backend registry; wait for it so the
                    // series is created on the right price scale the first time.
                    if (!indicatorRegistry) return;
                    // Node ids are "<method>_<n>" (e.g. "RSI_14"); look the method up.
                    const scale = getIndicatorPane(indKey.split('_')[0]) || 'oscillator';
                    let scaleId = 'left'; // default: oscillator pane
                    if (scale === 'overlay') scaleId = 'right';
                    else if (scale === 'volume') scaleId = '';

                    indicatorSeriesRef.current[seriesId] = chartRef.current.addSeries(LineSeries, {
                        color: getColor(seriesId), lineWidth: 2,
                        priceScaleId: scaleId,
                        title: `${indKey}`, lastValueVisible: true, priceLineVisible: true,
                    });
                } 
                const series = indicatorSeriesRef.current[seriesId]; 
                
                const uniqueLineData = [];
                candleTimes.forEach(time => {
                    const botData = snappedSignalMap[time]?.[botName]?.extra_data;
                    if (botData && botData[indKey] !== undefined) {
                        const val = Number(botData[indKey]);
                        if (!isNaN(val)) {
                            uniqueLineData.push({ time, value: val });
                        }
                    }
                });
                 
                try { 
                  if (uniqueLineData.length > 0) { 
                      series.setData(uniqueLineData); 
                      series.applyOptions({ visible: true }); 
                  } else { 
                      series.applyOptions({ visible: false }); 
                  } 
                } catch { /* silent */ } 
            } else { 
                if (indicatorSeriesRef.current[seriesId]) indicatorSeriesRef.current[seriesId].applyOptions({ visible: false }); 
            } 
        }); 
    }); 
  }, [signals, orders, positions, botConfigs, getSnappedTime, snappedTradeMap, candleTimes, snappedSignalMap, indicatorRegistry]); 

  const toggleBotSetting = (botName, settingKey) => {
      setBotConfigs(prev => ({
          ...prev,
          [botName]: { ...prev[botName], [settingKey]: !prev[botName][settingKey] }
      }));
  };

  const toggleIndicatorConfig = (targetBotName, indKey) => {
      setBotConfigs(prev => ({
          ...prev,
          [targetBotName]: {
              ...prev[targetBotName],
              indicators: { ...prev[targetBotName].indicators, [indKey]: !prev[targetBotName].indicators[indKey] }
          }
      }));
  }; 

  const toggleMenuBot = (botName) => setExpandedMenuBot(expandedMenuBot === botName ? null : botName); 

  const formatChange = (num) => { 
    if (num === undefined || num === null) return 'N/A'; 
    const val = parseFloat(num); 
    return val > 0 ? `+${val.toFixed(2)}%` : `${val.toFixed(2)}%`; 
  }; 

  if (!dataset || !dataset.symbol) return null;

  return (
    <div className="flex flex-col w-full h-full bg-bg rounded-none overflow-hidden">

      {/* Toolbar — pl-14 md:pl-20 keeps the text clear of the hamburger button on all breakpoints */}
      <div className="h-14 bg-raised/80 backdrop-blur-xl border-b border-border flex items-center justify-between pl-14 md:pl-20 pr-4 md:pr-6 shrink-0 relative z-30">
        <div className="flex items-center space-x-3 md:space-x-6">
          <div className="flex flex-col">
            <div className="flex items-center space-x-2">
              <span className="text-text font-bold tracking-wider text-xs md:text-sm font-num">{dataset.symbol}</span>
              <span className="bg-info/10 border border-info/30 text-info text-3xs md:text-2xs px-1.5 py-0.5 rounded uppercase font-bold tracking-widest">{(dataset.exchange || 'okx').toUpperCase()}</span>
              <span className="bg-overlay border border-border text-text text-3xs md:text-2xs px-1.5 py-0.5 rounded uppercase font-bold tracking-widest font-num">{dataset.timeframe}</span>
            </div>
            {marketInfo && <span className={`text-2xs md:text-xs font-num font-medium mt-0.5 ${marketInfo.change_24h >= 0 ? 'text-success' : 'text-danger'}`}>{formatNum(marketInfo.last)}</span>}
          </div>

          {marketInfo && (
            <>
              <div className="hidden md:flex flex-col border-l border-border pl-6">
                <span className="text-muted text-2xs uppercase">24h Change</span>
                <span className={`text-xs font-num mt-0.5 ${marketInfo.change_24h >= 0 ? 'text-success' : 'text-danger'}`}>{formatChange(marketInfo.change_24h)}</span>
              </div>
              <div className="hidden md:flex flex-col border-l border-border pl-6">
                <span className="text-muted text-2xs uppercase">24h High</span>
                <span className="text-text text-xs font-num mt-0.5">{formatNum(marketInfo.high_24h)}</span>
              </div>
              <div className="hidden lg:flex flex-col border-l border-border pl-6">
                <span className="text-muted text-2xs uppercase">24h Low</span>
                <span className="text-text text-xs font-num mt-0.5">{formatNum(marketInfo.low_24h)}</span>
              </div>
              <div className="hidden xl:flex flex-col border-l border-border pl-6">
                <span className="text-muted text-2xs uppercase">24h Volume</span>
                <span className="text-text text-xs font-num mt-0.5">{formatNum(marketInfo.vol_24h)}</span>
              </div>
            </>
          )}
        </div>

        <div ref={menuRef} className="flex items-center space-x-2 md:space-x-4 relative">

          <Badge variant={isLiveStreamActive ? 'success' : 'accent'} dot pulse={isLiveStreamActive}>
            {isLiveStreamActive ? 'Synced: Live' : 'Synced: Static'}
          </Badge>

          <button
            onClick={() => setShowMenu(!showMenu)}
            aria-label="Algorithm overlay menu"
            className="p-1.5 rounded-md bg-overlay hover:bg-border transition-all duration-200 border border-border hover:border-border-strong flex items-center justify-center ml-1 md:ml-0">
            <svg className="w-4 h-4 md:w-5 md:h-5 text-text" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M4 6h16M4 12h16M4 18h16" /></svg>
          </button>

          {showMenu && (
            <div className="absolute top-12 right-0 w-[calc(100vw-2rem)] sm:w-80 max-h-[70vh] overflow-y-auto custom-scrollbar bg-overlay/95 backdrop-blur-xl border border-border rounded-lg shadow-pop py-2 z-50">
              <div className="px-4 py-3 text-xs font-bold text-muted uppercase border-b border-border mb-1">Algorithm Overlay</div>
              {Object.keys(botConfigs).length === 0 && otherExchangeBots.length === 0 ? (
                <div className="px-4 py-3 text-xs text-muted">No algorithms active on this chart.</div>
              ) : (
                Object.keys(botConfigs).map(botName => {
                  const config = botConfigs[botName];
                  const isExpanded = expandedMenuBot === botName;

                  return (
                    <div key={botName} className="border-b border-border/50 last:border-0 transition-colors">
                      <button onClick={() => toggleMenuBot(botName)} className="w-full px-4 py-3 flex items-center justify-between hover:bg-border/50 transition-colors">
                        <div className="text-xs md:text-sm font-bold text-text flex items-center">
                           <span className="w-1.5 h-1.5 rounded-full mr-2 bg-success"></span>{botName}
                        </div>
                        <svg className={`w-4 h-4 text-muted transition-transform duration-200 ${isExpanded ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M19 9l-7 7-7-7" /></svg>
                      </button>

                      {isExpanded && (
                        <div className="flex flex-col space-y-4 pl-6 md:pl-8 pr-4 pb-4 bg-bg/50 border-l-2 border-border ml-4 mt-1">
                            <div className="flex flex-col space-y-2 mt-2">
                                <span className="text-3xs md:text-2xs font-bold text-info uppercase tracking-wider">Live & Paper Mode</span>
                                <label className="flex items-center cursor-pointer"><input type="checkbox" className="form-checkbox h-3 w-3 text-info rounded border-border bg-inset" checked={config.showRealTrades} onChange={() => toggleBotSetting(botName, 'showRealTrades')} /><span className="ml-2 text-xs text-text">Real Trades (T-BUY / T-SELL · T-SHORT / T-COVER)</span></label>
                                <label className="flex items-center cursor-pointer"><input type="checkbox" className="form-checkbox h-3 w-3 text-info rounded border-border bg-inset" checked={config.showRealPositions} onChange={() => toggleBotSetting(botName, 'showRealPositions')} /><span className="ml-2 text-xs text-text">Real Position Line</span></label>
                            </div>
                            <div className="flex flex-col space-y-2">
                                <span className="text-3xs md:text-2xs font-bold text-accent uppercase tracking-wider">Backtest Mode</span>
                                <label className="flex items-center cursor-pointer"><input type="checkbox" className="form-checkbox h-3 w-3 text-accent rounded border-border bg-inset" checked={config.showBacktestTrades} onChange={() => toggleBotSetting(botName, 'showBacktestTrades')} /><span className="ml-2 text-xs text-text-secondary">Historical Trades (T-BUY / T-SELL · T-SHORT / T-COVER)</span></label>
                                <label className="flex items-center cursor-pointer"><input type="checkbox" className="form-checkbox h-3 w-3 text-accent rounded border-border bg-inset" checked={config.showBacktestPositions} onChange={() => toggleBotSetting(botName, 'showBacktestPositions')} /><span className="ml-2 text-xs text-text-secondary">Historical Position Line</span></label>
                            </div>
                            <div className="h-px bg-border w-full my-1"></div>
                            <label className="flex items-center cursor-pointer"><input type="checkbox" className="form-checkbox h-3.5 w-3.5 text-success rounded border-border bg-inset" checked={config.showSignals} onChange={() => toggleBotSetting(botName, 'showSignals')} /><span className="ml-2 text-xs text-text italic">Engine Thoughts (S-B / S-S · S-SH / S-CV)</span></label>
                            {Object.keys(config.indicators).map(indKey => (
                                <label key={indKey} className="flex items-center cursor-pointer"><input type="checkbox" className="form-checkbox h-3.5 w-3.5 text-accent rounded border-border bg-inset" checked={config.indicators[indKey]} onChange={() => toggleIndicatorConfig(botName, indKey)} /><span className="ml-2 text-xs text-text">Draw Line: <span className="font-num">{indKey}</span></span></label>
                            ))}
                        </div>
                      )}
                    </div>
                  );
                })
              )}
              {otherExchangeBots.length > 0 && (
                <div className="px-4 py-3 border-t border-border/50 space-y-1">
                  <div className="text-3xs md:text-2xs font-bold text-muted uppercase tracking-wider">Same pair, other exchange</div>
                  {otherExchangeBots.map(b => (
                    <div key={b.name} className="text-xs text-muted">
                      <span className="text-text-secondary">{b.name}</span> runs on {b.exchange.toUpperCase()} — open {dataset.symbol} on {b.exchange.toUpperCase()} to see its overlays.
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="flex-1 relative w-full h-full">
        {loading && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-bg/90 backdrop-blur-sm z-20">
            <svg className="spin w-6 h-6 text-accent" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
              <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
            </svg>
            <span className="text-muted text-2xs font-bold tracking-[0.3em] uppercase">Loading candles…</span>
          </div>
        )}
        {errorMsg && !loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-bg/90 z-20 px-6">
            <div className="terminal-card p-6 max-w-sm w-full text-center space-y-4">
              <div className="w-12 h-12 mx-auto rounded-lg bg-danger/10 border border-danger/30 flex items-center justify-center text-danger">
                <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M12 8v4m0 4h.01M12 3l9 16H3l9-16z" /></svg>
              </div>
              <div>
                <h3 className="text-sm font-semibold text-text mb-1">Chart failed to load</h3>
                <p className="text-xs text-muted leading-relaxed">{errorMsg}</p>
              </div>
              <div className="flex items-center justify-center gap-2.5">
                <Button variant="secondary" size="sm" onClick={() => setRetryTick(t => t + 1)}>Retry</Button>
                {errorMsg.startsWith('No data found') && openDataVault && (
                  <Button variant="primary" size="sm" onClick={openDataVault}>Open Data Vault</Button>
                )}
              </div>
            </div>
          </div>
        )}

        {hoverData && !loading && !errorMsg && (
          <div className="absolute top-2 left-2 md:top-3 md:left-3 z-10 bg-raised/80 backdrop-blur-sm border border-border p-1.5 md:p-2 rounded-lg text-3xs md:text-xs font-num pointer-events-none shadow-card max-w-[95%] md:max-w-[80%] flex flex-wrap gap-y-1 md:gap-y-2">
            <div className="flex space-x-2 md:space-x-3 items-center flex-wrap gap-y-1 md:gap-y-2">
              <div className="flex space-x-1"><span className="text-muted">O</span><span className={hoverData.open > hoverData.close ? 'text-danger' : 'text-success'}>{formatNum(hoverData.open)}</span></div>
              <div className="flex space-x-1"><span className="text-muted">H</span><span className="text-text">{formatNum(hoverData.high)}</span></div>
              <div className="flex space-x-1"><span className="text-muted">L</span><span className="text-text">{formatNum(hoverData.low)}</span></div>
              <div className="flex space-x-1"><span className="text-muted">C</span><span className={hoverData.close >= hoverData.open ? 'text-success' : 'text-danger'}>{formatNum(hoverData.close)}</span></div>
              <div className="flex space-x-1 border-l border-border pl-2 md:pl-3 ml-1"><span className="text-muted">V</span><span className="text-text">{formatNum(hoverData.value)}</span></div>

              {Object.keys(botConfigs).map(botName => {
                  const config = botConfigs[botName];
                  const activeInds = Object.keys(config.indicators).filter(k => config.indicators[k]);
                  if (activeInds.length === 0 || !hoverData.time) return null;
                  const botDataAtTime = snappedSignalMap[hoverData.time]?.[botName]?.extra_data || {};

                  return activeInds.map(indKey => {
                      const val = botDataAtTime[indKey];
                      if (val === undefined) return null;
                      return (
                          <div key={`${botName}-${indKey}`} className="flex space-x-1 border-l border-border pl-2 md:pl-3 ml-1 items-center">
                              <span className="text-muted text-3xs md:text-2xs uppercase">{indKey}</span><span className="text-accent">{formatNum(val)}</span>
                          </div>
                      );
                  });
              })}
            </div>
          </div>
        )}

        {/* Signal-marker legend */}
        {!loading && !errorMsg && (
          <div className="absolute top-2 right-2 md:top-3 md:right-3 z-10 hidden sm:flex items-center gap-3 bg-raised/80 backdrop-blur-sm border border-border px-2.5 py-1.5 rounded-lg pointer-events-none">
            <span className="flex items-center gap-1 text-3xs font-bold uppercase tracking-wider text-muted" title="Buy signal"><span className="text-success text-2xs leading-none">▲</span> S-B</span>
            <span className="flex items-center gap-1 text-3xs font-bold uppercase tracking-wider text-muted" title="Sell signal"><span className="text-danger text-2xs leading-none">▼</span> S-S</span>
            <span className="flex items-center gap-1 text-3xs font-bold uppercase tracking-wider text-muted" title="Short signal"><span className="text-danger text-2xs leading-none">▼</span> S-SH</span>
            <span className="flex items-center gap-1 text-3xs font-bold uppercase tracking-wider text-muted" title="Cover signal"><span className="text-success text-2xs leading-none">▲</span> S-CV</span>
            <span className="flex items-center gap-1 text-3xs font-bold uppercase tracking-wider text-muted" title="Buy fill (open long)"><span className="w-1.5 h-1.5 rounded-full bg-info" /> T-BUY</span>
            <span className="flex items-center gap-1 text-3xs font-bold uppercase tracking-wider text-muted" title="Sell fill (close long)"><span className="w-1.5 h-1.5 rounded-full bg-purple" /> T-SELL</span>
            <span className="flex items-center gap-1 text-3xs font-bold uppercase tracking-wider text-muted" title="Short fill (open short)"><span className="w-1.5 h-1.5 rounded-full bg-info" /> T-SHORT</span>
            <span className="flex items-center gap-1 text-3xs font-bold uppercase tracking-wider text-muted" title="Cover fill (close short)"><span className="w-1.5 h-1.5 rounded-full bg-purple" /> T-COVER</span>
          </div>
        )}

        {hoverData && snappedTradeMap[hoverData.time] && snappedTradeMap[hoverData.time].length > 0 && (
          <div className="absolute top-12 left-2 md:top-14 md:left-3 z-20 flex flex-col space-y-2 pointer-events-none max-w-[calc(100vw-1rem)] md:max-w-none">
            {snappedTradeMap[hoverData.time].map((trade, idx) => {
                // Prices are in the pair's quote; the total and PnL are in the
                // cash currency (settle on a swap — the base coin on inverse)
                const quote = symbolParts(trade.symbol).quote;
                const ccy = rowCurrency(trade.position) || rowCurrency(trade);
                const inverse = rowKind(trade.position || trade) === 'inverse';
                const totalValue = inverse ? trade.amount : trade.price * trade.amount;
                const isOpening = isOpeningOrder(trade);
                const dir = trade.position?.side === 'short' ? -1 : 1;
                // Realized PnL: the booked, after-fee figure when the position
                // is closed; the gross price move only as a fallback
                const booked = trade.position && trade.position.status === 'closed' && trade.position.profit_abs != null && Number.isFinite(Number(trade.position.profit_abs));
                const grossAbs = trade.position ? positionPnl({ ...trade.position, amount: trade.amount }, trade.price) : 0;
                const grossPct = trade.position ? (dir * (trade.price - trade.position.entry_price) / trade.position.entry_price) * 100 : 0;
                const realAbs = booked ? Number(trade.position.profit_abs) : grossAbs;
                const realPct = booked && trade.position.profit_pct != null ? Number(trade.position.profit_pct) : grossPct;
                const isWin = trade.position ? realAbs >= 0 : true;
                const pnlPct = realPct.toFixed(2);
                const pnlAbs = fmtMoney(Math.abs(realAbs), ccy);
                const headline = isOpening ? (trade.side === 'sell' ? 'SHORT ENTRY' : 'ENTRY EXECUTION') : (trade.side === 'buy' ? 'COVER EXECUTION' : 'EXIT EXECUTION');

                return (
                    <div key={idx} className={`bg-raised/95 backdrop-blur-md border p-3 rounded-lg shadow-pop flex flex-col min-w-[240px] md:min-w-[260px] ${isOpening ? 'border-info' : 'border-purple'}`}>
                        <div className="flex justify-between items-center mb-2 pb-2 border-b border-border">
                            <span className={`text-2xs md:text-xs font-bold uppercase tracking-wider ${isOpening ? 'text-info' : 'text-purple'}`}>
                                {headline}
                            </span>
                            <span className="bg-overlay border border-border text-text text-3xs px-1.5 py-0.5 rounded uppercase font-bold">{trade.mode}</span>
                        </div>
                        <div className="grid grid-cols-2 gap-y-3 gap-x-4">
                            <div className="flex flex-col">
                                <span className="text-3xs md:text-3xs text-muted uppercase font-bold">Price</span>
                                <span className="text-2xs md:text-xs text-text font-num" title={fmtMoneyTitle(quote)}>{fmtMoney(trade.price, quote)}</span>
                            </div>
                            <div className="flex flex-col text-right">
                                <span className="text-3xs md:text-3xs text-muted uppercase font-bold">Size</span>
                                <span className="text-2xs md:text-xs text-text font-num">{formatCrypto(trade.amount)}</span>
                            </div>

                            <div className="flex flex-col">
                                <span className="text-3xs md:text-3xs text-muted uppercase font-bold">Total</span>
                                <span className="text-2xs md:text-xs text-text font-num" title={inverse ? 'inverse contract: notional in the base coin' : fmtMoneyTitle(ccy)}>{fmtMoney(totalValue, ccy)}</span>
                            </div>
                            <div className="flex flex-col text-right">
                                <span className="text-3xs md:text-3xs text-muted uppercase font-bold">Type</span>
                                <span className="text-2xs md:text-xs text-text uppercase">{trade.order_type || 'Market'}</span>
                            </div>

                            {!isOpening && trade.position && (
                                <div className="flex flex-col col-span-2 pt-2 border-t border-border">
                                    <span className="text-3xs md:text-3xs text-muted uppercase font-bold mb-1">PnL</span>
                                    <div className="grid grid-cols-2 gap-2 bg-inset p-2 rounded-lg border border-border">
                                        <div className="flex flex-col">
                                            <span className="text-3xs text-muted uppercase">Avg Entry</span>
                                            <span className="text-3xs md:text-2xs text-text font-num" title={fmtMoneyTitle(quote)}>{fmtMoney(trade.position.entry_price, quote)}</span>
                                        </div>
                                        <div className="flex flex-col text-right">
                                            <span className="text-3xs text-muted uppercase">{booked ? 'Realized (after fees)' : 'Realized (gross)'}</span>
                                            <span className={`text-3xs md:text-2xs font-num font-bold ${isWin ? 'text-success' : 'text-danger'}`}>
                                                {isWin ? '+' : '-'}{pnlAbs} ({pnlPct}%)
                                            </span>
                                        </div>
                                    </div>
                                </div>
                            )}

                            <div className="flex flex-col col-span-2 pt-2 border-t border-border">
                                <span className="text-3xs md:text-3xs text-muted uppercase font-bold">Source</span>
                                <span className="text-2xs md:text-xs text-accent truncate">{trade.bot_name}</span>
                            </div>
                        </div>
                    </div>
                )
            })}
          </div>
        )}

        <div ref={chartContainerRef} className="absolute inset-0 z-0" />
      </div>
    </div>
  );
}

export default memo(ChartEngine);