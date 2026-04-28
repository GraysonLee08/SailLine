/* Common wireframe primitives + helpers, exposed on window */

const Box = ({ label, children, style, className = '', dashed, subtle, thin }) => (
    <div
        className={`frame ${dashed ? 'dashed' : ''} ${subtle ? 'subtle' : ''} ${thin ? 'thin' : ''} ${className}`}
        style={style}
    >
        {label && <span className="frame-label">{label}</span>}
        {children}
    </div>
);

const Lbl = ({ children, style, dk, className = '' }) => (
    <div className={`lbl ${dk ? 'dk' : ''} ${className}`} style={style}>{children}</div>
);

const Mono = ({ children, style, className = '' }) => (
    <span className={`mono ${className}`} style={style}>{children}</span>
);

const Hand = ({ children, style, className = '' }) => (
    <span className={`hand ${className}`} style={style}>{children}</span>
);

const Note = ({ children, style }) => (
    <span className="note" style={style}>{children}</span>
);

// floating annotation in absolute position
const Callout = ({ x, y, children, w, blue, style }) => (
    <div className={`callout ${blue ? 'blue' : ''}`} style={{ left: x, top: y, width: w, ...style }}>
        {children}
    </div>
);

// Simple curved arrow svg (start, end relative to its container box)
const Arrow = ({ from, to, w = 200, h = 80, curve = 30, color, dash }) => {
    const c = color || 'currentColor';
    const x1 = from[0], y1 = from[1], x2 = to[0], y2 = to[1];
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2 - curve;
    return (
        <svg width={w} height={h} style={{ position: 'absolute', left: 0, top: 0, overflow: 'visible' }}>
            <path
                d={`M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`}
                stroke={c}
                strokeWidth="1.5"
                fill="none"
                strokeDasharray={dash ? '4 3' : ''}
            />
            <path
                d={`M ${x2} ${y2} l -8 -3 m 8 3 l -3 -8`}
                stroke={c}
                strokeWidth="1.5"
                fill="none"
            />
        </svg>
    );
};

// data row: label : value
const Datum = ({ label, value, big, mono = true, style }) => (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', ...style }}>
        <span className="lbl">{label}</span>
        <span className={mono ? 'mono' : ''} style={{ fontWeight: 600, fontSize: big ? 18 : 13 }}>{value}</span>
    </div>
);

// chip
const Chip = ({ children, dot, solid, live, style }) => (
    <span className={`chip ${dot ? 'dot' : ''} ${solid ? 'solid' : ''} ${live ? 'live' : ''}`} style={style}>{children}</span>
);

// Btn
const Btn = ({ children, solid, ghost, lg, sm, style }) => (
    <button className={`btn ${solid ? 'solid' : ''} ${ghost ? 'ghost' : ''} ${lg ? 'lg' : ''} ${sm ? 'sm' : ''}`} style={style}>{children}</button>
);

// scribbled-line div for separators
const Scrib = ({ w = '100%', dark, style }) => (
    <div style={{ height: 1, width: w, background: dark ? 'var(--ink)' : 'var(--rule)', ...style }} />
);

// Slide chrome wrapper
const SlideShell = ({ num, title, meta, children, foot, foothint }) => (
    <div className="full">
        <div className="slide-meta">
            <div className="lhs">
                <span className="num">{num}</span>
                <span>—</span>
                <span className="title">{title}</span>
                {meta && <span style={{ color: 'var(--ink-4)' }}>· {meta}</span>}
            </div>
            <div className="rhs">SAILLINE · WIREFRAME · v1</div>
        </div>
        <div className="slide-body">{children}</div>
        <div className="slide-foot">
            <div>{foot}</div>
            <div>{foothint}</div>
        </div>
    </div>
);

// Variant switcher
const Variants = ({ id, options, children, defaultIdx = 0 }) => {
    const [i, setI] = React.useState(defaultIdx);
    return (
        <>
            <div className="variant-tabs">
                {options.map((o, idx) => (
                    <button key={idx} className={i === idx ? 'active' : ''} onClick={() => setI(idx)}>{o}</button>
                ))}
            </div>
            <div className="variant-stage">
                {React.Children.map(children, (child, idx) => (
                    <div className={`variant ${i === idx ? 'active' : ''}`}>{child}</div>
                ))}
            </div>
        </>
    );
};

// Sketchy hand-drawn rect via SVG (for accent)
const SketchRect = ({ w, h, color = 'var(--accent)', strokeW = 2 }) => (
    <svg width={w} height={h} style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
        <rect x={3} y={3} width={w - 6} height={h - 6} fill="none" stroke={color} strokeWidth={strokeW} strokeDasharray="3 4" rx="6" />
    </svg>
);

// Wave / sea sketch in svg (placeholder waterway for map)
const MapPlaceholder = ({ children, style }) => (
    <div className="map-bg" style={{ position: 'relative', overflow: 'hidden', ...style }}>
        {/* faint coastline scribble */}
        <svg width="100%" height="100%" style={{ position: 'absolute', inset: 0 }} preserveAspectRatio="none" viewBox="0 0 1200 800">
            <path d="M 0 120 Q 200 60 320 180 T 580 240 Q 700 280 760 380 T 880 540 Q 920 660 1100 720 L 1200 720 L 1200 0 L 0 0 Z"
                fill="rgba(0,0,0,0.04)" stroke="rgba(0,0,0,0.35)" strokeWidth="1.2" strokeDasharray="0" />
            <path d="M 0 760 Q 200 720 360 760 T 700 740 Q 880 720 1000 780 L 1200 800 L 0 800 Z"
                fill="rgba(0,0,0,0.06)" stroke="rgba(0,0,0,0.3)" strokeWidth="1.2" />
            {/* depth scribbles */}
            <g stroke="rgba(0,0,0,0.18)" strokeWidth="0.8" fill="none" strokeDasharray="2 4">
                <path d="M 100 300 Q 300 350 500 320 T 900 420" />
                <path d="M 80 420 Q 300 480 600 440 T 1000 540" />
                <path d="M 200 560 Q 400 600 700 560 T 1100 640" />
            </g>
            {/* lat/lon ticks */}
            <g stroke="rgba(0,0,0,0.2)" strokeWidth="0.5">
                {[150, 350, 550, 750, 950, 1150].map(x => <line key={x} x1={x} y1={0} x2={x} y2={6} />)}
                {[150, 350, 550, 750].map(y => <line key={y} x1={0} y1={y} x2={6} y2={y} />)}
            </g>
        </svg>
        {children}
    </div>
);

// Wind barb tiny
const WindBarb = ({ x, y, dir = 30, kts = 14, faint, style }) => (
    <div style={{ position: 'absolute', left: x, top: y, transform: `rotate(${dir}deg)`, opacity: faint ? 0.4 : 0.85, ...style }}>
        <svg width="34" height="34" viewBox="0 0 34 34">
            <line x1="17" y1="2" x2="17" y2="32" stroke="var(--ink)" strokeWidth="1.2" />
            <line x1="17" y1="4" x2="27" y2="0" stroke="var(--ink)" strokeWidth="1.2" />
            <line x1="17" y1="9" x2="25" y2="6" stroke="var(--ink)" strokeWidth="1.2" />
        </svg>
    </div>
);

// Isochrone bands (concentric scribble curves)
const Isochrones = ({ cx = 600, cy = 400 }) => (
    <svg style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }} width="100%" height="100%" viewBox="0 0 1200 800" preserveAspectRatio="none">
        <g stroke="rgba(0,0,0,0.55)" fill="none" strokeWidth="1.2">
            <path d={`M ${cx - 60} ${cy} Q ${cx} ${cy - 90} ${cx + 80} ${cy - 30} Q ${cx + 100} ${cy + 60} ${cx + 20} ${cy + 90}`} />
            <path d={`M ${cx - 120} ${cy + 10} Q ${cx - 30} ${cy - 160} ${cx + 140} ${cy - 60} Q ${cx + 200} ${cy + 100} ${cx + 60} ${cy + 170}`} strokeDasharray="3 4" />
            <path d={`M ${cx - 180} ${cy + 30} Q ${cx - 70} ${cy - 220} ${cx + 220} ${cy - 80} Q ${cx + 300} ${cy + 140} ${cx + 100} ${cy + 240}`} strokeDasharray="2 5" opacity="0.7" />
        </g>
    </svg>
);

// route line (start-mark-mark-finish)
const RouteLine = ({ d, dash, color = 'var(--ink)', w = 1.8 }) => (
    <svg style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }} width="100%" height="100%" viewBox="0 0 1200 800" preserveAspectRatio="none">
        <path d={d} fill="none" stroke={color} strokeWidth={w} strokeDasharray={dash ? '6 5' : ''} />
    </svg>
);

Object.assign(window, {
    Box, Lbl, Mono, Hand, Note, Callout, Arrow, Datum, Chip, Btn, Scrib,
    SlideShell, Variants, SketchRect, MapPlaceholder, WindBarb, Isochrones, RouteLine
});
