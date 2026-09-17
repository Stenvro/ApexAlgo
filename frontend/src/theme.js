/**
 * Runtime theme switching — dark (default) / light.
 *
 * The entire palette lives in CSS custom properties (src/index.css):
 * `@theme` defines the dark values on :root, and an `html.light { … }`
 * block overrides every token. Switching themes is therefore just
 * toggling the `light` class on <html>.
 *
 * - getTheme()  -> 'dark' | 'light'   (localStorage `apex_theme` wins; otherwise
 *                  prefers-color-scheme, falling back to dark)
 * - setTheme(t) -> applies class + persists + dispatches `apex-theme-changed`
 * - initTheme() -> call once before first render to avoid a flash
 * - getToken(n) -> resolved CSS token value, e.g. getToken('success') === '#2ebd85'.
 *                  Use for canvas/chart libraries that need raw color values.
 */

const STORAGE_KEY = 'apex_theme';

export function getTheme() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === 'light' || stored === 'dark') return stored;
    return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  } catch {
    return 'dark';
  }
}

function hasStoredTheme() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === 'light' || stored === 'dark';
  } catch {
    return false;
  }
}

function applyTheme(theme) {
  const root = document.documentElement;
  root.classList.toggle('light', theme === 'light');

  // Keep native browser chrome (address bar on mobile) in sync
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    const bg = getComputedStyle(root).getPropertyValue('--color-bg').trim();
    if (bg) meta.setAttribute('content', bg);
  }
}

export function setTheme(theme) {
  const next = theme === 'light' ? 'light' : 'dark';
  try { localStorage.setItem(STORAGE_KEY, next); } catch { /* private mode */ }
  applyTheme(next);
  window.dispatchEvent(new CustomEvent('apex-theme-changed', { detail: { theme: next } }));
}

export function initTheme() {
  applyTheme(getTheme());

  // Follow OS preference changes as long as the user has no explicit choice
  // stored. Dispatch apex-theme-changed so canvas consumers (ChartEngine,
  // Builder) re-read their tokens, exactly like a manual toggle.
  const media = window.matchMedia?.('(prefers-color-scheme: light)');
  media?.addEventListener?.('change', () => {
    if (hasStoredTheme()) return;
    const next = getTheme();
    applyTheme(next);
    window.dispatchEvent(new CustomEvent('apex-theme-changed', { detail: { theme: next } }));
  });
}

/** Read a resolved `--color-*` token (trimmed), e.g. getToken('accent'). */
export function getToken(name) {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(`--color-${name}`)
    .trim();
}
