/**
 * Toast system.
 *
 * Mount <Toaster /> once (done in App.jsx). Fire toasts from anywhere:
 *
 *   import { toast } from './ui/Toast';        // adjust path
 *   toast.success('Bot deployed');
 *   toast.error('Order failed: insufficient balance');
 *   toast.info('Backfill started');
 *
 * Or via window event (works from any component with no import):
 *   window.dispatchEvent(new CustomEvent('apex-toast', {
 *     detail: { type: 'success'|'error'|'info'|'warn', message: '...' }
 *   }));
 *
 * Toasts stack bottom-right and auto-dismiss after 4.5s (pause on hover,
 * dismiss on click of the X). Errors stay until dismissed — a validation
 * message you have to act on should not vanish while you read it.
 */
import { useEffect, useRef, useState } from 'react';

// eslint-disable-next-line react-refresh/only-export-components -- toast helper is the public API of this module
export const toast = {
  success: (message) => emit('success', message),
  error: (message) => emit('error', message),
  info: (message) => emit('info', message),
  warn: (message) => emit('warn', message),
};

function emit(type, message) {
  window.dispatchEvent(new CustomEvent('apex-toast', { detail: { type, message } }));
}

const STYLES = {
  success: { border: 'border-success/40', text: 'text-success', glow: 'shadow-glow-success' },
  error: { border: 'border-danger/40', text: 'text-danger', glow: 'shadow-glow-danger' },
  warn: { border: 'border-warn/40', text: 'text-warn', glow: '' },
  info: { border: 'border-info/40', text: 'text-info', glow: '' },
};

const ICONS = {
  success: <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />,
  error: <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M12 3l9 16H3l9-16z" />,
  warn: <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M12 3l9 16H3l9-16z" />,
  info: <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />,
};

let nextId = 1;

const ToastItem = ({ item, onDismiss }) => {
  const [exiting, setExiting] = useState(false);
  const timerRef = useRef(null);
  const sticky = item.type === 'error';
  const remainingRef = useRef(4500);
  const startedRef = useRef(0);

  const close = () => {
    setExiting(true);
    setTimeout(() => onDismiss(item.id), 180);
  };

  const startTimer = () => {
    if (sticky) return;
    startedRef.current = Date.now();
    timerRef.current = setTimeout(close, remainingRef.current);
  };
  const pauseTimer = () => {
    clearTimeout(timerRef.current);
    remainingRef.current -= Date.now() - startedRef.current;
  };

  useEffect(() => {
    startTimer();
    return () => clearTimeout(timerRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const s = STYLES[item.type] || STYLES.info;

  return (
    <div
      role={sticky ? 'alert' : 'status'}
      onMouseEnter={pauseTimer}
      onMouseLeave={startTimer}
      className={`${exiting ? 'toast-exit' : 'toast-enter'} pointer-events-auto flex items-start gap-3 w-80 max-w-[calc(100vw-2rem)] bg-overlay/95 backdrop-blur-xl border ${s.border} ${s.glow} rounded-lg px-4 py-3 shadow-pop`}
    >
      <svg className={`w-4 h-4 mt-0.5 shrink-0 ${s.text}`} fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
        {ICONS[item.type] || ICONS.info}
      </svg>
      <p className="flex-1 text-xs text-text leading-relaxed break-words whitespace-pre-line">{item.message}</p>
      <button
        onClick={close}
        aria-label="Dismiss"
        className="text-faint hover:text-text transition-colors shrink-0 -mr-1 p-1"
      >
        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>
  );
};

export const Toaster = () => {
  const [items, setItems] = useState([]);

  useEffect(() => {
    const handler = (e) => {
      const { type = 'info', message = '' } = e.detail || {};
      if (!message) return;
      setItems((prev) => [...prev.slice(-4), { id: nextId++, type, message }]);
    };
    window.addEventListener('apex-toast', handler);
    return () => window.removeEventListener('apex-toast', handler);
  }, []);

  const dismiss = (id) => setItems((prev) => prev.filter((t) => t.id !== id));

  if (items.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-[200] flex flex-col gap-2 items-end pointer-events-none">
      {items.map((item) => (
        <ToastItem key={item.id} item={item} onDismiss={dismiss} />
      ))}
    </div>
  );
};

export default Toaster;
