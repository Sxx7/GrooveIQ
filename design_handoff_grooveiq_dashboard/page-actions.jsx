// Page 3: Actions — three different shapes, side by side

// Shape A: Single hub with all triggers as cards
const ActionsHub = () => (
  <Window navStyle="side-icon" currentBucket="Actions"
    subnav={<SubNavBreadcrumb trail={['Actions']} current="All actions" switcher />}
    kids={
      <div style={{ padding: 16, height: '100%', overflow: 'auto', background: 'var(--bg)' }}>
        <div style={{ marginBottom: 12 }}>
          <div className="eyebrow">Shape A · single hub</div>
          <div className="display" style={{ fontSize: 18, fontWeight: 600 }}>Actions</div>
          <div style={{ fontSize: 11, color: 'var(--ink-3)' }}>Every trigger on one page · grouped headings</div>
        </div>

        {[
          ['Pipeline & ML', [['Run Pipeline', 'last: 14m ago · ok'], ['Reset Pipeline', null], ['Backfill CLAP', 'last: 2d ago'], ['Cleanup Stale', null]]],
          ['Library', [['Scan Library', 'running · 64%'], ['Sync IDs', null]]],
          ['Discovery', [['Lidarr Discovery', null], ['Fill Library', null], ['Backfill now', null], ['Soulseek Bulk', null]]],
        ].map(([group, items]) => (
          <div key={group} style={{ marginBottom: 14 }}>
            <div className="eyebrow" style={{ marginBottom: 6 }}>{group}</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 8 }}>
              {items.map(([name, sub], i) => (
                <div key={i} className="frame" style={{ padding: 12 }}>
                  <div className="row between">
                    <div>
                      <div style={{ fontSize: 13, fontWeight: 600 }}>{name}</div>
                      {sub && <div className="mono" style={{ fontSize: 10, color: 'var(--ink-3)', marginTop: 2 }}>{sub}</div>}
                    </div>
                    <div className="btn sm accent">▶ Run</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    } />
);

// Shape B: Grouped pages
const ActionsGrouped = () => (
  <Window navStyle="side-icon" currentBucket="Actions"
    subnav={<SubNavSide title="Actions" current="Pipeline & ML" items={['Pipeline & ML', 'Library', 'Discovery', 'Charts', 'Downloads']} />}
    kids={
      <div style={{ padding: 16, height: '100%', overflow: 'auto', background: 'var(--bg)' }}>
        <div style={{ marginBottom: 14 }}>
          <div className="eyebrow">Shape B · grouped pages</div>
          <div className="display" style={{ fontSize: 18, fontWeight: 600 }}>Pipeline & ML</div>
          <div style={{ fontSize: 11, color: 'var(--ink-3)' }}>One page per group, multiple actions stacked</div>
        </div>

        {[
          ['Run Pipeline', 'Triggers full pipeline. Auto-redirects to Monitor → Pipeline.', 'good', 'last run · 14m ago'],
          ['Reset Pipeline', 'Clears state and rebuilds. Required after algorithm changes.', null, null],
          ['Backfill CLAP', 'Generates CLAP embeddings for all tracks missing them.', null, '2,143 tracks pending'],
          ['Cleanup Stale Tracks', 'Removes tracks no longer in library. Destructive.', 'bad', null],
        ].map(([name, desc, badge, sub], i) => (
          <div key={i} className="frame" style={{ padding: 14, marginBottom: 10 }}>
            <div className="row between" style={{ marginBottom: 6 }}>
              <div className="row gap-3">
                <span style={{ fontSize: 14, fontWeight: 600 }}>{name}</span>
                {badge === 'good' && <span className="dot good" />}
                {badge === 'bad' && <span className="chip bad" style={{ fontSize: 9, padding: '0 6px' }}>destructive</span>}
              </div>
              <div className="btn sm accent">▶ Run</div>
            </div>
            <div style={{ fontSize: 11, color: 'var(--ink-2)' }}>{desc}</div>
            {sub && <div className="mono" style={{ fontSize: 10, color: 'var(--ink-3)', marginTop: 6 }}>{sub}</div>}
          </div>
        ))}
      </div>
    } />
);

// Shape C: One page per action
const ActionsSingle = () => (
  <Window navStyle="side-icon" currentBucket="Actions"
    subnav={<SubNavSide title="Actions" current="Run Pipeline" items={['Run Pipeline', 'Reset Pipeline', 'Backfill CLAP', 'Cleanup Stale', 'Scan Library', 'Sync IDs', '...']} />}
    kids={
      <div style={{ padding: 20, height: '100%', overflow: 'auto', background: 'var(--bg)' }}>
        <div style={{ marginBottom: 16 }}>
          <div className="eyebrow">Shape C · one page per action</div>
          <div className="display" style={{ fontSize: 22, fontWeight: 600 }}>Run Pipeline</div>
          <div style={{ fontSize: 12, color: 'var(--ink-3)', marginTop: 4 }}>Full focused page per trigger · room for parameters and history</div>
        </div>

        <div className="frame" style={{ padding: 16, marginBottom: 12, textAlign: 'center' }}>
          <div className="hand" style={{ fontSize: 16, color: 'var(--ink-3)', marginBottom: 8 }}>last run · 14m ago · ✓ ok</div>
          <div className="btn accent" style={{ fontSize: 15, padding: '10px 22px' }}>▶ Run Pipeline now</div>
          <div style={{ fontSize: 11, color: 'var(--ink-3)', marginTop: 8 }}>Will auto-redirect to Monitor → Pipeline with SSE connected.</div>
        </div>

        <div className="frame" style={{ padding: 14 }}>
          <div className="eyebrow" style={{ marginBottom: 8 }}>Recent runs</div>
          {[
            ['14m ago', 'ok', '4m 12s'],
            ['1h ago', 'ok', '4m 04s'],
            ['3h ago', 'failed', 'step 6'],
            ['yesterday', 'ok', '4m 22s'],
          ].map(([when, state, dur], i) => (
            <div key={i} className="row between" style={{ padding: '6px 0', borderTop: i > 0 ? '1px solid var(--line-faint)' : 'none', fontSize: 11 }}>
              <span className="mono">{when}</span>
              <span className={`chip ${state === 'ok' ? 'good' : 'bad'}`} style={{ fontSize: 9, padding: '0 6px' }}>{state}</span>
              <span className="mono" style={{ color: 'var(--ink-3)' }}>{dur}</span>
            </div>
          ))}
        </div>
      </div>
    } />
);

window.ActionsHub = ActionsHub;
window.ActionsGrouped = ActionsGrouped;
window.ActionsSingle = ActionsSingle;
