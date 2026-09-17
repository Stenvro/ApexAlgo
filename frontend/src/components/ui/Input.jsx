/**
 * Input / Select / Textarea — form primitives with label + error state.
 *
 * <Input>
 * @param {object} props
 * @param {string} [props.label]      Small uppercase label above the field.
 * @param {string} [props.error]      Error message; turns border red.
 * @param {string} [props.hint]       Muted helper text below the field.
 * @param {boolean} [props.mono]      Render value in JetBrains Mono (keys, numbers).
 * @param {string} [props.className]  Extra classes on the <input>.
 * ...rest forwarded to <input> (type, value, onChange, placeholder, …)
 *
 * <Select> — same props; pass <option> elements as children.
 * <Textarea> — same props.
 *
 * Usage:
 *   <Input label="Bot name" value={name} onChange={e => setName(e.target.value)} />
 *   <Select label="Exchange" value={ex} onChange={...}><option>okx</option></Select>
 */
const fieldBase =
  'w-full bg-inset border rounded-md px-3 py-2 text-xs text-text placeholder-faint outline-none transition-colors duration-200 disabled:opacity-50 disabled:cursor-not-allowed';

const borderFor = (error) =>
  error
    ? 'border-danger/60 focus:border-danger'
    : 'border-border hover:border-border-strong focus:border-accent/70';

const FieldWrap = ({ label, error, hint, children }) => (
  <label className="block w-full text-left">
    {label && (
      <span className="block text-2xs font-bold uppercase tracking-wider text-muted mb-1.5">
        {label}
      </span>
    )}
    {children}
    {error && <span className="block text-xs text-danger mt-1.5">{error}</span>}
    {!error && hint && <span className="block text-xs text-faint mt-1.5">{hint}</span>}
  </label>
);

export const Input = ({ label, error, hint, mono = false, className = '', ...rest }) => (
  <FieldWrap label={label} error={error} hint={hint}>
    <input
      className={`${fieldBase} ${borderFor(error)} ${mono ? 'font-num' : ''} ${className}`}
      {...rest}
    />
  </FieldWrap>
);

export const Select = ({ label, error, hint, className = '', children, ...rest }) => (
  <FieldWrap label={label} error={error} hint={hint}>
    <div className="relative">
      <select
        className={`${fieldBase} ${borderFor(error)} appearance-none pr-9 cursor-pointer ${className}`}
        {...rest}
      >
        {children}
      </select>
      <svg
        className="w-3.5 h-3.5 text-muted absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none"
        fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"
      >
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
      </svg>
    </div>
  </FieldWrap>
);

export const Textarea = ({ label, error, hint, mono = false, className = '', rows = 4, ...rest }) => (
  <FieldWrap label={label} error={error} hint={hint}>
    <textarea
      rows={rows}
      className={`${fieldBase} ${borderFor(error)} resize-y ${mono ? 'font-num' : ''} ${className}`}
      {...rest}
    />
  </FieldWrap>
);

export default Input;
