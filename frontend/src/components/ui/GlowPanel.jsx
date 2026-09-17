/**
 * GlowPanel — terminal-card with an optional ambient glow.
 *
 * @param {object} props
 * @param {'accent'|'info'|'success'|'purple'} [props.glowColor]  Omit for no glow.
 *   Legacy color-name values (gold|cyan|green) are still accepted as aliases.
 * @param {boolean} [props.noPadding=false]
 * @param {string} [props.className]
 */
const GLOW_CLASSES = {
  accent: 'glow-panel',
  info: 'glow-panel-cyan',
  success: 'glow-panel-green',
  purple: 'glow-panel-purple',
  // Legacy aliases — remove once no call site uses them
  gold: 'glow-panel',
  cyan: 'glow-panel-cyan',
  green: 'glow-panel-green',
};

const GlowPanel = ({ children, className = '', glowColor, noPadding = false }) => (
  <div className={`terminal-card hover:border-border-strong transition-all duration-300 ${glowColor ? GLOW_CLASSES[glowColor] || '' : ''} ${noPadding ? '' : 'p-4'} ${className}`}>
    {children}
  </div>
);

export default GlowPanel;
