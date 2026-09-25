# ApexAlgo Design System — "Terminal Green"

Contract for all view work. Tokens live in `src/index.css` under `@theme`.
Primitives live in `src/components/ui/`. **Follow this file; do not invent ad-hoc styles.**

Aesthetic: CMC/TradingView professionalism + DexScreener terminal density.
Neutral cool-gray dark palette, restrained mint accent, flat solid surfaces
(no glass blur, no decorative glows), tight radii, dense mono data.

## Theming (dark + light)

The app ships two runtime themes. Dark is the default; with no stored choice
the OS preference (`prefers-color-scheme`) decides, and the Sidebar-footer
toggle stores an explicit override in localStorage `apex_theme` (applied as a
`light` class on `<html>`). Everything is driven by CSS variables:

- `@theme` tokens reference `--apx-*` runtime vars; `:root` holds the dark
  values and `html.light { … }` overrides every one of them. Utilities like
  `bg-raised` / `text-muted` therefore re-theme automatically — never assume
  a dark background in a view.
- **Never hardcode a theme-specific color.** Use token utilities, or
  `var(--color-…)` in inline styles/SVG `style` props, or
  `color-mix(in srgb, var(--color-…) N%, transparent)` for alpha tints.
- **All color token values stay 6-digit hex** — `getToken()` consumers append
  hex alpha (`getToken('success') + '80'`); `oklch`/`rgb()` values break them silently.
- Canvas/chart libraries that need raw values (lightweight-charts, ReactFlow
  MiniMap/Background) read tokens via `getToken(name)` from `src/theme.js`
  and re-render on the `apex-theme-changed` window event.
- `text-white` / `bg-white/...` / `bg-black/...` are forbidden — use `text-text`,
  `hover:bg-overlay/50` row hovers, and the `.backdrop` class for scrims.
- Accent split: `accent` is the *text/border/icon* mint (darkens in light
  mode for contrast); `accent-fill` (+`accent-fill-hover`) is the mint
  *surface* that always carries `accent-ink` text (buttons). Solid danger/
  success fills carry `text-danger-ink` / `text-success-ink`.
- **Accent ≠ success.** `accent` (`#3ddba9` pale mint) is interaction chrome:
  buttons, focus, active nav, selection. `success` (`#16c784` deep market
  green) means profit/up — only ever PnL and up-state. Never swap them.
- The dark values listed below remain the canonical reference palette.

## Tokens (use as Tailwind utilities)

### Surfaces (darkest → lightest)
| Token | Utility | Use |
|---|---|---|
| `bg` `#06080a` | `bg-bg` | App background |
| `surface` `#0b0e12` | `bg-surface` | Page-level panels, sticky table headers |
| `raised` `#10141a` | `bg-raised` | Cards, sidebar, headers |
| `overlay` `#161b23` | `bg-overlay` | Modals, dropdowns, hover fills |
| `inset` `#030507` | `bg-inset` | Input fields, wells, code blocks |

### Borders
- `border-border` (`#1c222c`) — default everywhere.
- `border-border-strong` (`#2b3441`) — hover / emphasis only.

### Text
- `text-text` (`#e6edf3`) — primary content.
- `text-text-secondary` (`#b0bac5`) — body copy, descriptions.
- `text-muted` (`#7d8896`) — labels, captions, table headers.
- `text-faint` (`#566170`) — least important (timestamps, hints).

### Brand & semantic
- `accent` `#3ddba9` (+ `accent-hover`; `accent-fill` `#2fe6a8` + `accent-ink` for mint surfaces) — primary actions, focus, active nav.
- `danger` `#ea3943` (+ `danger-ink`), `success` `#16c784` (+ `success-ink`), `warn` `#f0b90b`, `info` `#0ea5e9`, `purple` `#8b5cf6`.
- `chart-1|2|3` (`#d946ef`/`#ff9800`/`#00bcd4`) — extra chart-series hues, `getToken()` only.
- P&L convention: positive = `text-success`, negative = `text-danger`, always `.font-num`.

### Type ramp
| Utility | Size | Use |
|---|---|---|
| `text-3xs` | 9px | micro-labels, uppercase table headers |
| `text-2xs` | 10px | captions, chips, sub-lines |
| `text-xs` | 11px | dense body: table cells, buttons, inputs |
| `text-sm` | 13px | body copy, card titles |
| `text-base` | 15px | emphasized values |
| `text-lg`–`text-2xl` | 17–24px | headings / hero |

Never write `text-[9px]`-style arbitrary sizes — use the ramp.
Labels/captions: `text-3xs`/`text-2xs` + `font-bold uppercase tracking-wider text-muted`.

### Radii & shadows
- `rounded-sm|md|lg|xl` = 4/6/8/12px (tight, terminal). Buttons/inputs: `rounded-md`. Cards: `rounded-lg` (built into `terminal-card`). Chips/badges: `rounded-sm`.
- `shadow-card` (flat), `shadow-pop` (modals/toasts). The `shadow-glow-*` tokens survive for Button/Toast but are near-flat — do not add new glows or blur blobs.

### Typography
- UI: Inter (`font-sans`, default on body).
- **All numbers, prices, symbols, timeframes, hashes: `.font-num`** (JetBrains Mono + tabular-nums). Never `font-mono` + proportional digits for data.
- No new fonts (also blocked by CSP `font-src 'self'`).

### Layout conventions
- Wrap every page in `<PageShell>`; section gap is `space-y-5` (built in), gutters are responsive (`px-3 sm:px-4 lg:px-6`).
- Card padding `p-3`/`p-4`; table cells `px-3 py-1.5` at `text-xs` with `text-3xs` uppercase headers on `bg-surface`; control gaps `gap-2`/`gap-3`.
- Row hover: `hover:bg-overlay/50`. Scrims: the `.backdrop` class (tokenized, both themes).
- Tables scroll horizontally on small screens: `overflow-x-auto` + a sensible `min-w-[…]` — never reflow data columns.
- Utility classes that must keep working: `terminal-card`, `backdrop`, `page-container`, `grid-background`, `glow-panel(-cyan|-green|-purple)` (now near-flat), `fade-in`, `fade-in-delay-1..6`, `modal-enter`.

## Primitives (`src/components/ui/`)

Every file has full JSDoc at the top — read it before use.
Color props are **semantic only**: `accent|info|success|danger|purple|neutral`.

| Component | Import | Signature (key props) |
|---|---|---|
| Button | `default from './ui/Button'` | `variant: primary\|secondary\|ghost\|danger\|success`, `size: sm\|md\|lg`, `loading`, `disabled`, `fullWidth`, `icon` |
| Input / Select / Textarea | `{ Input, Select, Textarea } from './ui/Input'` | `label`, `error`, `hint`, `mono` (+ native props) |
| Badge | `default from './ui/Badge'` | `variant: success\|danger\|warn\|info\|accent\|purple\|neutral`, `dot`, `pulse` |
| ModeBadge | `default from './ui/ModeBadge'` | `mode: live\|paper\|forward_test\|backtest`, `short`. **The only way to label a trading mode.** Live = accent (mint) + ● glyph, Paper = info, Forward test = purple, Backtest = neutral. Never `success`/`danger` for a mode — those mean profit/loss. Bots carry `execution_mode` from `/api/bots/summary`. |
| Toast | `{ toast } from './ui/Toast'` | `toast.success/error/info/warn('msg')`. Multi-line messages render as lines. Errors are sticky (`role="alert"`, dismissed by the user); the rest auto-dismiss. Toaster is mounted in App — never mount again. |
| ConfirmDialog | `{ confirmDialog } from './ui/ConfirmDialog'` | `await confirmDialog({ title, message, confirmText, type: 'danger'\|'warning'\|'info' })` → `boolean`. Add `secondaryText` for a three-way choice → `true \| 'secondary' \| false`. `message` keeps line breaks. Host mounted in App. |
| Modal | `default from './ui/Modal'` | `config: { type, title, message, onConfirm, onCancel, confirmText, cancelText, busy }`, `customBody`. Esc + backdrop close built in. |
| DataTable | `default from './ui/DataTable'` | `columns: [{key, label, align, render}]`, `data`, `emptyMessage`, `emptyState` (node), `maxHeight` (sticky header), `onRowClick` |
| StatCard | `default from './ui/StatCard'` | `label`, `value`, `color: accent\|info\|success\|danger\|purple\|neutral`, `sub`, `icon` |
| Skeleton | `{ Skeleton, SkeletonText, SkeletonCard } from './ui/Skeleton'` | size via className |
| EmptyState | `default from './ui/EmptyState'` | `icon`, `title`, `description`, `action` (usually a Button) |
| SectionHeader | `default from './ui/SectionHeader'` | `title`, `subtitle`, `action`, `accentColor` (semantic) |
| PageShell / GlowPanel | defaults | PageShell takes no styling props (`glowColor` is accepted but ignored); GlowPanel: `glowColor: accent\|info\|success\|purple`, `noPadding` |

### Examples
```jsx
<Button variant="danger" size="sm" loading={deleting} onClick={handleDelete}>Delete</Button>

<Input label={`Capital (${cashCcy})`} mono value={cap} onChange={e => setCap(e.target.value)}
       error={capError} hint="Backtest starting equity — cash currency of the whitelist" />

// Money: never a bare "$"/"USD". Format through utils/money.js with the currency
// the row is denominated in; sum per currency, never across.
fmtMoney(1234.5, 'USDT')  // '1,234.50 USDT'   fmtMoney(0.0213, 'BTC') // '0.0213 BTC'
fmtByCurrency(sumByCurrency(positions, p => p.profit_abs))  // '12.30 USDT · 0.001 BTC'

<Badge variant="success" dot pulse>Running</Badge>

<StatCard label="Win rate" value="63.4%" color="success" sub="142 trades" />

toast.success('Bot deployed');

const ok = await confirmDialog({
  title: 'Delete bot', message: `Remove "${bot.name}" permanently?`,
  confirmText: 'Delete', type: 'danger',
});
if (!ok) return;
```

## Do's & Don'ts

**Do**
- Use token utilities (`bg-raised`, `text-muted`, `border-border`, …) for every color.
- `.font-num` on every numeric/price/symbol/timeframe value.
- Type-ramp utilities (`text-3xs`…`text-2xl`) for every font size.
- Loading state for every async region: `Skeleton*` while fetching, `loading` on the triggering Button.
- Empty state for every list/table: `EmptyState` (or DataTable's `emptyState` slot) with a helpful action.
- `toast.*` for operation feedback (success and failure).
- Keep animations subtle; `prefers-reduced-motion` is handled globally — don't add JS-driven animation.

**Don't**
- No ad-hoc hex colors (`text-[#3ddba9]` etc.). Exception: chart-library option objects that require raw hex — take values via `getToken()` (incl. `chart-1|2|3`).
- No decorative blur blobs, glass `backdrop-blur` surfaces, or new glow shadows — the terminal look is flat and crisp.
- Never `alert()`, `window.confirm()`, or `prompt()` → use `toast` / `confirmDialog`.
- No new fonts, no icon libraries — inline SVG (stroke `1.8`, `w-4 h-4` default).
- Don't hand-roll buttons/inputs/badges/tables when a primitive exists.
- Don't use the retired color-name prop values (`gold|cyan|green|red|white`) — semantic names only.
- Don't remove or rename the legacy utility classes listed above; other views depend on them.
- Don't mount a second `<Toaster />` or `<ConfirmDialogHost />`.
- No TypeScript, no state libraries; cross-component signals via `window` CustomEvents (existing: `open-builder`, `api-key-invalid`, `apex-toast`, `apex-confirm`, `apex-theme-changed`).
