import Badge from './Badge';

/**
 * ModeBadge — the one way to label real vs simulated money.
 *
 * Every view (Home, bot cards, Analytics rows) renders a trade/bot mode
 * through this component so the vocabulary and colour never drift:
 *
 *   live          → accent (mint) + ● glyph  — real orders, real money
 *   paper         → info                     — sandbox key, no money
 *   forward_test  → purple                   — local simulation on live candles
 *   backtest      → neutral                  — historical simulation
 *
 * Never use `success`/`danger` for a mode: those mean profit/loss elsewhere.
 *
 * @param {object} props
 * @param {'live'|'paper'|'forward_test'|'backtest'|string} props.mode
 * @param {boolean} [props.short=false]  Compact label ("Fwd" instead of "Forward test").
 * @param {string}  [props.className]
 */
const MODE_META = {
  live: { label: 'Live', short: 'Live', variant: 'accent', glyph: '●', title: 'Live — real orders on the exchange' },
  paper: { label: 'Paper', short: 'Paper', variant: 'info', glyph: null, title: 'Paper — sandbox key, no real money' },
  forward_test: { label: 'Forward test', short: 'Fwd', variant: 'purple', glyph: null, title: 'Forward test — simulated fills on live candles, no orders sent' },
  backtest: { label: 'Backtest', short: 'BT', variant: 'neutral', glyph: null, title: 'Backtest — simulated on historical candles' },
};

const ModeBadge = ({ mode, short = false, className = '' }) => {
  const meta = MODE_META[mode] || { label: String(mode || '—'), short: String(mode || '—'), variant: 'neutral', glyph: null, title: mode };
  return (
    <Badge variant={meta.variant} className={className} title={meta.title}>
      {meta.glyph && <span aria-hidden="true" className="text-[8px] leading-none">{meta.glyph}</span>}
      {short ? meta.short : meta.label}
    </Badge>
  );
};

export default ModeBadge;
