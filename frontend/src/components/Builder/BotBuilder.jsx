import React, { useState, useCallback, useRef, useEffect } from 'react';
import ReactFlow, { MiniMap, Controls, Background, useNodesState, useEdgesState, addEdge, ReactFlowProvider } from 'reactflow';
import 'reactflow/dist/style.css';
import { BotConfigNode, WhitelistNode, BacktestNode, ApiKeyNode, IndicatorNode, ConditionNode, LogicNode, StopLossNode, TakeProfitNode, ActionNode, PriceDataNode } from './CustomNodes';
import { apiClient } from '../../api/client';
import { loadIndicators } from './indicatorConfig';
import { DEFAULT_PAIR, parsePairs } from './pairs';
import { humanizeApiError } from '../../api/errors';
import { getToken } from '../../theme';
import Button from '../ui/Button';
import { toast } from '../ui/Toast';
import { confirmDialog } from '../ui/ConfirmDialog';
import { marketCaps } from '../../api/exchanges';
import { whitelistCurrency } from '../../utils/money';

const nodeTypes = {
  botConfig: BotConfigNode,
  whitelist: WhitelistNode,
  backtest: BacktestNode,
  apiKey: ApiKeyNode,
  indicator: IndicatorNode,
  condition: ConditionNode,
  logic: LogicNode,
  stopLoss: StopLossNode,
  takeProfit: TakeProfitNode,
  action: ActionNode,
  priceData: PriceDataNode,
};

const getId = () => `node_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;

// Parses trigger/close values to floats while leaving other string fields untouched
const parseSafeFloat = (val) => {
    if (val === "" || val === undefined || val === null) return "";
    const parsed = parseFloat(String(val).replace(',', '.'));
    return isNaN(parsed) ? "" : parsed;
};

/**
 * Reconstruct ReactFlow nodes + edges from bot settings when ui_layout is empty.
 * This handles bots created programmatically or imported without visual layout data.
 */
function rebuildLayoutFromSettings(settings, updateNodeData, deleteNode) {
    const nodes = [];
    const edges = [];
    // CSS var resolves at render time, so edges follow the active theme
    const edgeStyle = { stroke: 'var(--color-muted)', strokeWidth: 2 };
    const GAP = 50; // universal gap between nodes

    // ── Measured rendered widths and heights from CSS width + content ──
    // Config is w-[340px] fixed; the context blocks render wider than their
    // min-w because of content, so measure generously to avoid overlap
    const SIZE = {
        config:    { w: 340, h: 1100 }, // 12 fields incl. guards + live allocation; cooldown row is conditional
        whitelist: { w: 320, h: 200 },
        backtest:  { w: 320, h: 220 },
        apiKey:    { w: 320, h: 240 },
        indicator: { w: 270, h: 200 }, // base; grows with params
        priceData: { w: 240, h: 190 },
        condition: { w: 280, h: 200 },
        logic:     { w: 220, h: 90  },
        action:    { w: 340, h: 280 },
        actionExit:{ w: 340, h: 200 },
        tp:        { w: 300, h: 180 },
        sl:        { w: 300, h: 180 },
    };

    // ── AREA 1: Setup & Context ──
    // Config left, context blocks stacked right
    nodes.push({
        id: 'rebuilt_config', type: 'botConfig', position: { x: 50, y: 50 },
        data: {
            onChange: updateNodeData, onDelete: deleteNode, botName: '',
            timeframe: settings.timeframe || '1m',
            executionMode: settings.api_execution ? 'exchange' : 'paper',
            maxPositions: settings.max_positions ?? 1,
            maxPositionsScope: settings.max_positions_scope || 'per_pair',
            cooldownTrades: settings.cooldown_trades ?? 0,
            cooldownCandles: settings.cooldown_candles ?? 0,
            maxDrawdown: settings.max_drawdown ?? 0,
            drawdownAction: settings.drawdown_action || 'close_all',
            maxCapitalLoss: settings.max_capital_loss ?? 0,
            drawdownCooldownDays: settings.drawdown_cooldown_days ?? 7,
            liveAllocationPct: settings.live_allocation_pct ?? 100,
            maxOrderValue: settings.max_order_value ?? 0,
        }
    });

    const ctxX = 50 + SIZE.config.w + GAP + 20;
    let ctxY = 50;

    const pairs = settings.symbols?.join(', ') || settings.symbol || DEFAULT_PAIR;
    nodes.push({ id: 'rebuilt_whitelist', type: 'whitelist', position: { x: ctxX, y: ctxY },
        data: { onChange: updateNodeData, onDelete: deleteNode, pairs } });
    ctxY += SIZE.whitelist.h + GAP + 20;

    if (settings.backtest_on_start !== undefined) {
        nodes.push({ id: 'rebuilt_backtest', type: 'backtest', position: { x: ctxX, y: ctxY },
            data: { onChange: updateNodeData, onDelete: deleteNode,
                runOnStart: settings.backtest_on_start ?? true,
                capital: settings.backtest_capital ?? 1000,
                lookback: settings.backtest_lookback ?? 150 } });
        ctxY += SIZE.backtest.h + GAP + 20;
    }

    if (settings.api_key_name || settings.data_exchange) {
        nodes.push({ id: 'rebuilt_apikey', type: 'apiKey', position: { x: ctxX, y: ctxY },
            data: { onChange: updateNodeData, onDelete: deleteNode,
                apiKeyName: settings.api_key_name || null,
                dataExchange: settings.data_exchange || 'okx',
                marketType: settings.market_type || 'spot',
                leverage: settings.leverage ?? 1,
                marginMode: settings.margin_mode || 'isolated' } });
    }

    // ── AREA 2: Strategy logic ──
    const settingsNodes = settings.nodes || {};
    const ordered = [];
    const visited = new Set();
    function collectDeps(nid) {
        if (!nid || visited.has(nid) || !settingsNodes[nid]) return;
        visited.add(nid);
        const n = settingsNodes[nid];
        if (n.left && typeof n.left === 'string') collectDeps(n.left);
        if (n.right && typeof n.right === 'string') collectDeps(n.right);
        ordered.push(nid);
    }
    if (settings.entry_node) collectDeps(settings.entry_node);
    if (settings.exit_node) collectDeps(settings.exit_node);
    if (settings.short_node) collectDeps(settings.short_node);
    if (settings.cover_node) collectDeps(settings.cover_node);
    for (const nid of Object.keys(settingsNodes)) if (!visited.has(nid)) collectDeps(nid);

    // Column x-positions: each column starts after previous column's width + generous gap
    // Using 80px gaps to account for connection handles (20px each side) + visual breathing room
    const COL_GAP = 80;
    const C1_X = 50;                                     // indicators / price data
    const C2_X = C1_X + SIZE.indicator.w + COL_GAP;      // 400: conditions
    const C3_X = C2_X + SIZE.condition.w + COL_GAP;      // 760: logic gates
    const C4_X = C3_X + SIZE.logic.w + COL_GAP;          // 1060: actions
    const C5_X = C4_X + SIZE.action.w + COL_GAP;         // 1480: TP / SL

    const strategyY = 50 + SIZE.config.h + GAP;          // below the config column
    let c1Y = strategyY, c2Y = strategyY, c3Y = strategyY;

    // Compute indicator height based on param count + output line selector
    function indicatorHeight(n) {
        const paramCount = n.params ? Object.keys(n.params).length : 1;
        // output_idx > 0 means multi-line indicator with dropdown selector (~60px extra)
        const hasMultiLine = n.output_idx !== undefined && n.output_idx > 0;
        return 140 + paramCount * 45 + (hasMultiLine ? 70 : 0);
    }

    for (const nid of ordered) {
        const n = settingsNodes[nid];
        const cls = n.class;

        if (cls === 'indicator') {
            const h = indicatorHeight(n);
            nodes.push({ id: nid, type: 'indicator', position: { x: C1_X, y: c1Y },
                data: { onChange: updateNodeData, onDelete: deleteNode,
                    indicator: n.method || 'rsi', params: n.params || { length: 14 },
                    outputIdx: n.output_idx ?? 0 } });
            c1Y += h + GAP;
        } else if (cls === 'price_data') {
            nodes.push({ id: nid, type: 'priceData', position: { x: C1_X, y: c1Y },
                data: { onChange: updateNodeData, onDelete: deleteNode,
                    priceType: n.type || 'close', offset: n.offset ?? 0 } });
            c1Y += SIZE.priceData.h + GAP;
        } else if (cls === 'condition') {
            const rightVal = (n.right != null && !settingsNodes[n.right]) ? String(n.right) : '';
            nodes.push({ id: nid, type: 'condition', position: { x: C2_X, y: c2Y },
                data: { onChange: updateNodeData, onDelete: deleteNode,
                    operator: n.operator || '>', rightValue: rightVal } });
            if (n.left && settingsNodes[n.left])
                edges.push({ id: `e_${n.left}_${nid}_l`, source: n.left, target: nid, targetHandle: 'left', animated: true, style: edgeStyle });
            if (n.right && settingsNodes[n.right])
                edges.push({ id: `e_${n.right}_${nid}_r`, source: n.right, target: nid, targetHandle: 'right', animated: true, style: edgeStyle });
            c2Y += SIZE.condition.h + GAP + 10;
        } else if (cls === 'logic') {
            nodes.push({ id: nid, type: 'logic', position: { x: C3_X, y: c3Y },
                data: { onChange: updateNodeData, onDelete: deleteNode,
                    logicType: n.operator || 'and' } });
            if (n.left && settingsNodes[n.left])
                edges.push({ id: `e_${n.left}_${nid}_1`, source: n.left, target: nid, targetHandle: 'in1', animated: true, style: edgeStyle });
            if (n.right && settingsNodes[n.right])
                edges.push({ id: `e_${n.right}_${nid}_2`, source: n.right, target: nid, targetHandle: 'in2', animated: true, style: edgeStyle });
            c3Y += SIZE.logic.h + GAP;
        }
    }

    // ── Action nodes ──
    const entryTs = settings.trade_settings?.entry || {};
    const entryId = 'rebuilt_entry';
    nodes.push({ id: entryId, type: 'action', position: { x: C4_X, y: strategyY },
        data: { onChange: updateNodeData, onDelete: deleteNode, actionType: 'buy',
            orderType: entryTs.order_type || 'market', amountType: entryTs.amount_type || 'percentage',
            amountValue: entryTs.amount_value ?? 100, fee: entryTs.fee ?? 0.1,
            slippage: entryTs.slippage ?? 0.05 } });
    if (settings.entry_node && settingsNodes[settings.entry_node])
        edges.push({ id: `e_${settings.entry_node}_entry`, source: settings.entry_node, target: entryId, targetHandle: 'logic', animated: true, style: edgeStyle });

    // TP/SL column
    let tpslY = strategyY;
    (entryTs.take_profits || []).forEach((tp, i) => {
        const tpId = `rebuilt_tp_${i}`;
        nodes.push({ id: tpId, type: 'takeProfit', position: { x: C5_X, y: tpslY },
            data: { onChange: updateNodeData, onDelete: deleteNode,
                triggerType: tp.type || 'percentage', triggerValue: tp.value ?? '',
                closeType: tp.close_amount_type || 'percentage', closeValue: tp.close_amount_value ?? 100 } });
        edges.push({ id: `e_entry_${tpId}`, source: entryId, sourceHandle: 'tp', target: tpId, animated: true, style: edgeStyle });
        tpslY += SIZE.tp.h + GAP + 10;
    });
    (entryTs.stop_losses || []).forEach((sl, i) => {
        const slId = `rebuilt_sl_${i}`;
        nodes.push({ id: slId, type: 'stopLoss', position: { x: C5_X, y: tpslY },
            data: { onChange: updateNodeData, onDelete: deleteNode,
                triggerType: sl.type || 'percentage', triggerValue: sl.value ?? '',
                closeType: sl.close_amount_type || 'percentage', closeValue: sl.close_amount_value ?? 100 } });
        edges.push({ id: `e_entry_${slId}`, source: entryId, sourceHandle: 'sl', target: slId, animated: true, style: edgeStyle });
        tpslY += SIZE.sl.h + GAP + 10;
    });

    // Exit action below entry
    const exitTs = settings.trade_settings?.exit || {};
    const exitId = 'rebuilt_exit';
    nodes.push({ id: exitId, type: 'action', position: { x: C4_X, y: strategyY + SIZE.action.h + COL_GAP },
        data: { onChange: updateNodeData, onDelete: deleteNode, actionType: 'sell',
            orderType: exitTs.order_type || 'market', amountType: exitTs.amount_type || 'percentage',
            amountValue: exitTs.amount_value ?? 100, fee: exitTs.fee ?? 0.1,
            slippage: exitTs.slippage ?? 0.05 } });
    if (settings.exit_node && settingsNodes[settings.exit_node])
        edges.push({ id: `e_${settings.exit_node}_exit`, source: settings.exit_node, target: exitId, targetHandle: 'logic', animated: true, style: edgeStyle });

    // ── Short leg (phase 3) — only when the bot actually has short nodes, so
    // long-only bots rebuild exactly as before ──
    if (settings.short_node || settings.cover_node) {
        const shortTs = settings.trade_settings?.short || entryTs;
        const shortId = 'rebuilt_short';
        let shortY = strategyY + 2 * (SIZE.action.h + COL_GAP);
        nodes.push({ id: shortId, type: 'action', position: { x: C4_X, y: shortY },
            data: { onChange: updateNodeData, onDelete: deleteNode, actionType: 'short',
                orderType: shortTs.order_type || 'market', amountType: shortTs.amount_type || 'percentage',
                amountValue: shortTs.amount_value ?? 100, fee: shortTs.fee ?? 0.1,
                slippage: shortTs.slippage ?? 0.05 } });
        if (settings.short_node && settingsNodes[settings.short_node])
            edges.push({ id: `e_${settings.short_node}_short`, source: settings.short_node, target: shortId, targetHandle: 'logic', animated: true, style: edgeStyle });
        let stpslY = Math.max(tpslY, shortY);
        (shortTs.take_profits || []).forEach((tp, i) => {
            const tpId = `rebuilt_short_tp_${i}`;
            nodes.push({ id: tpId, type: 'takeProfit', position: { x: C5_X, y: stpslY },
                data: { onChange: updateNodeData, onDelete: deleteNode,
                    triggerType: tp.type || 'percentage', triggerValue: tp.value ?? '',
                    closeType: tp.close_amount_type || 'percentage', closeValue: tp.close_amount_value ?? 100 } });
            edges.push({ id: `e_short_${tpId}`, source: shortId, sourceHandle: 'tp', target: tpId, animated: true, style: edgeStyle });
            stpslY += SIZE.tp.h + GAP + 10;
        });
        (shortTs.stop_losses || []).forEach((sl, i) => {
            const slId = `rebuilt_short_sl_${i}`;
            nodes.push({ id: slId, type: 'stopLoss', position: { x: C5_X, y: stpslY },
                data: { onChange: updateNodeData, onDelete: deleteNode,
                    triggerType: sl.type || 'percentage', triggerValue: sl.value ?? '',
                    closeType: sl.close_amount_type || 'percentage', closeValue: sl.close_amount_value ?? 100 } });
            edges.push({ id: `e_short_${slId}`, source: shortId, sourceHandle: 'sl', target: slId, animated: true, style: edgeStyle });
            stpslY += SIZE.sl.h + GAP + 10;
        });
        const coverTs = settings.trade_settings?.cover || exitTs;
        const coverId = 'rebuilt_cover';
        shortY += SIZE.action.h + COL_GAP;
        nodes.push({ id: coverId, type: 'action', position: { x: C4_X, y: shortY },
            data: { onChange: updateNodeData, onDelete: deleteNode, actionType: 'cover',
                orderType: coverTs.order_type || 'market', amountType: coverTs.amount_type || 'percentage',
                amountValue: coverTs.amount_value ?? 100, fee: coverTs.fee ?? 0.1,
                slippage: coverTs.slippage ?? 0.05 } });
        if (settings.cover_node && settingsNodes[settings.cover_node])
            edges.push({ id: `e_${settings.cover_node}_cover`, source: settings.cover_node, target: coverId, targetHandle: 'logic', animated: true, style: edgeStyle });
    }

    return { nodes, edges };
}

// Stable fingerprint of what Save would persist: node identity, position and
// user-editable data (runtime keys pushed in by effects are ignored), plus
// edge topology. Used to decide whether closing would lose work.
const TRANSIENT_DATA_KEYS = new Set(['onChange', 'onDelete', 'availableKeys', 'knownSymbols', 'liveContext', 'supportedTimeframes']);
// Builder-pushed hint on action blocks (which market the routing key trades);
// stripped like the other transients — the key/settings remain the source.
TRANSIENT_DATA_KEYS.add('liveMarketType');
// Builder-pushed cash-currency picture of the whitelist (code, inverse pairs)
// for the capital/order-cap labels; derived from the market list, not saved.
TRANSIENT_DATA_KEYS.add('marketCtx');
// ReactFlow bookkeeping on the node object itself (selection, measured size,
// drag state). Never persisted and never part of the dirty snapshot: a bot
// reopened from ui_layout must compare equal until the user edits something.
const TRANSIENT_NODE_KEYS = ['selected', 'width', 'height', 'dragging', 'measured', 'positionAbsolute'];
const stripNodeTransients = (n) => {
    const safe = { ...n, data: { ...(n.data || {}) } };
    for (const k of TRANSIENT_NODE_KEYS) delete safe[k];
    for (const k of TRANSIENT_DATA_KEYS) delete safe.data[k];
    return safe;
};
const graphSnapshot = (nodes, edges) => JSON.stringify({
    nodes: nodes.map(n => ({
        id: n.id, type: n.type,
        x: Math.round(n.position?.x || 0), y: Math.round(n.position?.y || 0),
        data: Object.fromEntries(Object.entries(n.data || {}).filter(([k]) => !TRANSIENT_DATA_KEYS.has(k))),
    })),
    edges: edges.map(e => [e.source, e.sourceHandle || null, e.target, e.targetHandle || null]),
});

const BotBuilderFlow = ({ closeBuilder, editingBot }) => {
  const reactFlowWrapper = useRef(null);
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [reactFlowInstance, setReactFlowInstance] = useState(null);
  const [availableKeys, setAvailableKeys] = useState([]);
  
  const [saving, setSaving] = useState(false);
  const [toolboxOpen, setToolboxOpen] = useState(false);
  const [supportedTimeframes, setSupportedTimeframes] = useState(null);
  // { exchange, symbols, known } for the routing block's exchange — whitelist validation
  const [knownSymbols, setKnownSymbols] = useState(null);

  const initRef = useRef(false);

  const updateNodeData = useCallback((id, field, value) => {
    let safeValue = value;
    // Only coerce numeric fields; leave string fields as-is
    if (field === 'triggerValue' || field === 'closeValue') {
        safeValue = parseSafeFloat(value);
    }

    setNodes((nds) => nds.map((node) => {
        if (node.id === id) { 
            return { ...node, data: { ...node.data, [field]: safeValue } }; 
        }
        return node;
    }));
  }, [setNodes]);

  const deleteNode = useCallback((id) => {
      setNodes((nds) => nds.filter((n) => n.id !== id));
      setEdges((eds) => eds.filter((e) => e.source !== id && e.target !== id));
  }, [setNodes, setEdges]);

  // Prefetch the indicator palette so nodes render fully on first paint.
  // Errors surface inside IndicatorNode; nothing to do here.
  useEffect(() => { loadIndicators().catch(() => {}); }, []);

  useEffect(() => {
    if (initRef.current) return;

    apiClient.get('/api/keys').then(res => {
        const keys = res.data;
        setAvailableKeys(keys);
        setNodes(nds => nds.map(n => {
            if (n.type === 'apiKey') return { ...n, data: { ...n.data, availableKeys: keys } };
            return n;
        }));
    }).catch(() => {});

    const hasLayout = editingBot && editingBot.settings.ui_layout && editingBot.settings.ui_layout.nodes && editingBot.settings.ui_layout.nodes.length > 0;
    if (hasLayout) {
        const restoredNodes = editingBot.settings.ui_layout.nodes.map(n => {
            const node = stripNodeTransients(n);
            node.data.onChange = updateNodeData;
            node.data.onDelete = deleteNode;
            // The persisted layout may carry a stale name (duplicate/import
            // rename): the bot record is the truth.
            if (node.type === 'botConfig') node.data.botName = editingBot.name;
            // Same correction ApiKeyNode would apply on mount — do it here so
            // the restored graph is already settled when the baseline is taken.
            if (node.type === 'apiKey' && !node.data.apiKeyName && node.data.marketType === 'swap'
                && !marketCaps(node.data.dataExchange || 'okx', 'swap')) node.data.marketType = 'spot';
            return node;
        });
        setNodes(restoredNodes);
        setEdges(editingBot.settings.ui_layout.edges || []);
        initRef.current = true;
    } else if (editingBot && editingBot.settings && Object.keys(editingBot.settings.nodes || {}).length > 0) {
        // Reconstruct visual layout from settings (imported/programmatic bot with no ui_layout)
        const rebuilt = rebuildLayoutFromSettings(editingBot.settings, updateNodeData, deleteNode);
        // Set the bot name on the config node
        const cfgNode = rebuilt.nodes.find(n => n.type === 'botConfig');
        if (cfgNode) cfgNode.data.botName = editingBot.name;
        setNodes(rebuilt.nodes);
        setEdges(rebuilt.edges);
        initRef.current = true;
    } else {
        setNodes([
            { id: getId(), type: 'botConfig', position: { x: 50, y: 50 }, data: { onChange: updateNodeData, onDelete: deleteNode, botName: editingBot ? editingBot.name : 'Apex Strategy Alpha', timeframe: editingBot?.settings?.timeframe || '1m', executionMode: 'paper', maxPositions: 1, maxPositionsScope: 'per_pair', cooldownTrades: 0, cooldownCandles: 0 } },
            { id: getId(), type: 'whitelist', position: { x: 470, y: 50 }, data: { onChange: updateNodeData, onDelete: deleteNode, pairs: editingBot?.settings?.symbols?.join(', ') || editingBot?.settings?.symbol || DEFAULT_PAIR } },
            { id: getId(), type: 'backtest', position: { x: 470, y: 320 }, data: { onChange: updateNodeData, onDelete: deleteNode, runOnStart: true, capital: 1000, lookback: 150 } },
            { id: getId(), type: 'apiKey', position: { x: 470, y: 610 }, data: { onChange: updateNodeData, onDelete: deleteNode, apiKeyName: null, dataExchange: 'okx', marketType: 'spot', leverage: 1, marginMode: 'isolated' } }
        ]);
        initRef.current = true;
    }
  }, [editingBot, updateNodeData, deleteNode, setNodes, setEdges]);

  useEffect(() => {
      if (!initRef.current || availableKeys.length === 0) return;
      setNodes(nds => nds.map(n => {
          if (n.type === 'apiKey') {
              return { ...n, data: { ...n.data, availableKeys } };
          }
          return n;
      }));
  }, [availableKeys, setNodes]);

  // Derive active exchange from nodes and fetch supported timeframes
  const activeApiKeyNode = nodes.find(n => n.type === 'apiKey');
  const activeApiKeyName = activeApiKeyNode?.data.apiKeyName;
  const activeDataExchange = activeApiKeyNode?.data.dataExchange;
  // Market type: the linked key decides (a key is bound to one market),
  // otherwise the routing block's own choice. Swap symbols are a separate
  // market list (BASE/QUOTE:SETTLE).
  const activeKeyMarketType = activeApiKeyName ? (availableKeys?.find(k => k.name === activeApiKeyName)?.market_type || null) : null;
  const activeMarketType = activeKeyMarketType || activeApiKeyNode?.data.marketType || 'spot';
  useEffect(() => {
      if (!initRef.current) return;
      let exchange = activeDataExchange || 'okx';
      if (activeApiKeyName) {
          const keyRecord = availableKeys?.find(k => k.name === activeApiKeyName);
          if (keyRecord) exchange = keyRecord.exchange;
      }
      apiClient.get(`/api/data/timeframes/${exchange}`).then(res => {
          setSupportedTimeframes(res.data.timeframes);
      }).catch(() => setSupportedTimeframes(null));
      let cancelled = false;
      apiClient.get(`/api/data/symbols/${exchange}`, { params: { market_type: activeMarketType } }).then(res => {
          // `markets` (per-symbol kind/settle/quote) is additive — older backends only send `symbols`
          if (!cancelled) setKnownSymbols({ exchange, marketType: activeMarketType, symbols: res.data.symbols || [], known: !!res.data.known, markets: res.data.markets || {} });
      }).catch(() => { if (!cancelled) setKnownSymbols({ exchange, marketType: activeMarketType, symbols: [], known: false, markets: {} }); });
      return () => { cancelled = true; };
  }, [activeApiKeyName, activeDataExchange, activeMarketType, availableKeys]);

  // Pass supported timeframes to config node
  useEffect(() => {
      if (!initRef.current || supportedTimeframes === null) return;
      setNodes(nds => nds.map(n => {
          if (n.type === 'botConfig') return { ...n, data: { ...n.data, supportedTimeframes } };
          return n;
      }));
  }, [supportedTimeframes, setNodes]);

  // Pass the exchange's market list to the whitelist block
  useEffect(() => {
      if (!initRef.current || knownSymbols === null) return;
      setNodes(nds => nds.map(n => {
          if (n.type === 'whitelist') return { ...n, data: { ...n.data, knownSymbols } };
          return n;
      }));
  }, [knownSymbols, setNodes]);

  // Go-live context for the config block: which key (if any) orders would go
  // through and the modelled entry fee. Derived from primitives so the effect
  // only fires when one of them actually changes.
  const activeKeyRecord = activeApiKeyName ? availableKeys?.find(k => k.name === activeApiKeyName) : null;
  const liveKeyName = activeKeyRecord?.name || null;
  const liveKeyExchange = activeKeyRecord?.exchange || null;
  const liveKeySandbox = !!activeKeyRecord?.is_sandbox;
  const entryActionNode = nodes.find(n => n.type === 'action' && n.data.actionType === 'buy');
  const entryFee = entryActionNode ? (entryActionNode.data.fee === '' || entryActionNode.data.fee === undefined ? 0.1 : Number(entryActionNode.data.fee)) : null;
  const liveLeverage = activeMarketType === 'swap' ? (Number(activeApiKeyNode?.data.leverage) || 1) : 1;
  const liveMarginMode = activeApiKeyNode?.data.marginMode || 'isolated';
  // Short leg presence for the go-live checklist: a SHORT block, and whether
  // it has any way out (COVER block or a stop-loss on its SL port).
  const shortActionNode = nodes.find(n => n.type === 'action' && n.data.actionType === 'short');
  const hasShort = !!shortActionNode;
  const hasCover = nodes.some(n => n.type === 'action' && n.data.actionType === 'cover');
  const hasShortStop = !!shortActionNode && edges.some(e => e.source === shortActionNode.id && e.sourceHandle === 'sl');
  useEffect(() => {
      if (!initRef.current) return;
      const liveContext = { keyName: liveKeyName, exchange: liveKeyExchange, isSandbox: liveKeySandbox, entryFee,
                            marketType: activeMarketType, leverage: liveLeverage, marginMode: liveMarginMode,
                            hasShort, hasCover, hasShortStop };
      setNodes(nds => nds.map(n => {
          if (n.type === 'action') {
              // Action blocks only need the market type (SHORT/COVER are swap-only)
              if (n.data.liveMarketType === activeMarketType) return n;
              return { ...n, data: { ...n.data, liveMarketType: activeMarketType } };
          }
          if (n.type !== 'botConfig') return n;
          const cur = n.data.liveContext;
          if (cur && cur.keyName === liveContext.keyName && cur.exchange === liveContext.exchange && cur.isSandbox === liveContext.isSandbox && cur.entryFee === liveContext.entryFee
              && cur.marketType === liveContext.marketType && cur.leverage === liveContext.leverage && cur.marginMode === liveContext.marginMode
              && cur.hasShort === hasShort && cur.hasCover === hasCover && cur.hasShortStop === hasShortStop) return n;
          return { ...n, data: { ...n.data, liveContext } };
      }));
  }, [liveKeyName, liveKeyExchange, liveKeySandbox, entryFee, activeMarketType, liveLeverage, liveMarginMode, hasShort, hasCover, hasShortStop, setNodes]);

  // Cash currency of the whitelist (from the exchange's market list, else the
  // symbol form) for the capital / order-cap labels and the inverse note.
  // Serialised so the effect only fires when the picture actually changes.
  const whitelistPairsText = nodes.find(n => n.type === 'whitelist')?.data.pairs;
  const marketCtxKey = JSON.stringify(whitelistCurrency(knownSymbols?.markets, parsePairs(whitelistPairsText !== undefined ? whitelistPairsText : DEFAULT_PAIR)));
  useEffect(() => {
      if (!initRef.current) return;
      const marketCtx = JSON.parse(marketCtxKey);
      setNodes(nds => nds.map(n => {
          if (n.type !== 'botConfig' && n.type !== 'backtest' && n.type !== 'apiKey' && n.type !== 'action') return n;
          if (JSON.stringify(n.data.marketCtx) === marketCtxKey) return n;
          return { ...n, data: { ...n.data, marketCtx } };
      }));
  }, [marketCtxKey, setNodes]);

  // Esc asks to close (same dirty-check as the buttons); a browser reload with
  // unsaved work gets the native "leave page?" prompt.
  const requestCloseRef = useRef(null);  // set below, once requestClose exists
  useEffect(() => {
      const onKey = (e) => {
          if (e.key !== 'Escape' || e.defaultPrevented) return;
          const t = e.target;
          if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
          if (document.querySelector('[data-apex-modal]')) return;
          requestCloseRef.current?.();
      };
      const onUnload = (e) => {
          if (baselineRef.current !== null && graphSnapshot(nodesRef.current, edgesRef.current) !== baselineRef.current) {
              e.preventDefault();
              e.returnValue = '';
          }
      };
      window.addEventListener('keydown', onKey);
      window.addEventListener('beforeunload', onUnload);
      return () => { window.removeEventListener('keydown', onKey); window.removeEventListener('beforeunload', onUnload); };
  }, []);
  const nodesRef = useRef(nodes); nodesRef.current = nodes;
  const edgesRef = useRef(edges); edgesRef.current = edges;

  const onConnect = useCallback((params) => setEdges((eds) => addEdge({ ...params, animated: true, style: { stroke: 'var(--color-muted)', strokeWidth: 2 } }, eds)), [setEdges]);
  const onDragOver = useCallback((event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; }, []);

  // Background dots + MiniMap node/mask colors are painted into SVG/canvas
  // attributes where CSS vars don't resolve — read the live tokens and
  // re-render when the theme flips.
  const [themeTick, setThemeTick] = useState(0);
  useEffect(() => {
      const onTheme = () => setThemeTick(t => t + 1);
      window.addEventListener('apex-theme-changed', onTheme);
      return () => window.removeEventListener('apex-theme-changed', onTheme);
  }, []);
  const canvasColors = React.useMemo(() => ({
      dots: getToken('border'),
      node: getToken('muted'),
      mask: getToken('bg'),
  }), [themeTick]); // eslint-disable-line react-hooks/exhaustive-deps -- themeTick invalidates the getToken reads

  const getDefaultData = useCallback((type) => {
      const defaultData = { onChange: updateNodeData, onDelete: deleteNode };
      if (type === 'apiKey') { defaultData.availableKeys = availableKeys; defaultData.apiKeyName = null; defaultData.dataExchange = 'okx'; defaultData.marketType = 'spot'; defaultData.leverage = 1; defaultData.marginMode = 'isolated'; }
      if (type === 'indicator') { defaultData.indicator = 'rsi'; defaultData.params = {length: 14}; defaultData.outputIdx = 0; }
      if (type === 'priceData') { defaultData.priceType = 'close'; defaultData.offset = 0; }
      if (type === 'condition') { defaultData.operator = '>'; defaultData.rightValue = ''; }
      if (type === 'logic') defaultData.logicType = 'and';
      if (type === 'botConfig') { defaultData.botName = 'My Bot'; defaultData.timeframe = '1m'; defaultData.executionMode = 'paper'; defaultData.maxPositions = 1; defaultData.maxPositionsScope = 'per_pair'; defaultData.cooldownTrades = 0; defaultData.cooldownCandles = 0; }
      if (type === 'whitelist') defaultData.pairs = DEFAULT_PAIR;
      if (type === 'backtest') { defaultData.runOnStart = true; defaultData.capital = 1000; defaultData.lookback = 150; }
      if (type === 'stopLoss' || type === 'takeProfit') {
          defaultData.triggerType = 'percentage'; defaultData.triggerValue = '';
          defaultData.closeType = 'percentage'; defaultData.closeValue = 100;
      }
      if (type === 'action') {
          defaultData.actionType = 'buy';
          defaultData.orderType = 'market';
          defaultData.amountType = 'percentage';
          defaultData.amountValue = 100;
          defaultData.slippage = 0.05;
          defaultData.fee = 0.1;
      }
      return defaultData;
  }, [updateNodeData, deleteNode, availableKeys]);

  const onDrop = useCallback(
    (event) => {
      event.preventDefault();
      const type = event.dataTransfer.getData('application/reactflow');
      if (typeof type === 'undefined' || !type) return;
      if (!reactFlowInstance) return;

      const position = reactFlowInstance.screenToFlowPosition({ x: event.clientX, y: event.clientY });
      const newNode = { id: getId(), type, position, data: getDefaultData(type) };
      setNodes((nds) => [...nds, newNode]);
    },
    [reactFlowInstance, getDefaultData, setNodes]
  );

  const onDragStart = (event, nodeType) => {
    event.dataTransfer.setData('application/reactflow', nodeType);
    event.dataTransfer.effectAllowed = 'move';
  };

  // Click-to-add: places the node in the center of the current viewport
  // (works on desktop and mobile; dragging still works on desktop).
  const handleAddNode = (type) => {
      let position = { x: 50, y: 150 };
      if (reactFlowInstance) {
          const rect = reactFlowWrapper.current?.getBoundingClientRect();
          const cx = rect ? rect.left + rect.width / 2 : window.innerWidth / 2;
          const cy = rect ? rect.top + rect.height / 2 : window.innerHeight / 2;
          position = reactFlowInstance.screenToFlowPosition({ x: cx, y: cy });
      }
      const newNode = { id: getId(), type, position, data: getDefaultData(type) };
      setNodes((nds) => [...nds, newNode]);
      if (window.innerWidth < 768) setToolboxOpen(false);
  };

  // Validation issues stay on the canvas until fixed or dismissed; the toast
  // is just the first ping. Node ids quoted as "Node 'x'" get a danger ring.
  const [issues, setIssues] = useState(null);
  const showError = (msg, warnings = []) => {
      const text = msg || 'Compile error.';
      const errors = text.split('\n').map(t => t.trim()).filter(Boolean);
      const nodeIds = new Set();
      for (const line of [...errors, ...warnings]) {
          const m = /Node '([^']+)'/.exec(line);
          if (m) nodeIds.add(m[1]);
      }
      setIssues({ errors, warnings, nodeIds });
      toast.error(text);
  };
  const flaggedNodes = issues?.nodeIds?.size
      ? nodes.map(n => (issues.nodeIds.has(n.id) ? { ...n, className: `${n.className || ''} apex-node-invalid`.trim() } : n))
      : nodes;
  const focusNode = (id) => {
      const n = nodes.find(x => x.id === id);
      if (n && reactFlowInstance) reactFlowInstance.setCenter(n.position.x + 150, n.position.y + 80, { zoom: 1, duration: 300 });
  };

  // Dirty-check: the baseline is the graph as first laid out (or last saved).
  const baselineRef = useRef(null);
  useEffect(() => {
      if (baselineRef.current === null && initRef.current && nodes.length > 0) {
          baselineRef.current = graphSnapshot(nodes, edges);
      }
  }, [nodes, edges]);
  const isDirty = () => baselineRef.current !== null && graphSnapshot(nodes, edges) !== baselineRef.current;

  // Re-entrancy guard: a second Close/Esc while the "Unsaved changes" dialog is
  // open must not fire another confirmDialog (which would resolve the first
  // one as "cancelled" and leave the user with a dialog that does nothing).
  const closingRef = useRef(false);
  const requestClose = async () => {
      if (saving || closingRef.current) return;
      if (!isDirty()) return closeBuilder();
      closingRef.current = true;
      try {
          const choice = await confirmDialog({
              type: 'warning',
              title: 'Unsaved changes',
              message: editingBot
                  ? `"${editingBot.name}" has changes that are not saved. Save & close keeps them; Discard throws them away and the bot stays as it was.`
                  : 'This strategy has not been saved yet. Save & close keeps it; Discard throws the whole draft away.',
              confirmText: 'Save & close',
              secondaryText: 'Discard',
              cancelText: 'Keep editing',
          });
          if (choice === true) await handleSaveAndCompile();
          else if (choice === 'secondary') closeBuilder();
      } finally {
          closingRef.current = false;
      }
  };
  requestCloseRef.current = requestClose;

  const handleSaveAndCompile = async () => {
    setSaving(true);
    try {
        const configNode = nodes.find(n => n.type === 'botConfig');
        const whitelistNode = nodes.find(n => n.type === 'whitelist');
        const backtestNode = nodes.find(n => n.type === 'backtest');
        const apiKeyNode = nodes.find(n => n.type === 'apiKey');
        const apiKeyName = apiKeyNode?.data.apiKeyName || null;
        const apiKeyRecord = apiKeyName ? availableKeys?.find(k => k.name === apiKeyName) : null;
        const dataExchange = apiKeyRecord?.exchange || apiKeyNode?.data.dataExchange || 'okx';
        // The key's market wins; leverage/margin mode only matter on swaps
        // (the backend strips the defaults from the config fingerprint)
        const marketType = apiKeyRecord?.market_type || apiKeyNode?.data.marketType || 'spot';
        const leverage = marketType === 'swap' ? (parseInt(apiKeyNode?.data.leverage) || 1) : 1;
        const marginMode = marketType === 'swap' ? (apiKeyNode?.data.marginMode || 'isolated') : 'isolated';

        if (!configNode) return showError("Missing 'Main Configuration' block.");
        if (!whitelistNode) return showError("Missing 'Asset Whitelist' block.");

        const symbolsList = parsePairs(whitelistNode.data.pairs);
        if (symbolsList.length === 0) return showError("Whitelist must contain at least one pair.");
        if (marketType === 'swap') {
            const spotPairs = symbolsList.filter(sym => !sym.includes(':'));
            if (spotPairs.length) return showError(`Perpetual swaps use the BASE/QUOTE:SETTLE form (e.g. BTC/USDT:USDT for a linear, BTC/USD:BTC for an inverse contract); fix ${spotPairs.join(', ')} in the whitelist.`);
        } else {
            const swapPairs = symbolsList.filter(sym => sym.includes(':'));
            if (swapPairs.length) return showError(`${swapPairs.join(', ')} are perpetual swaps; a spot bot needs BASE/QUOTE pairs (or pick a perps market in the Exchange Routing block).`);
        }
        if (knownSymbols?.known && knownSymbols.exchange === dataExchange && (knownSymbols.marketType || 'spot') === marketType) {
            const listed = new Set(knownSymbols.symbols);
            const unknown = symbolsList.filter(sym => !listed.has(sym));
            if (unknown.length) return showError(`Not listed on ${dataExchange.toUpperCase()}: ${unknown.join(', ')}. Fix the whitelist before saving.`);
        }
        const wantsExchange = configNode.data.executionMode === 'exchange';
        if (wantsExchange && !apiKeyRecord) return showError("Exchange orders need an API key — select one in the Exchange Routing block or switch Execution back to forward test.");

        // Strip non-serialisable function refs, runtime keys and ReactFlow
        // bookkeeping (selected/width/height/…) before persisting
        const uiNodesSafe = nodes.map(stripNodeTransients);

        const payload = {
            name: configNode.data.botName || "Untitled Algorithm",
            is_active: false,
            is_sandbox: true,
            settings: {
                symbol: symbolsList[0], 
                symbols: symbolsList,   
                timeframe: configNode.data.timeframe || "1m",
                max_positions: configNode.data.maxPositions || 1,
                max_positions_scope: configNode.data.maxPositionsScope || 'per_pair',
                cooldown_trades: configNode.data.cooldownTrades || 0,
                cooldown_candles: configNode.data.cooldownCandles || 0,
                max_drawdown: configNode.data.maxDrawdown || 0,
                drawdown_action: configNode.data.drawdownAction || 'close_all',
                max_capital_loss: configNode.data.maxCapitalLoss || 0,
                drawdown_cooldown_days: configNode.data.drawdownCooldownDays === "" || configNode.data.drawdownCooldownDays === undefined ? 7 : configNode.data.drawdownCooldownDays,
                live_allocation_pct: configNode.data.liveAllocationPct === "" || configNode.data.liveAllocationPct === undefined ? 100 : configNode.data.liveAllocationPct,
                max_order_value: configNode.data.maxOrderValue || 0,
                api_execution: wantsExchange,
                backtest_on_start: backtestNode ? backtestNode.data.runOnStart : false,
                backtest_capital: backtestNode ? (backtestNode.data.capital || 1000) : 1000,
                backtest_lookback: backtestNode ? (backtestNode.data.lookback || 150) : 150,
                api_key_name: apiKeyName,
                data_exchange: dataExchange,
                market_type: marketType,
                leverage,
                margin_mode: marginMode,
                trade_settings: {}, 
                nodes: {},
                ui_layout: {
                    nodes: uiNodesSafe, 
                    edges: edges
                }
            }
        };

        nodes.forEach(n => {
            if (n.type === 'indicator') {
                payload.settings.nodes[n.id] = { 
                    class: "indicator", 
                    method: n.data.indicator || 'rsi', 
                    params: n.data.params || { length: 14 }, 
                    output_idx: n.data.outputIdx || 0 
                };
            }
        });

        const entryNode = nodes.find(n => n.type === 'action' && n.data.actionType === 'buy');
        const exitNode = nodes.find(n => n.type === 'action' && n.data.actionType === 'sell');

        if (entryNode) {
             const tpEdges = edges.filter(e => e.source === entryNode.id && e.sourceHandle === 'tp');
             const slEdges = edges.filter(e => e.source === entryNode.id && e.sourceHandle === 'sl');

             payload.settings.trade_settings.entry = {
                 order_type: entryNode.data.orderType || 'market',
                 amount_type: entryNode.data.amountType || 'percentage',
                 amount_value: entryNode.data.amountValue !== undefined && entryNode.data.amountValue !== "" ? entryNode.data.amountValue : 100,
                 fee: entryNode.data.fee !== undefined && entryNode.data.fee !== "" ? entryNode.data.fee : 0.1,
                 slippage: entryNode.data.slippage !== undefined && entryNode.data.slippage !== "" ? entryNode.data.slippage : 0.05,
                 take_profits: tpEdges.map(e => {
                     const n = nodes.find(nd => nd.id === e.target);
                     if(!n) return null;
                     return { type: n.data.triggerType, value: parseFloat(n.data.triggerValue) || 0, close_amount_type: n.data.closeType, close_amount_value: parseFloat(n.data.closeValue) || 100 };
                 }).filter(Boolean),
                 stop_losses: slEdges.map(e => {
                     const n = nodes.find(nd => nd.id === e.target);
                     if(!n) return null;
                     return { type: n.data.triggerType, value: parseFloat(n.data.triggerValue) || 0, close_amount_type: n.data.closeType, close_amount_value: parseFloat(n.data.closeValue) || 100 };
                 }).filter(Boolean)
             };
        }
        
        const closingLeg = (node) => ({
            order_type: node.data.orderType || 'market',
            amount_type: node.data.amountType || 'percentage',
            amount_value: node.data.amountValue !== undefined && node.data.amountValue !== "" ? node.data.amountValue : 100,
            fee: node.data.fee !== undefined && node.data.fee !== "" ? node.data.fee : 0.1,
            slippage: node.data.slippage !== undefined && node.data.slippage !== "" ? node.data.slippage : 0.05
        });
        if (exitNode) payload.settings.trade_settings.exit = closingLeg(exitNode);

        // Short leg (phase 3): serialized only when a short/cover action exists
        // on the canvas — a long-only bot keeps its exact settings shape (and
        // config fingerprint). Short TP/SL hang off the short node's ports.
        const shortNode = nodes.find(n => n.type === 'action' && n.data.actionType === 'short');
        const coverNode = nodes.find(n => n.type === 'action' && n.data.actionType === 'cover');
        if (shortNode) {
            const ruleFromEdge = (e) => {
                const n = nodes.find(nd => nd.id === e.target);
                if (!n) return null;
                return { type: n.data.triggerType, value: parseFloat(n.data.triggerValue) || 0, close_amount_type: n.data.closeType, close_amount_value: parseFloat(n.data.closeValue) || 100 };
            };
            payload.settings.trade_settings.short = {
                ...closingLeg(shortNode),
                take_profits: edges.filter(e => e.source === shortNode.id && e.sourceHandle === 'tp').map(ruleFromEdge).filter(Boolean),
                stop_losses: edges.filter(e => e.source === shortNode.id && e.sourceHandle === 'sl').map(ruleFromEdge).filter(Boolean),
            };
        }
        if (coverNode) payload.settings.trade_settings.cover = closingLeg(coverNode);

        const traverse = (targetId) => {
            const incomingEdge = edges.find(e => e.target === targetId && (e.targetHandle === 'logic' || e.targetHandle === 'left' || e.targetHandle === 'in1' || !e.targetHandle));
            if (!incomingEdge) return null;
            
            const sourceNode = nodes.find(n => n.id === incomingEdge.source);
            if (!sourceNode) return null;

            if (sourceNode.type === 'indicator') {
                const params = sourceNode.data.params || { length: sourceNode.data.period || 14 };
                payload.settings.nodes[sourceNode.id] = { class: "indicator", method: sourceNode.data.indicator || 'rsi', params: params, output_idx: sourceNode.data.outputIdx || 0 };
                return sourceNode.id;
            }
            if (sourceNode.type === 'priceData') {
                payload.settings.nodes[sourceNode.id] = { class: "price_data", type: sourceNode.data.priceType || "close", offset: sourceNode.data.offset || 0 };
                return sourceNode.id;
            }
            if (sourceNode.type === 'condition') {
                const leftEdge = edges.find(e => e.target === sourceNode.id && (e.targetHandle === 'left' || !e.targetHandle));
                const rightEdge = edges.find(e => e.target === sourceNode.id && e.targetHandle === 'right');
                
                payload.settings.nodes[sourceNode.id] = { 
                    class: "condition", 
                    left: leftEdge ? traverseByEdge(leftEdge) : null, 
                    operator: sourceNode.data.operator || ">", 
                    right: rightEdge ? traverseByEdge(rightEdge) : (sourceNode.data.rightValue !== undefined && sourceNode.data.rightValue !== "" ? sourceNode.data.rightValue : null) 
                };
                return sourceNode.id;
            }
            if (sourceNode.type === 'logic') {
                const incomingEdges = edges.filter(e => e.target === sourceNode.id);
                payload.settings.nodes[sourceNode.id] = { 
                    class: "logic", 
                    operator: sourceNode.data.logicType || "and", 
                    left: incomingEdges.length > 0 ? traverseByEdge(incomingEdges[0]) : null, 
                    right: sourceNode.data.logicType === "not" ? null : (incomingEdges.length > 1 ? traverseByEdge(incomingEdges[1]) : null) 
                };
                return sourceNode.id;
            }
            return null;
        };

        const traverseByEdge = (edge) => {
             const sourceNode = nodes.find(n => n.id === edge.source);
             if(!sourceNode) return null;
             if (sourceNode.type === 'indicator') { 
                 const params = sourceNode.data.params || { length: sourceNode.data.period || 14 };
                 payload.settings.nodes[sourceNode.id] = { class: "indicator", method: sourceNode.data.indicator || 'rsi', params: params, output_idx: sourceNode.data.outputIdx || 0 }; 
                 return sourceNode.id; 
             }
             if (sourceNode.type === 'priceData') { payload.settings.nodes[sourceNode.id] = { class: "price_data", type: sourceNode.data.priceType || "close", offset: sourceNode.data.offset || 0 }; return sourceNode.id; }
             if (sourceNode.type === 'condition') { 
                 const leftEdge = edges.find(e => e.target === sourceNode.id && (e.targetHandle === 'left' || !e.targetHandle));
                 const rightEdge = edges.find(e => e.target === sourceNode.id && e.targetHandle === 'right');
                 payload.settings.nodes[sourceNode.id] = { 
                     class: "condition", 
                     left: leftEdge ? traverseByEdge(leftEdge) : null, 
                     operator: sourceNode.data.operator || ">", 
                     right: rightEdge ? traverseByEdge(rightEdge) : (sourceNode.data.rightValue !== undefined && sourceNode.data.rightValue !== "" ? sourceNode.data.rightValue : null) 
                 }; 
                 return sourceNode.id; 
             }
             if (sourceNode.type === 'logic') {
                 const incomingEdges = edges.filter(e => e.target === sourceNode.id);
                 payload.settings.nodes[sourceNode.id] = { 
                     class: "logic", 
                     operator: sourceNode.data.logicType || "and", 
                     left: incomingEdges.length > 0 ? traverseByEdge(incomingEdges[0]) : null, 
                     right: sourceNode.data.logicType === "not" ? null : (incomingEdges.length > 1 ? traverseByEdge(incomingEdges[1]) : null) 
                 };
                 return sourceNode.id;
             }
             return null;
        };

        if (entryNode) payload.settings.entry_node = traverse(entryNode.id);
        if (exitNode) payload.settings.exit_node = traverse(exitNode.id);
        if (shortNode) payload.settings.short_node = traverse(shortNode.id);
        if (coverNode) payload.settings.cover_node = traverse(coverNode.id);
        // PUT merges settings, so a short leg removed from the canvas must be
        // cleared explicitly (null = engine default, fingerprint-neutral)
        if (editingBot?.settings) {
            if (!shortNode && editingBot.settings.short_node) payload.settings.short_node = null;
            if (!coverNode && editingBot.settings.cover_node) payload.settings.cover_node = null;
        }

        const hasLogic = !!payload.settings.entry_node || !!payload.settings.short_node;

        const res = editingBot
            ? await apiClient.put(`/api/bots/${editingBot.id}`, { name: payload.name, settings: payload.settings })
            : await apiClient.post('/api/bots/', payload);
        const savedWarnings = Array.isArray(res?.data?.validation_warnings) ? res.data.validation_warnings : [];
        setIssues(null);
        if (savedWarnings.length) toast.warn(savedWarnings.join('\n'));

        if (hasLogic) {
            toast.success(editingBot ? 'Algorithm configuration updated.' : 'Algorithm successfully compiled & deployed.');
        } else {
            toast.warn('Draft saved without logic — the engine will ignore it until you connect an Entry (or Short) signal.');
        }
        baselineRef.current = graphSnapshot(nodes, edges);
        closeBuilder();

    } catch (err) {
        console.error(err);
        showError(humanizeApiError(err, 'Compile error.'));
    } finally {
        setSaving(false);
    }
  };

  const configNodeForName = nodes.find(n => n.type === 'botConfig');

  // Show a getting-started hint while the canvas only holds the setup nodes
  const SETUP_NODE_TYPES = ['botConfig', 'whitelist', 'backtest', 'apiKey'];
  const hasStrategyNodes = nodes.some(n => !SETUP_NODE_TYPES.includes(n.type));

  // Palette item styling per node class — colors are token utilities
  const paletteItem = (accentClasses) =>
      `p-3 bg-inset border rounded-md text-xs font-bold cursor-pointer md:cursor-grab transition-colors uppercase tracking-wider select-none ${accentClasses}`;

  const PALETTE = [
      { title: '1. Setup & Context', items: [
          { type: 'botConfig', label: 'Main Configuration', cls: 'border-purple/50 text-purple hover:bg-purple/10' },
          { type: 'whitelist', label: 'Asset Whitelist', cls: 'border-warn/50 text-warn hover:bg-warn/10' },
          { type: 'backtest', label: 'Backtest Engine', cls: 'border-accent/50 text-accent hover:bg-accent/10' },
          { type: 'apiKey', label: 'Exchange Routing', cls: 'border-info/50 text-info hover:bg-info/10' },
      ]},
      { title: '2. Market Logic', items: [
          { type: 'indicator', label: 'Technical Indicator', cls: 'border-border text-text hover:bg-overlay hover:border-border-strong' },
          { type: 'priceData', label: 'Price Data', cls: 'border-border text-text hover:bg-overlay hover:border-border-strong' },
          { type: 'condition', label: 'Data Condition', cls: 'border-border text-text hover:bg-overlay hover:border-border-strong' },
          { type: 'logic', label: 'Logic Gate (AND, OR, NOT)', cls: 'border-success/50 text-success hover:bg-success/10' },
      ]},
      { title: '3. Risk Management', items: [
          { type: 'takeProfit', label: 'Take Profit (Target)', cls: 'border-success/50 text-success hover:bg-success/10' },
          { type: 'stopLoss', label: 'Stop Loss (Risk)', cls: 'border-danger/50 text-danger hover:bg-danger/10' },
      ]},
      { title: '4. Execution', items: [
          { type: 'action', label: 'Entry / Exit Actions (Buy · Sell · Short · Cover)', cls: 'border-text/20 text-text hover:bg-text/10' },
      ]},
  ];

  return (
    <div className="flex w-full h-[100dvh] bg-bg absolute inset-0 z-[100] fade-in flex-col md:flex-row" role="dialog" aria-modal="true" aria-label={editingBot ? `Edit strategy ${editingBot.name}` : 'New strategy'}>

      {/* Mobile header */}
      <div className="md:hidden flex h-14 bg-raised/80 backdrop-blur-xl border-b border-border items-center justify-between px-4 shrink-0 z-50">
          <Button variant="secondary" size="sm" onClick={() => setToolboxOpen(true)}
              icon={<svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M4 6h16M4 12h16M4 18h16" /></svg>}>
              Toolbox
          </Button>
          <div className="flex gap-2 items-center">
              <Button variant="ghost" size="sm" onClick={requestClose} disabled={saving}>Close</Button>
              <Button variant="primary" size="sm" onClick={handleSaveAndCompile} loading={saving}>Save</Button>
          </div>
      </div>

      {/* Mobile overlay backdrop for toolbox */}
      {toolboxOpen && <div className="fixed inset-0 backdrop z-[105] md:hidden fade-in" onClick={() => setToolboxOpen(false)}></div>}

      {/* Left sidebar / toolbox — slides in from the left on mobile */}
      <div className={`fixed md:static inset-y-0 left-0 z-[110] w-72 bg-raised/95 backdrop-blur-xl border-r border-border flex flex-col shadow-pop transform transition-transform duration-300 ease-in-out ${toolboxOpen ? 'translate-x-0' : '-translate-x-full'} md:translate-x-0 h-[100dvh]`}>
        <div className="relative p-4 border-b border-border bg-bg/50 flex justify-between items-center md:block overflow-hidden">
          <div className="absolute -top-8 -left-8 w-32 h-32 rounded-full blur-[60px] bg-accent/5 pointer-events-none" />
          <div className="relative">
            <h2 className="text-text font-bold tracking-wider text-lg">APEX<span className="text-accent">ALGO</span></h2>
            <span className="text-2xs text-muted uppercase tracking-widest">{editingBot ? 'Editing Architecture' : 'Algorithm Builder'}</span>
          </div>
          <button onClick={() => setToolboxOpen(false)} className="md:hidden text-muted hover:text-text p-2 font-bold text-lg transition-colors" aria-label="Close toolbox">✕</button>
        </div>

        <div className="flex-1 overflow-y-auto p-5 space-y-6 custom-scrollbar pb-24 md:pb-5">
            {PALETTE.map(section => (
                <div key={section.title} className="space-y-3">
                    <span className="text-2xs font-bold text-muted uppercase tracking-wider block border-b border-border pb-1">{section.title}</span>
                    {section.items.map(item => (
                        <div key={item.type}
                            className={paletteItem(item.cls)}
                            onDragStart={(event) => onDragStart(event, item.type)}
                            onClick={() => handleAddNode(item.type)}
                            draggable>
                            {item.label}
                        </div>
                    ))}
                </div>
            ))}
        </div>

        <div className="hidden md:flex p-5 border-t border-border gap-3 bg-bg/50 shrink-0">
             <Button variant="secondary" fullWidth onClick={requestClose} disabled={saving}>Close</Button>
             <Button variant="primary" fullWidth onClick={handleSaveAndCompile} loading={saving}>{editingBot ? 'Update' : 'Save Bot'}</Button>
        </div>
      </div>

      {/* REACT FLOW CANVAS */}
      <div className="flex-1 w-full h-full relative" ref={reactFlowWrapper}>
        {/* Floating top bar — bot name bound to the Main Configuration node */}
        {configNodeForName && (
            <div className="hidden md:flex absolute top-4 right-4 z-10 items-center gap-2 bg-raised/90 backdrop-blur-xl border border-border rounded-lg px-3 py-2 shadow-card">
                <span className="text-3xs font-bold uppercase tracking-wider text-muted whitespace-nowrap">Algorithm</span>
                <input
                    type="text"
                    value={configNodeForName.data.botName ?? ''}
                    onChange={(e) => updateNodeData(configNodeForName.id, 'botName', e.target.value)}
                    placeholder="Untitled Algorithm"
                    className="w-52 bg-inset border border-border hover:border-border-strong focus:border-accent/70 rounded-md px-2.5 py-1.5 text-xs text-text placeholder-faint outline-none transition-colors"
                />
            </div>
        )}
        {nodes.length > 0 && !hasStrategyNodes && !issues && (
          <div className="absolute bottom-6 left-1/2 -translate-x-1/2 z-10 pointer-events-none px-4 w-full max-w-md">
            <div className="bg-raised/90 backdrop-blur-xl border border-border rounded-lg px-4 py-3 shadow-card text-center fade-in">
              <p className="text-xs text-text-secondary leading-relaxed">
                <span className="font-semibold text-text">Build:</span> Indicator → Condition → Entry Action.
              </p>
              <p className="text-2xs text-muted mt-1">Drag or click blocks from the toolbox. Actions: Buy · Sell (spot and perps), Short · Cover (perps only).</p>
            </div>
          </div>
        )}
        {issues && issues.errors.length > 0 && (
          <div className="absolute bottom-6 left-1/2 -translate-x-1/2 z-10 px-4 w-full max-w-lg" role="alert">
            <div className="bg-raised/95 backdrop-blur-xl border border-danger/50 rounded-lg shadow-card fade-in overflow-hidden">
              <div className="flex items-center gap-2 px-4 py-2 border-b border-danger/30 bg-danger/[0.06]">
                <svg className="w-3.5 h-3.5 text-danger shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M12 3l9 16H3l9-16z" />
                </svg>
                <span className="text-2xs font-bold uppercase tracking-wider text-danger flex-1">Not saved — {issues.errors.length} issue{issues.errors.length === 1 ? '' : 's'} to fix</span>
                <button type="button" onClick={() => setIssues(null)} aria-label="Dismiss validation issues" className="text-faint hover:text-text p-1 -mr-1 transition-colors">
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" /></svg>
                </button>
              </div>
              <ul className="max-h-40 overflow-y-auto custom-scrollbar divide-y divide-border/50">
                {issues.errors.map((line, i) => {
                  const m = /Node '([^']+)'/.exec(line);
                  return (
                    <li key={i} className="px-4 py-2 text-xs text-text leading-snug flex items-start gap-2">
                      <span className="flex-1">{line}</span>
                      {m && nodes.some(n => n.id === m[1]) && (
                        <button type="button" onClick={() => focusNode(m[1])} className="text-2xs text-info hover:underline shrink-0">show</button>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          </div>
        )}
        <ReactFlow
          nodes={flaggedNodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onDrop={onDrop}
          onDragOver={onDragOver}
          onInit={setReactFlowInstance}
          fitView
          attributionPosition="bottom-right"
        >
          {/* Background/MiniMap paint into SVG/canvas attributes, so their
              colors are resolved from the live tokens via getToken and
              refreshed by the apex-theme-changed re-render (themeTick) */}
          <Background color={canvasColors.dots} gap={20} size={2} />
          {/* Offset controls upward on mobile to clear the bottom nav bar */}
          <Controls style={{ display: 'flex', flexDirection: 'column', backgroundColor: 'var(--color-raised)', border: '1px solid var(--color-border)', borderRadius: '8px', overflow: 'hidden', position: 'absolute', bottom: window.innerWidth < 768 ? '70px' : '20px', left: '20px', boxShadow: 'var(--shadow-card)' }} />
          <MiniMap nodeColor={() => canvasColors.node} maskColor={canvasColors.mask} style={{ backgroundColor: 'var(--color-raised)', border: '1px solid var(--color-border)', borderRadius: '8px', display: window.innerWidth < 768 ? 'none' : 'block', boxShadow: 'var(--shadow-card)' }} />
        </ReactFlow>
      </div>
    </div>
  );
};

export default React.memo(function BotBuilderWrapper(props) {
  return (
    <ReactFlowProvider>
      <BotBuilderFlow {...props} />
    </ReactFlowProvider>
  );
});