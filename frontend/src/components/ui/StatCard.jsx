/**
 * StatCard — compact KPI tile with a colored accent edge.
 *
 * @param {object} props
 * @param {string} props.label                    Uppercase caption.
 * @param {React.ReactNode} props.value           The stat (rendered in .font-num).
 * @param {'accent'|'info'|'success'|'danger'|'purple'|'neutral'} [props.color='accent']
 * @param {string} [props.sub]                    Optional muted sub-line under the value.
 * @param {React.ReactNode} [props.icon]          Optional small icon, top-right.
 *
 * Usage:
 *   <StatCard label="Win rate" value="63.4%" color="success" sub="142 trades" />
 */
// CSS vars resolve at paint time — accents follow the active theme
const COLORS = {
  accent: 'var(--color-accent)',
  info: 'var(--color-info)',
  success: 'var(--color-success)',
  danger: 'var(--color-danger)',
  purple: 'var(--color-purple)',
  neutral: 'var(--color-text)',
};

const StatCard = ({ label, value, color = 'accent', sub, icon }) => {
  const accent = COLORS[color] || COLORS.accent;

  return (
    <div
      className="terminal-card relative p-3 border-l-2 transition-all duration-300 hover:border-border-strong hover:-translate-y-px overflow-hidden"
      style={{ borderLeftColor: accent }}
    >
      <div className="flex items-start justify-between gap-2">
        <p className="text-3xs font-bold uppercase tracking-wider text-muted mb-1">{label}</p>
        {icon && <span className="text-faint shrink-0">{icon}</span>}
      </div>
      <p className="text-base font-num font-bold leading-tight" style={{ color: accent }}>
        {value}
      </p>
      {sub && <p className="text-2xs text-faint mt-1 font-num">{sub}</p>}
    </div>
  );
};

export default StatCard;
