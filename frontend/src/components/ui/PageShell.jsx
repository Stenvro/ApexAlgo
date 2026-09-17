/**
 * PageShell — standard page wrapper: grid background, max-width container
 * with consistent responsive padding and vertical rhythm.
 *
 * @param {object} props
 * @param {string} [props.glowColor]  Deprecated — accepted for compatibility, no longer rendered.
 * @param {React.ReactNode} props.children
 */
const PageShell = ({ children }) => (
  <div className="page-container overflow-y-auto h-full">
    <div className="relative z-10 max-w-[1400px] mx-auto px-3 sm:px-4 lg:px-6 py-5 space-y-5 fade-in">
      {children}
    </div>
  </div>
);

export default PageShell;
