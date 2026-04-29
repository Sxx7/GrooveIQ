// Page 4: Explore → Recommendations + cross-link to Debug

const PageRecommendations = () => {
  const [pillOpen, setPillOpen] = React.useState(false);
  const pill = (
    <div style={{ position: 'relative' }}>
      <ActivityPill expanded={pillOpen} onToggle={() => setPillOpen(!pillOpen)} />
    </div>
  );
  return (
    <Window navStyle="side" currentBucket="Explore" activityPill={pill}
      subnav={<SubNavTabs items={['Recommendations', 'Radio', 'Playlists', 'Tracks', 'Text Search', 'Music Map', 'Charts', 'Artists', 'News']} current="Recommendations" />}
      kids={
        <div style={{ padding: 20, height: '100%', overflow: 'auto', background: 'var(--bg)' }}>
          <div className="row between" style={{ marginBottom: 16 }}>
            <div>
              <div className="eyebrow">Explore</div>
              <div className="display" style={{ fontSize: 24, fontWeight: 600 }}>Recommendations</div>
            </div>
            <div className="row gap-3">
              <div className="row gap-3" style={{ border: '1.5px solid var(--line)', borderRadius: 8, padding: '4px 10px', fontSize: 12 }}>
                <span style={{ color: 'var(--ink-3)' }}>user</span>
                <span style={{ fontWeight: 600 }}>alex</span>
                <span style={{ color: 'var(--ink-3)' }}>▾</span>
              </div>
              <div className="btn sm">seed track</div>
              <div className="btn sm">limit · 25</div>
              <div className="btn sm primary">Get Recs</div>
            </div>
          </div>

          {/* Cross-link banner */}
          <div className="row gap-4" style={{ marginBottom: 14, padding: '8px 12px', background: 'var(--paper)', border: '1.5px dashed var(--line-soft)', borderRadius: 8, fontSize: 11 }}>
            <span style={{ color: 'var(--ink-3)' }}>request_id</span>
            <span className="mono">7f2a-3c8e</span>
            <span className="grow" />
            <JumpLink to="Monitor" label="Debug this request" />
          </div>

          <div className="frame">
            <div className="row" style={{ padding: '8px 14px', borderBottom: '1.5px solid var(--line-soft)', background: 'var(--bg)' }}>
              <span className="eyebrow grow">Track</span>
              <span className="eyebrow" style={{ width: 60 }}>Source</span>
              <span className="eyebrow" style={{ width: 50 }}>Score</span>
              <span className="eyebrow" style={{ width: 50 }}>BPM</span>
              <span className="eyebrow" style={{ width: 60 }}>Mood</span>
              <span className="eyebrow" style={{ width: 30 }}></span>
            </div>
            {[
              ['Comptine d\'un autre été', 'Yann Tiersen', 'cf', '0.94', '78', 'calm'],
              ['Nuvole Bianche', 'Ludovico Einaudi', 'cf', '0.92', '64', 'pensive'],
              ['Una Mattina', 'Ludovico Einaudi', 'content', '0.89', '60', 'calm'],
              ['Saman', 'Ólafur Arnalds', 'session', '0.86', '92', 'pensive'],
              ['Re:Stacks', 'Bon Iver', 'sasrec', '0.82', '88', 'wistful'],
              ['Holocene', 'Bon Iver', 'lastfm', '0.79', '88', 'warm'],
            ].map(([t, a, src, score, bpm, mood], i) => (
              <div key={i} className="row" style={{ padding: '10px 14px', borderBottom: i < 5 ? '1px solid var(--line-faint)' : 'none', fontSize: 12 }}>
                <div className="grow" style={{ minWidth: 0 }}>
                  <div style={{ fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t}</div>
                  <div style={{ fontSize: 11, color: 'var(--ink-3)' }}>{a}</div>
                </div>
                <div style={{ width: 60 }}>
                  <span className="mono" style={{
                    fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.06em',
                    border: '1px solid var(--line-soft)', borderRadius: 3, padding: '1px 5px', color: 'var(--ink-2)',
                  }}>{src}</span>
                </div>
                <div style={{ width: 50 }}>
                  <div style={{ width: 40, height: 4, background: 'var(--line-faint)', borderRadius: 2, overflow: 'hidden' }}>
                    <div style={{ width: `${parseFloat(score) * 100}%`, height: '100%', background: 'var(--ink)' }} />
                  </div>
                  <span className="mono" style={{ fontSize: 10, color: 'var(--ink-3)' }}>{score}</span>
                </div>
                <div style={{ width: 50 }} className="mono">{bpm}</div>
                <div style={{ width: 60, fontSize: 11, color: 'var(--ink-2)' }}>{mood}</div>
                <div style={{ width: 30, textAlign: 'right', color: 'var(--accent)', fontSize: 10, cursor: 'pointer', fontWeight: 500 }}>debug→</div>
              </div>
            ))}
          </div>
        </div>
      } />
  );
};

window.PageRecommendations = PageRecommendations;
