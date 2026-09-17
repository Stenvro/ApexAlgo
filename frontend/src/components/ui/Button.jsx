/**
 * Button — the only way to render a button in ApexAlgo views.
 *
 * @param {object} props
 * @param {'primary'|'secondary'|'ghost'|'danger'|'success'} [props.variant='primary']
 * @param {'sm'|'md'|'lg'} [props.size='md']
 * @param {boolean} [props.loading=false]  Shows spinner, disables the button.
 * @param {boolean} [props.disabled=false]
 * @param {boolean} [props.fullWidth=false]
 * @param {React.ReactNode} [props.icon]   Optional leading icon (small SVG).
 * @param {string} [props.className]
 * @param {'button'|'submit'} [props.type='button']
 *
 * Usage:
 *   <Button onClick={save} loading={saving}>Save</Button>
 *   <Button variant="danger" size="sm" onClick={remove}>Delete</Button>
 */
const VARIANTS = {
  primary:
    'bg-accent-fill text-accent-ink hover:bg-accent-fill-hover shadow-glow-accent-sm hover:shadow-glow-accent border border-transparent',
  secondary:
    'bg-raised text-text border border-border hover:border-border-strong hover:bg-overlay',
  ghost:
    'bg-transparent text-muted border border-transparent hover:text-text hover:bg-raised',
  danger:
    'bg-danger/10 text-danger border border-danger/40 hover:bg-danger hover:text-danger-ink hover:shadow-glow-danger',
  success:
    'bg-success/10 text-success border border-success/40 hover:bg-success hover:text-success-ink hover:shadow-glow-success',
};

const SIZES = {
  sm: 'px-3 py-1.5 text-2xs gap-1.5',
  md: 'px-4 py-2 text-xs gap-2',
  lg: 'px-6 py-3 text-xs gap-2',
};

const Spinner = ({ className = 'w-3.5 h-3.5' }) => (
  <svg className={`spin ${className}`} viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
    <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
  </svg>
);

const Button = ({
  variant = 'primary',
  size = 'md',
  loading = false,
  disabled = false,
  fullWidth = false,
  icon = null,
  className = '',
  type = 'button',
  children,
  ...rest
}) => (
  <button
    type={type}
    disabled={disabled || loading}
    className={`inline-flex items-center justify-center font-bold uppercase tracking-wider rounded-md transition-all duration-200 select-none disabled:opacity-45 disabled:cursor-not-allowed disabled:shadow-none ${VARIANTS[variant] || VARIANTS.primary} ${SIZES[size] || SIZES.md} ${fullWidth ? 'w-full' : ''} ${className}`}
    {...rest}
  >
    {loading ? <Spinner /> : icon}
    {children}
  </button>
);

export { Spinner };
export default Button;
