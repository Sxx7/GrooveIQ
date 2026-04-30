// Realistic mockup — Monitor → Overview rendered at full fidelity
// Exact palette: #a887ce, #7b6e7f, #9c526d, #4d3e50, #292631, #171821

const Realistic = () => {
  const [pillOpen, setPillOpen] = React.useState(false);
  const [collapsed, setCollapsed] = React.useState(false);

  const navItems = [
    { label: 'Explore', icon: '♪', count: 9 },
    { label: 'Actions', icon: '⚡', count: 5 },
    { label: 'Monitor', icon: '◉', count: 11, active: true },
    { label: 'Settings', icon: '⚙', count: 6 },
  ];
  const subNav = ['Overview', 'Pipeline', 'Models', 'System Health', 'Recs Debug', 'User Diagnostics', 'Integrations', 'Downloads', 'Lidarr Backfill', 'Discovery', 'Charts'];

  return (
    <div style={{
      width: '100%', height: '100%',
      background: '#171821',
      color: '#ece8f2',
      display: 'flex',
      fontFamily: 'Inter, system-ui, sans-serif',
      fontSize: 13,
      overflow: 'hidden',
    }}>
      {/* SIDEBAR */}
      <div style={{
        width: collapsed ? 60 : 220,
        flexShrink: 0,
        background: '#292631',
        borderRight: '1px solid rgba(236,232,242,0.06)',
        display: 'flex', flexDirection: 'column',
        transition: 'width .2s ease',
        padding: collapsed ? '14px 8px' : '16px 12px',
      }}>
        {/* Logo + collapse */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 18, padding: collapsed ? 0 : '0 4px' }}>
          {!collapsed ? (
            <div style={{ fontFamily: 'Inter Tight, sans-serif', fontSize: 17, fontWeight: 700, letterSpacing: '-0.02em' }}>
              groove<span style={{ color: '#a887ce' }}>iq</span>
            </div>
          ) : (
            <div style={{ width: '100%', textAlign: 'center', fontSize: 17, fontWeight: 700, color: '#a887ce' }}>g</div>
          )}
          {!collapsed && (
            <div onClick={() => setCollapsed(true)} style={{
              cursor: 'pointer', color: '#7b6e7f', fontSize: 12, padding: 4,
              borderRadius: 4, lineHeight: 1,
            }}>«</div>
          )}
        </div>
        {collapsed && (
          <div onClick={() => setCollapsed(false)} style={{
            width: '100%', height: 28,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: '#7b6e7f', fontSize: 12, cursor: 'pointer',
            marginBottom: 4,
          }}>»</div>
        )}

        {/* Nav items */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          {navItems.map(it => (
            <div key={it.label} title={collapsed ? it.label : undefined} style={{
              display: 'flex', alignItems: 'center', gap: 11,
              padding: collapsed ? 0 : '9px 11px',
              height: collapsed ? 40 : 'auto',
              justifyContent: collapsed ? 'center' : 'flex-start',
              borderRadius: 8,
              background: it.active ? 'rgba(168,135,206,0.14)' : 'transparent',
              color: it.active ? '#a887ce' : '#b8b0c4',
              fontWeight: it.active ? 600 : 450,
              fontSize: 13,
              cursor: 'pointer',
              position: 'relative',
            }}>
              {it.active && <span style={{ position: 'absolute', left: -12, top: 9, bottom: 9, width: 2, borderRadius: 2, background: '#a887ce' }} />}
              <span style={{ width: 14, fontSize: 13, textAlign: 'center', flexShrink: 0 }}>{it.icon}</span>
              {!collapsed && <span style={{ flex: 1 }}>{it.label}</span>}
              {!collapsed && (
                <span style={{
                  fontFamily: 'JetBrains Mono, ui-monospace, monospace',
                  fontSize: 10, color: it.active ? '#a887ce' : '#7b6e7f',
                  background: it.active ? 'rgba(168,135,206,0.14)' : 'rgba(236,232,242,0.05)',
                  padding: '1px 6px', borderRadius: 999,
                }}>{it.count}</span>
              )}
            </div>
          ))}
        </div>

        <div style={{ flex: 1 }} />

        {/* Activity pill — sidebar variant */}
        <div style={{
          marginBottom: 10,
          background: 'rgba(168,135,206,0.10)',
          border: '1px solid rgba(168,135,206,0.30)',
          borderRadius: collapsed ? 8 : 10,
          padding: collapsed ? '8px 0' : '10px 12px',
          cursor: 'pointer',
          display: 'flex', flexDirection: collapsed ? 'column' : 'row',
          alignItems: 'center', gap: collapsed ? 4 : 8,
          justifyContent: collapsed ? 'center' : 'flex-start',
        }} onClick={() => setPillOpen(!pillOpen)}>
          <span style={{ width: 7, height: 7, borderRadius: '50%', background: '#a887ce', boxShadow: '0 0 8px rgba(168,135,206,0.6)', animation: 'pulse 1.6s ease-in-out infinite', flexShrink: 0 }} />
          {!collapsed ? (
            <>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 11, fontWeight: 600 }}>3 active</div>
                <div style={{ fontSize: 10, color: '#7b6e7f', marginTop: 1 }}>pipeline · scan · 2 dl</div>
              </div>
              <span style={{ color: '#7b6e7f', fontSize: 10 }}>▾</span>
            </>
          ) : (
            <span style={{ fontSize: 9, color: '#a887ce', fontWeight: 600 }}>3</span>
          )}
        </div>

        {/* Search shortcut */}
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: collapsed ? 0 : '7px 10px',
          height: collapsed ? 32 : 'auto',
          justifyContent: collapsed ? 'center' : 'flex-start',
          background: 'rgba(236,232,242,0.04)',
          borderRadius: 7,
          color: '#7b6e7f', fontSize: 11,
          cursor: 'pointer',
        }}>
          <span style={{ fontSize: 11 }}>⌕</span>
          {!collapsed && (
            <>
              <span style={{ flex: 1 }}>Search</span>
              <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 9, padding: '1px 5px', border: '1px solid rgba(236,232,242,0.10)', borderRadius: 3 }}>⌘K</span>
            </>
          )}
        </div>
      </div>

      {/* MAIN */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        {/* Top bar — subnav */}
        <div style={{
          height: 46, flexShrink: 0,
          borderBottom: '1px solid rgba(236,232,242,0.06)',
          display: 'flex', alignItems: 'center',
          padding: '0 22px',
          gap: 0,
          background: '#171821',
          overflowX: 'auto',
          scrollbarWidth: 'none',
        }}>
          {subNav.map((it, i) => (
            <div key={it} style={{
              padding: '0 14px', height: '100%',
              display: 'flex', alignItems: 'center',
              borderBottom: i === 0 ? '2px solid #a887ce' : '2px solid transparent',
              color: i === 0 ? '#ece8f2' : '#7b6e7f',
              fontWeight: i === 0 ? 600 : 450,
              fontSize: 12,
              whiteSpace: 'nowrap',
              cursor: 'pointer',
            }}>{it}</div>
          ))}
          <div style={{ flex: 1 }} />
          <div style={{
            display: 'flex', alignItems: 'center', gap: 7,
            background: 'rgba(168,135,206,0.10)',
            border: '1px solid rgba(168,135,206,0.25)',
            borderRadius: 999, padding: '4px 11px',
            fontSize: 11, color: '#a887ce',
            cursor: 'pointer',
          }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#a887ce' }} />
            SSE live
          </div>
        </div>

        {/* Page header */}
        <div style={{ padding: '22px 28px 16px', display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: '#7b6e7f', letterSpacing: '0.14em', textTransform: 'uppercase', marginBottom: 6 }}>Monitor</div>
            <div style={{ fontFamily: 'Inter Tight, sans-serif', fontSize: 26, fontWeight: 600, letterSpacing: '-0.02em' }}>Overview</div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11, color: '#7b6e7f' }}>last update · 2s ago</div>
            <div style={{ display: 'flex', gap: 2, background: '#292631', borderRadius: 7, padding: 2, border: '1px solid rgba(236,232,242,0.06)' }}>
              {['1h', '24h', '7d', '30d'].map((r, i) => (
                <div key={r} style={{
                  padding: '4px 12px', fontSize: 11,
                  background: i === 1 ? '#4d3e50' : 'transparent',
                  color: i === 1 ? '#ece8f2' : '#7b6e7f',
                  borderRadius: 5, cursor: 'pointer', fontWeight: i === 1 ? 600 : 400,
                }}>{r}</div>
              ))}
            </div>
          </div>
        </div>

        {/* Content scroll */}
        <div style={{ flex: 1, overflow: 'auto', padding: '0 28px 28px' }}>
          {/* Stat row */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 12, marginBottom: 16 }}>
            {[
              ['Events', '1.24M', '+8.2%', 'up'],
              ['Users', '142', '+3', 'up'],
              ['Tracks', '48,201', '+412', 'up'],
              ['Playlists', '1,024', '+18', 'up'],
              ['Events / hr', '2,847', '−12%', 'down'],
              ['Ranker', 'ready', 'ndcg 0.412', 'flat'],
            ].map(([label, val, delta, dir], i) => (
              <RealStat key={i} label={label} value={val} delta={delta} dir={dir} />
            ))}
          </div>

          {/* 2-col body */}
          <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 14 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              {/* Big chart */}
              <Panel
                title="Event ingest"
                sub="play_end · like · skip · pause · etc · last 24h · 5m bins"
                action="View full breakdown →"
              >
                <RealAreaChart />
              </Panel>

              {/* Two-up */}
              <div style={{ display: 'grid', gridTemplateColumns: '1.1fr 1fr', gap: 14 }}>
                <Panel title="Top tracks" sub="last 24h · by play_end count">
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
                    {[
                      ['Comptine d\'un autre été', 'Yann Tiersen', 142],
                      ['Nuvole Bianche', 'Ludovico Einaudi', 124],
                      ['Una Mattina', 'Ludovico Einaudi', 106],
                      ['Saman', 'Ólafur Arnalds', 88],
                      ['Holocene', 'Bon Iver', 71],
                      ['Re:Stacks', 'Bon Iver', 62],
                    ].map(([t, a, n], i) => (
                      <div key={i} style={{
                        display: 'flex', alignItems: 'center', gap: 10,
                        padding: '7px 0',
                        borderBottom: i < 5 ? '1px solid rgba(236,232,242,0.05)' : 'none',
                      }}>
                        <div style={{
                          width: 22, height: 22, borderRadius: 4,
                          background: '#4d3e50', flexShrink: 0,
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: '#7b6e7f',
                        }}>{i + 1}</div>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontSize: 12, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t}</div>
                          <div style={{ fontSize: 11, color: '#7b6e7f' }}>{a}</div>
                        </div>
                        <div style={{ width: 60, height: 3, background: 'rgba(236,232,242,0.05)', borderRadius: 2, overflow: 'hidden' }}>
                          <div style={{ width: `${(n / 142) * 100}%`, height: '100%', background: '#a887ce' }} />
                        </div>
                        <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11, color: '#b8b0c4', width: 32, textAlign: 'right' }}>{n}</div>
                      </div>
                    ))}
                  </div>
                </Panel>

                <Panel title="Event types" sub="last 24h · proportion">
                  <RealEventBars />
                </Panel>
              </div>

              {/* Recent events */}
              <Panel title="Recent events" sub="live tail · 4 most recent" badge="LIVE">
                <div>
                  {[
                    ['2s ago', 'play_end', 'alex', 'Comptine d\'un autre été · Yann Tiersen', '4:19'],
                    ['8s ago', 'like', 'jamie', 'Saman · Ólafur Arnalds', null],
                    ['14s ago', 'skip', 'sam', 'Holocene · Bon Iver', '0:42'],
                    ['22s ago', 'play_end', 'alex', 'Nuvole Bianche · Ludovico Einaudi', '5:58'],
                  ].map(([t, type, user, track, dur], i) => (
                    <div key={i} style={{
                      display: 'flex', alignItems: 'center', gap: 12,
                      padding: '8px 0',
                      borderBottom: i < 3 ? '1px solid rgba(236,232,242,0.05)' : 'none',
                      fontSize: 12,
                    }}>
                      <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: '#7b6e7f', width: 60 }}>{t}</div>
                      <div style={{
                        fontFamily: 'JetBrains Mono, monospace', fontSize: 9,
                        textTransform: 'uppercase', letterSpacing: '0.08em',
                        padding: '2px 7px', borderRadius: 3,
                        border: '1px solid rgba(236,232,242,0.12)',
                        color: '#b8b0c4',
                        width: 70, textAlign: 'center',
                        flexShrink: 0,
                      }}>{type}</div>
                      <div style={{ color: '#a887ce', fontWeight: 500, width: 50 }}>{user}</div>
                      <div style={{ flex: 1, color: '#b8b0c4', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{track}</div>
                      {dur && <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11, color: '#7b6e7f' }}>{dur}</div>}
                    </div>
                  ))}
                </div>
              </Panel>
            </div>

            {/* Right column */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              {/* Models */}
              <Panel title="Models" sub="readiness · 6 surfaces" action="See all →">
                {[
                  ['Ranker', 'ready', 'ndcg 0.412', 'good'],
                  ['Collaborative', 'ready', 'rebuilt 2h ago', 'good'],
                  ['Embeddings', 'ready', '48,201 vectors', 'good'],
                  ['SASRec', 'stale', 'last train 3d ago', 'warn'],
                  ['Session GRU', 'ready', 'fresh', 'good'],
                  ['Last.fm cache', 'ready', '94% hit rate', 'good'],
                ].map(([name, state, sub, kind], i) => (
                  <div key={i} style={{
                    display: 'flex', alignItems: 'center', gap: 10,
                    padding: '9px 0',
                    borderBottom: i < 5 ? '1px solid rgba(236,232,242,0.05)' : 'none',
                  }}>
                    <span style={{
                      width: 7, height: 7, borderRadius: '50%',
                      background: kind === 'good' ? '#a887ce' : '#9c526d',
                    }} />
                    <div style={{ flex: 1 }}>
                      <div style={{ fontSize: 12, fontWeight: 500 }}>{name}</div>
                      <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: '#7b6e7f', marginTop: 1 }}>{sub}</div>
                    </div>
                    <div style={{
                      fontFamily: 'JetBrains Mono, monospace', fontSize: 9,
                      textTransform: 'uppercase', letterSpacing: '0.08em',
                      color: kind === 'good' ? '#a887ce' : '#9c526d',
                    }}>{state}</div>
                  </div>
                ))}
              </Panel>

              {/* Library scan card */}
              <Panel title="Library scan" sub="phase 2 of 3 · indexing" badge="LIVE">
                <div style={{ marginTop: 4 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: '#b8b0c4', marginBottom: 6 }}>
                    <span>14,302 / ~22,000 files</span>
                    <span style={{ fontFamily: 'JetBrains Mono, monospace', color: '#a887ce' }}>65%</span>
                  </div>
                  <div style={{ height: 6, background: 'rgba(236,232,242,0.06)', borderRadius: 3, overflow: 'hidden' }}>
                    <div style={{ width: '65%', height: '100%', background: 'linear-gradient(90deg, #4d3e50 0%, #a887ce 100%)', borderRadius: 3 }} />
                  </div>
                  <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 8 }}>
                    {[
                      ['Found', '14,302'],
                      ['New', '+412'],
                      ['Updated', '38'],
                      ['Removed', '6'],
                    ].map(([k, v], i) => (
                      <div key={i} style={{ background: 'rgba(236,232,242,0.04)', borderRadius: 6, padding: '8px 10px' }}>
                        <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 9, color: '#7b6e7f', textTransform: 'uppercase', letterSpacing: '0.08em' }}>{k}</div>
                        <div style={{ fontFamily: 'Inter Tight, sans-serif', fontSize: 16, fontWeight: 600, marginTop: 2 }}>{v}</div>
                      </div>
                    ))}
                  </div>
                </div>
              </Panel>

              {/* Quick actions */}
              <Panel title="Quick run" sub="jumps to Actions">
                {[
                  ['Run pipeline', '14m ago · ok'],
                  ['Scan library', 'running'],
                  ['Build charts', '2h ago'],
                  ['Backfill CLAP', '2d ago'],
                ].map(([name, sub], i) => (
                  <div key={i} style={{
                    display: 'flex', alignItems: 'center', gap: 10,
                    padding: '9px 0',
                    borderBottom: i < 3 ? '1px solid rgba(236,232,242,0.05)' : 'none',
                    cursor: 'pointer',
                  }}>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontSize: 12, fontWeight: 500 }}>{name}</div>
                      <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: '#7b6e7f', marginTop: 1 }}>{sub}</div>
                    </div>
                    <div style={{ fontSize: 11, color: '#a887ce' }}>→</div>
                  </div>
                ))}
              </Panel>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

const Panel = ({ title, sub, action, badge, children }) => (
  <div style={{
    background: '#292631',
    border: '1px solid rgba(236,232,242,0.06)',
    borderRadius: 10,
    padding: 16,
  }}>
    <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 12 }}>
      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>{title}</div>
          {badge && (
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              fontFamily: 'JetBrains Mono, monospace', fontSize: 8,
              letterSpacing: '0.1em', padding: '2px 6px',
              background: 'rgba(168,135,206,0.16)',
              color: '#a887ce',
              borderRadius: 3, fontWeight: 700,
            }}>
              <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#a887ce' }} />
              {badge}
            </span>
          )}
        </div>
        {sub && <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: '#7b6e7f', marginTop: 3 }}>{sub}</div>}
      </div>
      {action && <div style={{ fontSize: 11, color: '#a887ce', cursor: 'pointer', fontWeight: 500 }}>{action}</div>}
    </div>
    {children}
  </div>
);

const RealStat = ({ label, value, delta, dir }) => (
  <div style={{
    background: '#292631',
    border: '1px solid rgba(236,232,242,0.06)',
    borderRadius: 10,
    padding: '14px 16px',
    minWidth: 0,
  }}>
    <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: '#7b6e7f', letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>{label}</div>
    <div style={{ fontFamily: 'Inter Tight, sans-serif', fontSize: 22, fontWeight: 600, letterSpacing: '-0.02em', marginBottom: 4 }}>{value}</div>
    <div style={{
      fontSize: 11,
      color: dir === 'up' ? '#a887ce' : dir === 'down' ? '#9c526d' : '#7b6e7f',
      display: 'flex', alignItems: 'center', gap: 4,
    }}>
      {dir === 'up' && '↑'}
      {dir === 'down' && '↓'}
      {delta}
    </div>
  </div>
);

// Smooth area chart with gradient fill (lavender)
const RealAreaChart = () => {
  const w = 720, h = 180;
  const points = 48;
  const seed = 42;
  const rand = (i) => {
    const x = Math.sin(seed * 9301 + i * 49297) * 233280;
    return x - Math.floor(x);
  };
  // dual series: total + likes (smaller)
  const series1 = Array.from({ length: points }, (_, i) => 0.4 + Math.sin(i / 4) * 0.15 + rand(i) * 0.35);
  const series2 = Array.from({ length: points }, (_, i) => 0.18 + Math.sin(i / 5 + 1) * 0.08 + rand(i + 99) * 0.15);

  const toPath = (vals, smooth = true) => {
    return vals.map((v, i) => {
      const x = (i / (points - 1)) * w;
      const y = h - v * (h - 20) - 10;
      if (i === 0) return `M ${x.toFixed(1)} ${y.toFixed(1)}`;
      const px = ((i - 1) / (points - 1)) * w;
      const py = h - vals[i - 1] * (h - 20) - 10;
      const cx1 = px + (x - px) / 2;
      const cx2 = px + (x - px) / 2;
      return `C ${cx1.toFixed(1)} ${py.toFixed(1)}, ${cx2.toFixed(1)} ${y.toFixed(1)}, ${x.toFixed(1)} ${y.toFixed(1)}`;
    }).join(' ');
  };
  const p1 = toPath(series1);
  const p2 = toPath(series2);
  const fill1 = `${p1} L ${w} ${h} L 0 ${h} Z`;
  const fill2 = `${p2} L ${w} ${h} L 0 ${h} Z`;

  return (
    <div style={{ position: 'relative', width: '100%' }}>
      <svg viewBox={`0 0 ${w} ${h}`} style={{ display: 'block', width: '100%', height: 'auto' }} preserveAspectRatio="none">
        <defs>
          <linearGradient id="area1" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#a887ce" stopOpacity="0.45" />
            <stop offset="100%" stopColor="#a887ce" stopOpacity="0" />
          </linearGradient>
          <linearGradient id="area2" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#9c526d" stopOpacity="0.35" />
            <stop offset="100%" stopColor="#9c526d" stopOpacity="0" />
          </linearGradient>
        </defs>
        {/* gridlines */}
        {[0.25, 0.5, 0.75].map(g => (
          <line key={g} x1="0" y1={h * g} x2={w} y2={h * g}
            stroke="rgba(236,232,242,0.04)" strokeDasharray="2 4" />
        ))}
        <path d={fill1} fill="url(#area1)" />
        <path d={p1} stroke="#a887ce" strokeWidth="2" fill="none" />
        <path d={fill2} fill="url(#area2)" />
        <path d={p2} stroke="#9c526d" strokeWidth="1.5" fill="none" strokeOpacity="0.7" />
      </svg>
      {/* axis labels */}
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        fontFamily: 'JetBrains Mono, monospace', fontSize: 9, color: '#7b6e7f',
        marginTop: 6,
      }}>
        {['00:00', '04:00', '08:00', '12:00', '16:00', '20:00', '23:59'].map(t => <span key={t}>{t}</span>)}
      </div>
      {/* legend */}
      <div style={{ display: 'flex', gap: 14, marginTop: 8, fontSize: 11 }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6, color: '#b8b0c4' }}>
          <span style={{ width: 8, height: 8, borderRadius: 2, background: '#a887ce' }} /> All events
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6, color: '#b8b0c4' }}>
          <span style={{ width: 8, height: 8, borderRadius: 2, background: '#9c526d' }} /> Engagement (likes + plays-through)
        </span>
      </div>
    </div>
  );
};

// Horizontal bar list (event types)
const RealEventBars = () => {
  const types = [
    ['play_end', 4820, '#a887ce'],
    ['like', 1240, '#a887ce'],
    ['skip', 982, '#7b6e7f'],
    ['pause', 612, '#7b6e7f'],
    ['volume', 318, '#7b6e7f'],
    ['dislike', 84, '#9c526d'],
  ];
  const max = 4820;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 9, marginTop: 4 }}>
      {types.map(([name, val, color]) => (
        <div key={name}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, marginBottom: 4 }}>
            <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: '#b8b0c4', letterSpacing: '0.06em' }}>{name}</span>
            <span style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: '#7b6e7f' }}>{val.toLocaleString()}</span>
          </div>
          <div style={{ height: 5, background: 'rgba(236,232,242,0.05)', borderRadius: 3, overflow: 'hidden' }}>
            <div style={{ width: `${(val / max) * 100}%`, height: '100%', background: color, borderRadius: 3 }} />
          </div>
        </div>
      ))}
    </div>
  );
};

window.Realistic = Realistic;
