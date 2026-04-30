// Shared wireframe primitives — sketchy SVG shapes, hand annotations, frame chrome

const SketchBox = ({ w = '100%', h = 60, label, sub, scribble, dashed, style }) => (
  <div style={{
    position: 'relative',
    width: w, height: h,
    border: `1.5px ${dashed ? 'dashed' : 'solid'} var(--line${dashed ? '-soft' : ''})`,
    borderRadius: 6,
    background: scribble
      ? 'repeating-linear-gradient(-45deg, transparent 0 6px, var(--line-faint) 6px 7px)'
      : 'var(--paper)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    flexDirection: 'column', gap: 2,
    color: 'var(--ink-3)',
    ...style,
  }}>
    {label && <div className="hand-sm hand">{label}</div>}
    {sub && <div className="mono" style={{ color: 'var(--ink-3)' }}>{sub}</div>}
  </div>
);

// Hand-drawn arrow (curved, with arrowhead)
const HandArrow = ({ d, color = 'var(--accent)', label, labelPos }) => (
  <svg style={{ position: 'absolute', inset: 0, pointerEvents: 'none', overflow: 'visible' }} width="100%" height="100%">
    <defs>
      <marker id="arrow-h" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto">
        <path d="M 0 0 L 10 5 L 0 10 z" fill={color} />
      </marker>
    </defs>
    <path d={d} stroke={color} strokeWidth="1.5" fill="none" strokeLinecap="round" markerEnd="url(#arrow-h)" />
    {label && labelPos && (
      <text x={labelPos.x} y={labelPos.y} fontFamily="Caveat" fontSize="16" fill={color}>{label}</text>
    )}
  </svg>
);

// Sketchy underline path (for inline highlights)
const Squiggle = ({ w = 80, color = 'var(--accent)' }) => (
  <svg width={w} height="6" viewBox={`0 0 ${w} 6`} style={{ display: 'block' }}>
    <path d={`M 1,3 Q ${w/8},1 ${w/4},3 T ${w/2},3 T ${3*w/4},3 T ${w-1},3`}
      stroke={color} strokeWidth="1.5" fill="none" strokeLinecap="round" />
  </svg>
);

// App window chrome
const Window = ({ title, kids, height, subnav, activityPill, navStyle, currentBucket = 'Monitor', collapsed: collapsedProp }) => {
  const [collapsedState, setCollapsedState] = React.useState(collapsedProp ?? false);
  const collapsed = collapsedProp !== undefined ? collapsedProp : collapsedState;
  const onToggle = () => setCollapsedState(c => !c);
  return (
    <div className="frame" style={{ height: height || 'auto', display: 'flex', flexDirection: 'column' }}>
      <div className="frame-bar">
        <div className="frame-dot" style={{ background: '#ff5f57', borderColor: '#e0443e' }} />
        <div className="frame-dot" style={{ background: '#febc2e', borderColor: '#dea123' }} />
        <div className="frame-dot" style={{ background: '#28c840', borderColor: '#1aab29' }} />
        <div className="grow" />
        <div className="mono" style={{ color: 'var(--ink-3)' }}>grooveiq.local/dashboard</div>
        <div className="grow" />
        <div style={{ width: 50 }} />
      </div>
      {navStyle === 'top' && <TopNav current={currentBucket} activityPill={activityPill} />}
      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        {navStyle === 'side' && <SideNav current={currentBucket} activityPill={activityPill} collapsed={collapsed} onToggle={onToggle} />}
        {navStyle === 'side-icon' && <SideNav current={currentBucket} activityPill={activityPill} collapsed={true} onToggle={onToggle} />}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, overflow: 'hidden' }}>
          {subnav}
          <div style={{ flex: 1, overflow: 'hidden', position: 'relative' }}>
            {kids}
          </div>
        </div>
      </div>
    </div>
  );
};

// Nav variant: top horizontal tabs
const TopNav = ({ current, activityPill }) => {
  const items = [
    { label: 'Explore', icon: '♪' },
    { label: 'Actions', icon: '⚡' },
    { label: 'Monitor', icon: '◉' },
    { label: 'Settings', icon: '⚙' },
  ];
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 0,
      padding: '0 16px',
      borderBottom: '1.5px solid var(--line)',
      background: 'var(--paper)',
      height: 48, flexShrink: 0,
    }}>
      <div className="display" style={{ fontSize: 15, marginRight: 24, fontWeight: 700, letterSpacing: '-0.02em' }}>
        groove<span style={{ color: 'var(--accent)' }}>iq</span>
      </div>
      <div style={{ display: 'flex', height: '100%' }}>
        {items.map(it => (
          <div key={it.label} style={{
            display: 'flex', alignItems: 'center', gap: 6,
            padding: '0 18px', height: '100%',
            borderBottom: it.label === current ? '2px solid var(--accent)' : '2px solid transparent',
            color: it.label === current ? 'var(--ink)' : 'var(--ink-3)',
            fontWeight: it.label === current ? 600 : 400,
            fontSize: 13,
            cursor: 'pointer',
          }}>
            <span style={{ fontSize: 11 }}>{it.icon}</span> {it.label}
          </div>
        ))}
      </div>
      <div className="grow" />
      {activityPill}
      <div className="chip ghost" style={{ marginLeft: 8 }}>⌘K</div>
      <div className="chip" style={{ marginLeft: 6, padding: '2px 8px' }}>👤 admin</div>
    </div>
  );
};

// Nav variant: side rail — collapsible (merges B+C from the first pass)
const SideNav = ({ current, activityPill, collapsed, onToggle }) => {
  const items = [
    { label: 'Explore', icon: '♪', count: 9 },
    { label: 'Actions', icon: '⚡', count: 5 },
    { label: 'Monitor', icon: '◉', count: 11 },
    { label: 'Settings', icon: '⚙', count: 6 },
  ];
  const w = collapsed ? 56 : 184;
  return (
    <div style={{
      width: w, flexShrink: 0,
      borderRight: '1.5px solid var(--line-soft)',
      background: 'var(--paper)',
      display: 'flex', flexDirection: 'column',
      padding: collapsed ? '12px 8px' : '14px 10px',
      transition: 'width .2s ease',
    }}>
      <div className="row between" style={{ marginBottom: 12, padding: collapsed ? 0 : '0 6px', justifyContent: collapsed ? 'center' : 'space-between' }}>
        {!collapsed && (
          <div className="display" style={{ fontSize: 16, fontWeight: 700, letterSpacing: '-0.02em' }}>
            groove<span style={{ color: 'var(--accent)' }}>iq</span>
          </div>
        )}
        {collapsed && (
          <div className="display" style={{ fontSize: 16, fontWeight: 700, color: 'var(--accent)' }}>g</div>
        )}
        {!collapsed && (
          <span onClick={onToggle} style={{ cursor: 'pointer', color: 'var(--ink-3)', fontSize: 12, padding: 4 }}>«</span>
        )}
      </div>
      {collapsed && (
        <div onClick={onToggle} title="Expand" style={{
          width: 38, height: 28, alignSelf: 'center',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: 'var(--ink-3)', fontSize: 11, cursor: 'pointer',
          borderRadius: 6, marginBottom: 4,
        }}>»</div>
      )}
      {items.map(it => {
        const active = it.label === current;
        return (
          <div key={it.label} title={collapsed ? it.label : undefined} style={{
            display: 'flex', alignItems: 'center', gap: 10,
            padding: collapsed ? '0' : '8px 10px',
            height: collapsed ? 38 : 'auto',
            justifyContent: collapsed ? 'center' : 'flex-start',
            borderRadius: 7,
            background: active ? 'var(--accent-soft)' : 'transparent',
            color: active ? 'var(--accent)' : 'var(--ink-2)',
            fontWeight: active ? 600 : 400,
            fontSize: 13,
            cursor: 'pointer',
            marginBottom: 2,
            position: 'relative',
          }}>
            {active && <span style={{ position: 'absolute', left: -10, top: 8, bottom: 8, width: 2, borderRadius: 2, background: 'var(--accent)' }} />}
            <span style={{ width: 14, fontSize: 12, textAlign: 'center' }}>{it.icon}</span>
            {!collapsed && <span className="grow">{it.label}</span>}
            {!collapsed && <span className="mono" style={{ fontSize: 10, color: 'var(--ink-3)' }}>{it.count}</span>}
          </div>
        );
      })}
      <div className="grow" />
      {activityPill && <div style={{ marginBottom: 8, display: 'flex', justifyContent: collapsed ? 'center' : 'flex-start' }}>{activityPill}</div>}
      {!collapsed && <div className="chip ghost" style={{ alignSelf: 'flex-start', fontSize: 11 }}>⌘K Search</div>}
      {collapsed && <div className="chip ghost" style={{ alignSelf: 'center', fontSize: 11, padding: '2px 6px' }}>⌘K</div>}
    </div>
  );
};

// Backwards-compat: SideNavIcon = collapsed SideNav
const SideNavIcon = (props) => <SideNav {...props} collapsed={true} />;

// Subnav variants
const SubNavTabs = ({ items, current }) => (
  <div style={{
    display: 'flex', alignItems: 'center', gap: 0,
    padding: '0 16px',
    borderBottom: '1.5px solid var(--line-soft)',
    background: 'var(--paper)',
    height: 38, flexShrink: 0,
    overflowX: 'auto',
  }}>
    {items.map(it => (
      <div key={it} style={{
        padding: '0 12px', height: '100%',
        display: 'flex', alignItems: 'center',
        borderBottom: it === current ? '2px solid var(--ink)' : '2px solid transparent',
        color: it === current ? 'var(--ink)' : 'var(--ink-3)',
        fontWeight: it === current ? 600 : 400,
        fontSize: 12,
        whiteSpace: 'nowrap',
        cursor: 'pointer',
      }}>{it}</div>
    ))}
  </div>
);

const SubNavSide = ({ items, current, title }) => (
  <div style={{
    width: 180, flexShrink: 0,
    borderRight: '1.5px solid var(--line-soft)',
    background: 'var(--paper)',
    padding: 12,
  }}>
    <div className="eyebrow" style={{ marginBottom: 10 }}>{title}</div>
    {items.map(it => (
      <div key={it} style={{
        padding: '6px 10px',
        borderRadius: 5,
        background: it === current ? 'var(--ink)' : 'transparent',
        color: it === current ? 'var(--paper)' : 'var(--ink-2)',
        fontWeight: it === current ? 500 : 400,
        fontSize: 12,
        cursor: 'pointer',
        marginBottom: 1,
      }}>{it}</div>
    ))}
  </div>
);

const SubNavBreadcrumb = ({ trail, current, switcher }) => (
  <div style={{
    display: 'flex', alignItems: 'center', gap: 8,
    padding: '10px 16px',
    borderBottom: '1.5px solid var(--line-soft)',
    background: 'var(--paper)',
    height: 42, flexShrink: 0,
    fontSize: 12,
  }}>
    {trail.map((t, i) => (
      <React.Fragment key={i}>
        <span style={{ color: 'var(--ink-3)' }}>{t}</span>
        {i < trail.length - 1 && <span style={{ color: 'var(--ink-4)' }}>›</span>}
      </React.Fragment>
    ))}
    <div className="row gap-3" style={{
      padding: '3px 8px', border: '1px solid var(--line-soft)', borderRadius: 5,
      cursor: 'pointer',
    }}>
      <span style={{ fontWeight: 600 }}>{current}</span>
      <span style={{ color: 'var(--ink-3)' }}>▾</span>
    </div>
  </div>
);

// Activity pill — collapsed and expanded
const ActivityPill = ({ expanded, onToggle, compact }) => {
  if (!expanded) {
    return (
      <div onClick={onToggle} style={{
        display: 'inline-flex', alignItems: 'center', gap: 6,
        padding: '4px 10px',
        border: '1.25px solid var(--line)',
        borderRadius: 999,
        cursor: 'pointer',
        fontSize: 12,
        background: 'var(--paper)',
        whiteSpace: 'nowrap',
      }}>
        <span className="dot live" />
        {compact ? '3 active' : <span>3 active <span style={{ color: 'var(--ink-3)' }}>· pipeline · 2 dl</span></span>}
        <span style={{ color: 'var(--ink-3)' }}>▾</span>
      </div>
    );
  }
  return (
    <div style={{
      position: 'absolute', top: 'calc(100% + 6px)', right: 0,
      width: 320,
      background: 'var(--paper)',
      border: '1.5px solid var(--line)',
      borderRadius: 8,
      boxShadow: '0 8px 24px rgba(0,0,0,0.08)',
      zIndex: 100,
      overflow: 'hidden',
    }}>
      <div style={{ padding: '10px 12px', borderBottom: '1.5px solid var(--line-soft)' }} className="row between">
        <div className="row gap-3"><span className="dot live" /><span style={{ fontSize: 12, fontWeight: 600 }}>Activity</span></div>
        <span onClick={onToggle} style={{ cursor: 'pointer', color: 'var(--ink-3)', fontSize: 11 }}>close ✕</span>
      </div>
      <ActivityRow icon="◉" label="Pipeline run" sub="step 4 of 10 · scoring" badge="LIVE" cta="View →" />
      <ActivityRow icon="↓" label="Downloads" sub="2 in flight · 18 queued" cta="View →" />
      <ActivityRow icon="⚙" label="Library scan" sub="phase 2/3 · 14,302 / ~22k" badge="LIVE" cta="View →" />
      <div style={{ padding: '8px 12px', fontSize: 11, color: 'var(--ink-3)', borderTop: '1.5px solid var(--line-soft)' }}>
        Last build · 2h ago &nbsp;·&nbsp; SSE connected
      </div>
    </div>
  );
};
const ActivityRow = ({ icon, label, sub, badge, cta }) => (
  <div className="row" style={{ padding: '10px 12px', gap: 10, borderBottom: '1px solid var(--line-faint)' }}>
    <div style={{ width: 22, height: 22, borderRadius: 5, background: 'var(--accent-soft)', color: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 11, flexShrink: 0 }}>{icon}</div>
    <div className="grow" style={{ minWidth: 0 }}>
      <div className="row gap-3" style={{ fontSize: 12, fontWeight: 600 }}>
        {label}
        {badge && <span className="chip" style={{ padding: '0 5px', fontSize: 9, background: 'var(--accent)', color: 'white', borderColor: 'var(--accent)' }}>{badge}</span>}
      </div>
      <div style={{ fontSize: 11, color: 'var(--ink-3)' }}>{sub}</div>
    </div>
    <div style={{ fontSize: 11, color: 'var(--accent)', fontWeight: 500, cursor: 'pointer' }}>{cta}</div>
  </div>
);

// Cross-link "jump" pill
const JumpLink = ({ to, label, dim }) => (
  <div className="row gap-3" style={{
    fontSize: 11,
    color: dim ? 'var(--ink-3)' : 'var(--accent)',
    fontWeight: 500,
    cursor: 'pointer',
    border: '1.25px dashed var(--line-soft)',
    borderRadius: 999,
    padding: '2px 9px',
    whiteSpace: 'nowrap',
    background: 'var(--paper)',
  }}>
    <span style={{ fontSize: 9, color: 'var(--ink-4)' }}>{to}</span>
    <span>{label}</span>
    <span>→</span>
  </div>
);

// Stat tile — clean
const Stat = ({ label, value, delta, deltaKind, mono }) => (
  <div style={{
    border: '1.5px solid var(--line)',
    borderRadius: 8,
    padding: 14,
    background: 'var(--paper)',
    display: 'flex', flexDirection: 'column', gap: 4,
    minWidth: 0,
  }}>
    <div className="eyebrow">{label}</div>
    <div className={mono ? 'mono' : 'display'} style={{ fontSize: mono ? 18 : 24, fontWeight: 600, letterSpacing: '-0.02em' }}>{value}</div>
    {delta && (
      <div className="row gap-3" style={{ fontSize: 11, color: deltaKind === 'good' ? 'var(--good)' : deltaKind === 'bad' ? 'var(--bad)' : 'var(--ink-3)' }}>
        {deltaKind === 'good' && '↑'}
        {deltaKind === 'bad' && '↓'}
        {delta}
      </div>
    )}
  </div>
);

// Spark / mini chart (sketchy)
const SparkLine = ({ w = 200, h = 40, points = 24, peak = 0.8 }) => {
  // generate pseudo-random sparkline
  const seed = w + h + points;
  const rand = (i) => {
    const x = Math.sin(seed * 9301 + i * 49297) * 233280;
    return x - Math.floor(x);
  };
  const pts = Array.from({ length: points }, (_, i) => {
    const x = (i / (points - 1)) * w;
    const y = h - (0.2 + rand(i) * peak) * h * 0.85;
    return [x, y];
  });
  const path = pts.map(([x, y], i) => `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`).join(' ');
  const fill = `${path} L ${w} ${h} L 0 ${h} Z`;
  return (
    <svg width={w} height={h} style={{ display: 'block' }}>
      <path d={fill} className="spark-fill" fill="var(--line-faint)" />
      <path d={path} fill="none" stroke="var(--ink)" strokeWidth="1.5" />
    </svg>
  );
};

// Bar chart sketchy
const BarChart = ({ w = 240, h = 80, bars = 12, accentIdx = -1 }) => {
  const seed = w + h + bars;
  const rand = (i) => {
    const x = Math.sin(seed * 9301 + i * 49297) * 233280;
    return x - Math.floor(x);
  };
  const bw = (w - (bars - 1) * 3) / bars;
  return (
    <svg width={w} height={h} style={{ display: 'block' }}>
      {Array.from({ length: bars }, (_, i) => {
        const bh = (0.2 + rand(i) * 0.8) * h;
        return <rect key={i} x={i * (bw + 3)} y={h - bh} width={bw} height={bh}
          fill={i === accentIdx ? 'var(--accent)' : 'var(--ink-2)'}
          rx={1} />;
      })}
    </svg>
  );
};

// Section header on the design canvas
const CanvasHeader = ({ kicker, title, sub }) => (
  <div style={{ marginBottom: 18 }}>
    {kicker && <div className="eyebrow" style={{ marginBottom: 6 }}>{kicker}</div>}
    <div className="canvas-section-title">{title}</div>
    {sub && <div className="canvas-section-sub" style={{ marginTop: 4 }}>{sub}</div>}
  </div>
);

const ArtboardCaption = ({ num, title, sub }) => (
  <div className="artboard-caption">
    {num && <span className="num">{num}</span>}
    {title}
    {sub && <span style={{ color: 'var(--ink-3)', fontWeight: 400, marginLeft: 6 }}>· {sub}</span>}
  </div>
);

// Margin note (hand-drawn annotation with arrow)
const MarginNote = ({ text, arrow, style }) => (
  <div style={{ position: 'absolute', ...style }}>
    <div className="note">{text}</div>
    {arrow && (
      <svg width="60" height="40" style={{ position: 'absolute', ...arrow.pos, overflow: 'visible' }}>
        <defs>
          <marker id="mn-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--accent)" />
          </marker>
        </defs>
        <path d={arrow.d} className="note-line" markerEnd="url(#mn-arrow)" />
      </svg>
    )}
  </div>
);

Object.assign(window, {
  SketchBox, HandArrow, Squiggle, Window,
  TopNav, SideNav, SideNavIcon,
  SubNavTabs, SubNavSide, SubNavBreadcrumb,
  ActivityPill, ActivityRow, JumpLink,
  Stat, SparkLine, BarChart,
  CanvasHeader, ArtboardCaption, MarginNote,
});
