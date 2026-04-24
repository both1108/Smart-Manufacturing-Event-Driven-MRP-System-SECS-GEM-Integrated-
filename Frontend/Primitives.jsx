// Shared primitives for the SECS/GEM UI kit.
// Exposed on window so all Babel <script> files can use them without imports.

const { useState, useEffect, useRef, useMemo } = React;

// ---------- Icon (Lucide via CDN global) ---------------------------------
function Icon({ name, size = 14, color, style, ...rest }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current && window.lucide) {
      ref.current.innerHTML = '';
      const el = document.createElement('i');
      el.setAttribute('data-lucide', name);
      ref.current.appendChild(el);
      window.lucide.createIcons({ attrs: { width: size, height: size, 'stroke-width': 1.5 } });
    }
  }, [name, size]);
  return <span ref={ref} style={{ display: 'inline-flex', color, lineHeight: 0, ...style }} {...rest} />;
}

// ---------- StatusChip ---------------------------------------------------
const STATE_MAP = {
  RUN:         { color: '#5ee38f', fill: 'rgba(94,227,143,0.14)' },
  IDLE:        { color: '#ffc048', fill: 'rgba(255,192,72,0.14)' },
  ALARM:       { color: '#ff5470', fill: 'rgba(255,84,112,0.20)' },
  DOWN:        { color: '#6c7a92', fill: 'rgba(108,122,146,0.15)' },
  OFFLINE:     { color: '#4a5670', fill: 'rgba(74,86,112,0.18)' },
  SETUP:       { color: '#4a9eff', fill: 'rgba(74,158,255,0.12)' },
  PAUSED:      { color: '#c27bff', fill: 'rgba(194,123,255,0.14)' },
  MAINTENANCE: { color: '#26c6da', fill: 'rgba(38,198,218,0.14)' },
  'E-STOP':    { color: '#ff2d4a', fill: 'rgba(255,45,74,0.28)' },
  CRITICAL:    { color: '#ff2d4a', fill: 'rgba(255,45,74,0.32)' },
  MAJOR:       { color: '#ffc048', fill: 'rgba(255,192,72,0.16)' },
  MINOR:       { color: '#8fb7ff', fill: 'rgba(143,183,255,0.12)' },
  OK:          { color: '#5ee38f', fill: 'rgba(94,227,143,0.14)' },
};
function StatusChip({ state, dot = false, children }) {
  const s = STATE_MAP[state] || STATE_MAP.DOWN;
  const isCrit = state === 'CRITICAL' || state === 'E-STOP';
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      padding: '2px 8px', borderRadius: 20,
      background: s.fill,
      border: `1px solid ${s.color}`,
      color: isCrit ? '#fff' : s.color,
      fontSize: 10, fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase',
      fontFamily: 'Inter, sans-serif', whiteSpace: 'nowrap',
      boxShadow: isCrit ? `0 0 0 1px ${s.color}, 0 0 8px ${s.color}44` : 'none',
    }}>
      {dot && <span style={{ width: 6, height: 6, borderRadius: '50%', background: s.color }} />}
      {children || state}
    </span>
  );
}

// ---------- Card ---------------------------------------------------------
function Card({ title, meta, right, children, padding = 14, style, raised }) {
  return (
    <div style={{
      background: raised ? '#12315a' : '#0f1b33',
      border: '1px solid #17375f',
      borderRadius: 10,
      padding,
      ...style,
    }}>
      {(title || right) && (
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10, gap: 10 }}>
          <div>
            {title && <h3 style={{ margin: 0, fontSize: 13, fontWeight: 600, color: '#eaf2ff', letterSpacing: '0.02em' }}>{title}</h3>}
            {meta && <div style={{ fontSize: 10, color: '#5a7aa8', marginTop: 2, fontFamily: 'JetBrains Mono, monospace', letterSpacing: '0.04em' }}>{meta}</div>}
          </div>
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

// ---------- Button -------------------------------------------------------
function Button({ variant = 'default', size = 'md', onClick, children, disabled, icon, style }) {
  const variants = {
    default: { bg: '#102c57', border: '#1f4f8a', color: '#eaf2ff', hover: '#1a3f75' },
    go:      { bg: 'rgba(88,214,141,0.1)', border: '#58d68d', color: '#58d68d', hover: 'rgba(88,214,141,0.22)' },
    stop:    { bg: 'rgba(255,107,107,0.1)', border: '#ff6b6b', color: '#ff6b6b', hover: 'rgba(255,107,107,0.22)' },
    ghost:   { bg: 'transparent', border: '#1f4f8a', color: '#8fb7ff', hover: '#102c57' },
  };
  const v = variants[variant];
  const [hover, setHover] = useState(false);
  const pad = size === 'sm' ? '3px 8px' : size === 'lg' ? '8px 18px' : '5px 12px';
  const fs = size === 'sm' ? 10 : size === 'lg' ? 13 : 12;
  const isCommand = variant === 'go' || variant === 'stop';
  return (
    <button
      onClick={disabled ? undefined : onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      disabled={disabled}
      style={{
        background: hover && !disabled ? v.hover : v.bg,
        border: `1px solid ${v.border}`,
        color: v.color,
        padding: pad,
        fontSize: fs,
        fontFamily: 'Inter, sans-serif',
        fontWeight: isCommand ? 600 : 500,
        letterSpacing: isCommand ? '0.08em' : '0.02em',
        textTransform: isCommand ? 'uppercase' : 'none',
        borderRadius: 6,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.4 : 1,
        transition: 'background 0.15s ease-out',
        display: 'inline-flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap',
        ...style,
      }}
    >
      {icon && <Icon name={icon} size={fs + 2} color={v.color} />}
      {children}
    </button>
  );
}

// ---------- Input / Select ----------------------------------------------
function Input({ label, value, onChange, placeholder, style }) {
  const [focused, setFocused] = useState(false);
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 6, ...style }}>
      {label && <span style={{ fontSize: 11, color: '#8fb7ff', letterSpacing: '0.08em', textTransform: 'uppercase', fontWeight: 500 }}>{label}</span>}
      <input
        value={value} onChange={onChange} placeholder={placeholder}
        onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
        style={{
          background: '#102c57',
          border: `1px solid ${focused ? '#4a9eff' : '#1f4f8a'}`,
          color: '#eaf2ff', borderRadius: 6, padding: '7px 10px',
          fontSize: 13, outline: 'none', fontFamily: 'Inter, sans-serif',
          transition: 'border-color 0.15s',
        }}
      />
    </label>
  );
}

function Select({ label, value, options, onChange, style }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 6, ...style }}>
      {label && <span style={{ fontSize: 11, color: '#8fb7ff', letterSpacing: '0.08em', textTransform: 'uppercase', fontWeight: 500 }}>{label}</span>}
      <select value={value} onChange={onChange} style={{
        background: '#102c57', border: '1px solid #1f4f8a', color: '#eaf2ff',
        borderRadius: 6, padding: '7px 10px', fontSize: 13, outline: 'none', fontFamily: 'Inter, sans-serif',
      }}>
        {options.map(o => <option key={o.value ?? o} value={o.value ?? o}>{o.label ?? o}</option>)}
      </select>
    </label>
  );
}

// ---------- LED (pulsing status dot) -------------------------------------
function LED({ color = '#58d68d', pulse = false, size = 8 }) {
  return (
    <span style={{
      display: 'inline-block', width: size, height: size, borderRadius: '50%',
      background: color,
      boxShadow: `0 0 0 2px ${color}22`,
      animation: pulse ? 'secsPulse 1.2s infinite' : 'none',
    }} />
  );
}

// ---------- Sparkline SVG -----------------------------------------------
function Sparkline({ values, color = '#4a9eff', width = 120, height = 32, filled = true }) {
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const pts = values.map((v, i) => [
    (i / (values.length - 1)) * width,
    height - ((v - min) / range) * (height - 4) - 2,
  ]);
  const path = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  const area = `${path} L${width},${height} L0,${height} Z`;
  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      {filled && <path d={area} fill={color} opacity="0.15" />}
      <path d={path} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  );
}

// ---------- Page layout --------------------------------------------------
function Shell({ children }) {
  return (
    <div style={{ display: 'flex', minHeight: '100vh', background: '#081224', color: '#eaf2ff' }}>
      {children}
    </div>
  );
}

function Sidebar({ active, onNav }) {
  const items = [
    { id: 'dashboard', label: 'Dashboard', icon: 'layout-grid' },
    { id: 'monitor',   label: 'Monitor',   icon: 'activity' },
    { id: 'alarms',    label: 'Alarms',    icon: 'alert-triangle' },
    { id: 'events',    label: 'Events',    icon: 'list' },
    { id: 'control',   label: 'Control',   icon: 'sliders-horizontal' },
  ];
  return (
    <aside style={{
      width: 188, flex: '0 0 188px',
      background: '#0a1830', borderRight: '1px solid #17375f',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ padding: '14px 14px', borderBottom: '1px solid #17375f', display: 'flex', alignItems: 'center', gap: 9 }}>
        <svg viewBox="0 0 64 64" width="22" height="22" fill="none" stroke="#eaf2ff" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="32" cy="32" r="22"/>
          <path d="M32 10 L28 14 L36 14 Z" fill="#eaf2ff" stroke="none"/>
          <line x1="32" y1="18" x2="32" y2="46"/>
          <line x1="18" y1="32" x2="46" y2="32"/>
          <circle cx="32" cy="32" r="3" fill="#eaf2ff" stroke="none"/>
        </svg>
        <div>
          <div style={{ fontSize: 12, fontWeight: 600, color: '#eaf2ff' }}>SECS/GEM</div>
          <div style={{ fontSize: 9, color: '#8fb7ff', letterSpacing: '0.14em', fontWeight: 500 }}>MONITOR</div>
        </div>
      </div>
      <nav style={{ padding: 6, flex: 1 }}>
        {items.map(it => {
          const isActive = active === it.id;
          return (
            <button key={it.id} onClick={() => onNav(it.id)} style={{
              display: 'flex', alignItems: 'center', gap: 9,
              width: '100%', padding: '7px 10px', marginBottom: 1,
              background: isActive ? 'rgba(74,158,255,0.10)' : 'transparent',
              color: isActive ? '#eaf2ff' : '#8fb7ff',
              border: 'none', borderLeft: `2px solid ${isActive ? '#4a9eff' : 'transparent'}`,
              borderRadius: isActive ? '0 4px 4px 0' : 4,
              fontFamily: 'Inter, sans-serif', fontSize: 12, fontWeight: 500,
              cursor: 'pointer', textAlign: 'left', transition: 'background 0.15s, color 0.15s',
            }}>
              <Icon name={it.icon} size={14} color={isActive ? '#4a9eff' : '#8fb7ff'} />
              {it.label}
            </button>
          );
        })}
      </nav>
      <div style={{ padding: 10, borderTop: '1px solid #17375f', fontSize: 10, color: '#5a7aa8', fontFamily: 'JetBrains Mono, monospace', lineHeight: 1.6 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <LED color="#5ee38f" pulse /> Host connected
        </div>
        <div>GEM v1.0.3 · E5/E30</div>
      </div>
    </aside>
  );
}

function TopBar({ title, lang, onLang, clock }) {
  return (
    <header style={{
      padding: '10px 18px',
      background: 'linear-gradient(90deg, #0c1f3f, #102c57)',
      borderBottom: '1px solid #1f4f8a',
      display: 'flex', alignItems: 'center', gap: 16, height: 56, boxSizing: 'border-box',
    }}>
      <div>
        <h1 style={{ margin: 0, fontSize: 15, fontWeight: 600, letterSpacing: '-0.01em' }}>{title}</h1>
        <div style={{ fontSize: 10, color: '#8fb7ff', marginTop: 2, fontFamily: 'JetBrains Mono, monospace' }}>
          Last updated · <span style={{ color: '#eaf2ff' }}>{clock}</span>
        </div>
      </div>
      <div style={{ flex: 1 }} />
      <div style={{ display: 'flex', gap: 6 }}>
        {['EN', '中文'].map(l => (
          <button key={l} onClick={() => onLang(l)} style={{
            background: lang === l ? '#1a3f75' : '#102c57',
            border: '1px solid #1f4f8a', color: '#eaf2ff',
            padding: '4px 12px', borderRadius: 5, cursor: 'pointer',
            fontSize: 11, fontFamily: 'Inter, sans-serif', fontWeight: 500,
          }}>{l}</button>
        ))}
      </div>
    </header>
  );
}

Object.assign(window, {
  Icon, StatusChip, Card, Button, Input, Select, LED, Sparkline,
  Shell, Sidebar, TopBar, STATE_MAP,
});
