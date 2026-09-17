import { useState, useEffect, useRef, useCallback } from 'react';
import { apiClient } from '../api/client';

const LEVEL_COLOR = {
  INFO:  'text-muted',
  WARN:  'text-warn',
  ERROR: 'text-danger',
};

export default function BotConsole({ botName, isOpen, clearSignal = 0 }) {
  const [entries, setEntries]       = useState([]);
  const [cursor, setCursor]         = useState(0);
  const [autoScroll, setAutoScroll] = useState(true);
  const [prevClearSignal, setPrevClearSignal] = useState(clearSignal);
  const scrollRef  = useRef(null);
  const bottomRef  = useRef(null);
  const cursorRef  = useRef(0);

  // Keep cursorRef in sync so the interval closure always sees the latest value
  useEffect(() => { cursorRef.current = cursor; }, [cursor]);

  // Reset entries when cache is wiped (clearSignal incremented by parent).
  // Render-phase reset avoids a cascading effect render.
  if (clearSignal !== prevClearSignal) {
    setPrevClearSignal(clearSignal);
    if (clearSignal > 0) {
      setEntries([]);
      setCursor(0); // cursorRef syncs via the effect below before the next poll tick
    }
  }

  // Poll only while open
  useEffect(() => {
    if (!isOpen) return;

    const poll = async () => {
      try {
        const res = await apiClient.get(
          `/api/bots/console/logs?bot_name=${encodeURIComponent(botName)}&since=${cursorRef.current}`
        );
        const newEntries = res.data?.entries ?? [];
        if (newEntries.length > 0) {
          setEntries(prev => {
            const combined = [...prev, ...newEntries];
            return combined.length > 1000 ? combined.slice(-1000) : combined;
          });
          setCursor(newEntries[newEntries.length - 1].seq);
        }
      } catch {
        // silently ignore — backend may be restarting
      }
    };

    poll(); // immediate first fetch
    const id = setInterval(poll, 2000);
    return () => clearInterval(id);
  }, [isOpen, botName]);

  // Auto-scroll to bottom when new entries arrive
  useEffect(() => {
    if (autoScroll && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [entries.length, autoScroll]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollTop >= el.scrollHeight - el.clientHeight - 24;
    if (!atBottom) setAutoScroll(false);
  }, []);

  const jumpToBottom = () => {
    setAutoScroll(true);
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const clearLocal = () => setEntries([]);

  return (
    <div className="bg-inset border-t border-border">
      {/* Console toolbar */}
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-border/50">
        <div className="flex items-center gap-2 min-w-0">
          <span className="flex items-center gap-1.5 min-w-0">
            <svg className="w-3 h-3 text-faint shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4-4 4M12 16h6M3 4h18v16H3z" />
            </svg>
            <span className="text-3xs font-bold uppercase tracking-widest text-muted truncate">{botName}</span>
          </span>
          <span className="text-3xs font-num text-faint shrink-0">{entries.length} lines</span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={clearLocal}
            title="Clear console output (local only)"
            className="text-3xs font-bold uppercase text-muted hover:text-text transition-colors px-1.5 py-0.5 rounded-sm hover:bg-overlay"
          >
            CLEAR
          </button>
          <button
            onClick={jumpToBottom}
            title={autoScroll ? 'Auto-scroll on' : 'Click to resume auto-scroll'}
            className={`text-3xs font-bold uppercase px-1.5 py-0.5 rounded-sm transition-colors ${
              autoScroll
                ? 'text-success bg-success/10'
                : 'text-muted hover:text-text hover:bg-overlay'
            }`}
          >
            ↓ FOLLOW
          </button>
        </div>
      </div>

      {/* Log lines */}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="h-40 overflow-y-auto font-num"
        style={{ scrollbarWidth: 'thin' }}
      >
        {entries.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <span className="text-3xs text-faint">No activity yet — start the engine to stream events</span>
          </div>
        ) : (
          entries.map((e) => (
            <div
              key={e.seq}
              className="flex items-start gap-2 px-3 py-[2px] hover:bg-overlay/60 fade-in"
            >
              <span className="text-faint text-3xs shrink-0 select-none pt-[1px]">{e.ts}</span>
              <span className={`text-3xs font-bold shrink-0 w-9 pt-[1px] ${LEVEL_COLOR[e.level] ?? 'text-faint'}`}>
                {e.level}
              </span>
              <span className={`text-3xs break-all leading-relaxed ${e.level === 'ERROR' ? 'text-danger/90' : e.level === 'WARN' ? 'text-warn/90' : 'text-text-secondary'}`}>
                {e.msg}
              </span>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
