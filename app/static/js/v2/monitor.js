/* monitor.js — Monitor bucket pages.
 * Overview (session 02), Pipeline / Models / Recs Debug (session 06).
 * Other sub-pages (system-health, user-diagnostics, integrations,
 * downloads, lidarr-backfill, discovery, charts) remain stubs and are
 * filled in by sessions 07–08.
 */

(function () {
    GIQ.pages.monitor = GIQ.pages.monitor || {};

    const STUBS = [
        'system-health', 'user-diagnostics', 'integrations',
        'downloads', 'lidarr-backfill', 'discovery', 'charts',
    ];
    for (const sp of STUBS) {
        GIQ.pages.monitor[sp] = function (root) {
            const label = GIQ.router.SUBPAGE_LABELS[sp] || sp;
            root.innerHTML = '<div class="page-stub">'
                + '<div class="eyebrow">MONITOR</div>'
                + '<h1>' + GIQ.fmt.esc(label) + '</h1>'
                + '<p class="muted">Page: monitor → ' + GIQ.fmt.esc(sp) + ' — TBD</p>'
                + '</div>';
        };
    }

    GIQ.pages.monitor.overview = renderOverview;
    GIQ.pages.monitor.pipeline = renderPipeline;
    GIQ.pages.monitor.models = renderModels;
    GIQ.pages.monitor['recs-debug'] = renderRecsDebug;

    /* ---- Step metadata (used across Pipeline + Models pages) --------- */

    const STEP_META = {
        sessionizer:        { icon: '⏱',  label: 'Sessionizer',    desc: 'Groups raw events into listening sessions' },
        track_scoring:      { icon: '★',  label: 'Scoring',        desc: 'Computes per-track satisfaction scores' },
        taste_profiles:     { icon: '◎',  label: 'Taste Profiles', desc: 'Builds user audio preference profiles' },
        collab_filter:      { icon: '↔',  label: 'Collab Filter',  desc: 'User-user & item-item similarity' },
        ranker:             { icon: '◆',  label: 'Ranker',         desc: 'Trains LightGBM ranking model' },
        session_embeddings: { icon: '⌬',  label: 'Embeddings',     desc: 'Word2Vec skip-gram on sessions' },
        lastfm_candidates:  { icon: '⌖',  label: 'Last.fm',        desc: 'External CF via Last.fm similar tracks' },
        lastfm_cache:       { icon: '⌖',  label: 'Last.fm',        desc: 'External CF via Last.fm similar tracks' },
        sasrec:             { icon: '⚡', label: 'SASRec',         desc: 'Transformer sequential model' },
        session_gru:        { icon: '∿',  label: 'Session GRU',    desc: 'Taste drift via GRU over sessions' },
        music_map:          { icon: '◈',  label: 'Music Map',      desc: 'UMAP projection of audio embeddings to 2D' },
    };
    const STEP_ORDER = [
        'sessionizer', 'track_scoring', 'taste_profiles',
        'collab_filter', 'ranker', 'session_embeddings',
        'lastfm_candidates', 'sasrec', 'session_gru', 'music_map',
    ];
    const RICH_DETAIL_STEPS = new Set(['sessionizer', 'track_scoring', 'taste_profiles', 'ranker']);

    const MODEL_ROWS = [
        { key: 'ranker', label: 'Ranker', sub: 'LightGBM' },
        { key: 'collab_filter', label: 'Collaborative', sub: 'user/item CF' },
        { key: 'session_embeddings', label: 'Embeddings', sub: 'session skip-gram' },
        { key: 'sasrec', label: 'SASRec', sub: 'sequential' },
        { key: 'session_gru', label: 'Session GRU', sub: 'taste drift' },
        { key: 'lastfm_cache', label: 'Last.fm cache', sub: 'similar-track CF' },
    ];

    const QUICK_RUN_ROWS = [
        { name: 'Run pipeline', href: '#/actions/pipeline-ml', defaultSub: 'manual trigger' },
        { name: 'Scan library', href: '#/actions/library', defaultSub: 'rescan music root' },
        { name: 'Build charts', href: '#/actions/charts', defaultSub: 'rebuild Last.fm charts' },
        { name: 'Backfill CLAP', href: '#/actions/pipeline-ml', defaultSub: 'fill missing CLAP embeddings' },
    ];

    /* =====================================================================
     * Monitor → Overview (session 02)
     * ===================================================================== */

    function renderOverview(root) {
        const state = {
            range: '24h',
            stats: null,
            models: null,
            events: null,
            recentEvents: null,
            pipeline: null,
            lastUpdate: 0,
            destroyed: false,
        };

        const lastUpdateEl = document.createElement('span');
        lastUpdateEl.className = 'last-update';
        lastUpdateEl.textContent = 'last update · —';

        const rangeEl = GIQ.components.rangeToggle({
            values: ['1h', '24h', '7d', '30d'],
            current: state.range,
            onChange: (v) => { state.range = v; refreshAll(); },
        });

        const headerRight = document.createElement('div');
        headerRight.style.display = 'flex';
        headerRight.style.alignItems = 'center';
        headerRight.style.gap = '10px';
        headerRight.appendChild(lastUpdateEl);
        headerRight.appendChild(rangeEl);

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'MONITOR',
            title: 'Overview',
            right: headerRight,
        }));

        const body = document.createElement('div');
        body.className = 'overview-body';
        root.appendChild(body);

        const statRow = document.createElement('div');
        statRow.className = 'overview-stat-row';
        body.appendChild(statRow);

        const grid = document.createElement('div');
        grid.className = 'overview-grid';
        body.appendChild(grid);

        const leftCol = document.createElement('div');
        leftCol.className = 'overview-col';
        const rightCol = document.createElement('div');
        rightCol.className = 'overview-col';
        grid.appendChild(leftCol);
        grid.appendChild(rightCol);

        const eventChartHost = document.createElement('div');
        eventChartHost.className = 'panel-host';
        const twoUpHost = document.createElement('div');
        twoUpHost.className = 'overview-twoup';
        const recentEventsHost = document.createElement('div');
        recentEventsHost.className = 'panel-host';

        leftCol.appendChild(eventChartHost);
        leftCol.appendChild(twoUpHost);
        leftCol.appendChild(recentEventsHost);

        const topTracksHost = document.createElement('div');
        topTracksHost.className = 'panel-host';
        const eventTypesHost = document.createElement('div');
        eventTypesHost.className = 'panel-host';
        twoUpHost.appendChild(topTracksHost);
        twoUpHost.appendChild(eventTypesHost);

        const modelsHost = document.createElement('div');
        modelsHost.className = 'panel-host';
        const scanHost = document.createElement('div');
        scanHost.className = 'panel-host';
        const quickRunHost = document.createElement('div');
        quickRunHost.className = 'panel-host';
        rightCol.appendChild(modelsHost);
        rightCol.appendChild(scanHost);
        rightCol.appendChild(quickRunHost);

        renderOverviewStatRow(statRow, state);
        renderOverviewEventChart(eventChartHost, state);
        renderOverviewTopTracks(topTracksHost, state);
        renderOverviewEventTypes(eventTypesHost, state);
        renderOverviewRecentEvents(recentEventsHost, state);
        renderOverviewModels(modelsHost, state);
        renderOverviewScan(scanHost, state);
        renderOverviewQuickRun(quickRunHost, state);

        async function refreshAll() {
            if (state.destroyed) return;
            const apiKey = GIQ.state.apiKey;
            if (!apiKey) {
                renderOverviewStatRow(statRow, state);
                lastUpdateEl.textContent = 'connect API key to load data';
                return;
            }
            const [stats, models, events, recent, pipeline] = await Promise.all([
                GIQ.api.get('/v1/stats').catch((e) => ({ _err: e })),
                GIQ.api.get('/v1/pipeline/models').catch((e) => ({ _err: e })),
                GIQ.api.get('/v1/pipeline/stats/events').catch((e) => ({ _err: e })),
                GIQ.api.get('/v1/events?limit=4').catch((e) => ({ _err: e })),
                GIQ.api.get('/v1/pipeline/status?limit=10').catch((e) => ({ _err: e })),
            ]);
            if (state.destroyed) return;
            state.stats = stats && !stats._err ? stats : null;
            state.models = models && !models._err ? models : null;
            state.events = events && !events._err ? events : null;
            state.recentEvents = Array.isArray(recent) ? recent : null;
            state.pipeline = pipeline && !pipeline._err ? pipeline : null;
            state.lastUpdate = Date.now();

            renderOverviewStatRow(statRow, state);
            renderOverviewEventChart(eventChartHost, state);
            renderOverviewTopTracks(topTracksHost, state);
            renderOverviewEventTypes(eventTypesHost, state);
            renderOverviewRecentEvents(recentEventsHost, state);
            renderOverviewModels(modelsHost, state);
            renderOverviewScan(scanHost, state);
            renderOverviewQuickRun(quickRunHost, state);

            lastUpdateEl.textContent = 'last update · just now';
        }

        refreshAll();

        const fullTimer = setInterval(refreshAll, 15000);
        const recentTimer = setInterval(async () => {
            if (state.destroyed) return;
            if (!GIQ.state.apiKey) return;
            try {
                const recent = await GIQ.api.get('/v1/events?limit=4');
                state.recentEvents = Array.isArray(recent) ? recent : null;
                renderOverviewRecentEvents(recentEventsHost, state);
            } catch (_) { /* ignore */ }
        }, 5000);

        const tickTimer = setInterval(() => {
            if (state.destroyed) return;
            if (!state.lastUpdate) return;
            const ago = Math.floor((Date.now() - state.lastUpdate) / 1000);
            lastUpdateEl.textContent = 'last update · ' + (
                ago < 60 ? ago + 's ago'
                : ago < 3600 ? Math.floor(ago / 60) + 'm ago'
                : Math.floor(ago / 3600) + 'h ago'
            );
        }, 1000);

        const sseUnsubs = [];
        if (GIQ.sse) {
            const refreshOnPipeline = () => refreshAll();
            sseUnsubs.push(GIQ.sse.subscribe('pipeline_start', refreshOnPipeline));
            sseUnsubs.push(GIQ.sse.subscribe('pipeline_end', refreshOnPipeline));
            sseUnsubs.push(GIQ.sse.subscribe('step_complete', refreshOnPipeline));
        }

        return function cleanup() {
            state.destroyed = true;
            clearInterval(fullTimer);
            clearInterval(recentTimer);
            clearInterval(tickTimer);
            sseUnsubs.forEach(u => u && u());
        };
    }

    function renderOverviewStatRow(host, state) {
        host.innerHTML = '';
        const stats = state.stats;
        const models = state.models;

        const tiles = [];

        const totalEvents = stats?.total_events;
        const e24 = stats?.events_last_24h;
        tiles.push(GIQ.components.statTile({
            label: 'Events',
            value: totalEvents != null ? Number(totalEvents).toLocaleString() : '—',
            delta: e24 != null ? '+' + Number(e24).toLocaleString() + ' / 24h' : null,
            deltaKind: e24 > 0 ? 'good' : 'flat',
        }));
        tiles.push(GIQ.components.statTile({
            label: 'Users',
            value: stats?.total_users != null ? String(stats.total_users) : '—',
            delta: null, deltaKind: 'flat',
        }));
        tiles.push(GIQ.components.statTile({
            label: 'Tracks',
            value: stats?.total_tracks_analyzed != null
                ? Number(stats.total_tracks_analyzed).toLocaleString() : '—',
            delta: stats?.analysis_version ? 'analysis v' + stats.analysis_version : null,
            deltaKind: 'flat',
        }));
        tiles.push(GIQ.components.statTile({
            label: 'Playlists',
            value: stats?.total_playlists != null ? String(stats.total_playlists) : '—',
            delta: null, deltaKind: 'flat',
        }));
        let perHrDelta = null;
        let perHrKind = 'flat';
        if (stats?.events_last_1h != null && stats?.events_last_24h != null) {
            const avg = stats.events_last_24h / 24;
            if (avg > 0) {
                const ratio = (stats.events_last_1h - avg) / avg;
                const sign = ratio >= 0 ? '+' : '−';
                perHrDelta = sign + Math.abs(Math.round(ratio * 100)) + '% vs 24h avg';
                perHrKind = ratio > 0.02 ? 'good' : ratio < -0.02 ? 'bad' : 'flat';
            }
        }
        tiles.push(GIQ.components.statTile({
            label: 'Events / hr',
            value: stats?.events_last_1h != null
                ? Number(stats.events_last_1h).toLocaleString() : '—',
            delta: perHrDelta, deltaKind: perHrKind,
        }));
        const r = models?.ranker;
        const rankerReady = r && (r.trained || r.built);
        let rankerDelta = null;
        if (models?.latest_evaluation?.ndcg_at_10 != null) {
            rankerDelta = 'ndcg ' + models.latest_evaluation.ndcg_at_10.toFixed(3);
        } else if (r?.training_samples) {
            rankerDelta = Number(r.training_samples).toLocaleString() + ' samples';
        }
        tiles.push(GIQ.components.statTile({
            label: 'Ranker',
            value: rankerReady ? 'ready' : (r ? 'not trained' : '—'),
            delta: rankerDelta,
            deltaKind: rankerReady ? 'good' : 'flat',
        }));

        tiles.forEach(t => host.appendChild(t));
    }

    function renderOverviewEventChart(host, state) {
        host.innerHTML = '';
        const evs = state.events;
        const buckets = (evs && evs.buckets) || [];

        let series = [];
        let labels = [];
        if (buckets.length) {
            const total = buckets.map(b => b.count || 0);
            series = [{
                name: 'All events',
                color: '#a887ce',
                values: total,
                strokeWidth: 2,
                fillOpacity: 0.45,
            }];
            const n = buckets.length;
            const fmtHM = (ts) => {
                const d = new Date(ts * 1000);
                const hh = String(d.getHours()).padStart(2, '0');
                const mm = String(d.getMinutes()).padStart(2, '0');
                return hh + ':' + mm;
            };
            for (let i = 0; i < 7; i++) {
                const idx = Math.round(i * (n - 1) / 6);
                labels.push(fmtHM(buckets[idx].timestamp));
            }
        }

        const chart = GIQ.components.areaChart({
            series, labels, height: 180,
            emptyText: state.events ? 'No events in window' : 'Loading…',
        });

        const action = document.createElement('a');
        action.className = 'panel-action';
        action.textContent = 'View full breakdown →';
        action.href = '#/monitor/system-health';

        host.appendChild(GIQ.components.panel({
            title: 'Event ingest',
            sub: 'play_end · like · skip · pause · etc · last 24h · 15m bins',
            action, children: chart,
        }));
    }

    function renderOverviewTopTracks(host, state) {
        host.innerHTML = '';
        const tracks = (state.stats?.top_tracks_24h) || [];
        const max = tracks.reduce((m, t) => Math.max(m, t.events || 0), 1);

        const list = document.createElement('div');
        list.className = 'top-tracks-list';
        if (!tracks.length) {
            list.innerHTML = '<div class="empty-row">No track activity in the last 24h.</div>';
        } else {
            tracks.slice(0, 6).forEach((t, i) => {
                const row = document.createElement('div');
                row.className = 'top-track-row';
                const title = t.title || t.track_id || '—';
                const artist = t.artist || '—';
                const n = t.events || 0;
                const pct = Math.max(2, Math.min(100, (n / max) * 100));
                row.innerHTML = '<div class="rank-chip">' + (i + 1) + '</div>'
                    + '<div class="top-track-meta">'
                    + '<div class="top-track-title">' + GIQ.fmt.esc(title) + '</div>'
                    + '<div class="top-track-artist">' + GIQ.fmt.esc(artist) + '</div>'
                    + '</div>'
                    + '<div class="top-track-bar"><div class="top-track-bar-fill"'
                        + ' style="width:' + pct.toFixed(1) + '%"></div></div>'
                    + '<div class="top-track-count">' + n + '</div>';
                list.appendChild(row);
            });
        }

        host.appendChild(GIQ.components.panel({
            title: 'Top tracks',
            sub: 'last 24h · by play_end count',
            children: list,
        }));
    }

    function renderOverviewEventTypes(host, state) {
        host.innerHTML = '';
        const types = state.stats?.event_types_24h || {};
        const keys = Object.keys(types);

        const list = document.createElement('div');
        list.className = 'event-types-list';
        if (!keys.length) {
            list.innerHTML = '<div class="empty-row">No events in window.</div>';
        } else {
            keys.sort((a, b) => (types[b] || 0) - (types[a] || 0));
            const max = keys.reduce((m, k) => Math.max(m, types[k] || 0), 1);
            keys.forEach(k => {
                const v = types[k] || 0;
                const cls = colorClassForEvent(k);
                const row = document.createElement('div');
                row.className = 'event-type-row';
                row.innerHTML = '<div class="event-type-head">'
                    + '<span class="event-type-name">' + GIQ.fmt.esc(k) + '</span>'
                    + '<span class="event-type-val">' + Number(v).toLocaleString() + '</span>'
                    + '</div>'
                    + '<div class="event-type-track">'
                    + '<div class="event-type-fill ' + cls + '" style="width:'
                        + Math.max(2, Math.min(100, (v / max) * 100)).toFixed(1) + '%"></div>'
                    + '</div>';
                list.appendChild(row);
            });
        }

        host.appendChild(GIQ.components.panel({
            title: 'Event types',
            sub: 'last 24h · proportion',
            children: list,
        }));
    }

    function colorClassForEvent(name) {
        if (name === 'dislike') return 'wine';
        if (name === 'play_end' || name === 'like' || name === 'play_start') return 'accent';
        return 'muted';
    }

    function renderOverviewRecentEvents(host, state) {
        host.innerHTML = '';
        const evs = state.recentEvents || [];

        const list = document.createElement('div');
        list.className = 'recent-events-list';
        if (!evs.length) {
            list.innerHTML = '<div class="empty-row">No recent events.</div>';
        } else {
            evs.forEach(e => {
                const row = document.createElement('div');
                row.className = 'recent-event-row';
                let dur = '';
                if (e.dwell_ms != null && e.dwell_ms > 0) {
                    dur = formatDur(Math.round(e.dwell_ms / 1000));
                } else if (e.value != null && (e.event_type === 'play_end' || e.event_type === 'skip')) {
                    dur = (e.value * 100).toFixed(0) + '%';
                }
                const trackLabel = e.track_id || '—';
                row.innerHTML = '<div class="recent-event-time">'
                        + GIQ.fmt.esc(GIQ.fmt.timeAgo(e.timestamp)) + '</div>'
                    + '<div class="recent-event-type">' + GIQ.fmt.esc(e.event_type || '?') + '</div>'
                    + '<div class="recent-event-user">' + GIQ.fmt.esc(e.user_id || '—') + '</div>'
                    + '<div class="recent-event-track">' + GIQ.fmt.esc(trackLabel) + '</div>'
                    + '<div class="recent-event-dur">' + GIQ.fmt.esc(dur) + '</div>';
                list.appendChild(row);
            });
        }

        host.appendChild(GIQ.components.panel({
            title: 'Recent events',
            sub: 'live tail · 4 most recent',
            badge: 'LIVE',
            children: list,
        }));
    }

    function formatDur(secs) {
        if (secs == null) return '';
        const m = Math.floor(secs / 60);
        const s = Math.floor(secs % 60);
        return m + ':' + String(s).padStart(2, '0');
    }

    function renderOverviewModels(host, state) {
        host.innerHTML = '';
        const models = state.models || {};

        const list = buildModelsSummaryList(models);

        const action = document.createElement('a');
        action.className = 'panel-action';
        action.textContent = 'See all →';
        action.href = '#/monitor/models';

        host.appendChild(GIQ.components.panel({
            title: 'Models',
            sub: 'readiness · 6 surfaces',
            action, children: list,
        }));
    }

    function buildModelsSummaryList(models) {
        const list = document.createElement('div');
        list.className = 'models-list';
        MODEL_ROWS.forEach((m) => {
            const data = models?.[m.key];
            const ready = !!(data && (data.trained || data.built));
            const stateLabel = ready ? 'ready' : (data ? 'stale' : '—');
            const subParts = [];
            if (data) {
                if (data.training_samples) subParts.push(Number(data.training_samples).toLocaleString() + ' samples');
                if (data.vocab_size) subParts.push(Number(data.vocab_size).toLocaleString() + ' vocab');
                if (data.n_features) subParts.push(data.n_features + ' features');
                if (data.users) subParts.push(data.users + ' users');
                if (data.tracks) subParts.push(Number(data.tracks).toLocaleString() + ' tracks');
                if (data.seeds_cached) subParts.push(data.seeds_cached + ' seeds');
                if (data.trained_at) subParts.push(GIQ.fmt.timeAgo(data.trained_at));
            } else {
                subParts.push('no data');
            }
            const sub = subParts.length ? subParts.slice(0, 2).join(' · ') : m.sub;
            const dotCls = ready ? 'good' : (data ? 'wine' : 'muted');
            const stateCls = ready ? 'good' : (data ? 'wine' : 'muted');
            const row = document.createElement('div');
            row.className = 'model-row';
            row.innerHTML = '<span class="model-dot dot-' + dotCls + '"></span>'
                + '<div class="model-meta">'
                + '<div class="model-name">' + GIQ.fmt.esc(m.label) + '</div>'
                + '<div class="model-sub">' + GIQ.fmt.esc(sub) + '</div>'
                + '</div>'
                + '<div class="model-state state-' + stateCls + '">' + GIQ.fmt.esc(stateLabel) + '</div>';
            list.appendChild(row);
        });
        return list;
    }

    function renderOverviewScan(host, state) {
        host.innerHTML = '';
        const scan = state.stats?.latest_scan;
        const running = scan && scan.status === 'running';

        const body = document.createElement('div');
        body.className = 'scan-body';

        if (!scan) {
            body.innerHTML = '<div class="empty-row">No scans yet.</div>'
                + '<a class="panel-action" href="#/actions/library">Start scan →</a>';
        } else {
            const found = scan.files_found || 0;
            const proc = (scan.files_analyzed || 0) + (scan.files_skipped || 0) + (scan.files_failed || 0);
            const pct = scan.percent_complete != null
                ? Number(scan.percent_complete).toFixed(0)
                : (found > 0 ? Math.round(proc / found * 100) : 0);

            body.innerHTML = '<div class="scan-progress-head">'
                + '<span>' + Number(scan.files_analyzed || 0).toLocaleString()
                    + ' / ~' + Number(found).toLocaleString() + ' files</span>'
                + '<span class="scan-pct">' + pct + '%</span>'
                + '</div>'
                + '<div class="scan-bar"><div class="scan-bar-fill" style="width:'
                    + pct + '%"></div></div>'
                + '<div class="scan-mini-grid">'
                + scanCell('Found', Number(found).toLocaleString())
                + scanCell('Analyzed', Number(scan.files_analyzed || 0).toLocaleString())
                + scanCell('Skipped', Number(scan.files_skipped || 0).toLocaleString())
                + scanCell('Failed', Number(scan.files_failed || 0).toLocaleString())
                + '</div>';
        }

        let sub = 'idle';
        if (running) {
            const phase = scan?.current_file ? 'indexing' : 'preparing';
            sub = 'phase · ' + phase;
        } else if (scan) {
            sub = 'last run · ' + (scan.status || 'unknown');
        } else {
            sub = 'no runs yet';
        }

        host.appendChild(GIQ.components.panel({
            title: 'Library scan', sub,
            badge: running ? 'LIVE' : null,
            children: body,
        }));
    }

    function scanCell(label, value) {
        return '<div class="scan-cell">'
            + '<div class="eyebrow">' + GIQ.fmt.esc(label) + '</div>'
            + '<div class="scan-cell-value">' + GIQ.fmt.esc(value) + '</div>'
            + '</div>';
    }

    function renderOverviewQuickRun(host, state) {
        host.innerHTML = '';
        const list = document.createElement('div');
        list.className = 'quick-run-list';

        const runs = state.pipeline?.history || [];
        let pipelineSub = 'no recent runs';
        const current = state.pipeline?.current;
        if (current && current.status === 'running') {
            pipelineSub = 'running';
        } else if (runs.length) {
            const latest = runs[0];
            const ts = latest.ended_at || latest.started_at;
            pipelineSub = (ts ? GIQ.fmt.timeAgo(ts) : 'recently')
                + ' · ' + (latest.status || 'ok');
        }

        const scan = state.stats?.latest_scan;
        let scanSub = 'no scans yet';
        if (scan) {
            if (scan.status === 'running') scanSub = 'running';
            else if (scan.ended_at) scanSub = GIQ.fmt.timeAgo(scan.ended_at) + ' · ' + (scan.status || 'ok');
            else scanSub = scan.status || 'unknown';
        }

        const subs = {
            'Run pipeline': pipelineSub,
            'Scan library': scanSub,
            'Build charts': QUICK_RUN_ROWS[2].defaultSub,
            'Backfill CLAP': QUICK_RUN_ROWS[3].defaultSub,
        };

        QUICK_RUN_ROWS.forEach((q) => {
            const row = document.createElement('a');
            row.className = 'quick-run-row';
            row.href = q.href;
            row.innerHTML = '<div class="quick-run-meta">'
                + '<div class="quick-run-name">' + GIQ.fmt.esc(q.name) + '</div>'
                + '<div class="quick-run-sub">' + GIQ.fmt.esc(subs[q.name] || q.defaultSub) + '</div>'
                + '</div>'
                + '<span class="quick-run-arrow">→</span>';
            list.appendChild(row);
        });

        host.appendChild(GIQ.components.panel({
            title: 'Quick run',
            sub: 'jumps to Actions',
            children: list,
        }));
    }

    /* =====================================================================
     * Monitor → Pipeline (session 06)
     * ===================================================================== */

    function renderPipeline(root) {
        const state = {
            destroyed: false,
            current: null,
            history: [],
            models: null,
            selectedRunId: null,
            selectedStep: null,
            stepDetailCache: {},
        };

        const sseToggleBtn = document.createElement('button');
        sseToggleBtn.type = 'button';
        sseToggleBtn.className = 'vc-btn vc-btn-ghost';
        const runBtn = document.createElement('a');
        runBtn.href = '#/actions/pipeline-ml';
        runBtn.className = 'vc-btn vc-btn-primary';
        runBtn.textContent = 'Run Pipeline';
        const resetBtn = document.createElement('a');
        resetBtn.href = '#/actions/pipeline-ml';
        resetBtn.className = 'vc-btn vc-btn-ghost';
        resetBtn.textContent = 'Reset Pipeline';

        function syncSseBtn() {
            const on = !!(GIQ.sse && GIQ.sse.isConnected());
            sseToggleBtn.textContent = on ? 'Disconnect SSE' : 'Connect SSE';
        }
        sseToggleBtn.addEventListener('click', () => {
            if (!GIQ.sse) return;
            if (GIQ.sse.isConnected()) GIQ.sse.disconnect();
            else GIQ.sse.connect();
            syncSseBtn();
        });
        syncSseBtn();

        const headerRight = document.createElement('div');
        headerRight.style.display = 'flex';
        headerRight.style.alignItems = 'center';
        headerRight.style.gap = '8px';
        headerRight.appendChild(sseToggleBtn);
        headerRight.appendChild(runBtn);
        headerRight.appendChild(resetBtn);

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'MONITOR',
            title: 'Pipeline',
            right: headerRight,
        }));

        const body = document.createElement('div');
        body.className = 'pipeline-body';
        root.appendChild(body);

        const runHeaderHost = document.createElement('div');
        runHeaderHost.className = 'panel-host';
        const flowHost = document.createElement('div');
        flowHost.className = 'panel-host';
        const stepDetailHost = document.createElement('div');
        stepDetailHost.className = 'panel-host pipeline-step-detail-host';
        const richDetailHost = document.createElement('div');
        richDetailHost.className = 'panel-host pipeline-rich-detail-host';
        const grid = document.createElement('div');
        grid.className = 'pipeline-bottom-grid';
        const modelsSummaryHost = document.createElement('div');
        modelsSummaryHost.className = 'panel-host';
        const errorsHost = document.createElement('div');
        errorsHost.className = 'panel-host';
        const historyHost = document.createElement('div');
        historyHost.className = 'panel-host';

        body.appendChild(runHeaderHost);
        body.appendChild(flowHost);
        body.appendChild(stepDetailHost);
        body.appendChild(richDetailHost);
        body.appendChild(grid);
        grid.appendChild(modelsSummaryHost);
        grid.appendChild(errorsHost);
        body.appendChild(historyHost);

        async function refreshAll() {
            if (state.destroyed) return;
            if (!GIQ.state.apiKey) {
                renderEmpty();
                return;
            }
            try {
                const [status, models] = await Promise.all([
                    GIQ.api.get('/v1/pipeline/status?limit=20').catch(() => null),
                    GIQ.api.get('/v1/pipeline/models').catch(() => null),
                ]);
                if (state.destroyed) return;
                state.current = status?.current || null;
                state.history = (status && Array.isArray(status.history)) ? status.history : [];
                state.models = models;
                if (state.current) state.selectedRunId = state.current.run_id;
                else if (!state.selectedRunId && state.history.length) state.selectedRunId = state.history[0].run_id;
                renderAll();
            } catch (e) {
                console.error('pipeline refresh failed', e);
            }
        }

        function selectedRun() {
            if (!state.selectedRunId) return state.current || (state.history[0] || null);
            if (state.current && state.current.run_id === state.selectedRunId) return state.current;
            for (const r of state.history) if (r.run_id === state.selectedRunId) return r;
            return state.current || (state.history[0] || null);
        }

        function renderEmpty() {
            runHeaderHost.innerHTML = '<div class="pipeline-empty">Connect API key to load pipeline status.</div>';
            flowHost.innerHTML = '';
            stepDetailHost.innerHTML = '';
            richDetailHost.innerHTML = '';
            modelsSummaryHost.innerHTML = '';
            errorsHost.innerHTML = '';
            historyHost.innerHTML = '';
        }

        function renderAll() {
            renderRunHeader(runHeaderHost, selectedRun());
            renderFlow(flowHost, selectedRun(), state.selectedStep, (name) => {
                state.selectedStep = state.selectedStep === name ? null : name;
                renderAll();
            });
            renderStepDetail(stepDetailHost, selectedRun(), state.selectedStep);
            renderRichDetail(richDetailHost, state.selectedStep, state);
            renderModelsSummary(modelsSummaryHost, state.models);
            renderErrors(errorsHost, state.history);
            renderHistory(historyHost, state.history, state.selectedRunId, (rid) => {
                state.selectedRunId = rid;
                state.selectedStep = null;
                renderAll();
            });
        }

        refreshAll();
        const fullTimer = setInterval(refreshAll, 15000);

        const sseUnsubs = [];
        if (GIQ.sse) {
            const refreshOnPipelineEdge = () => refreshAll();
            const onStepEvent = (data) => {
                if (state.destroyed) return;
                if (!data || !data.run_id) return;
                let target = null;
                if (state.current && state.current.run_id === data.run_id) target = state.current;
                else for (const r of state.history) if (r.run_id === data.run_id) { target = r; break; }
                if (!target || !Array.isArray(target.steps)) return;
                const step = target.steps.find(s => s.name === data.step);
                if (!step) return;
                if (data.event === 'step_start' || (data.status === 'running')) {
                    step.status = 'running';
                    step.started_at = data.timestamp || step.started_at;
                } else if (data.duration_ms != null && data.error) {
                    step.status = 'failed';
                    step.duration_ms = data.duration_ms;
                    step.error = data.error;
                    step.ended_at = data.timestamp || step.ended_at;
                } else if (data.duration_ms != null) {
                    step.status = 'completed';
                    step.duration_ms = data.duration_ms;
                    step.metrics = data.metrics || step.metrics || {};
                    step.ended_at = data.timestamp || step.ended_at;
                }
                renderAll();
            };
            sseUnsubs.push(GIQ.sse.subscribe('pipeline_start', refreshOnPipelineEdge));
            sseUnsubs.push(GIQ.sse.subscribe('pipeline_end', refreshOnPipelineEdge));
            sseUnsubs.push(GIQ.sse.subscribe('step_start', (d) => onStepEvent({ ...(d || {}), event: 'step_start' })));
            sseUnsubs.push(GIQ.sse.subscribe('step_complete', (d) => onStepEvent({ ...(d || {}), event: 'step_complete' })));
            sseUnsubs.push(GIQ.sse.subscribe('step_failed', (d) => onStepEvent({ ...(d || {}), event: 'step_failed' })));
            sseUnsubs.push(GIQ.sse.subscribe('connected', () => syncSseBtn()));
            sseUnsubs.push(GIQ.sse.subscribe('disconnected', () => syncSseBtn()));
        }

        return function cleanup() {
            state.destroyed = true;
            clearInterval(fullTimer);
            sseUnsubs.forEach(u => u && u());
        };
    }

    function renderRunHeader(host, run) {
        host.innerHTML = '';
        if (!run) {
            host.appendChild(GIQ.components.panel({
                title: 'Pipeline run',
                sub: 'no runs yet',
                children: '<div class="empty-row">No pipeline has run on this server yet.</div>',
            }));
            return;
        }
        const wrap = document.createElement('div');
        wrap.className = 'run-header-grid';
        wrap.innerHTML = ''
            + chip('TRIGGER', run.trigger || '—', 'mono')
            + chip('STATUS', (run.status || 'unknown').toUpperCase(),
                run.status === 'completed' ? 'good' : run.status === 'failed' ? 'wine' : 'muted')
            + chip('CONFIG', run.config_version != null ? 'v' + run.config_version : '—', 'mono')
            + chip('RUN ID', String(run.run_id || '').slice(0, 12), 'mono small')
            + chip('DURATION', run.duration_ms != null ? fmtMs(run.duration_ms) : '—', 'mono')
            + chip('STARTED', run.started_at ? GIQ.fmt.fmtTime(run.started_at) : '—', 'mono small');
        host.appendChild(GIQ.components.panel({
            title: 'Run header',
            sub: 'current / latest pipeline run',
            children: wrap,
        }));
    }

    function chip(label, value, kind) {
        const k = (kind || '').split(' ').filter(Boolean);
        return '<div class="run-chip ' + k.join(' ') + '">'
            + '<div class="eyebrow">' + GIQ.fmt.esc(label) + '</div>'
            + '<div class="run-chip-value">' + GIQ.fmt.esc(value) + '</div>'
            + '</div>';
    }

    function renderFlow(host, run, selectedStep, onSelect) {
        host.innerHTML = '';
        const flow = document.createElement('div');
        flow.className = 'pipeline-flow';

        STEP_ORDER.forEach((name, i) => {
            const meta = STEP_META[name] || { icon: '·', label: name, desc: '' };
            let step = null;
            if (run && Array.isArray(run.steps)) step = run.steps.find(s => s.name === name) || null;
            const status = step ? (step.status || 'pending') : 'pending';

            if (i > 0) {
                const arrow = document.createElement('div');
                arrow.className = 'pipe-arrow ' + (status === 'completed' ? 'done' : status === 'running' ? 'active' : '');
                arrow.textContent = '→';
                flow.appendChild(arrow);
            }

            const node = document.createElement('button');
            node.type = 'button';
            node.className = 'pipe-node status-' + status + (selectedStep === name ? ' selected' : '');
            node.addEventListener('click', () => onSelect(name));

            const head = document.createElement('div');
            head.className = 'pipe-node-head';
            head.innerHTML = '<span class="pipe-icon">' + GIQ.fmt.esc(meta.icon) + '</span>'
                + '<span class="pipe-label">' + GIQ.fmt.esc(meta.label) + '</span>';
            node.appendChild(head);

            const sub = document.createElement('div');
            sub.className = 'pipe-sub mono';
            if (status === 'running') sub.textContent = 'running…';
            else if (step && step.duration_ms != null) sub.textContent = fmtMs(step.duration_ms);
            else sub.textContent = '—';
            node.appendChild(sub);

            const metric = document.createElement('div');
            metric.className = 'pipe-metric mono';
            if (step && step.metrics) {
                const mk = firstMetricKey(step.metrics);
                if (mk) metric.textContent = fmtMetricVal(step.metrics[mk]) + ' ' + fmtMetricLabel(mk);
            }
            node.appendChild(metric);

            flow.appendChild(node);
        });

        host.appendChild(GIQ.components.panel({
            title: 'Flow',
            sub: '10 steps · click any to expand',
            children: flow,
        }));
    }

    function renderStepDetail(host, run, selectedStep) {
        host.innerHTML = '';
        if (!selectedStep) return;
        const meta = STEP_META[selectedStep] || { icon: '·', label: selectedStep, desc: '' };
        let step = null;
        if (run && Array.isArray(run.steps)) step = run.steps.find(s => s.name === selectedStep) || null;
        if (!step) {
            host.appendChild(GIQ.components.panel({
                title: meta.label,
                sub: meta.desc,
                children: '<div class="empty-row">No data for this step in the selected run.</div>',
            }));
            return;
        }
        const body = document.createElement('div');
        body.className = 'step-detail-body';
        const status = step.status || 'pending';
        body.innerHTML = '<div class="step-detail-status-row">'
            + '<span class="step-status-chip status-' + status + '">' + GIQ.fmt.esc(status.toUpperCase()) + '</span>'
            + (step.duration_ms != null ? '<span class="mono muted">duration · ' + fmtMs(step.duration_ms) + '</span>' : '')
            + (step.started_at ? '<span class="mono muted">started · ' + GIQ.fmt.esc(GIQ.fmt.fmtTime(step.started_at)) + '</span>' : '')
            + '</div>';

        const metrics = step.metrics || {};
        const mkeys = Object.keys(metrics);
        if (mkeys.length) {
            const grid = document.createElement('div');
            grid.className = 'step-metric-grid';
            mkeys.forEach(k => {
                grid.innerHTML += '<div class="step-metric-cell">'
                    + '<div class="eyebrow">' + GIQ.fmt.esc(fmtMetricLabel(k)) + '</div>'
                    + '<div class="step-metric-val">' + GIQ.fmt.esc(fmtMetricVal(metrics[k])) + '</div>'
                    + '</div>';
            });
            body.appendChild(grid);
        }

        if (step.error) {
            const errBlock = document.createElement('div');
            errBlock.className = 'step-error-block';
            errBlock.innerHTML = '<div class="eyebrow">Error</div>'
                + '<pre></pre>';
            errBlock.querySelector('pre').textContent = step.error;
            body.appendChild(errBlock);
        }

        host.appendChild(GIQ.components.panel({
            title: meta.icon + ' ' + meta.label,
            sub: meta.desc,
            children: body,
        }));
    }

    function renderRichDetail(host, selectedStep, pipelineState) {
        host.innerHTML = '';
        if (!selectedStep || !RICH_DETAIL_STEPS.has(selectedStep)) return;

        const wrap = document.createElement('div');
        wrap.className = 'rich-detail-wrap';
        wrap.innerHTML = '<div class="empty-row">Loading rich detail…</div>';
        host.appendChild(wrap);

        if (selectedStep === 'sessionizer') {
            GIQ.api.get('/v1/pipeline/stats/sessionizer')
                .then(d => renderSessionizerRich(wrap, d))
                .catch(e => wrap.innerHTML = '<div class="empty-row" style="color:var(--wine)">' + GIQ.fmt.esc(e.message) + '</div>');
        } else if (selectedStep === 'track_scoring') {
            GIQ.api.get('/v1/pipeline/stats/scoring')
                .then(d => renderScoringRich(wrap, d))
                .catch(e => wrap.innerHTML = '<div class="empty-row" style="color:var(--wine)">' + GIQ.fmt.esc(e.message) + '</div>');
        } else if (selectedStep === 'taste_profiles') {
            renderTasteRich(wrap);
        } else if (selectedStep === 'ranker') {
            Promise.all([
                GIQ.api.get('/v1/pipeline/models').catch(() => null),
                GIQ.api.get('/v1/recommend/stats/model').catch(() => null),
            ]).then(([models, eval_]) => renderRankerRich(wrap, models, eval_))
              .catch(e => wrap.innerHTML = '<div class="empty-row" style="color:var(--wine)">' + GIQ.fmt.esc(e.message) + '</div>');
        }
    }

    function renderSessionizerRich(host, data) {
        host.innerHTML = '';
        if (!data || !data.total_sessions) {
            host.innerHTML = '<div class="empty-row">No sessions yet.</div>';
            return;
        }
        const stats = document.createElement('div');
        stats.className = 'rich-stats-row';
        stats.appendChild(GIQ.components.statTile({
            label: 'Total sessions', value: Number(data.total_sessions).toLocaleString(),
        }));
        stats.appendChild(GIQ.components.statTile({
            label: 'Avg duration', value: GIQ.fmt.fmtDuration(data.avg_duration_s),
        }));
        stats.appendChild(GIQ.components.statTile({
            label: 'Avg tracks / session', value: data.avg_tracks_per_session != null ? Number(data.avg_tracks_per_session).toFixed(1) : '—',
        }));
        stats.appendChild(GIQ.components.statTile({
            label: 'Avg skip rate', value: data.avg_skip_rate != null ? (data.avg_skip_rate * 100).toFixed(1) + '%' : '—',
        }));
        host.appendChild(stats);

        const grid = document.createElement('div');
        grid.className = 'rich-two-col';

        const distRows = [];
        const dist = data.skip_rate_distribution || {};
        for (const k of Object.keys(dist)) distRows.push({ label: k, value: dist[k] });
        const distMax = distRows.reduce((m, r) => Math.max(m, r.value), 1);
        grid.appendChild(GIQ.components.panel({
            title: 'Skip rate distribution',
            sub: 'sessions per bucket',
            children: barList(distRows, distMax, 'accent'),
        }));

        const perUser = (data.sessions_per_user || []).slice(0, 12);
        const userMax = perUser.reduce((m, r) => Math.max(m, r.sessions || 0), 1);
        grid.appendChild(GIQ.components.panel({
            title: 'Sessions per user',
            sub: 'top ' + Math.min(perUser.length, 12),
            children: barList(perUser.map(u => ({ label: u.user_id, value: u.sessions })), userMax, 'accent'),
        }));

        host.appendChild(grid);
    }

    function renderScoringRich(host, data) {
        host.innerHTML = '';
        if (!data || !data.total_interactions) {
            host.innerHTML = '<div class="empty-row">No interactions yet.</div>';
            return;
        }
        const top = document.createElement('div');
        top.className = 'rich-stats-row';
        top.appendChild(GIQ.components.statTile({
            label: 'Total interactions', value: Number(data.total_interactions).toLocaleString(),
        }));
        host.appendChild(top);

        const grid = document.createElement('div');
        grid.className = 'rich-two-col';
        const bins = data.score_distribution || [];
        const binMax = bins.reduce((m, b) => Math.max(m, b.count || 0), 1);
        const binRows = bins.map(b => ({ label: b.range, value: b.count || 0 }));
        grid.appendChild(GIQ.components.panel({
            title: 'Satisfaction distribution',
            sub: '10-bin histogram',
            children: barList(binRows, binMax, 'accent'),
        }));

        const sigKeys = ['full_listens', 'likes', 'repeats', 'playlist_adds', 'early_skips', 'dislikes'];
        const sig = data.signal_counts || {};
        const sigMax = sigKeys.reduce((m, k) => Math.max(m, sig[k] || 0), 1);
        const sigRows = sigKeys.map(k => ({
            label: fmtMetricLabel(k),
            value: sig[k] || 0,
            colorClass: (k === 'early_skips' || k === 'dislikes') ? 'wine' : 'accent',
        }));
        grid.appendChild(GIQ.components.panel({
            title: 'Signal breakdown',
            sub: 'positive vs negative',
            children: barList(sigRows, sigMax),
        }));
        host.appendChild(grid);

        const tracks = document.createElement('div');
        tracks.className = 'rich-two-col';
        tracks.appendChild(scoreTable('Top scored tracks', data.top_tracks || [], 'accent'));
        tracks.appendChild(scoreTable('Lowest scored tracks', data.bottom_tracks || [], 'wine'));
        host.appendChild(tracks);
    }

    function scoreTable(title, tracks, colorClass) {
        const list = document.createElement('div');
        list.className = 'rich-track-list';
        if (!tracks.length) {
            list.innerHTML = '<div class="empty-row">No data.</div>';
        } else {
            tracks.forEach(t => {
                const name = (t.artist && t.title) ? (t.artist + ' — ' + t.title) : (t.track_id || '—');
                const row = document.createElement('div');
                row.className = 'rich-track-row';
                row.innerHTML = '<div class="rich-track-name">' + GIQ.fmt.esc(name) + '</div>'
                    + '<div class="rich-track-score state-' + colorClass + '">' + GIQ.fmt.esc(t.score != null ? t.score.toFixed(3) : '—') + '</div>'
                    + '<div class="rich-track-plays mono muted">' + GIQ.fmt.esc(String(t.plays || 0)) + ' plays</div>';
                list.appendChild(row);
            });
        }
        return GIQ.components.panel({
            title, sub: 'by satisfaction score', children: list,
        });
    }

    function renderTasteRich(host) {
        host.innerHTML = '';
        const wrap = document.createElement('div');
        wrap.className = 'taste-rich-wrap';
        const head = document.createElement('div');
        head.className = 'taste-rich-head';
        head.innerHTML = '<label class="eyebrow">USER</label>'
            + '<select class="taste-user-select"><option value="">Select user…</option></select>';
        wrap.appendChild(head);
        const body = document.createElement('div');
        body.className = 'taste-rich-body';
        body.innerHTML = '<div class="empty-row">Pick a user to view their taste profile.</div>';
        wrap.appendChild(body);
        host.appendChild(wrap);

        const select = head.querySelector('select');
        GIQ.api.get('/v1/users').then(users => {
            const list = Array.isArray(users) ? users : (users?.users || []);
            list.forEach(u => {
                const o = document.createElement('option');
                o.value = u.user_id; o.textContent = u.user_id;
                select.appendChild(o);
            });
        }).catch(() => { /* ignore */ });

        select.addEventListener('change', async () => {
            const uid = select.value;
            if (!uid) { body.innerHTML = '<div class="empty-row">Pick a user to view their taste profile.</div>'; return; }
            body.innerHTML = '<div class="empty-row">Loading…</div>';
            try {
                const profile = await GIQ.api.get('/v1/users/' + encodeURIComponent(uid) + '/profile');
                renderTasteForUser(body, profile);
            } catch (e) {
                body.innerHTML = '<div class="empty-row" style="color:var(--wine)">' + GIQ.fmt.esc(e.message) + '</div>';
            }
        });
    }

    function renderTasteForUser(host, profile) {
        host.innerHTML = '';
        const tp = profile?.taste_profile;
        if (!tp) {
            host.innerHTML = '<div class="empty-row">No taste profile computed yet.</div>';
            return;
        }
        const grid = document.createElement('div');
        grid.className = 'rich-two-col';

        const ap = tp.audio_preferences || {};
        const ts = tp.timescale_audio || {};
        const shortP = ts.short || {};
        const longP = ts.long || {};
        const axes = ['energy', 'valence', 'danceability', 'acousticness', 'instrumentalness'];
        const labels = ['Energy', 'Valence', 'Dance', 'Acoustic', 'Instrum.'];
        const series = [
            { values: axes.map(a => ap[a]?.mean != null ? ap[a].mean : 0.5), color: '#a887ce', label: 'All-time' },
            { values: axes.map(a => shortP[a] != null ? shortP[a] : 0.5), color: '#9c526d', label: '7-day' },
            { values: axes.map(a => longP[a] != null ? longP[a] : 0.5), color: '#b8b0c4', label: 'Long' },
        ];
        const radarPanel = GIQ.components.panel({
            title: 'Audio preferences (radar)',
            sub: 'all-time · 7-day · long-term',
            children: buildRadarChart(axes, labels, series),
        });
        grid.appendChild(radarPanel);

        const timeP = tp.time_patterns || {};
        const heatmap = document.createElement('div');
        heatmap.className = 'taste-heatmap';
        let maxTime = 0.01;
        for (let i = 0; i < 24; i++) {
            const v = parseFloat(timeP[String(i)] || 0);
            if (v > maxTime) maxTime = v;
        }
        for (let i = 0; i < 24; i++) {
            const v = parseFloat(timeP[String(i)] || 0);
            const intensity = maxTime > 0 ? v / maxTime : 0;
            const cell = document.createElement('div');
            cell.className = 'taste-heat-cell';
            cell.style.background = 'rgba(168, 135, 206, ' + (intensity * 0.85 + 0.05).toFixed(2) + ')';
            cell.title = i + ':00 — ' + v.toFixed(3);
            cell.textContent = i;
            heatmap.appendChild(cell);
        }
        grid.appendChild(GIQ.components.panel({
            title: 'Time-of-day pattern',
            sub: '24 hourly buckets',
            children: heatmap,
        }));
        host.appendChild(grid);

        const patternGrid = document.createElement('div');
        patternGrid.className = 'rich-two-col';
        const patternSets = [
            ['Mood', tp.mood_preferences || {}],
            ['Device', tp.device_patterns || {}],
            ['Context type', tp.context_type_patterns || {}],
            ['Output', tp.output_patterns || {}],
            ['Location', tp.location_patterns || {}],
        ];
        patternSets.forEach(([name, data]) => {
            const keys = Object.keys(data).sort((a, b) => data[b] - data[a]);
            if (!keys.length) return;
            const max = data[keys[0]] || 1;
            const rows = keys.slice(0, 8).map(k => ({ label: k, value: data[k] }));
            patternGrid.appendChild(GIQ.components.panel({
                title: name + ' patterns',
                sub: keys.length + ' values',
                children: barList(rows, max, 'accent', { fmtVal: v => Number(v).toFixed(2) }),
            }));
        });
        host.appendChild(patternGrid);

        const b = tp.behaviour || {};
        if (b.total_plays != null) {
            const stats = document.createElement('div');
            stats.className = 'rich-stats-row';
            stats.appendChild(GIQ.components.statTile({ label: 'Total plays', value: Number(b.total_plays || 0).toLocaleString() }));
            stats.appendChild(GIQ.components.statTile({ label: 'Active days', value: String(b.active_days || 0) }));
            stats.appendChild(GIQ.components.statTile({ label: 'Avg session tracks', value: b.avg_session_tracks != null ? Number(b.avg_session_tracks).toFixed(1) : '—' }));
            stats.appendChild(GIQ.components.statTile({ label: 'Skip rate', value: b.skip_rate != null ? (b.skip_rate * 100).toFixed(1) + '%' : '—' }));
            stats.appendChild(GIQ.components.statTile({ label: 'Avg completion', value: b.avg_completion != null ? (b.avg_completion * 100).toFixed(1) + '%' : '—' }));
            host.appendChild(stats);
        }
    }

    function renderRankerRich(host, models, evalReport) {
        host.innerHTML = '';
        const ranker = models?.ranker;
        if (!ranker || !ranker.trained) {
            host.innerHTML = '<div class="empty-row">Ranker model not trained yet.</div>';
            return;
        }
        const stats = document.createElement('div');
        stats.className = 'rich-stats-row';
        stats.appendChild(GIQ.components.statTile({ label: 'Training samples', value: Number(ranker.training_samples || 0).toLocaleString() }));
        stats.appendChild(GIQ.components.statTile({ label: 'Features', value: String(ranker.n_features || 0) }));
        stats.appendChild(GIQ.components.statTile({ label: 'Engine', value: String(ranker.engine || '—') }));
        stats.appendChild(GIQ.components.statTile({ label: 'Trained', value: ranker.trained_at ? GIQ.fmt.timeAgo(ranker.trained_at) : '—' }));

        const ev = evalReport?.latest_evaluation || models?.latest_evaluation;
        if (ev?.ndcg_at_10 != null) {
            const lift = ev.lift_over_popularity_pct;
            stats.appendChild(GIQ.components.statTile({
                label: 'NDCG@10', value: ev.ndcg_at_10.toFixed(4),
                delta: lift != null ? (lift > 0 ? '+' : '') + lift + '% vs popularity' : null,
                deltaKind: lift > 0 ? 'good' : lift < 0 ? 'bad' : 'flat',
            }));
        }
        host.appendChild(stats);

        const fi = ranker.feature_importances || {};
        const fiKeys = Object.keys(fi).sort((a, b) => fi[b] - fi[a]).slice(0, 20);
        if (fiKeys.length) {
            const max = fi[fiKeys[0]] || 1;
            const rows = fiKeys.map(k => ({ label: k, value: fi[k] }));
            host.appendChild(GIQ.components.panel({
                title: 'Feature importance',
                sub: 'top 20',
                children: barList(rows, max, 'accent', { fmtVal: v => Math.round(v).toLocaleString() }),
            }));
        }

        if (ev) {
            const grid = document.createElement('div');
            grid.className = 'rich-two-col';
            const baseRows = [
                { label: 'Model', value: ev.ndcg_at_10 || 0, colorClass: 'accent' },
                { label: 'Popularity', value: ev.baseline_popularity_ndcg_at_10 || 0, colorClass: 'muted' },
                { label: 'Random', value: ev.baseline_random_ndcg_at_10 || 0, colorClass: 'muted' },
            ];
            const baseMax = baseRows.reduce((m, r) => Math.max(m, r.value), 0.001);
            grid.appendChild(GIQ.components.panel({
                title: 'NDCG@10 vs baselines',
                sub: 'absolute scores',
                children: barList(baseRows, baseMax, 'accent', { fmtVal: v => Number(v).toFixed(4) }),
            }));

            const imp = evalReport?.impressions || models?.impressions;
            if (imp) {
                const impMax = Math.max(imp.impressions || 1, 1);
                const impRows = [
                    { label: 'Impressions', value: imp.impressions || 0, colorClass: 'muted' },
                    { label: 'Streams', value: imp.streams_from_reco || 0, colorClass: 'accent' },
                ];
                const i2sNote = (imp.i2s_rate != null) ? '<div class="mono muted" style="margin-top:8px">I2S rate · ' + (imp.i2s_rate * 100).toFixed(1) + '%</div>' : '';
                const inner = document.createElement('div');
                inner.appendChild(barList(impRows, impMax));
                if (i2sNote) {
                    const note = document.createElement('div');
                    note.innerHTML = i2sNote;
                    inner.appendChild(note);
                }
                grid.appendChild(GIQ.components.panel({
                    title: 'Impression-to-stream funnel',
                    sub: 'cumulative',
                    children: inner,
                }));
            }
            host.appendChild(grid);
        }
    }

    function renderModelsSummary(host, models) {
        host.innerHTML = '';
        if (!models) return;
        const list = buildModelsSummaryList(models);
        const action = document.createElement('a');
        action.className = 'panel-action';
        action.textContent = 'See all →';
        action.href = '#/monitor/models';
        host.appendChild(GIQ.components.panel({
            title: 'Models',
            sub: 'readiness · 6 surfaces',
            action, children: list,
        }));
    }

    function renderErrors(host, history) {
        host.innerHTML = '';
        const errors = collectErrors(history);
        if (!errors.length) {
            host.appendChild(GIQ.components.panel({
                title: 'Errors',
                sub: 'last 20 across runs',
                children: '<div class="empty-row">No errors recorded.</div>',
            }));
            return;
        }
        const list = document.createElement('div');
        list.className = 'errors-list';
        errors.forEach(err => {
            const item = document.createElement('details');
            item.className = 'error-entry';
            const summary = document.createElement('summary');
            summary.innerHTML = '<span class="error-step">' + GIQ.fmt.esc(STEP_META[err.step]?.label || err.step) + '</span>'
                + '<span class="error-time mono muted">' + GIQ.fmt.esc(GIQ.fmt.fmtTime(err.timestamp)) + ' · run ' + GIQ.fmt.esc((err.run_id || '').slice(0, 8)) + '</span>';
            item.appendChild(summary);
            const pre = document.createElement('pre');
            pre.textContent = err.error || '';
            item.appendChild(pre);
            list.appendChild(item);
        });
        host.appendChild(GIQ.components.panel({
            title: 'Errors',
            sub: 'last ' + errors.length + ' across runs',
            children: list,
        }));
    }

    function collectErrors(runs) {
        const errs = [];
        (runs || []).forEach(run => {
            (run.steps || []).forEach(step => {
                if (step.error) {
                    errs.push({
                        run_id: run.run_id,
                        step: step.name,
                        error: step.error,
                        timestamp: step.ended_at || step.started_at || run.started_at,
                    });
                }
            });
        });
        errs.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
        return errs.slice(0, 20);
    }

    function renderHistory(host, history, selectedRunId, onSelect) {
        host.innerHTML = '';
        if (!history.length) {
            host.appendChild(GIQ.components.panel({
                title: 'Run history',
                sub: 'last 20 runs',
                children: '<div class="empty-row">No runs yet.</div>',
            }));
            return;
        }
        const table = document.createElement('div');
        table.className = 'history-table';
        const headRow = document.createElement('div');
        headRow.className = 'history-row history-head';
        headRow.innerHTML = '<span>Run ID</span><span>Status</span><span>Trigger</span><span>Cfg</span><span>Steps</span><span>Duration</span><span>When</span>';
        table.appendChild(headRow);
        history.forEach(rn => {
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'history-row' + (rn.run_id === selectedRunId ? ' selected' : '');
            row.addEventListener('click', () => onSelect(rn.run_id));
            const stepDots = (rn.steps || []).map(s =>
                '<span class="step-dot status-' + (s.status || 'pending') + '" title="' + GIQ.fmt.esc(s.name) + ' · ' + GIQ.fmt.esc(s.status || '?') + '"></span>').join('');
            const statusCls = rn.status === 'completed' ? 'good' : rn.status === 'failed' ? 'wine' : 'muted';
            row.innerHTML = '<span class="mono">' + GIQ.fmt.esc((rn.run_id || '').slice(0, 8)) + '</span>'
                + '<span class="status-chip status-' + (rn.status || 'pending') + '">' + GIQ.fmt.esc((rn.status || '?').toUpperCase()) + '</span>'
                + '<span class="mono muted">' + GIQ.fmt.esc(rn.trigger || '—') + '</span>'
                + '<span class="mono muted">' + (rn.config_version != null ? 'v' + rn.config_version : '—') + '</span>'
                + '<span class="step-dots">' + stepDots + '</span>'
                + '<span class="mono muted">' + GIQ.fmt.esc(rn.duration_ms != null ? fmtMs(rn.duration_ms) : '—') + '</span>'
                + '<span class="mono muted">' + GIQ.fmt.esc(rn.started_at ? GIQ.fmt.timeAgo(rn.started_at) : '—') + '</span>';
            // Override status chip color
            const chip = row.querySelector('.status-chip');
            if (chip) chip.className = 'status-chip state-' + statusCls;
            table.appendChild(row);
        });

        host.appendChild(GIQ.components.panel({
            title: 'Run history',
            sub: 'last ' + history.length + ' runs · click to select',
            children: table,
        }));
    }

    /* ---- Pipeline helpers -------------------------------------------- */

    function fmtMs(ms) {
        if (ms == null) return '—';
        if (ms < 1000) return ms + 'ms';
        if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
        return Math.floor(ms / 60000) + 'm ' + Math.round((ms % 60000) / 1000) + 's';
    }

    const PRIMARY_METRICS = [
        'sessions_created', 'interactions_scored', 'profiles_built',
        'training_samples', 'vocab_size', 'similar_users', 'tracks_mapped',
        'seeds_cached',
    ];
    function firstMetricKey(metrics) {
        if (!metrics) return null;
        for (const k of PRIMARY_METRICS) if (metrics[k] != null) return k;
        return Object.keys(metrics)[0] || null;
    }
    function fmtMetricLabel(k) {
        return String(k || '').replace(/_/g, ' ');
    }
    function fmtMetricVal(v) {
        if (v == null) return '—';
        if (typeof v === 'number') {
            if (!Number.isFinite(v)) return String(v);
            if (Number.isInteger(v)) return Number(v).toLocaleString();
            if (v >= 100) return Number(v.toFixed(0)).toLocaleString();
            return v.toFixed(3);
        }
        return String(v);
    }

    function barList(rows, max, defaultColorClass, opts) {
        const wrap = document.createElement('div');
        wrap.className = 'bar-list';
        if (!rows.length) {
            wrap.innerHTML = '<div class="empty-row">No data.</div>';
            return wrap;
        }
        const fmtVal = (opts && opts.fmtVal) || ((v) => Number(v).toLocaleString());
        rows.forEach(r => {
            const pct = max > 0 ? Math.max(2, Math.min(100, (r.value / max) * 100)) : 0;
            const cls = r.colorClass || defaultColorClass || 'accent';
            const row = document.createElement('div');
            row.className = 'bar-row';
            row.innerHTML = '<div class="bar-label">' + GIQ.fmt.esc(r.label) + '</div>'
                + '<div class="bar-track"><div class="bar-fill bar-fill-' + cls + '" style="width:' + pct.toFixed(1) + '%"></div></div>'
                + '<div class="bar-count mono">' + GIQ.fmt.esc(fmtVal(r.value)) + '</div>';
            wrap.appendChild(row);
        });
        return wrap;
    }

    function buildRadarChart(axes, labels, series) {
        const wrap = document.createElement('div');
        wrap.className = 'radar-wrap';
        const cx = 130, cy = 130, r = 96, n = axes.length;
        const svgNS = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('viewBox', '0 0 260 280');
        svg.classList.add('radar-svg');
        for (let ring = 1; ring <= 4; ring++) {
            const rr = r * ring / 4;
            const pts = [];
            for (let i = 0; i < n; i++) {
                const ang = (Math.PI * 2 * i / n) - Math.PI / 2;
                pts.push((cx + rr * Math.cos(ang)).toFixed(1) + ',' + (cy + rr * Math.sin(ang)).toFixed(1));
            }
            const poly = document.createElementNS(svgNS, 'polygon');
            poly.setAttribute('points', pts.join(' '));
            poly.setAttribute('fill', 'none');
            poly.setAttribute('stroke', ring === 4 ? 'rgba(236,232,242,0.18)' : 'rgba(236,232,242,0.06)');
            poly.setAttribute('stroke-width', '1');
            svg.appendChild(poly);
        }
        for (let i = 0; i < n; i++) {
            const ang = (Math.PI * 2 * i / n) - Math.PI / 2;
            const x2 = cx + r * Math.cos(ang);
            const y2 = cy + r * Math.sin(ang);
            const line = document.createElementNS(svgNS, 'line');
            line.setAttribute('x1', cx); line.setAttribute('y1', cy);
            line.setAttribute('x2', x2.toFixed(1)); line.setAttribute('y2', y2.toFixed(1));
            line.setAttribute('stroke', 'rgba(236,232,242,0.10)');
            line.setAttribute('stroke-width', '1');
            svg.appendChild(line);
            const lx = cx + (r + 14) * Math.cos(ang);
            const ly = cy + (r + 14) * Math.sin(ang);
            const text = document.createElementNS(svgNS, 'text');
            text.setAttribute('x', lx.toFixed(1));
            text.setAttribute('y', (ly + 4).toFixed(1));
            text.setAttribute('class', 'radar-axis-label');
            text.setAttribute('text-anchor', Math.abs(Math.cos(ang)) < 0.1 ? 'middle' : Math.cos(ang) > 0 ? 'start' : 'end');
            text.textContent = labels[i];
            svg.appendChild(text);
        }
        series.forEach(s => {
            const pts = [];
            for (let i = 0; i < n; i++) {
                const ang = (Math.PI * 2 * i / n) - Math.PI / 2;
                const v = Math.max(0, Math.min(1, s.values[i] || 0));
                pts.push((cx + r * v * Math.cos(ang)).toFixed(1) + ',' + (cy + r * v * Math.sin(ang)).toFixed(1));
            }
            const poly = document.createElementNS(svgNS, 'polygon');
            poly.setAttribute('points', pts.join(' '));
            poly.setAttribute('fill', s.color);
            poly.setAttribute('fill-opacity', '0.10');
            poly.setAttribute('stroke', s.color);
            poly.setAttribute('stroke-width', '2');
            svg.appendChild(poly);
            for (let i = 0; i < n; i++) {
                const ang = (Math.PI * 2 * i / n) - Math.PI / 2;
                const v = Math.max(0, Math.min(1, s.values[i] || 0));
                const c = document.createElementNS(svgNS, 'circle');
                c.setAttribute('cx', (cx + r * v * Math.cos(ang)).toFixed(1));
                c.setAttribute('cy', (cy + r * v * Math.sin(ang)).toFixed(1));
                c.setAttribute('r', '3');
                c.setAttribute('fill', s.color);
                svg.appendChild(c);
            }
        });
        wrap.appendChild(svg);

        const legend = document.createElement('div');
        legend.className = 'radar-legend';
        series.forEach(s => {
            const item = document.createElement('span');
            item.className = 'radar-legend-item';
            item.innerHTML = '<span class="radar-swatch" style="background:' + s.color + '"></span>'
                + '<span>' + GIQ.fmt.esc(s.label) + '</span>';
            legend.appendChild(item);
        });
        wrap.appendChild(legend);
        return wrap;
    }

    /* =====================================================================
     * Monitor → Models (session 06)
     * ===================================================================== */

    function renderModels(root) {
        const state = { destroyed: false, models: null, evalReport: null };

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'MONITOR',
            title: 'Models',
        }));

        const body = document.createElement('div');
        body.className = 'models-page-body';
        root.appendChild(body);

        const cardsHost = document.createElement('div');
        cardsHost.className = 'models-cards-grid';
        body.appendChild(cardsHost);

        const rankerHost = document.createElement('div');
        rankerHost.className = 'panel-host';
        body.appendChild(rankerHost);

        function refreshAll() {
            if (state.destroyed) return;
            if (!GIQ.state.apiKey) {
                cardsHost.innerHTML = '<div class="empty-row">Connect API key to load model status.</div>';
                rankerHost.innerHTML = '';
                return;
            }
            Promise.all([
                GIQ.api.get('/v1/pipeline/models').catch(() => null),
                GIQ.api.get('/v1/recommend/stats/model').catch(() => null),
            ]).then(([m, ev]) => {
                if (state.destroyed) return;
                state.models = m;
                state.evalReport = ev;
                renderAll();
            });
        }

        function renderAll() {
            cardsHost.innerHTML = '';
            const models = state.models || {};
            const cards = [
                { key: 'ranker', label: 'Ranker', sub: 'LightGBM' },
                { key: 'collab_filter', label: 'Collaborative Filter', sub: 'user/item CF' },
                { key: 'session_embeddings', label: 'Session Embeddings', sub: 'Word2Vec skip-gram' },
                { key: 'sasrec', label: 'SASRec', sub: 'Transformer (sequential)' },
                { key: 'session_gru', label: 'Session GRU', sub: 'Taste drift' },
                { key: 'lastfm_cache', label: 'Last.fm Cache', sub: 'External similar-track CF' },
            ];
            cards.forEach(c => cardsHost.appendChild(buildModelCard(c, models[c.key])));

            rankerHost.innerHTML = '';
            renderRankerRich(rankerHost, models, state.evalReport);
        }

        refreshAll();
        const timer = setInterval(refreshAll, 30000);

        return function cleanup() {
            state.destroyed = true;
            clearInterval(timer);
        };
    }

    function buildModelCard(meta, data) {
        const ready = !!(data && (data.trained || data.built));
        const card = document.createElement('section');
        card.className = 'model-card' + (ready ? '' : ' not-ready');
        const status = data ? (ready ? 'READY' : 'NOT TRAINED') : 'NO DATA';
        const stateCls = data ? (ready ? 'good' : 'wine') : 'muted';

        const head = document.createElement('div');
        head.className = 'model-card-head';
        head.innerHTML = '<div class="model-card-name">' + GIQ.fmt.esc(meta.label) + '</div>'
            + '<div class="model-card-state state-' + stateCls + '">' + GIQ.fmt.esc(status) + '</div>';
        card.appendChild(head);

        const sub = document.createElement('div');
        sub.className = 'model-card-sub mono';
        sub.textContent = meta.sub;
        card.appendChild(sub);

        if (data) {
            const stats = document.createElement('div');
            stats.className = 'model-card-stats';
            const rows = [];
            if (data.training_samples != null) rows.push(['Training samples', Number(data.training_samples).toLocaleString()]);
            if (data.n_features != null) rows.push(['Features', String(data.n_features)]);
            if (data.engine) rows.push(['Engine', String(data.engine)]);
            if (data.vocab_size != null) rows.push(['Vocab', Number(data.vocab_size).toLocaleString()]);
            if (data.users != null) rows.push(['Users', String(data.users)]);
            if (data.tracks != null) rows.push(['Tracks', Number(data.tracks).toLocaleString()]);
            if (data.seeds_cached != null) rows.push(['Seeds cached', String(data.seeds_cached)]);
            if (data.cache_age_seconds != null && data.cache_age_seconds > 0) rows.push(['Cache age', GIQ.fmt.fmtDuration(data.cache_age_seconds)]);
            if (data.model_version) rows.push(['Version', String(data.model_version)]);
            if (data.trained_at) rows.push(['Trained', GIQ.fmt.timeAgo(data.trained_at)]);
            if (rows.length) {
                rows.forEach(([k, v]) => {
                    const row = document.createElement('div');
                    row.className = 'model-card-stat-row';
                    row.innerHTML = '<span class="mono muted">' + GIQ.fmt.esc(k) + '</span>'
                        + '<span class="mono">' + GIQ.fmt.esc(v) + '</span>';
                    stats.appendChild(row);
                });
            } else {
                stats.innerHTML = '<div class="empty-row">No stats yet.</div>';
            }
            card.appendChild(stats);
        } else {
            const empty = document.createElement('div');
            empty.className = 'empty-row';
            empty.textContent = 'No data available.';
            card.appendChild(empty);
        }

        return card;
    }

    /* =====================================================================
     * Monitor → Recs Debug (session 06)
     * ===================================================================== */

    function renderRecsDebug(root, params) {
        const state = {
            destroyed: false,
            mode: 'sessions',
            user: '',
            surface: '',
            since: 30,
            limit: 50,
            offset: 0,
            sessions: [],
            users: [],
            requestId: null,
            detail: null,
            replayMode: 'rerank_only',
            replay: null,
            debugTraceId: null,
        };

        if (params && params.debug) {
            state.mode = 'debug';
            state.debugTraceId = params.debug;
        } else if (params && params.request) {
            state.mode = 'detail';
            state.requestId = params.request;
        }

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'MONITOR',
            title: 'Recs Debug',
            right: buildRecsDebugRight(state, () => renderAll()),
        }));

        const body = document.createElement('div');
        body.className = 'recs-debug-body';
        root.appendChild(body);

        function renderAll() {
            body.innerHTML = '';
            if (!GIQ.state.apiKey) {
                body.innerHTML = '<div class="empty-row" style="padding:24px">Connect API key to load audit data.</div>';
                return;
            }
            if (state.mode === 'sessions') renderRDSessions(body, state, renderAll);
            else if (state.mode === 'detail') renderRDDetail(body, state, renderAll);
            else if (state.mode === 'replay') renderRDReplay(body, state, renderAll);
            else if (state.mode === 'debug') renderRDLiveDebug(body, state, renderAll);
        }

        // Pre-fetch user list once for the filter dropdown
        if (GIQ.state.apiKey) {
            GIQ.api.get('/v1/users').then(users => {
                state.users = Array.isArray(users) ? users : (users?.users || []);
                if (state.mode === 'sessions') renderAll();
            }).catch(() => { /* ignore */ });
        }

        renderAll();

        return function cleanup() {
            state.destroyed = true;
        };
    }

    function buildRecsDebugRight(state, refresh) {
        const wrap = document.createElement('div');
        wrap.style.display = 'flex';
        wrap.style.gap = '8px';

        if (state.mode !== 'sessions') {
            const back = document.createElement('button');
            back.type = 'button';
            back.className = 'vc-btn vc-btn-ghost';
            back.textContent = state.mode === 'replay' ? '← Back to detail' : '← Back to sessions';
            back.addEventListener('click', () => {
                if (state.mode === 'replay') state.mode = 'detail';
                else { state.mode = 'sessions'; state.requestId = null; state.detail = null; state.replay = null; }
                refresh();
            });
            wrap.appendChild(back);
        }

        const stats = document.createElement('button');
        stats.type = 'button';
        stats.className = 'vc-btn vc-btn-ghost';
        stats.textContent = 'Storage stats';
        stats.addEventListener('click', () => showAuditStatsModal());
        wrap.appendChild(stats);

        return wrap;
    }

    async function showAuditStatsModal() {
        try {
            const s = await GIQ.api.get('/v1/recommend/audit/stats');
            const body = document.createElement('div');
            body.className = 'audit-stats-modal';
            const mb = (s.storage_bytes_estimate || 0) / 1024 / 1024;
            body.innerHTML = '<div class="audit-stats-row"><span class="mono muted">Enabled</span><span class="mono">' + (s.enabled ? 'yes' : 'no') + '</span></div>'
                + '<div class="audit-stats-row"><span class="mono muted">Total requests (all-time)</span><span class="mono">' + GIQ.fmt.esc(Number(s.total_requests_all || 0).toLocaleString()) + '</span></div>'
                + '<div class="audit-stats-row"><span class="mono muted">Last 30 days</span><span class="mono">' + GIQ.fmt.esc(Number(s.total_requests_30d || 0).toLocaleString()) + '</span></div>'
                + '<div class="audit-stats-row"><span class="mono muted">Total candidates</span><span class="mono">' + GIQ.fmt.esc(Number(s.total_candidates_all || 0).toLocaleString()) + '</span></div>'
                + '<div class="audit-stats-row"><span class="mono muted">Estimated storage</span><span class="mono">' + mb.toFixed(1) + ' MB</span></div>'
                + '<div class="audit-stats-row"><span class="mono muted">Retention</span><span class="mono">' + GIQ.fmt.esc(String(s.retention_days || '—')) + ' days</span></div>'
                + '<div class="audit-stats-row"><span class="mono muted">Max candidates / request</span><span class="mono">' + GIQ.fmt.esc(String(s.max_candidates_per_request || '—')) + '</span></div>';
            const close = document.createElement('button');
            close.type = 'button';
            close.className = 'vc-btn vc-btn-ghost';
            close.textContent = 'Close';
            const m = GIQ.components.modal({
                title: 'Audit storage stats',
                body, footer: [close], width: 'sm',
            });
            close.addEventListener('click', () => m.close());
        } catch (e) {
            GIQ.toast('Stats failed: ' + e.message, 'error');
        }
    }

    /* ---- Mode 1: Sessions list --------------------------------------- */

    function renderRDSessions(body, state, refresh) {
        const filter = document.createElement('div');
        filter.className = 'rd-filter';

        const userSel = document.createElement('select');
        userSel.className = 'rd-select';
        userSel.innerHTML = '<option value="">All users (admin)</option>';
        state.users.forEach(u => {
            const o = document.createElement('option');
            o.value = u.user_id; o.textContent = u.user_id;
            if (u.user_id === state.user) o.selected = true;
            userSel.appendChild(o);
        });

        const surfaceSel = document.createElement('select');
        surfaceSel.className = 'rd-select';
        [['', 'All surfaces'], ['recommend_api', 'Recommend API'], ['radio', 'Radio'], ['home', 'Home'], ['search', 'Search']]
            .forEach(([v, l]) => {
                const o = document.createElement('option');
                o.value = v; o.textContent = l;
                if (v === state.surface) o.selected = true;
                surfaceSel.appendChild(o);
            });

        const sinceSel = document.createElement('select');
        sinceSel.className = 'rd-select';
        [[1, '24h'], [7, '7 days'], [30, '30 days'], [90, '90 days'], [0, 'All time']]
            .forEach(([v, l]) => {
                const o = document.createElement('option');
                o.value = String(v); o.textContent = l;
                if (v === state.since) o.selected = true;
                sinceSel.appendChild(o);
            });

        const apply = document.createElement('button');
        apply.type = 'button';
        apply.className = 'vc-btn vc-btn-primary';
        apply.textContent = 'Apply';
        apply.addEventListener('click', () => {
            state.user = userSel.value;
            state.surface = surfaceSel.value;
            state.since = parseInt(sinceSel.value, 10);
            state.offset = 0;
            fetchSessions(state, refresh);
        });

        filter.appendChild(labeled('User', userSel));
        filter.appendChild(labeled('Surface', surfaceSel));
        filter.appendChild(labeled('Since', sinceSel));
        filter.appendChild(apply);

        body.appendChild(filter);

        const listHost = document.createElement('div');
        listHost.className = 'rd-list-host';
        listHost.innerHTML = '<div class="empty-row" style="padding:24px">Loading audit sessions…</div>';
        body.appendChild(listHost);

        fetchSessions(state, () => {
            listHost.innerHTML = '';
            if (!state.sessions.length) {
                listHost.innerHTML = '<div class="empty-row" style="padding:24px">No audit sessions found. Trigger /v1/recommend or start a radio session to populate.</div>';
                return;
            }
            const table = document.createElement('div');
            table.className = 'rd-sessions-table';
            const head = document.createElement('div');
            head.className = 'rd-row rd-head';
            head.innerHTML = '<span>When</span><span>User</span><span>Surface</span><span>Top track</span><span>Seed</span><span>Cands</span><span>Model</span><span>Cfg</span><span>Duration</span><span></span>';
            table.appendChild(head);

            state.sessions.forEach(r => {
                const top = r.top_track && (r.top_track.title || r.top_track.track_id)
                    ? (r.top_track.artist ? r.top_track.artist + ' — ' : '') + (r.top_track.title || r.top_track.track_id)
                    : '—';
                const seed = r.seed_track_id ? r.seed_track_id.slice(0, 14) + '…' : (r.context_id ? 'ctx:' + r.context_id.slice(0, 8) : '—');
                const row = document.createElement('div');
                row.className = 'rd-row';
                row.innerHTML = '<span class="mono muted" title="' + GIQ.fmt.esc(GIQ.fmt.fmtTime(r.created_at)) + '">' + GIQ.fmt.esc(GIQ.fmt.timeAgo(r.created_at)) + '</span>'
                    + '<span><strong>' + GIQ.fmt.esc(r.user_id) + '</strong></span>'
                    + '<span><span class="rd-chip">' + GIQ.fmt.esc(r.surface || '—') + '</span></span>'
                    + '<span class="rd-truncate" title="' + GIQ.fmt.esc(top) + '">' + GIQ.fmt.esc(top) + '</span>'
                    + '<span class="mono muted">' + GIQ.fmt.esc(seed) + '</span>'
                    + '<span class="mono">' + GIQ.fmt.esc(String(r.candidates_total || 0)) + '</span>'
                    + '<span class="mono muted">' + GIQ.fmt.esc(r.model_version || '—') + '</span>'
                    + '<span class="mono muted">v' + GIQ.fmt.esc(String(r.config_version != null ? r.config_version : '—')) + '</span>'
                    + '<span class="mono muted">' + GIQ.fmt.esc(String(r.duration_ms || 0)) + 'ms</span>'
                    + '<span></span>';
                const viewBtn = document.createElement('button');
                viewBtn.type = 'button';
                viewBtn.className = 'vc-btn vc-btn-ghost vc-btn-sm';
                viewBtn.textContent = 'View';
                viewBtn.addEventListener('click', () => {
                    state.requestId = r.request_id;
                    state.mode = 'detail';
                    refresh();
                });
                row.lastElementChild.appendChild(viewBtn);
                table.appendChild(row);
            });
            listHost.appendChild(table);

            const pag = document.createElement('div');
            pag.className = 'rd-pagination';
            pag.innerHTML = '<span class="mono muted">Showing ' + state.sessions.length + ' starting at ' + state.offset + '</span>';
            const btns = document.createElement('div');
            btns.style.display = 'flex'; btns.style.gap = '6px';
            if (state.offset > 0) {
                const prev = document.createElement('button');
                prev.type = 'button';
                prev.className = 'vc-btn vc-btn-ghost vc-btn-sm';
                prev.textContent = '← Prev';
                prev.addEventListener('click', () => {
                    state.offset = Math.max(0, state.offset - state.limit);
                    fetchSessions(state, refresh);
                });
                btns.appendChild(prev);
            }
            if (state.sessions.length === state.limit) {
                const next = document.createElement('button');
                next.type = 'button';
                next.className = 'vc-btn vc-btn-ghost vc-btn-sm';
                next.textContent = 'Next →';
                next.addEventListener('click', () => {
                    state.offset += state.limit;
                    fetchSessions(state, refresh);
                });
                btns.appendChild(next);
            }
            pag.appendChild(btns);
            listHost.appendChild(pag);
        });
    }

    function fetchSessions(state, onDone) {
        const params = ['limit=' + state.limit, 'offset=' + state.offset];
        if (state.user) params.push('user_id=' + encodeURIComponent(state.user));
        if (state.surface) params.push('surface=' + encodeURIComponent(state.surface));
        if (state.since && state.since > 0) params.push('since_days=' + state.since);
        GIQ.api.get('/v1/recommend/audit/sessions?' + params.join('&'))
            .then(rows => {
                state.sessions = Array.isArray(rows) ? rows : [];
                onDone();
            })
            .catch(err => {
                state.sessions = [];
                onDone();
                GIQ.toast('Failed to load sessions: ' + err.message, 'error');
            });
    }

    function labeled(label, control) {
        const wrap = document.createElement('label');
        wrap.className = 'rd-filter-field';
        const lbl = document.createElement('span');
        lbl.className = 'eyebrow';
        lbl.textContent = label;
        wrap.appendChild(lbl);
        wrap.appendChild(control);
        return wrap;
    }

    /* ---- Mode 2: Request detail -------------------------------------- */

    function renderRDDetail(body, state, refresh) {
        const wrap = document.createElement('div');
        wrap.className = 'rd-detail-wrap';
        wrap.innerHTML = '<div class="empty-row" style="padding:24px">Loading request detail…</div>';
        body.appendChild(wrap);

        GIQ.api.get('/v1/recommend/audit/' + encodeURIComponent(state.requestId))
            .then(d => {
                state.detail = d;
                drawDetail(wrap, d, state, refresh);
            })
            .catch(err => {
                wrap.innerHTML = '<div class="empty-row" style="color:var(--wine);padding:24px">' + GIQ.fmt.esc(err.message) + '</div>';
            });
    }

    function drawDetail(host, d, state, refresh) {
        host.innerHTML = '';

        const header = document.createElement('div');
        header.className = 'rd-detail-header';
        const ctx = d.request_context || {};
        const chipKeys = ['device_type', 'output_type', 'context_type', 'location_label', 'hour_of_day', 'day_of_week', 'seed_type', 'seed_value', 'genre', 'mood'];
        const chips = chipKeys
            .filter(k => ctx[k] != null && ctx[k] !== '')
            .map(k => '<span class="rd-context-chip"><span class="mono muted">' + GIQ.fmt.esc(k) + '</span> ' + GIQ.fmt.esc(String(ctx[k])) + '</span>')
            .join('');
        header.innerHTML = ''
            + '<div class="rd-detail-header-top">'
                + '<div>'
                + '<div class="eyebrow">REQUEST · ' + GIQ.fmt.esc(d.surface || '—') + '</div>'
                + '<div class="rd-detail-id mono">' + GIQ.fmt.esc(d.request_id) + '</div>'
                + '<div class="rd-detail-meta">'
                + '<strong>' + GIQ.fmt.esc(d.user_id) + '</strong>'
                + ' <span class="mono muted">· ' + GIQ.fmt.esc(GIQ.fmt.fmtTime(d.created_at)) + '</span>'
                + ' <span class="rd-chip">model ' + GIQ.fmt.esc(d.model_version || '—') + '</span>'
                + ' <span class="rd-chip">cfg v' + GIQ.fmt.esc(String(d.config_version != null ? d.config_version : '—')) + '</span>'
                + '</div>'
                + (chips ? '<div class="rd-context-chips">' + chips + '</div>' : '')
                + '</div>'
                + '<div class="rd-detail-actions"></div>'
            + '</div>';

        const actions = header.querySelector('.rd-detail-actions');
        const replayRr = document.createElement('button');
        replayRr.type = 'button';
        replayRr.className = 'vc-btn vc-btn-primary';
        replayRr.textContent = 'Replay (rerank only)';
        replayRr.addEventListener('click', () => {
            state.replayMode = 'rerank_only';
            state.mode = 'replay';
            refresh();
        });
        const replayFull = document.createElement('button');
        replayFull.type = 'button';
        replayFull.className = 'vc-btn vc-btn-ghost';
        replayFull.textContent = 'Replay (full)';
        replayFull.addEventListener('click', () => {
            state.replayMode = 'full';
            state.mode = 'replay';
            refresh();
        });
        actions.appendChild(replayRr);
        actions.appendChild(replayFull);
        host.appendChild(header);

        host.appendChild(GIQ.components.candidatePanel({
            candidatesByCount: d.candidates_by_source || {},
            candidates: d.candidates || [],
            limitRequested: d.limit_requested,
            candidatesTotal: d.candidates_total,
        }));
    }

    /* ---- Mode 3: Replay ---------------------------------------------- */

    function renderRDReplay(body, state, refresh) {
        const wrap = document.createElement('div');
        wrap.className = 'rd-replay-wrap';
        wrap.innerHTML = '<div class="empty-row" style="padding:24px">Running replay…</div>';
        body.appendChild(wrap);

        GIQ.api.post('/v1/recommend/audit/' + encodeURIComponent(state.requestId) + '/replay', { mode: state.replayMode })
            .then(r => {
                state.replay = r;
                drawReplay(wrap, r, state, refresh);
            })
            .catch(err => {
                wrap.innerHTML = '<div class="empty-row" style="color:var(--wine);padding:24px">Replay failed: ' + GIQ.fmt.esc(err.message) + '</div>';
            });
    }

    function drawReplay(host, r, state, refresh) {
        host.innerHTML = '';

        const header = document.createElement('div');
        header.className = 'rd-replay-header';
        header.innerHTML = ''
            + '<div>'
            + '<div class="eyebrow">REPLAY · MODE ' + GIQ.fmt.esc((r.mode || '').toUpperCase()) + '</div>'
            + '<div class="rd-detail-id mono">' + GIQ.fmt.esc(r.request_id) + '</div>'
            + '<div class="rd-detail-meta">'
            + '<span class="rd-chip">orig: ' + GIQ.fmt.esc(r.original_model_version || '—') + ' · cfg v' + GIQ.fmt.esc(String(r.original_config_version != null ? r.original_config_version : '—')) + '</span>'
            + ' <span class="rd-chip rd-chip-accent">new: ' + GIQ.fmt.esc(r.new_model_version || '—') + ' · cfg v' + GIQ.fmt.esc(String(r.new_config_version != null ? r.new_config_version : '—')) + '</span>'
            + '</div>'
            + '</div>'
            + '<div class="rd-detail-actions"></div>';

        const actions = header.querySelector('.rd-detail-actions');
        const rerankBtn = document.createElement('button');
        rerankBtn.type = 'button';
        rerankBtn.className = 'vc-btn vc-btn-' + (r.mode === 'rerank_only' ? 'primary' : 'ghost');
        rerankBtn.textContent = 'Replay (rerank only)';
        rerankBtn.disabled = r.mode === 'rerank_only';
        rerankBtn.addEventListener('click', () => { state.replayMode = 'rerank_only'; refresh(); });
        const fullBtn = document.createElement('button');
        fullBtn.type = 'button';
        fullBtn.className = 'vc-btn vc-btn-' + (r.mode === 'full' ? 'primary' : 'ghost');
        fullBtn.textContent = 'Replay (full)';
        fullBtn.disabled = r.mode === 'full';
        fullBtn.addEventListener('click', () => { state.replayMode = 'full'; refresh(); });
        actions.appendChild(rerankBtn);
        actions.appendChild(fullBtn);
        host.appendChild(header);

        const s = r.summary || {};
        const summary = document.createElement('div');
        summary.className = 'rd-replay-summary';
        summary.appendChild(GIQ.components.statTile({
            label: 'Top-10 overlap', value: s.top10_overlap != null ? (s.top10_overlap * 100).toFixed(0) + '%' : '—',
        }));
        summary.appendChild(GIQ.components.statTile({
            label: 'Kendall τ', value: s.kendall_tau != null ? s.kendall_tau.toFixed(3) : '—',
        }));
        summary.appendChild(GIQ.components.statTile({
            label: 'Avg |Δrank|', value: s.avg_abs_delta != null ? s.avg_abs_delta.toFixed(2) : '—',
        }));
        summary.appendChild(GIQ.components.statTile({
            label: 'New in top 10', value: String((s.new_top10_tracks || []).length),
        }));
        summary.appendChild(GIQ.components.statTile({
            label: 'Dropped from top 10', value: String((s.dropped_top10_tracks || []).length),
        }));
        host.appendChild(summary);

        const deltaTable = document.createElement('div');
        deltaTable.className = 'rd-delta-table';
        const head = document.createElement('div');
        head.className = 'rd-row rd-head';
        head.innerHTML = '<span>Track</span><span>Original</span><span>New</span><span>Δ</span><span>Orig score</span><span>New score</span>';
        deltaTable.appendChild(head);
        (r.rank_deltas || []).forEach(d => {
            const name = (d.artist ? d.artist + ' — ' : '') + (d.title || d.track_id || '—');
            let rowCls = 'rd-row';
            let deltaCell = '<span class="mono muted">—</span>';
            if (d.delta != null) {
                if (d.delta > 0) { deltaCell = '<span class="rd-up">↑' + d.delta + '</span>'; rowCls += ' delta-up'; }
                else if (d.delta < 0) { deltaCell = '<span class="rd-down">↓' + Math.abs(d.delta) + '</span>'; rowCls += ' delta-down'; }
                else { deltaCell = '<span class="mono muted">0</span>'; }
            } else if (d.original_position == null && d.new_position != null) {
                deltaCell = '<span class="rd-new-badge">NEW</span>'; rowCls += ' delta-new';
            } else if (d.new_position == null && d.original_position != null) {
                deltaCell = '<span class="rd-drop-badge">DROP</span>'; rowCls += ' delta-drop';
            }
            const row = document.createElement('div');
            row.className = rowCls;
            row.innerHTML = '<span class="rd-truncate" title="' + GIQ.fmt.esc(d.track_id || '') + '">' + GIQ.fmt.esc(name) + '</span>'
                + '<span class="mono muted">' + (d.original_position != null ? '#' + (d.original_position + 1) : '—') + '</span>'
                + '<span class="mono">' + (d.new_position != null ? '#' + (d.new_position + 1) : '—') + '</span>'
                + '<span>' + deltaCell + '</span>'
                + '<span class="mono muted">' + GIQ.fmt.esc(d.original_score != null ? d.original_score.toFixed(3) : '—') + '</span>'
                + '<span class="mono">' + GIQ.fmt.esc(d.new_score != null ? d.new_score.toFixed(3) : '—') + '</span>';
            deltaTable.appendChild(row);
        });
        host.appendChild(GIQ.components.panel({
            title: 'Rank deltas',
            sub: 'original vs replayed',
            children: deltaTable,
        }));
    }

    /* ---- Live Debug Recs --------------------------------------------- */

    function renderRDLiveDebug(body, state, refresh) {
        const wrap = document.createElement('div');
        wrap.className = 'rd-debug-wrap';
        const params = state.debugTraceId ? state.debugTraceId.split(':') : [];
        // debug param can be either a request_id (already-run debug trace via persisted audit
        // — same as detail mode but rendered as a Live Debug breadcrumb) or "user:<userId>" for
        // running a fresh debug=true trace.
        const isUserShorthand = params[0] === 'user' && params[1];
        const userId = isUserShorthand ? params[1] : null;

        if (userId) {
            wrap.innerHTML = '<div class="empty-row" style="padding:24px">Running debug trace for ' + GIQ.fmt.esc(userId) + '…</div>';
            body.appendChild(wrap);
            GIQ.api.get('/v1/recommend/' + encodeURIComponent(userId) + '?limit=25&debug=true')
                .then(d => drawLiveDebug(wrap, d, userId))
                .catch(err => {
                    wrap.innerHTML = '<div class="empty-row" style="color:var(--wine);padding:24px">Debug trace failed: ' + GIQ.fmt.esc(err.message) + '</div>';
                });
        } else {
            wrap.innerHTML = '<div class="empty-row" style="padding:24px">Loading audit detail for ' + GIQ.fmt.esc(state.debugTraceId) + '…</div>';
            body.appendChild(wrap);
            GIQ.api.get('/v1/recommend/audit/' + encodeURIComponent(state.debugTraceId))
                .then(d => {
                    state.detail = d; state.requestId = state.debugTraceId; state.mode = 'detail'; refresh();
                })
                .catch(err => {
                    wrap.innerHTML = '<div class="empty-row" style="color:var(--wine);padding:24px">Failed to load audit: ' + GIQ.fmt.esc(err.message) + '</div>';
                });
        }
    }

    function drawLiveDebug(host, d, userId) {
        host.innerHTML = '';
        const debug = d.debug || {};
        const tracks = d.tracks || [];

        const header = document.createElement('div');
        header.className = 'rd-detail-header';
        header.innerHTML = ''
            + '<div>'
            + '<div class="eyebrow">LIVE DEBUG · USER ' + GIQ.fmt.esc(userId) + '</div>'
            + '<div class="rd-detail-id mono">' + GIQ.fmt.esc(d.request_id || '—') + '</div>'
            + '<div class="rd-detail-meta">'
            + '<span class="rd-chip">model ' + GIQ.fmt.esc(d.model_version || '—') + '</span>'
            + ' <span class="rd-chip">' + GIQ.fmt.esc(String(tracks.length)) + ' tracks</span>'
            + ' <span class="rd-chip">' + GIQ.fmt.esc(String(debug.total_candidates || 0)) + ' candidates</span>'
            + '</div>'
            + '</div>';
        host.appendChild(header);

        const cbsRaw = debug.candidates_by_source || {};
        const cbsCount = {};
        Object.keys(cbsRaw).forEach(k => {
            const v = cbsRaw[k];
            cbsCount[k] = Array.isArray(v) ? v.length : (Number.isFinite(v) ? v : 0);
        });

        // Build candidate panel from live data — we have to reshape the live
        // debug shape into the audit-detail "candidates" array.
        const preRank = debug.pre_rerank || [];
        const prePos = {};
        for (let i = 0; i < preRank.length; i++) prePos[preRank[i].track_id] = i;
        const sourcesByTrack = {};
        Object.keys(cbsRaw).forEach(src => {
            const list = cbsRaw[src];
            if (Array.isArray(list)) list.forEach(c => {
                if (!sourcesByTrack[c.track_id]) sourcesByTrack[c.track_id] = [];
                sourcesByTrack[c.track_id].push(src);
            });
        });
        const actionsByTrack = {};
        (debug.reranker_actions || []).forEach(a => {
            if (!actionsByTrack[a.track_id]) actionsByTrack[a.track_id] = [];
            actionsByTrack[a.track_id].push(a);
        });
        const featureVectors = debug.feature_vectors || {};
        const candidates = tracks.map((t, i) => ({
            track_id: t.track_id,
            title: t.title,
            artist: t.artist,
            sources: sourcesByTrack[t.track_id] || [],
            raw_score: prePos[t.track_id] != null && preRank[prePos[t.track_id]].score != null
                ? preRank[prePos[t.track_id]].score : t.score,
            final_score: t.score,
            pre_rerank_position: prePos[t.track_id] != null ? prePos[t.track_id] : null,
            final_position: i,
            shown: true,
            reranker_actions: actionsByTrack[t.track_id] || [],
            feature_vector: featureVectors[t.track_id] || {},
        }));

        host.appendChild(GIQ.components.candidatePanel({
            candidatesByCount: cbsCount,
            candidates,
            limitRequested: tracks.length,
            candidatesTotal: debug.total_candidates,
        }));
    }

})();
