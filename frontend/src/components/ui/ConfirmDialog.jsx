/**
 * ConfirmDialog — promise-based replacement for window.confirm().
 *
 * Mount <ConfirmDialogHost /> once (done in App.jsx). Then anywhere:
 *
 *   import { confirmDialog } from './ui/ConfirmDialog';   // adjust path
 *   const ok = await confirmDialog({
 *     title: 'Delete bot',
 *     message: 'This permanently removes "Alpha-7" and its signals.',
 *     confirmText: 'Delete',
 *     type: 'danger',            // 'danger' | 'warning' | 'info' (default 'warning')
 *   });
 *   if (ok) { ... }
 *
 * Pass `secondaryText` for a three-way choice: the promise then resolves to
 * `true` (confirm), `'secondary'`, or `false` (cancel / Esc / backdrop).
 *
 * Also reachable without import via window event:
 *   window.dispatchEvent(new CustomEvent('apex-confirm', {
 *     detail: { title, message, confirmText, cancelText, type, resolve }
 *   }));
 */
import { useEffect, useState } from 'react';
import Modal from './Modal';

// eslint-disable-next-line react-refresh/only-export-components -- confirmDialog helper is the public API of this module
export function confirmDialog(options = {}) {
  return new Promise((resolve) => {
    window.dispatchEvent(
      new CustomEvent('apex-confirm', { detail: { ...options, resolve } })
    );
  });
}

export const ConfirmDialogHost = () => {
  const [request, setRequest] = useState(null);

  useEffect(() => {
    const handler = (e) =>
      setRequest((prev) => {
        // A new confirm while one is pending: resolve the old promise as
        // "cancelled" so its caller never hangs forever.
        prev?.resolve?.(false);
        return e.detail || null;
      });
    window.addEventListener('apex-confirm', handler);
    return () => window.removeEventListener('apex-confirm', handler);
  }, []);

  if (!request) return null;

  const finish = (result) => {
    request.resolve?.(result);
    setRequest(null);
  };

  return (
    <Modal
      config={{
        type: request.type || 'warning',
        title: request.title || 'Are you sure?',
        message: request.message || '',
        confirmText: request.confirmText || 'Confirm',
        cancelText: request.cancelText || 'Cancel',
        secondaryText: request.secondaryText,
        onConfirm: () => finish(true),
        onSecondary: request.secondaryText ? () => finish('secondary') : undefined,
        onCancel: () => finish(false),
      }}
    />
  );
};

export default ConfirmDialogHost;
