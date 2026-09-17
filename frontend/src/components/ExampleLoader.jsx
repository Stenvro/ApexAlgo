import { useEffect, useRef, useState } from 'react';
import { apiClient } from '../api/client';
import { humanizeApiError } from '../api/errors';
import { EXAMPLE_BOTS } from '../examples';
import Button from './ui/Button';
import { toast } from './ui/Toast';

/**
 * ExampleLoader — "Load example strategy" button with a small picker menu.
 *
 * Imports one of the bundled example bots via POST /api/bots/import and
 * notifies the caller so the bot list can refresh.
 *
 * @param {Function} onImported  Called after a successful import (refresh list).
 * @param {string}   [size]      Button size ('sm' | 'md' | 'lg').
 * @param {string}   [variant]   Button variant (default 'secondary').
 */
export default function ExampleLoader({ onImported, size = 'sm', variant = 'secondary' }) {
  const [open, setOpen] = useState(false);
  const [importing, setImporting] = useState(null); // example id being imported
  const wrapRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false);
    };
    const onEsc = (e) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onEsc);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onEsc);
    };
  }, [open]);

  const importExample = async (example) => {
    if (importing) return;
    setImporting(example.id);
    try {
      await apiClient.post('/api/bots/import', example.payload);
      toast.success(`'${example.payload?.bot?.name || example.name}' imported — find it under Algorithms`);
      setOpen(false);
      onImported?.();
    } catch (err) {
      toast.error(humanizeApiError(err, 'Failed to import the example strategy.'));
    }
    setImporting(null);
  };

  return (
    <div ref={wrapRef} className="relative inline-block text-left">
      <Button
        size={size}
        variant={variant}
        loading={Boolean(importing)}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        Load example strategy
      </Button>

      {open && (
        <div
          role="menu"
          className="absolute left-1/2 -translate-x-1/2 top-full mt-2 w-72 z-50 bg-overlay border border-border rounded-lg shadow-pop overflow-hidden fade-in"
        >
          {EXAMPLE_BOTS.map((ex) => (
            <button
              key={ex.id}
              role="menuitem"
              disabled={Boolean(importing)}
              onClick={() => importExample(ex)}
              className="w-full text-left px-4 py-3 hover:bg-raised transition-colors border-b border-border/50 last:border-b-0 disabled:opacity-50"
            >
              <span className="block text-xs font-semibold text-text">{ex.name}</span>
              <span className="block text-2xs text-muted mt-0.5 leading-relaxed">{ex.description}</span>
            </button>
          ))}
          <p className="px-4 py-2.5 text-3xs text-faint leading-relaxed bg-inset/60">
            More examples live in the <span className="font-num">examples/</span> directory of the repository —
            import them via the Import button.
          </p>
        </div>
      )}
    </div>
  );
}
