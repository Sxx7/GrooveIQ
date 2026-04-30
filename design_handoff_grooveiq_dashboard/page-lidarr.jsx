// Page 2: Lidarr Backfill — the 3-bucket split exemplar
// Shown as 3 small framed views side by side: Settings, Actions, Monitor

const LBSettings = () => (
  <Window navStyle="side-icon" currentBucket="Settings"
    subnav={<SubNavBreadcrumb trail={['Settings']} current="Lidarr Backfill" switcher />}
    kids={
      <div style={{ padding: 16, height: '100%', overflow: 'auto', background: 'var(--bg)' }}>
        <div className="row between" style={{ marginBottom: 12 }}>
          <div>
            <div className="eyebrow">Versioned config · v14</div>
            <div className="display" style={{ fontSize: 18, fontWeight: 600 }}>Lidarr Backfill Config</div>
          </div>
          <div className="row gap-3">
            <div className="btn sm ghost">History</div>
            <div className="btn sm ghost">Diff</div>
            <div className="btn sm primary">Save & Apply</div>
          </div>
        </div>

        {/* Cross-link rail */}
        <div className="row gap-3" style={{ marginBottom: 14, padding: '8px 12px', background: 'var(--paper)', border: '1.5px dashed var(--line-soft)', borderRadius: 8 }}>
          <span className="hand" style={{ fontSize: 14, color: 'var(--ink-3)' }}>related →</span>
          <JumpLink to="Actions" label="Queue management" />
          <JumpLink to="Monitor" label="Stats & ETA" />
        </div>

        {[
          ['Schedule', ['cadence_minutes · 30', 'max_concurrent · 4', 'pause_after_failures · 5']],
          ['Match policy', ['min_score · 0.62', 'prefer_album · true', 'allow_va · false']],
          ['Capacity', ['daily_cap_gb · 200', 'per_artist_cap · 12']],
        ].map(([group, fields]) => (
          <div key={group} className="frame" style={{ padding: 12, marginBottom: 8 }}>
            <div className="row between" style={{ marginBottom: 8 }}>
              <span style={{ fontSize: 12, fontWeight: 600 }}>{group}</span>
              <span style={{ color: 'var(--ink-3)', fontSize: 11 }}>▾</span>
            </div>
            {fields.map((f, i) => (
              <div key={i} className="row between" style={{ padding: '5px 0', fontSize: 11, borderTop: '1px solid var(--line-faint)' }}>
                <span className="mono" style={{ color: 'var(--ink-2)' }}>{f}</span>
                <SketchBox w={120} h={18} dashed style={{ borderRadius: 4 }} />
              </div>
            ))}
          </div>
        ))}
      </div>
    } />
);

const LBActions = () => (
  <Window navStyle="side-icon" currentBucket="Actions"
    subnav={<SubNavBreadcrumb trail={['Actions', 'Discovery']} current="Lidarr Backfill" switcher />}
    kids={
      <div style={{ padding: 16, height: '100%', overflow: 'auto', background: 'var(--bg)' }}>
        <div className="row between" style={{ marginBottom: 12 }}>
          <div>
            <div className="eyebrow">Action · queue management</div>
            <div className="display" style={{ fontSize: 18, fontWeight: 600 }}>Lidarr Backfill Queue</div>
          </div>
          <div className="row gap-3">
            <div className="btn sm">Pause</div>
            <div className="btn sm accent">▶ Run now</div>
          </div>
        </div>

        <div className="row gap-3" style={{ marginBottom: 14, padding: '8px 12px', background: 'var(--paper)', border: '1.5px dashed var(--line-soft)', borderRadius: 8 }}>
          <span className="hand" style={{ fontSize: 14, color: 'var(--ink-3)' }}>related →</span>
          <JumpLink to="Settings" label="Edit config" />
          <JumpLink to="Monitor" label="Live stats" />
        </div>

        {/* Filters */}
        <div className="row gap-3" style={{ marginBottom: 10 }}>
          {['All · 412', 'Queued · 88', 'In flight · 4', 'Failed · 12'].map((c, i) => (
            <div key={i} className={`chip ${i === 2 ? 'solid' : 'ghost'}`} style={{ fontSize: 11 }}>{c}</div>
          ))}
        </div>

        {/* Queue table */}
        <div className="frame">
          <div className="row" style={{ padding: '8px 12px', borderBottom: '1.5px solid var(--line-soft)', background: 'var(--bg)' }}>
            <span className="eyebrow grow">Artist · Album</span>
            <span className="eyebrow" style={{ width: 70 }}>State</span>
            <span className="eyebrow" style={{ width: 50 }}>Score</span>
            <span className="eyebrow" style={{ width: 110, textAlign: 'right' }}>Actions</span>
          </div>
          {[
            ['Nils Frahm · All Melody', 'in flight', '0.91', null],
            ['Ólafur Arnalds · re:member', 'in flight', '0.88', null],
            ['Max Richter · Voices', 'queued', '0.74', null],
            ['Hania Rani · Esja', 'queued', '0.72', null],
            ['Joep Beving · Solipsism', 'failed', '0.58', 'no match'],
            ['Poppy Ackroyd · Resolve', 'queued', '0.69', null],
          ].map(([name, state, score, note], i) => {
            const stateChip = state === 'in flight' ? 'accent' : state === 'failed' ? 'bad' : 'ghost';
            return (
              <div key={i} className="row" style={{ padding: '8px 12px', borderBottom: i < 5 ? '1px solid var(--line-faint)' : 'none', fontSize: 11 }}>
                <div className="grow">
                  <div style={{ fontWeight: 500 }}>{name}</div>
                  {note && <div style={{ fontSize: 10, color: 'var(--bad)' }}>{note}</div>}
                </div>
                <div style={{ width: 70 }}><span className={`chip ${stateChip}`} style={{ fontSize: 9, padding: '1px 6px' }}>{state}</span></div>
                <div style={{ width: 50 }} className="mono">{score}</div>
                <div style={{ width: 110, textAlign: 'right', color: 'var(--ink-3)' }} className="mono">retry · skip · forget</div>
              </div>
            );
          })}
        </div>
      </div>
    } />
);

const LBMonitor = () => (
  <Window navStyle="side-icon" currentBucket="Monitor"
    subnav={<SubNavBreadcrumb trail={['Monitor']} current="Lidarr Backfill" switcher />}
    kids={
      <div style={{ padding: 16, height: '100%', overflow: 'auto', background: 'var(--bg)' }}>
        <div className="row between" style={{ marginBottom: 12 }}>
          <div>
            <div className="eyebrow">Live observability</div>
            <div className="display" style={{ fontSize: 18, fontWeight: 600 }}>Lidarr Backfill Stats</div>
          </div>
          <div className="row gap-3">
            <span className="dot live" />
            <span className="mono" style={{ fontSize: 10, color: 'var(--ink-3)' }}>live</span>
          </div>
        </div>

        <div className="row gap-3" style={{ marginBottom: 14, padding: '8px 12px', background: 'var(--paper)', border: '1.5px dashed var(--line-soft)', borderRadius: 8 }}>
          <span className="hand" style={{ fontSize: 14, color: 'var(--ink-3)' }}>related →</span>
          <JumpLink to="Settings" label="Edit config" />
          <JumpLink to="Actions" label="Manage queue" />
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8, marginBottom: 12 }}>
          <Stat label="Missing" value="412" />
          <Stat label="Complete" value="1,847" delta="+24 / 24h" deltaKind="good" />
          <Stat label="Failed" value="12" delta="0.7%" deltaKind="bad" />
          <Stat label="Capacity" value="62%" delta="124 / 200 GB" />
          <Stat label="ETA" value="~3.4 d" mono />
          <Stat label="Run #" value="14" mono />
        </div>

        <div className="frame" style={{ padding: 12 }}>
          <div className="row between" style={{ marginBottom: 8 }}>
            <span style={{ fontSize: 12, fontWeight: 600 }}>Throughput · last 7 days</span>
            <span className="mono" style={{ fontSize: 10, color: 'var(--ink-3)' }}>albums / day</span>
          </div>
          <BarChart w={420} h={80} bars={14} accentIdx={11} />
        </div>
      </div>
    } />
);

window.LBSettings = LBSettings;
window.LBActions = LBActions;
window.LBMonitor = LBMonitor;
