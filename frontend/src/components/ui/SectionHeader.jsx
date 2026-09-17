/**
 * SectionHeader — titled section divider with optional action slot.
 *
 * @param {object} props
 * @param {string} props.title
 * @param {string} [props.subtitle]
 * @param {React.ReactNode} [props.action]   Right-aligned slot (usually a <Button>).
 * @param {'accent'|'info'|'success'|'purple'|'danger'|'neutral'} [props.accentColor='neutral']
 *   Legacy color-name values (gold|cyan|green|red|white) are still accepted as aliases.
 */
const ACCENT_COLORS = {
  accent: 'text-accent',
  info: 'text-info',
  success: 'text-success',
  purple: 'text-purple',
  danger: 'text-danger',
  neutral: 'text-text',
  // Legacy aliases — remove once no call site uses them
  gold: 'text-accent',
  cyan: 'text-info',
  green: 'text-success',
  red: 'text-danger',
  white: 'text-text',
};

const BAR_COLORS = {
  accent: 'bg-accent',
  info: 'bg-info',
  success: 'bg-success',
  purple: 'bg-purple',
  danger: 'bg-danger',
  neutral: 'bg-border-strong',
  // Legacy aliases — remove once no call site uses them
  gold: 'bg-accent',
  cyan: 'bg-info',
  green: 'bg-success',
  red: 'bg-danger',
  white: 'bg-border-strong',
};

const SectionHeader = ({ title, subtitle, action, accentColor = 'neutral' }) => (
  <div className="flex items-center justify-between gap-4">
    <div className="flex items-center gap-3 min-w-0">
      <span className={`w-1 h-8 rounded-full shrink-0 ${BAR_COLORS[accentColor] || BAR_COLORS.neutral}`} />
      <div className="min-w-0">
        <h2 className={`text-sm font-bold uppercase tracking-[0.15em] truncate ${ACCENT_COLORS[accentColor] || ACCENT_COLORS.neutral}`}>
          {title}
        </h2>
        {subtitle && (
          <p className="text-2xs text-muted mt-0.5 uppercase tracking-wider truncate">{subtitle}</p>
        )}
      </div>
    </div>
    {action && <div className="shrink-0">{action}</div>}
  </div>
);

export default SectionHeader;
