// Page 1: Monitor → Overview (the new landing page)

const PageMonitorOverview = ({ navStyle = 'side' }) => {
  const [pillOpen, setPillOpen] = React.useState(false);
  const pill = (
    <div style={{ position: 'relative' }}>
      <ActivityPill expanded={pillOpen} onToggle={() => setPillOpen(!pillOpen)} compact={navStyle === 'side-icon'} />
    </div>
  );
  const subnav = <SubNavTabs items={['Overview', 'Pipeline', 'Models', 'System Health', 'Recs Debug', 'User Diagnostics', 'Integrations', 'Downloads', 'Lidarr Backfill', 'Discovery', 'Charts']} current="Overview" />;

  return (
    <Window navStyle={navStyle} currentBucket="Monitor" activityPill={pill} subnav={subnav} kids={
      <div style={{ padding: 20, height: '100%', overflow: 'auto', background: 'var(--bg)' }}>
        {/* Header */}
        <div className="row between" style={{ marginBottom: 18, alignItems: 'flex-end' }}>
          <div>
            <div className="eyebrow">Monitor</div>
            <div className="display" style={{ fontSize: 26, fontWeight: 600, marginTop: 2 }}>Overview</div>
          </div>
          <div className="row gap-3">
            <span className="mono" style={{ fontSize: 11, color: 'var(--ink-3)' }}>updated 2s ago</span>
            <span className="dot live" />
          </div>
        </div>

        {/* Stat row */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 10, marginBottom: 16 }}>
          <Stat label="Events" value="1.2M" delta="+8% / 24h" deltaKind="good" />
          <Stat label="Users" value="142" delta="+3 this week" deltaKind="good" />
          <Stat label="Tracks" value="48,201" delta="+412 / week" />
          <Stat label="Playlists" value="1,024" />
          <Stat label="Events / hr" value="2,847" delta="↘ from peak" />
          <Stat label="Ranker" value="ready" mono delta="ndcg 0.412" deltaKind="good" />
        </div>

        {/* 2-col body */}
        <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 12 }}>
          <div className="col gap-4">
            {/* Event chart card */}
            <div className="frame" style={{ padding: 14 }}>
              <div className="row between" style={{ marginBottom: 10 }}>
                <div>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>Event ingest</div>
                  <div className="mono" style={{ fontSize: 10, color: 'var(--ink-3)' }}>last 24h · 1m bins</div>
                </div>
                <div className="row gap-3">
                  <div className="chip ghost" style={{ fontSize: 10 }}>1h</div>
                  <div className="chip" style={{ fontSize: 10 }}>24h</div>
                  <div className="chip ghost" style={{ fontSize: 10 }}>7d</div>
                </div>
              </div>
              <SparkLine w={520} h={120} points={48} peak={0.9} />
            </div>

            {/* Top tracks + recent events split */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <div className="frame" style={{ padding: 14 }}>
                <div className="row between" style={{ marginBottom: 8 }}>
                  <div style={{ fontWeight: 600, fontSize: 13 }}>Top tracks · 24h</div>
                  <span className="mono" style={{ fontSize: 10, color: 'var(--ink-3)' }}>by plays</span>
                </div>
                {['River Flows', 'Comptine d\'un autre été', 'Nuvole Bianche', 'Una Mattina', 'Cornfield Chase'].map((t, i) => (
                  <div key={i} className="row between" style={{ padding: '5px 0', borderBottom: i < 4 ? '1px solid var(--line-faint)' : 'none', fontSize: 12 }}>
                    <div className="row gap-3">
                      <span className="mono" style={{ color: 'var(--ink-3)', width: 14 }}>{i + 1}</span>
                      <span style={{ fontWeight: 500 }}>{t}</span>
                    </div>
                    <span className="mono" style={{ color: 'var(--ink-3)', fontSize: 11 }}>{142 - i * 18}</span>
                  </div>
                ))}
              </div>
              <div className="frame" style={{ padding: 14 }}>
                <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 8 }}>Event types · 24h</div>
                <BarChart w={240} h={92} bars={7} accentIdx={1} />
                <div className="row" style={{ flexWrap: 'wrap', gap: 4, marginTop: 8 }}>
                  {['play_end', 'like', 'skip', 'dislike', 'pause', 'vol↑', 'vol↓'].map(t => (
                    <span key={t} className="mono" style={{ fontSize: 9, color: 'var(--ink-3)' }}>{t}</span>
                  ))}
                </div>
              </div>
            </div>
          </div>

          <div className="col gap-4">
            {/* Models readiness */}
            <div className="frame" style={{ padding: 14 }}>
              <div className="row between" style={{ marginBottom: 10 }}>
                <div style={{ fontWeight: 600, fontSize: 13 }}>Models</div>
                <JumpLink to="" label="See all" />
              </div>
              {[
                ['Ranker', 'good', 'ndcg 0.412'],
                ['CF', 'good', 'rebuilt 2h ago'],
                ['Embeddings', 'good', '48k vectors'],
                ['SASRec', 'warn', 'stale · 3d'],
                ['Session GRU', 'good', 'fresh'],
                ['Last.fm cache', 'good', '94% hit'],
              ].map(([name, state, sub], i) => (
                <div key={i} className="row between" style={{ padding: '6px 0', borderBottom: i < 5 ? '1px solid var(--line-faint)' : 'none' }}>
                  <div className="row gap-3">
                    <span className={`dot ${state}`} />
                    <span style={{ fontSize: 12, fontWeight: 500 }}>{name}</span>
                  </div>
                  <span className="mono" style={{ fontSize: 10, color: 'var(--ink-3)' }}>{sub}</span>
                </div>
              ))}
            </div>

            {/* Quick actions card */}
            <div className="frame" style={{ padding: 14, borderStyle: 'dashed' }}>
              <div className="eyebrow" style={{ marginBottom: 8 }}>Quick run</div>
              <div className="col gap-3">
                <div className="row between">
                  <span style={{ fontSize: 12 }}>Run pipeline</span>
                  <JumpLink to="Actions" label="trigger" />
                </div>
                <div className="row between">
                  <span style={{ fontSize: 12 }}>Scan library</span>
                  <JumpLink to="Actions" label="trigger" />
                </div>
                <div className="row between">
                  <span style={{ fontSize: 12 }}>Build charts</span>
                  <JumpLink to="Actions" label="trigger" />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    } />
  );
};

window.PageMonitorOverview = PageMonitorOverview;
