/**
 * humanizeApiError — turn an axios error into a message fit for a toast.
 *
 * Rules:
 *  - No `err.response` (network level: backend down, cert refused, CORS)
 *    → "Cannot reach the server — check that the backend is running."
 *  - 5xx → "Server error — please try again."
 *  - Otherwise prefer `err.response.data.detail` (string, FastAPI validation
 *    array, or `{ validation_errors: [...] }` object), then `data.error`,
 *    then the provided fallback.
 *
 * @param {unknown} err                 Error thrown by an apiClient call.
 * @param {string}  [fallback]          Message when nothing better is known.
 * @returns {string}
 */
export function humanizeApiError(err, fallback = 'Something went wrong — please try again.') {
  if (!err || typeof err !== 'object') return fallback;

  if (!err.response) {
    return 'Cannot reach the server — check that the backend is running.';
  }

  const { status, data } = err.response;
  if (status >= 500) return 'Server error — please try again.';

  const detail = data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (typeof detail?.message === 'string' && detail.message.trim()) return detail.message;
  if (detail && Array.isArray(detail.validation_errors) && detail.validation_errors.length > 0) {
    return detail.validation_errors.join('\n');
  }
  if (Array.isArray(detail) && detail.length > 0) {
    // FastAPI request-validation errors: [{ loc, msg, type }, …]
    return detail
      .map((d) => (typeof d === 'string' ? d : d?.msg))
      .filter(Boolean)
      .join('\n') || fallback;
  }
  if (typeof data?.error === 'string' && data.error.trim()) return data.error;

  return fallback;
}
