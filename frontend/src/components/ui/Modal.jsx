/**
 * Modal — confirm/info dialog driven by a config object.
 *
 * @param {object} props
 * @param {object|null} props.config      Null hides the modal.
 * @param {'danger'|'warning'|'success'|'info'} [props.config.type='warning']
 * @param {string} props.config.title
 * @param {string} [props.config.message]
 * @param {Function} [props.config.onConfirm]   Renders confirm button when set.
 * @param {Function} [props.config.onCancel]    Renders cancel button; also Esc / backdrop click.
 * @param {string} [props.config.confirmText='Confirm']
 * @param {string} [props.config.cancelText='Cancel']
 * @param {Function} [props.config.onSecondary] Optional third choice, rendered between cancel and confirm.
 * @param {string} [props.config.secondaryText]
 * @param {boolean} [props.config.busy]         Disables confirm, shows "Processing…".
 * @param {React.ReactNode} [props.customBody]  Replaces the message paragraph.
 *
 * For simple yes/no flows prefer `confirmDialog()` from ui/ConfirmDialog.
 */
import { useEffect, useRef } from 'react';
import Button from './Button';

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Inline-style colors read the live tokens so both themes work
const mix = (token, pct) => `color-mix(in srgb, var(--color-${token}) ${pct}%, transparent)`;
const TYPE_COLORS = {
  danger: { accent: 'var(--color-danger)', bg: mix('danger', 8), variant: 'danger' },
  warning: { accent: 'var(--color-accent)', bg: mix('accent', 8), variant: 'primary' },
  success: { accent: 'var(--color-success)', bg: mix('success', 8), variant: 'success' },
  info: { accent: 'var(--color-info)', bg: mix('info', 8), variant: 'primary' },
};

const Modal = ({ config, customBody }) => {
  const onCancel = config?.onCancel;
  const busy = config?.busy;

  const panelRef = useRef(null);

  // Esc cancels; Tab is trapped inside the dialog; focus lands on the cancel
  // button (the safe choice) when opening and returns to the opener on close.
  useEffect(() => {
    if (!config) return undefined;
    const opener = document.activeElement;
    const panel = panelRef.current;
    const focusables = () => Array.from(panel?.querySelectorAll(FOCUSABLE) || []);
    const initial = focusables();
    (initial[0] || panel)?.focus?.();
    const onKey = (e) => {
      if (e.key === 'Escape') {
        if (onCancel && !busy) { e.preventDefault(); onCancel(); }
        return;
      }
      if (e.key !== 'Tab') return;
      const items = focusables();
      if (items.length === 0) return;
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      if (opener && typeof opener.focus === 'function' && document.contains(opener)) opener.focus();
    };
  }, [config, onCancel, busy]);

  if (!config) return null;

  const colors = TYPE_COLORS[config.type] || TYPE_COLORS.warning;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-labelledby="apex-modal-title" data-apex-modal="">
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-md backdrop-enter"
        onClick={busy ? undefined : config.onCancel}
      />
      <div ref={panelRef} tabIndex={-1} className="relative outline-none modal-enter bg-overlay/95 backdrop-blur-xl border border-border rounded-lg max-w-md w-full shadow-pop overflow-hidden">
        <div
          className="absolute top-0 left-0 right-0 h-px"
          style={{ background: `linear-gradient(90deg, transparent, color-mix(in srgb, ${colors.accent} 40%, transparent), transparent)` }}
        />
        <div className="px-5 py-4 border-b border-border" style={{ background: colors.bg }}>
          <h3 id="apex-modal-title" className="text-xs font-bold uppercase tracking-wider" style={{ color: colors.accent }}>
            {config.title}
          </h3>
        </div>

        <div className="px-5 py-4">
          {customBody || (
            <p className="text-xs text-text-secondary leading-relaxed whitespace-pre-line">{config.message}</p>
          )}
        </div>

        <div className="flex justify-end gap-3 px-5 py-3.5 border-t border-border bg-raised/50">
          {config.onCancel && (
            <Button variant="secondary" size="sm" onClick={config.onCancel} disabled={config.busy}>
              {config.cancelText || 'Cancel'}
            </Button>
          )}
          {config.onSecondary && (
            <Button variant="ghost" size="sm" onClick={config.onSecondary} disabled={config.busy}>
              {config.secondaryText || 'Other'}
            </Button>
          )}
          {config.onConfirm && (
            <Button
              variant={colors.variant}
              size="sm"
              onClick={config.onConfirm}
              loading={config.busy}
            >
              {config.busy ? 'Processing…' : (config.confirmText || 'Confirm')}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
};

export default Modal;
