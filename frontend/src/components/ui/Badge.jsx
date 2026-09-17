/**
 * Badge — small status pill.
 *
 * @param {object} props
 * @param {'success'|'danger'|'warn'|'info'|'accent'|'neutral'|'purple'} [props.variant='neutral']
 * @param {boolean} [props.dot=false]     Leading status dot.
 * @param {boolean} [props.pulse=false]   Animate the dot (live/running states).
 * @param {string} [props.className]
 * @param {string} [props.title]      Native tooltip.
 *
 * Usage:
 *   <Badge variant="success" dot pulse>Live</Badge>
 *   <Badge variant="neutral">backtest</Badge>
 */
const VARIANTS = {
  success: 'bg-success/10 text-success border-success/30',
  danger: 'bg-danger/10 text-danger border-danger/30',
  warn: 'bg-warn/10 text-warn border-warn/30',
  info: 'bg-info/10 text-info border-info/30',
  accent: 'bg-accent/10 text-accent border-accent/30',
  purple: 'bg-purple/10 text-purple border-purple/30',
  neutral: 'bg-raised text-muted border-border',
};

const Badge = ({ variant = 'neutral', dot = false, pulse = false, className = '', title, children }) => (
  <span
    title={title}
    className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full border text-[10px] font-bold uppercase tracking-wider whitespace-nowrap ${VARIANTS[variant] || VARIANTS.neutral} ${className}`}
  >
    {dot && (
      <span
        className={`w-1.5 h-1.5 rounded-full bg-current shrink-0 ${pulse ? 'animate-pulse' : ''}`}
      />
    )}
    {children}
  </span>
);

export default Badge;
