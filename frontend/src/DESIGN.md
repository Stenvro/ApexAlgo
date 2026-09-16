# ApexAlgo Design System

Contract for all view work. Tokens live in `src/index.css` under `@theme`.
Primitives live in `src/components/ui/`. **Follow this file; do not invent ad-hoc styles.**

## Theming (dark + light)

The app ships two runtime themes. Dark is the default; light is applied by a
`light` class on `<html>` (toggled from the Sidebar footer, persisted in
localStorage `apex_theme`). Everything is driven by CSS variables:

- `@theme` tokens reference `--apx-*` runtime vars; `:root` holds the dark
  values and `html.light { … }` overrides every one of them. Utilities like
  `bg-raised` / `text-muted` therefore re-theme automatically — never assume
  a dark background in a view.
- **Never hardcode a theme-specific color.** Use token utilities, or
  `var(--color-…)` in inline styles/SVG `style` props, or
  `color-mix(in srgb, var(--color-…) N%, transparent)` for alpha tints.
- Canvas/chart libraries that need raw values (lightweight-charts, ReactFlow
  MiniMap/Background) read tokens via `getToken(name)` from `src/theme.js`
  and re-render on the `apex-theme-changed` window event.
- `text-white` / `bg-white/...` are forbidden — use `text-text` and
  `bg-text/[0.03]`-style tints so hovers work on both themes.
- Accent split: `accent` is the *text/border/icon* gold (darkens in light
  mode for contrast); `accent-fill` (+`accent-fill-hover`) is the gold
  *surface* that always carries `accent-ink` text (buttons).
- The dark values listed below remain the canonical reference palette.

## Tokens (use as Tailwind utilities)

### Surfaces (darkest → lightest)
| Token | Utility | Use |
|---|---|---|
| `bg` `#080a0f` | `bg-bg` | App background |
| `surface` `#0e1118` | `bg-surface` | Page-level panels |
| `raised` `#12151c` | `bg-raised` | Cards, sidebar, headers |
| `overlay` `#171b24` | `bg-overlay` | Modals, dropdowns, hover fills |
| `inset` `#05070b` | `bg-inset` | Input fields, wells, code blocks |

### Borders
- `border-border` (`#202532`) — default everywhere.
- `border-border-strong` (`#2b3545`) — hover / emphasis only.

### Text
- `text-text` (`#eaecef`) — primary content.
- `text-text-secondary` (`#b7bdc6`) — body copy, descriptions.
- `text-muted` (`#848e9c`) — labels, captions, table headers.
- `text-faint` (`#5e6673`) — least important (timestamps, hints).

### Brand & semantic
- `accent` `#fcd535` (+ `accent-hover`, `accent-ink` for text on gold) — primary actions, focus, active nav.
- `danger` `#f6465d`, `success` `#2ebd85`, `warn` `#f0b90b`, `info` `#0ea5e9`, `purple` `#8b5cf6`.
- P&L convention: positive = `text-success`, negative = `text-danger`, always `.font-num`.

### Radii & shadows
- `rounded-sm|md|lg|xl` = 6/8/12/16px. Buttons/inputs: `rounded-md`. Cards: `rounded-lg` (built into `terminal-card`). Modals/heroes: `rounded-lg`/`rounded-xl`.
- `shadow-card`, `shadow-pop` (modals/toasts), `shadow-glow-accent|danger|success` (sparingly — one glow per view region max).

### Typography
- UI: Inter (`font-sans`, default on body).
- **All numbers, prices, symbols, timeframes, hashes: `.font-num`** (JetBrains Mono + tabular-nums). Never `font-mono` + proportional digits for data.
- Labels/captions: `text-[9px]`–`text-[10px] font-bold uppercase tracking-wider text-muted`.

### Layout conventions
- Wrap every page in `<PageShell>`; section gap is `space-y-6` (built in).
- Card padding `p-4`/`p-5`; table cells `px-4 py-2.5`; control gaps `gap-2`/`gap-3`.
- Utility classes that must keep working: `terminal-card`, `page-container`, `grid-background`, `glow-panel(-cyan|-green|-purple)`, `fade-in`, `fade-in-delay-1..6`, `modal-enter`.

## Primitives (`src/components/ui/`)

Every file has full JSDoc at the top — read it before use.

| Component | Import | Signature (key props) |
|---|---|---|
| Button | `default from './ui/Button'` | `variant: primary\|secondary\|ghost\|danger\|success`, `size: sm\|md\|lg`, `loading`, `disabled`, `fullWidth`, `icon` |
| Input / Select / Textarea | `{ Input, Select, Textarea } from './ui/Input'` | `label`, `error`, `hint`, `mono` (+ native props) |
| Badge | `default from './ui/Badge'` | `variant: success\|danger\|warn\|info\|accent\|purple\|neutral`, `dot`, `pulse` |
| Toast | `{ toast } from './ui/Toast'` | `toast.success/error/info/warn('msg')`. Toaster is mounted in App — never mount again. |
| ConfirmDialog | `{ confirmDialog } from './ui/ConfirmDialog'` | `await confirmDialog({ title, message, confirmText, type: 'danger'\|'warning'\|'info' })` → `boolean`. Add `secondaryText` for a three-way choice → `true \| 'secondary' \| false`. `message` keeps line breaks. Host mounted in App. |
| Modal | `default from './ui/Modal'` | `config: { type, title, message, onConfirm, onCancel, confirmText, cancelText, busy }`, `customBody`. Esc + backdrop close built in. |
| DataTable | `default from './ui/DataTable'` | `columns: [{key, label, align, render}]`, `data`, `emptyMessage`, `emptyState` (node), `maxHeight` (sticky header), `onRowClick` |
| StatCard | `default from './ui/StatCard'` | `label`, `value`, `color: gold\|cyan\|green\|red\|purple\|white`, `sub`, `icon` |
| Skeleton | `{ Skeleton, SkeletonText, SkeletonCard } from './ui/Skeleton'` | size via className |
| EmptyState | `default from './ui/EmptyState'` | `icon`, `title`, `description`, `action` (usually a Button) |
| SectionHeader | `default from './ui/SectionHeader'` | `title`, `subtitle`, `action`, `accentColor` |
| PageShell / GlowPanel | defaults | `glowColor: gold\|cyan\|green\|purple`; GlowPanel: `noPadding` |

### Examples
```jsx
<Button variant="danger" size="sm" loading={deleting} onClick={handleDelete}>Delete</Button>

<Input label="Capital (USDT)" mono value={cap} onChange={e => setCap(e.target.value)}
       error={capError} hint="Backtest starting equity" />

<Badge variant="success" dot pulse>Running</Badge>

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
- Loading state for every async region: `Skeleton*` while fetching, `loading` on the triggering Button.
- Empty state for every list/table: `EmptyState` (or DataTable's `emptyState` slot) with a helpful action.
- `toast.*` for operation feedback (success and failure).
- Keep animations subtle; `prefers-reduced-motion` is handled globally — don't add JS-driven animation.

**Don't**
- No ad-hoc hex colors (`text-[#fcd535]` etc.). Exception: chart-library option objects that require raw hex — take values from the token table.
- Never `alert()`, `window.confirm()`, or `prompt()` → use `toast` / `confirmDialog`.
- No new fonts, no icon libraries — inline SVG (stroke `1.8`, `w-4 h-4` default).
- Don't hand-roll buttons/inputs/badges/tables when a primitive exists.
- Don't remove or rename the legacy utility classes listed above; other views depend on them.
- Don't mount a second `<Toaster />` or `<ConfirmDialogHost />`.
- No TypeScript, no state libraries; cross-component signals via `window` CustomEvents (existing: `open-builder`, `api-key-invalid`, `apex-toast`, `apex-confirm`).
