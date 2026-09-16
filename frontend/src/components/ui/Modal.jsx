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
import { useEffect } from 'react';
import Button from './Button';

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

  useEffect(() => {
    if (!config) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape' && onCancel && !busy) onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [config, onCancel, busy]);

  if (!config) return null;

  const colors = TYPE_COLORS[config.type] || TYPE_COLORS.warning;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true">
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-md backdrop-enter"
        onClick={busy ? undefined : config.onCancel}
      />
      <div className="relative modal-enter bg-overlay/95 backdrop-blur-xl border border-border rounded-lg max-w-md w-full shadow-pop overflow-hidden">
        <div
          className="absolute top-0 left-0 right-0 h-px"
          style={{ background: `linear-gradient(90deg, transparent, color-mix(in srgb, ${colors.accent} 40%, transparent), transparent)` }}
        />
        <div className="px-5 py-4 border-b border-border" style={{ background: colors.bg }}>
          <h3 className="text-xs font-bold uppercase tracking-wider" style={{ color: colors.accent }}>
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
