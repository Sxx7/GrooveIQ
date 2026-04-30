/* monitor.js — Monitor bucket pages.
 * Overview (session 02), Pipeline / Models / Recs Debug (session 06),
 * System Health / User Diagnostics / Integrations (session 07),
 * Downloads / Lidarr Backfill / Discovery / Charts (session 08).
 * Monitor bucket is now complete.
 */

(function () {
    GIQ.pages.monitor = GIQ.pages.monitor || {};

    GIQ.pages.monitor.overview = renderOverview;
    GIQ.pages.monitor.pipeline = renderPipeline;
    GIQ.pages.monitor.models = renderModels;
    GIQ.pages.monitor['recs-debug'] = renderRecsDebug;
    GIQ.pages.monitor['system-health'] = renderSystemHealth;
    GIQ.pages.monitor['user-diagnostics'] = renderUserDiagnostics;
    GIQ.pages.monitor.integrations = renderIntegrations;
    GIQ.pages.monitor.downloads = renderDownloadsMonitor;
    GIQ.pages.monitor['lidarr-backfill'] = renderLidarrBackfillMonitor;
    GIQ.pages.monitor.discovery = renderDiscoveryMonitor;
    GIQ.pages.monitor.charts = renderChartsMonitor;

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

    function ingestEmptyText(state) {
        if (!state.eventsLoaded) return 'Loading…';
        if (state.eventsErr || state.events == null) return 'Failed to load · retry';
        const buckets = (state.events && state.events.buckets) || [];
        if (buckets.length === 0) return 'No events in window';
        return 'No events in window';
    }

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
            state.eventsErr = !!(events && events._err);
            state.eventsLoaded = true;
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
            emptyText: ingestEmptyText(state),
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
            body.innerHTML = '<div class="vc-loading">Loading taste profile…</div>';
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
                if (d.delta > 0) { deltaCell = '<span class="rd-up">▲︎' + d.delta + '</span>'; rowCls += ' delta-up'; }
                else if (d.delta < 0) { deltaCell = '<span class="rd-down">▼︎' + Math.abs(d.delta) + '</span>'; rowCls += ' delta-down'; }
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

    /* =====================================================================
     * Monitor → System Health (session 07)
     * ===================================================================== */

    function renderSystemHealth(root) {
        const state = {
            range: '24h',
            stats: null,
            events: null,
            activity: null,
            engagement: null,
            scan: null,
        };

        const onRangeChange = v => {
            state.range = v;
            renderAll();
            refreshAll();
        };
        const header = GIQ.components.pageHeader({
            eyebrow: 'MONITOR',
            title: 'System Health',
            right: GIQ.components.rangeToggle({
                values: ['24h', '7d', '30d'],
                current: state.range,
                onChange: onRangeChange,
            }),
        });
        root.appendChild(header);

        const body = document.createElement('div');
        body.className = 'sh-body';
        root.appendChild(body);

        renderAll();
        refreshAll();

        const fullTimer = setInterval(refreshAll, 30000);
        let scanTimer = null;

        function rangeDays() {
            switch (state.range) {
                case '1h': return 1;
                case '24h': return 1;
                case '7d': return 7;
                case '30d': return 30;
                default: return 1;
            }
        }

        function refreshAll() {
            const days = rangeDays();
            Promise.all([
                GIQ.api.get('/v1/stats').catch(() => null),
                GIQ.api.get('/v1/pipeline/stats/events').catch(() => null),
                GIQ.api.get('/v1/pipeline/stats/activity?days=' + days).catch(() => null),
                GIQ.api.get('/v1/pipeline/stats/engagement').catch(() => null),
            ]).then(([stats, events, activity, engagement]) => {
                state.stats = stats;
                state.events = events;
                state.activity = activity;
                state.engagement = engagement;
                state.eventsLoaded = true;
                state.scan = stats?.latest_scan || null;
                renderAll();
                if (state.scan && state.scan.status === 'running') startScanPoll();
                else stopScanPoll();
            });
        }

        function startScanPoll() {
            if (scanTimer) return;
            scanTimer = setInterval(() => {
                if (!state.scan) return;
                Promise.all([
                    GIQ.api.get('/v1/stats').catch(() => null),
                    GIQ.api.get('/v1/library/scan/' + state.scan.scan_id + '/logs?limit=50').catch(() => null),
                ]).then(([stats, logs]) => {
                    state.stats = stats || state.stats;
                    state.scan = stats?.latest_scan || null;
                    state.scanLogs = logs || state.scanLogs;
                    renderScanPanel(scanHostEl(), state);
                    if (!state.scan || state.scan.status !== 'running') stopScanPoll();
                });
            }, 3000);
        }

        function stopScanPoll() { if (scanTimer) { clearInterval(scanTimer); scanTimer = null; } }

        function renderAll() {
            body.innerHTML = '';

            const ingestHost = document.createElement('div');
            renderIngestPanel(ingestHost, state);
            body.appendChild(ingestHost);

            const coverageHost = document.createElement('div');
            renderCoverageOverview(coverageHost, state);
            body.appendChild(coverageHost);

            const activityHost = document.createElement('div');
            renderActivityTimelinePanel(activityHost, state);
            body.appendChild(activityHost);

            const engagementHost = document.createElement('div');
            renderEngagementPanel(engagementHost, state);
            body.appendChild(engagementHost);

            const scanWrap = document.createElement('div');
            scanWrap.className = 'sh-scan-wrap';
            renderScanPanel(scanWrap, state);
            scanWrap.dataset.role = 'scan';
            body.appendChild(scanWrap);
        }

        function scanHostEl() {
            return body.querySelector('[data-role="scan"]') || body;
        }

        return () => {
            clearInterval(fullTimer);
            stopScanPoll();
        };
    }

    function renderIngestPanel(host, state) {
        host.innerHTML = '';
        const buckets = state.events?.buckets || [];
        let total = 0;
        for (const b of buckets) total += b.count || 0;
        const sub = total ? Number(total).toLocaleString() + ' events · 15-min buckets · 24h' : 'no recent activity';

        let series = [];
        let labels = [];
        if (buckets.length) {
            series = [{
                name: 'Events',
                color: '#a887ce',
                values: buckets.map(b => b.count || 0),
                strokeWidth: 2,
                fillOpacity: 0.45,
            }];
            const n = buckets.length;
            const fmtHM = ts => {
                const d = new Date(ts * 1000);
                return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
            };
            for (let i = 0; i < 7; i++) {
                const idx = Math.round(i * (n - 1) / 6);
                labels.push(fmtHM(buckets[idx].timestamp));
            }
        }
        const chart = GIQ.components.areaChart({
            series, labels, height: 180,
            emptyText: ingestEmptyText(state),
        });

        host.appendChild(GIQ.components.panel({
            title: 'Event ingest',
            sub: sub,
            children: chart,
        }));
    }

    function renderCoverageOverview(host, state) {
        host.innerHTML = '';
        const cov = state.stats?.library_coverage;
        if (!cov) {
            host.appendChild(GIQ.components.panel({
                title: 'Library coverage',
                sub: '—',
                children: '<div class="empty-row">No library data yet.</div>',
            }));
            return;
        }
        const total = cov.total_files || 0;
        const analyzed = cov.total_analyzed || 0;
        const stale = cov.stale_rows || 0;
        const rawPct = total > 0 ? (analyzed / total * 100) : 0;
        const pct = Math.min(100, Math.max(0, rawPct));
        const failed = cov.failed_files || [];
        const vd = cov.version_distribution || {};
        const vKeys = Object.keys(vd);

        const inner = document.createElement('div');
        inner.className = 'sh-coverage';

        const top = document.createElement('div');
        top.className = 'sh-coverage-top';
        top.innerHTML = '<div class="sh-coverage-numbers">'
            + '<span class="sh-coverage-pct">' + pct.toFixed(1) + '%</span>'
            + '<span class="sh-coverage-meta mono muted">'
            + Number(analyzed).toLocaleString() + ' / ' + Number(total).toLocaleString() + ' files'
            + (stale > 0 ? ' · ' + Number(stale).toLocaleString() + ' stale rows' : '')
            + '</span></div>'
            + '<div class="sh-coverage-progress"><div class="sh-coverage-fill" style="width:'
            + pct.toFixed(1) + '%"></div></div>';
        inner.appendChild(top);

        if (vKeys.length) {
            let maxVer = 1;
            for (const k of vKeys) if (vd[k] > maxVer) maxVer = vd[k];
            const rows = vKeys
                .sort((a, b) => vd[b] - vd[a])
                .map(k => ({ label: 'v' + k, value: vd[k] }));
            const verPanel = document.createElement('div');
            verPanel.className = 'sh-coverage-versions';
            verPanel.innerHTML = '<div class="eyebrow muted">VERSION DISTRIBUTION</div>';
            verPanel.appendChild(barList(rows, maxVer, 'accent'));
            inner.appendChild(verPanel);
        }

        if (failed.length) {
            const failPanel = document.createElement('div');
            failPanel.className = 'sh-coverage-failed';
            const failHead = document.createElement('div');
            failHead.className = 'eyebrow muted';
            failHead.textContent = 'FAILED FILES (' + failed.length + ')';
            failPanel.appendChild(failHead);
            const list = document.createElement('div');
            list.className = 'sh-failed-list';
            failed.slice(0, 20).forEach(f => {
                const row = document.createElement('div');
                row.className = 'sh-failed-row mono';
                row.innerHTML = '<span class="sh-failed-name">' + GIQ.fmt.esc(f.filename || '') + '</span>'
                    + (f.message ? '<span class="sh-failed-msg muted">' + GIQ.fmt.esc(f.message) + '</span>' : '');
                list.appendChild(row);
            });
            failPanel.appendChild(list);
            inner.appendChild(failPanel);
        }

        host.appendChild(GIQ.components.panel({
            title: 'Library coverage',
            sub: total ? Number(total).toLocaleString() + ' indexed files' : 'no scan yet',
            children: inner,
        }));
    }

    function renderActivityTimelinePanel(host, state) {
        host.innerHTML = '';
        const activity = state.activity;
        const buckets = activity?.buckets || [];
        if (buckets.length < 2) {
            host.appendChild(GIQ.components.panel({
                title: 'Listening activity',
                sub: 'event types over time',
                children: '<div class="empty-row">Need at least 2 buckets of activity.</div>',
            }));
            return;
        }

        // Determine top-N most-frequent event types; aggregate the rest into "other".
        const totals = {};
        buckets.forEach(b => {
            Object.keys(b).forEach(k => {
                if (k === 'timestamp') return;
                totals[k] = (totals[k] || 0) + (b[k] || 0);
            });
        });
        const sorted = Object.keys(totals).sort((a, b) => totals[b] - totals[a]);
        const TOP_N = 5;
        const topTypes = sorted.slice(0, TOP_N);
        const otherTypes = sorted.slice(TOP_N);
        const series = otherTypes.length ? topTypes.concat(['other']) : topTypes;

        const w = 800, h = 200, pad = { top: 20, right: 20, bottom: 30, left: 40 };
        const n = buckets.length;
        const innerW = w - pad.left - pad.right;
        const innerH = h - pad.top - pad.bottom;
        const dx = innerW / (n - 1);
        const stackVals = buckets.map(b => {
            const out = {};
            topTypes.forEach(t => { out[t] = b[t] || 0; });
            if (otherTypes.length) {
                let other = 0;
                otherTypes.forEach(t => { other += b[t] || 0; });
                out.other = other;
            }
            return out;
        });
        let maxStack = 1;
        for (const sv of stackVals) {
            let sum = 0;
            for (const k of series) sum += sv[k] || 0;
            if (sum > maxStack) maxStack = sum;
        }

        // Monochrome lavender saturation ladder: hottest = full --accent, coolest = faint.
        const SAT = ['rgba(168,135,206,0.85)', 'rgba(168,135,206,0.62)',
                     'rgba(168,135,206,0.42)', 'rgba(168,135,206,0.28)',
                     'rgba(168,135,206,0.18)', 'rgba(168,135,206,0.10)'];

        const svgNS = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
        svg.classList.add('sh-activity-svg');

        // Gridlines + y-axis labels.
        for (let g = 0; g <= 3; g++) {
            const y = pad.top + innerH * g / 3;
            const line = document.createElementNS(svgNS, 'line');
            line.setAttribute('x1', pad.left); line.setAttribute('y1', y.toFixed(1));
            line.setAttribute('x2', pad.left + innerW); line.setAttribute('y2', y.toFixed(1));
            line.setAttribute('stroke', 'rgba(236,232,242,0.06)');
            line.setAttribute('stroke-dasharray', g === 3 ? '0' : '2,3');
            svg.appendChild(line);
            const lbl = document.createElementNS(svgNS, 'text');
            lbl.setAttribute('x', (pad.left - 6).toFixed(1));
            lbl.setAttribute('y', (y + 3).toFixed(1));
            lbl.setAttribute('class', 'sh-activity-tick mono');
            lbl.setAttribute('text-anchor', 'end');
            const v = Math.round(maxStack * (3 - g) / 3);
            lbl.textContent = String(v);
            svg.appendChild(lbl);
        }

        // Stacked area: paint deepest type first (background), highest on top.
        for (let s = series.length - 1; s >= 0; s--) {
            const t = series[s];
            const points = [];
            // Lower edge (sum of types ABOVE t in stack order).
            const lower = i => {
                let sum = 0;
                for (let k = 0; k < s; k++) sum += stackVals[i][series[k]] || 0;
                return sum;
            };
            const upper = i => lower(i) + (stackVals[i][t] || 0);
            for (let i = 0; i < n; i++) {
                const x = pad.left + i * dx;
                const y = pad.top + innerH - (upper(i) / maxStack) * innerH;
                points.push(x.toFixed(1) + ',' + y.toFixed(1));
            }
            for (let i = n - 1; i >= 0; i--) {
                const x = pad.left + i * dx;
                const y = pad.top + innerH - (lower(i) / maxStack) * innerH;
                points.push(x.toFixed(1) + ',' + y.toFixed(1));
            }
            const poly = document.createElementNS(svgNS, 'polygon');
            poly.setAttribute('points', points.join(' '));
            poly.setAttribute('fill', SAT[s] || SAT[SAT.length - 1]);
            poly.setAttribute('stroke', 'none');
            svg.appendChild(poly);
        }

        // X-axis ticks: first / mid / last.
        const xTickIdxs = [0, Math.floor(n / 2), n - 1];
        xTickIdxs.forEach(i => {
            const x = pad.left + i * dx;
            const t = buckets[i].timestamp;
            const lbl = document.createElementNS(svgNS, 'text');
            lbl.setAttribute('x', x.toFixed(1));
            lbl.setAttribute('y', (pad.top + innerH + 16).toFixed(1));
            lbl.setAttribute('class', 'sh-activity-tick mono');
            lbl.setAttribute('text-anchor', 'middle');
            const d = new Date(t * 1000);
            const days = state.range === '30d' ? 30 : state.range === '7d' ? 7 : 1;
            lbl.textContent = days > 1
                ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
                : d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
            svg.appendChild(lbl);
        });

        const wrap = document.createElement('div');
        wrap.className = 'sh-activity';
        wrap.appendChild(svg);

        const legend = document.createElement('div');
        legend.className = 'sh-activity-legend';
        series.forEach((t, i) => {
            const item = document.createElement('span');
            item.className = 'sh-activity-legend-item';
            item.innerHTML = '<span class="sh-activity-swatch" style="background:'
                + (SAT[i] || SAT[SAT.length - 1]) + '"></span>'
                + '<span class="mono">' + GIQ.fmt.esc(t) + '</span>'
                + '<span class="muted mono">· ' + Number(totals[t] || 0).toLocaleString() + '</span>';
            legend.appendChild(item);
        });
        if (otherTypes.length) {
            const note = document.createElement('div');
            note.className = 'sh-activity-other muted mono';
            note.textContent = 'other = ' + otherTypes.join(', ');
            legend.appendChild(note);
        }
        wrap.appendChild(legend);

        host.appendChild(GIQ.components.panel({
            title: 'Listening activity',
            sub: (activity?.days || 7) + ' days · stacked top ' + topTypes.length,
            children: wrap,
        }));
    }

    function renderEngagementPanel(host, state) {
        host.innerHTML = '';
        const data = state.engagement;
        const users = data?.users || [];
        if (!users.length) {
            host.appendChild(GIQ.components.panel({
                title: 'User engagement',
                sub: 'last 30 days',
                children: '<div class="empty-row">No engagement data yet.</div>',
            }));
            return;
        }
        const sortState = state._engSort = state._engSort || { col: 'plays', dir: 'desc' };

        function sortRows() {
            const rows = users.slice();
            const k = sortState.col;
            rows.sort((a, b) => {
                let av = a[k]; let bv = b[k];
                if (k === 'user_id') {
                    av = String(av || '').toLowerCase();
                    bv = String(bv || '').toLowerCase();
                    return sortState.dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
                }
                av = av == null ? -Infinity : av;
                bv = bv == null ? -Infinity : bv;
                return sortState.dir === 'asc' ? av - bv : bv - av;
            });
            return rows;
        }

        const wrap = document.createElement('div');
        wrap.className = 'sh-engagement';
        const tbl = document.createElement('table');
        tbl.className = 'sh-engagement-table';

        function headerCell(label, col) {
            const arrow = sortState.col === col ? (sortState.dir === 'asc' ? ' ▲' : ' ▼') : '';
            return '<th class="sortable" data-col="' + col + '">' + GIQ.fmt.esc(label) + arrow + '</th>';
        }

        function renderTable() {
            const rows = sortRows();
            tbl.innerHTML = '<thead><tr>'
                + headerCell('User', 'user_id')
                + headerCell('Plays', 'plays')
                + headerCell('Skip rate', 'skip_rate')
                + headerCell('Unique tracks', 'unique_tracks')
                + headerCell('Diversity', 'diversity')
                + headerCell('Last active', 'last_active')
                + '</tr></thead>';
            const tbody = document.createElement('tbody');
            rows.forEach(u => {
                const tr = document.createElement('tr');
                tr.className = 'sh-engagement-row';
                tr.innerHTML = '<td><a class="sh-user-link" href="#/monitor/user-diagnostics?user='
                    + encodeURIComponent(u.user_id) + '"><strong>' + GIQ.fmt.esc(u.user_id) + '</strong></a></td>'
                    + '<td class="mono">' + Number(u.plays || 0).toLocaleString() + '</td>'
                    + '<td class="mono' + (u.skip_rate > 0.5 ? ' sh-bad' : '') + '">'
                        + (u.skip_rate != null ? (u.skip_rate * 100).toFixed(1) + '%' : '—') + '</td>'
                    + '<td class="mono">' + Number(u.unique_tracks || 0).toLocaleString() + '</td>'
                    + '<td class="mono">' + (u.diversity != null ? (u.diversity * 100).toFixed(0) + '%' : '—') + '</td>'
                    + '<td class="mono muted">' + GIQ.fmt.esc(GIQ.fmt.timeAgo(u.last_active)) + '</td>';
                tbody.appendChild(tr);
            });
            tbl.appendChild(tbody);
        }

        renderTable();
        wrap.appendChild(tbl);
        tbl.addEventListener('click', e => {
            const th = e.target.closest('th.sortable');
            if (!th) return;
            const col = th.dataset.col;
            if (sortState.col === col) sortState.dir = sortState.dir === 'asc' ? 'desc' : 'asc';
            else { sortState.col = col; sortState.dir = col === 'user_id' ? 'asc' : 'desc'; }
            renderTable();
        });

        host.appendChild(GIQ.components.panel({
            title: 'User engagement',
            sub: users.length + ' users · last 30 days',
            children: wrap,
        }));
    }

    function renderScanPanel(host, state) {
        host.innerHTML = '';
        host.dataset.role = 'scan';
        host.className = 'sh-scan-wrap';
        const scan = state.scan;
        if (!scan) {
            const empty = document.createElement('div');
            empty.className = 'sh-scan-empty';
            empty.innerHTML = '<span class="muted">No scans yet.</span>'
                + ' <a class="sh-scan-link" href="#/actions/library">Start scan →</a>';
            host.appendChild(GIQ.components.panel({
                title: 'Library scan',
                sub: 'idle',
                children: empty,
            }));
            return;
        }

        const wrap = document.createElement('div');
        wrap.className = 'sh-scan';

        const phase = scanPhase(scan);
        const head = document.createElement('div');
        head.className = 'sh-scan-head';
        const phaseChip = document.createElement('span');
        phaseChip.className = 'sh-scan-phase status-' + (scan.status || 'idle');
        phaseChip.innerHTML = (scan.status === 'running'
            ? '<span class="sh-scan-pulse"></span> ' : '')
            + GIQ.fmt.esc(phase);
        head.appendChild(phaseChip);

        const timeBits = [];
        if (scan.elapsed_seconds != null) timeBits.push('elapsed · ' + GIQ.fmt.fmtDuration(scan.elapsed_seconds));
        if (scan.eta_seconds != null && scan.status === 'running') timeBits.push('ETA · ' + GIQ.fmt.fmtDuration(scan.eta_seconds));
        if (scan.rate_per_sec != null) timeBits.push('analyze · ' + scan.rate_per_sec + '/s');
        const timeRow = document.createElement('div');
        timeRow.className = 'sh-scan-times mono muted';
        timeRow.textContent = timeBits.join('  ·  ');
        head.appendChild(timeRow);
        wrap.appendChild(head);

        if (scan.files_found > 0) {
            const proc = (scan.files_analyzed || 0) + (scan.files_skipped || 0) + (scan.files_failed || 0);
            const pct = scan.percent_complete || 0;
            const bar = document.createElement('div');
            bar.className = 'sh-scan-progress';
            bar.innerHTML = '<div class="sh-scan-progress-fill" style="width:' + pct + '%"></div>'
                + '<div class="sh-scan-progress-text mono">' + pct + '%  ·  '
                + Number(proc).toLocaleString() + ' / ' + Number(scan.files_found).toLocaleString()
                + '</div>';
            wrap.appendChild(bar);
        }

        const grid = document.createElement('div');
        grid.className = 'sh-scan-grid';
        grid.appendChild(scanCell('FOUND', scan.files_found, 'muted'));
        grid.appendChild(scanCell('ANALYZED', scan.files_analyzed, 'accent'));
        grid.appendChild(scanCell('SKIPPED', scan.files_skipped || 0, 'muted'));
        grid.appendChild(scanCell('FAILED', scan.files_failed, 'wine'));
        wrap.appendChild(grid);

        if (scan.current_file) {
            const cur = document.createElement('div');
            cur.className = 'sh-scan-current mono';
            cur.innerHTML = '<span class="muted">processing · </span>' + GIQ.fmt.esc(scan.current_file);
            wrap.appendChild(cur);
        }

        const stamps = document.createElement('div');
        stamps.className = 'sh-scan-stamps mono muted';
        const stampBits = [];
        if (scan.started_at) stampBits.push('started · ' + GIQ.fmt.fmtTime(scan.started_at));
        if (scan.ended_at) stampBits.push('ended · ' + GIQ.fmt.fmtTime(scan.ended_at));
        stamps.textContent = stampBits.join('  ·  ');
        wrap.appendChild(stamps);

        // Activity log (live-pollable).
        const logHead = document.createElement('div');
        logHead.className = 'sh-scan-log-head';
        logHead.innerHTML = '<span class="eyebrow muted">ACTIVITY LOG</span>';
        if (scan.status === 'running') {
            const live = GIQ.components.liveBadge();
            logHead.appendChild(live);
        }
        wrap.appendChild(logHead);

        const log = document.createElement('div');
        log.className = 'sh-scan-log';
        const logs = state.scanLogs || [];
        if (!logs.length) {
            log.innerHTML = '<div class="sh-scan-log-line muted">Waiting for scan activity…</div>';
        } else {
            logs.slice(-100).forEach(l => {
                const line = document.createElement('div');
                const cls = l.level === 'ok' ? 'log-ok' : l.level === 'fail' ? 'log-fail' : 'log-info';
                line.className = 'sh-scan-log-line mono ' + cls;
                const icon = l.level === 'ok' ? '✓' : l.level === 'fail' ? '✗' : '·';
                line.textContent = icon + ' ' + (l.filename || '') + (l.message ? '  ' + l.message : '');
                log.appendChild(line);
            });
            // Auto-scroll on render.
            requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
        }
        wrap.appendChild(log);

        host.appendChild(GIQ.components.panel({
            title: 'Library scan',
            sub: scan.status,
            children: wrap,
        }));

        // Lazy-load logs on first render if absent.
        if (!state.scanLogs && scan.scan_id) {
            GIQ.api.get('/v1/library/scan/' + scan.scan_id + '/logs?limit=50').then(logs => {
                state.scanLogs = logs || [];
                if (state.scan && state.scan.scan_id === scan.scan_id) renderScanPanel(host, state);
            }).catch(() => {});
        }
    }

    function scanCell(label, value, kind) {
        const cell = document.createElement('div');
        cell.className = 'sh-scan-cell sh-scan-cell-' + (kind || 'muted');
        cell.innerHTML = '<div class="eyebrow muted">' + GIQ.fmt.esc(label) + '</div>'
            + '<div class="sh-scan-cell-value">' + Number(value || 0).toLocaleString() + '</div>';
        return cell;
    }

    function scanPhase(scan) {
        const status = scan.status;
        if (status === 'running') {
            if ((scan.files_analyzed || 0) > 0) return 'analyzing new/changed files';
            if ((scan.files_found || 0) > 0) return 'checking existing files';
            return 'preparing';
        }
        if (status === 'completed') return 'completed';
        if (status === 'failed') return 'failed';
        if (status === 'interrupted') return 'interrupted';
        return status || 'unknown';
    }

    /* =====================================================================
     * Monitor → User Diagnostics (session 07)
     * ===================================================================== */

    function renderUserDiagnostics(root, params) {
        const state = {
            userId: (params && params.user) ? params.user : null,
            users: [],
            profile: null,
            interactions: null,
            history: null,
            sessions: null,
            lastfmProfile: null,
            historyOffset: 0,
            historyLimit: 25,
            loading: false,
        };

        const dropdown = document.createElement('select');
        dropdown.className = 'sh-user-select';
        dropdown.innerHTML = '<option value="">Select user…</option>';

        const recsBtn = document.createElement('a');
        recsBtn.className = 'vc-btn vc-btn-primary sh-jump-btn';
        recsBtn.textContent = 'Get Recs →';
        recsBtn.href = '#';
        recsBtn.style.pointerEvents = 'none';
        recsBtn.style.opacity = '0.5';

        const editBtn = document.createElement('a');
        editBtn.className = 'vc-btn sh-jump-btn';
        editBtn.textContent = 'Edit user →';
        editBtn.href = '#';
        editBtn.style.pointerEvents = 'none';
        editBtn.style.opacity = '0.5';

        const right = document.createElement('div');
        right.className = 'sh-ud-actions';
        right.appendChild(dropdown);
        right.appendChild(recsBtn);
        right.appendChild(editBtn);

        const header = GIQ.components.pageHeader({
            eyebrow: 'MONITOR',
            title: 'User Diagnostics',
            right: right,
        });
        root.appendChild(header);

        const body = document.createElement('div');
        body.className = 'ud-body';
        root.appendChild(body);

        // Use cached users if available, else fetch.
        const usersPromise = (window.cachedUsers && Array.isArray(window.cachedUsers) && window.cachedUsers.length)
            ? Promise.resolve(window.cachedUsers)
            : GIQ.api.get('/v1/users').then(r => {
                const list = Array.isArray(r) ? r : (r?.users || []);
                window.cachedUsers = list;
                return list;
            }).catch(() => []);

        usersPromise.then(users => {
            state.users = users;
            users.forEach(u => {
                const o = document.createElement('option');
                o.value = u.user_id;
                o.textContent = u.user_id + (u.display_name ? ' (' + u.display_name + ')' : '');
                dropdown.appendChild(o);
            });
            // Resolve user: explicit ?user, else first user.
            if (!state.userId && users.length) state.userId = users[0].user_id;
            if (state.userId) {
                dropdown.value = state.userId;
                updateJumpButtons();
                loadUser();
            } else {
                body.innerHTML = '<div class="empty-row">No users available.</div>';
            }
        });

        dropdown.addEventListener('change', () => {
            const uid = dropdown.value;
            if (!uid || uid === state.userId) return;
            state.userId = uid;
            state.historyOffset = 0;
            updateJumpButtons();
            // URL stays in sync — navigate updates the hash.
            GIQ.router.navigate('monitor', 'user-diagnostics', { user: uid });
        });

        function updateJumpButtons() {
            if (!state.userId) return;
            recsBtn.href = '#/explore/recommendations?user=' + encodeURIComponent(state.userId);
            recsBtn.style.pointerEvents = '';
            recsBtn.style.opacity = '';
            editBtn.href = '#/settings/users?user=' + encodeURIComponent(state.userId);
            editBtn.style.pointerEvents = '';
            editBtn.style.opacity = '';
        }

        function loadUser() {
            if (!state.userId) return;
            state.loading = true;
            body.innerHTML = '<div class="vc-loading">Loading user diagnostics…</div>';
            const enc = encodeURIComponent(state.userId);
            const hl = state.historyLimit;
            const ho = state.historyOffset;

            Promise.all([
                GIQ.api.get('/v1/users/' + enc + '/profile'),
                GIQ.api.get('/v1/users/' + enc + '/interactions?limit=20&sort_by=satisfaction_score&sort_dir=desc').catch(() => null),
                GIQ.api.get('/v1/users/' + enc + '/sessions?limit=20').catch(() => null),
                GIQ.api.get('/v1/users/' + enc + '/lastfm/profile').catch(() => null),
                GIQ.api.get('/v1/users/' + enc + '/history?limit=' + hl + '&offset=' + ho).catch(() => null),
            ]).then(([profile, interactions, sessions, lastfm, history]) => {
                state.profile = profile;
                state.interactions = interactions;
                state.sessions = sessions;
                state.lastfmProfile = lastfm;
                state.history = history;
                const refreshHistory = () => {
                    GIQ.api.get('/v1/users/' + enc + '/history?limit=' + state.historyLimit
                        + '&offset=' + state.historyOffset).then(h => {
                        state.history = h;
                        renderUDBody(body, state, refreshHistory);
                    });
                };
                renderUDBody(body, state, refreshHistory);
            }).catch(e => {
                body.innerHTML = '<div class="empty-row" style="color:var(--wine)">'
                    + GIQ.fmt.esc(e.message) + '</div>';
            });
        }

        return () => { /* nothing to clean up */ };
    }

    function renderUDBody(host, state, refreshHistory) {
        host.innerHTML = '';
        const profile = state.profile;
        if (!profile) {
            host.innerHTML = '<div class="empty-row">No profile data.</div>';
            return;
        }

        const idHeader = document.createElement('div');
        idHeader.className = 'ud-id-header';
        const updated = profile.profile_updated_at
            ? '<span class="ud-id-meta mono muted">profile updated · ' + GIQ.fmt.esc(GIQ.fmt.timeAgo(profile.profile_updated_at)) + '</span>'
            : '';
        idHeader.innerHTML = '<span class="ud-id-userid">' + GIQ.fmt.esc(profile.user_id) + '</span>'
            + (profile.display_name ? '<span class="ud-id-display muted">· ' + GIQ.fmt.esc(profile.display_name) + '</span>' : '')
            + '<span class="ud-id-uid mono">UID ' + GIQ.fmt.esc(String(profile.uid)) + '</span>'
            + updated;
        host.appendChild(idHeader);

        const tp = profile.taste_profile;
        if (!tp) {
            host.appendChild(GIQ.components.panel({
                title: 'Taste profile',
                sub: 'no data',
                children: '<div class="empty-row">No taste profile computed yet for this user. Run the pipeline.</div>',
            }));
        } else {
            renderUDTasteProfile(host, profile);
            renderUDTimescale(host, tp);
        }

        renderUDLastfm(host, profile, state.lastfmProfile);
        renderUDInteractions(host, state.interactions);
        renderUDHistory(host, state, refreshHistory);
        renderUDSessions(host, state.sessions);
    }

    function renderUDTasteProfile(host, profile) {
        const tp = profile.taste_profile;

        const wrap = document.createElement('div');
        wrap.className = 'ud-taste-grid';

        const ap = tp.audio_preferences || {};
        const audioWrap = document.createElement('div');
        audioWrap.className = 'ud-stat-grid';
        const audioFields = [
            ['BPM', ap.bpm?.mean, 1],
            ['Energy', ap.energy?.mean, 2],
            ['Dance', ap.danceability?.mean, 2],
            ['Valence', ap.valence?.mean, 2],
            ['Acoustic', ap.acousticness?.mean, 2],
            ['Instrum.', ap.instrumentalness?.mean, 2],
            ['Loudness', ap.loudness?.mean, 1],
        ];
        audioFields.forEach(([label, val, dp]) => {
            audioWrap.appendChild(GIQ.components.statTile({
                label,
                value: val != null ? Number(val).toFixed(dp) : '—',
            }));
        });
        wrap.appendChild(GIQ.components.panel({
            title: 'Audio preferences',
            sub: 'mean values',
            children: audioWrap,
        }));

        const b = tp.behaviour || {};
        const behaveWrap = document.createElement('div');
        behaveWrap.className = 'ud-stat-grid';
        const behavefields = [
            ['Total plays', b.total_plays != null ? Number(b.total_plays).toLocaleString() : '—'],
            ['Active days', b.active_days != null ? String(b.active_days) : '—'],
            ['Avg session', b.avg_session_tracks != null ? Number(b.avg_session_tracks).toFixed(1) : '—'],
            ['Skip rate', b.skip_rate != null ? (b.skip_rate * 100).toFixed(1) + '%' : '—'],
            ['Completion', b.avg_completion != null ? (b.avg_completion * 100).toFixed(1) + '%' : '—'],
        ];
        behavefields.forEach(([label, value]) => {
            behaveWrap.appendChild(GIQ.components.statTile({ label, value }));
        });
        wrap.appendChild(GIQ.components.panel({
            title: 'Behaviour',
            sub: 'aggregates',
            children: behaveWrap,
        }));

        const mp = tp.mood_preferences || {};
        const moodKeys = Object.keys(mp).sort((a, b) => mp[b] - mp[a]).slice(0, 8);
        if (moodKeys.length) {
            const max = mp[moodKeys[0]] || 1;
            const rows = moodKeys.map(k => ({ label: k, value: mp[k] }));
            wrap.appendChild(GIQ.components.panel({
                title: 'Mood preferences',
                sub: 'top ' + moodKeys.length,
                children: barList(rows, max, 'accent', { fmtVal: v => Number(v).toFixed(2) }),
            }));
        }

        const kp = tp.key_preferences || {};
        const keyKeys = Object.keys(kp).sort((a, b) => kp[b] - kp[a]).slice(0, 12);
        if (keyKeys.length) {
            const max = kp[keyKeys[0]] || 1;
            const rows = keyKeys.map(k => ({ label: k, value: kp[k] }));
            wrap.appendChild(GIQ.components.panel({
                title: 'Key preferences',
                sub: 'top ' + keyKeys.length,
                children: barList(rows, max, 'accent', { fmtVal: v => Number(v).toFixed(2) }),
            }));
        }

        host.appendChild(wrap);
    }

    function renderUDTimescale(host, tp) {
        const ts = tp.timescale_audio || {};
        const ap = tp.audio_preferences || {};
        const shortP = ts.short || {};
        const longP = ts.long || {};
        const allTime = {
            energy: ap.energy?.mean,
            valence: ap.valence?.mean,
            danceability: ap.danceability?.mean,
            acousticness: ap.acousticness?.mean,
            instrumentalness: ap.instrumentalness?.mean,
        };
        // Skip if everything is null.
        const hasAny = Object.values(allTime).some(v => v != null) || Object.keys(shortP).length || Object.keys(longP).length;
        if (!hasAny) return;
        const axes = ['energy', 'valence', 'danceability', 'acousticness', 'instrumentalness'];
        const labels = ['Energy', 'Valence', 'Dance', 'Acoustic', 'Instrum.'];
        const series = [
            { values: axes.map(a => allTime[a] != null ? allTime[a] : 0.5), color: '#a887ce', label: 'All-time' },
            { values: axes.map(a => shortP[a] != null ? shortP[a] : 0.5), color: '#9c526d', label: '7-day' },
            { values: axes.map(a => longP[a] != null ? longP[a] : 0.5), color: '#b8b0c4', label: '30-day' },
        ];
        host.appendChild(GIQ.components.panel({
            title: 'Multi-timescale audio preferences',
            sub: 'all-time · 7-day · 30-day overlay',
            children: buildRadarChart(axes, labels, series),
        }));
    }

    function renderUDLastfm(host, profile, lastfmProfile) {
        const lfm = profile.lastfm;
        if (!lfm) {
            host.appendChild(GIQ.components.panel({
                title: 'Last.fm enrichment',
                sub: 'not connected',
                children: '<div class="empty-row">User has not connected a Last.fm account.</div>',
            }));
            return;
        }
        const lfData = (lastfmProfile && lastfmProfile.profile) || lfm.profile || null;

        const wrap = document.createElement('div');
        wrap.className = 'ud-lastfm';

        const head = document.createElement('div');
        head.className = 'ud-lastfm-head';
        head.innerHTML = '<div class="ud-lastfm-id"><span class="ud-lastfm-username">' + GIQ.fmt.esc(lfm.username) + '</span>'
            + '<span class="ud-lastfm-state ' + (lfm.scrobbling_enabled ? 'state-active' : 'state-readonly') + '">'
            + (lfm.scrobbling_enabled ? 'scrobbling active' : 'read-only') + '</span></div>'
            + '<div class="ud-lastfm-meta mono muted">last synced · ' + GIQ.fmt.esc(GIQ.fmt.timeAgo(lfm.synced_at)) + '</div>';
        wrap.appendChild(head);

        if (lfData?.user_info) {
            const ui = lfData.user_info;
            const stats = document.createElement('div');
            stats.className = 'ud-stat-grid';
            if (ui.playcount) stats.appendChild(GIQ.components.statTile({ label: 'Total scrobbles', value: Number(ui.playcount).toLocaleString() }));
            if (ui.country) stats.appendChild(GIQ.components.statTile({ label: 'Country', value: ui.country }));
            if (ui.registered) {
                const ts = ui.registered.unixtime || ui.registered['#text'];
                const yr = ts ? new Date(Number(ts) * 1000).getFullYear() : '—';
                stats.appendChild(GIQ.components.statTile({ label: 'Member since', value: String(yr) }));
            }
            wrap.appendChild(stats);
        }

        if (lfData?.top_artists) {
            const periods = [['7day', '7 days'], ['1month', '1 month'], ['overall', 'all time']];
            const tabsWrap = document.createElement('div');
            tabsWrap.className = 'ud-lastfm-tabs';
            const head = document.createElement('div');
            head.className = 'ud-lastfm-tabs-head';
            const body = document.createElement('div');
            body.className = 'ud-lastfm-tabs-body';

            let active = '7day';
            function renderTabs() {
                head.innerHTML = '';
                periods.forEach(([k, l]) => {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'ud-lastfm-tab' + (k === active ? ' is-active' : '');
                    btn.textContent = l;
                    btn.addEventListener('click', () => { active = k; renderTabs(); });
                    head.appendChild(btn);
                });
                renderTabBody();
            }
            function renderTabBody() {
                body.innerHTML = '';
                const list = (lfData.top_artists[active] || []);
                if (!list.length) {
                    body.innerHTML = '<div class="empty-row">No data for this period.</div>';
                    return;
                }
                list.slice(0, 10).forEach((a, i) => {
                    const row = document.createElement('div');
                    row.className = 'ud-lastfm-artist-row';
                    row.innerHTML = '<span class="ud-lastfm-rank mono muted">' + (i + 1) + '</span>'
                        + '<span class="ud-lastfm-artist">' + GIQ.fmt.esc(a.name) + '</span>'
                        + '<span class="ud-lastfm-count mono">' + GIQ.fmt.esc(a.playcount || '') + '</span>';
                    body.appendChild(row);
                });
            }
            renderTabs();
            tabsWrap.appendChild(head);
            tabsWrap.appendChild(body);
            wrap.appendChild(GIQ.components.panel({
                title: 'Top artists',
                sub: 'Last.fm',
                children: tabsWrap,
            }));
        }

        if (lfData?.loved_tracks?.length) {
            const list = document.createElement('div');
            list.className = 'ud-lastfm-loved';
            lfData.loved_tracks.slice(0, 10).forEach(t => {
                const ar = t.artist ? (t.artist.name || t.artist) : '';
                const row = document.createElement('div');
                row.className = 'ud-lastfm-loved-row';
                row.innerHTML = '<span class="ud-lastfm-heart">♥</span>'
                    + '<span class="ud-lastfm-loved-artist">' + GIQ.fmt.esc(ar) + '</span>'
                    + '<span class="muted">·</span>'
                    + '<span>' + GIQ.fmt.esc(t.name) + '</span>';
                list.appendChild(row);
            });
            wrap.appendChild(GIQ.components.panel({
                title: 'Loved tracks',
                sub: lfData.loved_tracks.length + ' total',
                children: list,
            }));
        }

        if (lfData?.genres) {
            const genres = Object.keys(lfData.genres);
            if (genres.length) {
                const cloud = document.createElement('div');
                cloud.className = 'ud-lastfm-genres';
                genres.slice(0, 30).forEach(g => {
                    const chip = document.createElement('span');
                    chip.className = 'ud-genre-chip mono';
                    chip.textContent = g;
                    cloud.appendChild(chip);
                });
                wrap.appendChild(GIQ.components.panel({
                    title: 'Genres',
                    sub: genres.length + ' tags',
                    children: cloud,
                }));
            }
        }

        host.appendChild(wrap);
    }

    function renderUDInteractions(host, interactions) {
        const ints = interactions?.interactions || [];
        const total = interactions?.total || ints.length;
        const wrap = document.createElement('div');
        wrap.className = 'ud-table-wrap';
        if (!ints.length) {
            wrap.innerHTML = '<div class="empty-row">No interactions yet for this user.</div>';
        } else {
            let maxSat = 0;
            ints.forEach(t => { if (t.satisfaction_score > maxSat) maxSat = t.satisfaction_score; });
            const tbl = document.createElement('table');
            tbl.className = 'ud-table';
            tbl.innerHTML = '<thead><tr>'
                + '<th>Track</th><th>Score</th><th>Plays</th><th>Skips</th>'
                + '<th>Likes</th><th>Completion</th><th>Last played</th>'
                + '</tr></thead>';
            const tbody = document.createElement('tbody');
            ints.forEach(t => {
                const trackLabel = t.title
                    ? (t.artist ? GIQ.fmt.esc(t.artist) + ' — ' + GIQ.fmt.esc(t.title) : GIQ.fmt.esc(t.title))
                    : '<span class="mono muted">' + GIQ.fmt.esc(t.track_id || '') + '</span>';
                const pct = maxSat > 0 ? (t.satisfaction_score / maxSat * 100) : 0;
                const tr = document.createElement('tr');
                tr.innerHTML = '<td class="ud-truncate" title="' + GIQ.fmt.esc(t.track_id || '') + '">' + trackLabel + '</td>'
                    + '<td><span class="ud-score-cell">'
                        + '<span class="ud-score-num mono">' + Number(t.satisfaction_score || 0).toFixed(2) + '</span>'
                        + '<span class="ud-score-bar"><span class="ud-score-fill" style="width:' + Math.max(2, pct).toFixed(1) + '%"></span></span>'
                        + '</span></td>'
                    + '<td class="mono">' + Number(t.play_count || 0) + '</td>'
                    + '<td class="mono">' + Number(t.skip_count || 0) + '</td>'
                    + '<td class="mono">' + Number(t.like_count || 0) + '</td>'
                    + '<td class="mono">' + (t.avg_completion != null ? (t.avg_completion * 100).toFixed(0) + '%' : '—') + '</td>'
                    + '<td class="mono muted">' + GIQ.fmt.esc(GIQ.fmt.timeAgo(t.last_played_at)) + '</td>';
                tbody.appendChild(tr);
            });
            tbl.appendChild(tbody);
            wrap.appendChild(tbl);
        }
        host.appendChild(GIQ.components.panel({
            title: 'Top tracks (interactions)',
            sub: ints.length + ' shown · ' + Number(total).toLocaleString() + ' total',
            children: wrap,
        }));
    }

    function renderUDHistory(host, state, refreshHistory) {
        const history = state.history;
        const hist = history?.history || [];
        const total = history?.total || hist.length;
        const wrap = document.createElement('div');
        wrap.className = 'ud-table-wrap';
        if (!hist.length) {
            wrap.innerHTML = '<div class="empty-row">No listening history.</div>';
        } else {
            const tbl = document.createElement('table');
            tbl.className = 'ud-table';
            tbl.innerHTML = '<thead><tr>'
                + '<th>Time</th><th>Artist</th><th>Title</th><th>Album</th>'
                + '<th>Duration</th><th>Listened</th><th>Completion</th>'
                + '<th>Result</th><th>Device</th>'
                + '</tr></thead>';
            const tbody = document.createElement('tbody');
            hist.forEach(e => {
                const tr = document.createElement('tr');
                const compClass = e.completion != null && e.completion < 0.5 ? ' sh-bad' : '';
                const resClass = e.reason_end === 'user_skip' ? ' sh-bad' : '';
                tr.innerHTML = '<td class="mono">' + GIQ.fmt.esc(GIQ.fmt.fmtTime(e.timestamp)) + '</td>'
                    + '<td class="ud-truncate">' + GIQ.fmt.esc(e.artist || '—') + '</td>'
                    + '<td class="ud-truncate">' + GIQ.fmt.esc(e.title || '—') + '</td>'
                    + '<td class="ud-truncate muted">' + GIQ.fmt.esc(e.album || '—') + '</td>'
                    + '<td class="mono">' + (e.duration != null ? GIQ.fmt.fmtDuration(Math.round(e.duration)) : '—') + '</td>'
                    + '<td class="mono">' + (e.dwell_ms != null ? GIQ.fmt.fmtDuration(Math.round(e.dwell_ms / 1000)) : '—') + '</td>'
                    + '<td class="mono' + compClass + '">' + (e.completion != null ? (e.completion * 100).toFixed(0) + '%' : '—') + '</td>'
                    + '<td class="mono' + resClass + '">' + GIQ.fmt.esc(e.reason_end || '—') + '</td>'
                    + '<td class="mono muted">' + GIQ.fmt.esc(e.device_type || '—') + '</td>';
                tbody.appendChild(tr);
            });
            tbl.appendChild(tbody);
            wrap.appendChild(tbl);

            // Pagination footer.
            const showStart = state.historyOffset + 1;
            const showEnd = state.historyOffset + hist.length;
            const pag = document.createElement('div');
            pag.className = 'ud-pagination';
            pag.innerHTML = '<span class="muted mono">showing ' + showStart + '–' + showEnd
                + ' of ' + Number(total).toLocaleString() + '</span>';
            const btnGroup = document.createElement('div');
            btnGroup.className = 'ud-pag-btns';
            const prev = document.createElement('button');
            prev.type = 'button';
            prev.className = 'vc-btn vc-btn-sm';
            prev.textContent = '← Prev';
            prev.disabled = state.historyOffset === 0;
            prev.addEventListener('click', () => {
                state.historyOffset = Math.max(0, state.historyOffset - state.historyLimit);
                if (refreshHistory) refreshHistory();
            });
            const next = document.createElement('button');
            next.type = 'button';
            next.className = 'vc-btn vc-btn-sm';
            next.textContent = 'Next →';
            next.disabled = state.historyOffset + state.historyLimit >= total;
            next.addEventListener('click', () => {
                state.historyOffset = state.historyOffset + state.historyLimit;
                if (refreshHistory) refreshHistory();
            });
            btnGroup.appendChild(prev);
            btnGroup.appendChild(next);
            pag.appendChild(btnGroup);
            wrap.appendChild(pag);
        }
        host.appendChild(GIQ.components.panel({
            title: 'Listening history',
            sub: Number(total).toLocaleString() + ' events',
            children: wrap,
        }));
    }

    function renderUDSessions(host, sessions) {
        const sess = sessions?.sessions || [];
        const total = sessions?.total || sess.length;
        const wrap = document.createElement('div');
        wrap.className = 'ud-table-wrap';
        if (!sess.length) {
            wrap.innerHTML = '<div class="empty-row">No sessions yet.</div>';
        } else {
            const tbl = document.createElement('table');
            tbl.className = 'ud-table';
            tbl.innerHTML = '<thead><tr>'
                + '<th>Started</th><th>Duration</th><th>Tracks</th><th>Plays</th>'
                + '<th>Skips</th><th>Skip rate</th><th>Completion</th>'
                + '<th>Context</th><th>Device</th>'
                + '</tr></thead>';
            const tbody = document.createElement('tbody');
            sess.forEach(s => {
                const tr = document.createElement('tr');
                const skipClass = s.skip_rate != null && s.skip_rate > 0.5 ? ' sh-bad' : '';
                tr.innerHTML = '<td class="mono">' + GIQ.fmt.esc(GIQ.fmt.fmtTime(s.started_at)) + '</td>'
                    + '<td class="mono">' + GIQ.fmt.esc(GIQ.fmt.fmtDuration(s.duration_s)) + '</td>'
                    + '<td class="mono">' + Number(s.track_count || 0) + '</td>'
                    + '<td class="mono">' + Number(s.play_count || 0) + '</td>'
                    + '<td class="mono">' + Number(s.skip_count || 0) + '</td>'
                    + '<td class="mono' + skipClass + '">' + (s.skip_rate != null ? (s.skip_rate * 100).toFixed(0) + '%' : '—') + '</td>'
                    + '<td class="mono">' + (s.avg_completion != null ? (s.avg_completion * 100).toFixed(0) + '%' : '—') + '</td>'
                    + '<td class="mono muted">' + GIQ.fmt.esc(s.dominant_context_type || '—') + '</td>'
                    + '<td class="mono muted">' + GIQ.fmt.esc(s.dominant_device_type || '—') + '</td>';
                tbody.appendChild(tr);
            });
            tbl.appendChild(tbody);
            wrap.appendChild(tbl);
        }
        host.appendChild(GIQ.components.panel({
            title: 'Recent sessions',
            sub: sess.length + ' shown · ' + Number(total).toLocaleString() + ' total',
            children: wrap,
        }));
    }

    /* =====================================================================
     * Monitor → Integrations (session 07)
     * ===================================================================== */

    const INTEGRATIONS_ORDER = [
        { key: 'media_server', label: 'Media Server', icon: '♪', desc: 'Navidrome or Plex — source of track IDs and library metadata.' },
        { key: 'lidarr', label: 'Lidarr', icon: '⤓', desc: 'Automatic music discovery and download management.' },
        { key: 'spotdl_api', label: 'spotdl-api', icon: '◇', desc: 'YouTube Music downloads matched via Spotify metadata.' },
        { key: 'streamrip_api', label: 'streamrip-api', icon: '◆', desc: 'Qobuz / Tidal / Deezer / SoundCloud lossless downloads.' },
        { key: 'slskd', label: 'Soulseek (slskd)', icon: '∴', desc: 'Peer-to-peer music downloads via the Soulseek network.' },
        { key: 'lastfm', label: 'Last.fm', icon: '♫', desc: 'Scrobbling, taste enrichment, similar tracks, and charts.' },
        { key: 'acousticbrainz_lookup', label: 'AcousticBrainz Lookup', icon: '∇', desc: 'Audio-feature similarity search across 29.5M tracks.' },
    ];

    function renderIntegrations(root) {
        const state = {
            data: null,
            checkedAt: null,
            latencyMs: null,
            probing: true,
            error: null,
        };

        const reprobeBtn = document.createElement('button');
        reprobeBtn.type = 'button';
        reprobeBtn.className = 'vc-btn vc-btn-primary';
        reprobeBtn.textContent = 'Re-probe all';

        const lastChecked = document.createElement('span');
        lastChecked.className = 'mono muted';
        lastChecked.textContent = '—';

        const right = document.createElement('div');
        right.className = 'integrations-actions';
        right.appendChild(lastChecked);
        right.appendChild(reprobeBtn);

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'MONITOR',
            title: 'Integrations',
            right,
        }));

        const grid = document.createElement('div');
        grid.className = 'integrations-grid';
        root.appendChild(grid);

        renderProbingState();
        probe();
        const pollTimer = setInterval(probe, 30000);
        reprobeBtn.addEventListener('click', () => {
            renderProbingState();
            probe();
        });

        function renderProbingState() {
            if (state.data) {
                renderGrid();
                return;
            }
            grid.innerHTML = '';
            INTEGRATIONS_ORDER.forEach(o => {
                grid.appendChild(GIQ.components.integrationCard({
                    name: o.label,
                    icon: o.icon,
                    description: o.desc,
                    mode: 'live',
                    status: 'probing',
                }));
            });
        }

        function probe() {
            const started = performance.now();
            reprobeBtn.disabled = true;
            reprobeBtn.textContent = 'Probing…';
            state.probing = true;
            GIQ.api.get('/v1/integrations/status').then(data => {
                state.latencyMs = performance.now() - started;
                state.data = data;
                state.checkedAt = data.checked_at || Math.floor(Date.now() / 1000);
                state.error = null;
                state.probing = false;
                lastChecked.textContent = 'last checked · ' + GIQ.fmt.timeAgo(state.checkedAt);
                renderGrid();
            }).catch(e => {
                state.error = e.message || 'Probe failed';
                state.probing = false;
                state.checkedAt = Math.floor(Date.now() / 1000);
                lastChecked.textContent = 'probe failed · ' + GIQ.fmt.timeAgo(state.checkedAt);
                renderGridError();
            }).finally(() => {
                reprobeBtn.disabled = false;
                reprobeBtn.textContent = 'Re-probe all';
            });
        }

        function renderGrid() {
            const integrations = state.data?.integrations || {};
            grid.innerHTML = '';
            const total = INTEGRATIONS_ORDER.length;
            // Approximate per-card latency: divide total round-trip by count.
            const perLatency = state.latencyMs != null
                ? Math.max(1, Math.round(state.latencyMs / Math.max(1, total)))
                : null;

            INTEGRATIONS_ORDER.forEach(o => {
                const s = integrations[o.key] || {};
                let status, error, type, version;
                if (!s.configured) {
                    status = 'not_configured';
                } else if (s.connected === true) {
                    status = 'healthy';
                } else if (s.connected === false) {
                    status = 'error';
                    error = s.error || 'Probe returned not connected.';
                } else {
                    status = 'probing';
                }
                if (s.type) type = s.type;
                if (s.version) version = s.version;

                grid.appendChild(GIQ.components.integrationCard({
                    name: o.label,
                    icon: o.icon,
                    description: o.desc,
                    mode: 'live',
                    status,
                    type,
                    version,
                    error,
                    latencyMs: status === 'healthy' || status === 'error' ? perLatency : null,
                    checkedAt: state.checkedAt,
                }));
            });
        }

        function renderGridError() {
            grid.innerHTML = '';
            const errPanel = document.createElement('div');
            errPanel.className = 'integrations-error';
            errPanel.textContent = 'Failed to probe integrations: ' + (state.error || 'unknown');
            grid.appendChild(errPanel);
        }

        return () => clearInterval(pollTimer);
    }

    /* =====================================================================
     * Session 08 — Ops surfaces (Downloads · Lidarr Backfill · Discovery · Charts)
     * ===================================================================== */

    /* Backend palette — neutral families per the brief.
     * Success-track backends use --accent, in-progress uses --ink-3,
     * failure-track uses --wine. The dot is a state marker, not a per-backend
     * rainbow. A backend's class is decided by row.bucket, not row.source.
     */
    function backendDot(bucket) {
        const cls = bucket === 'failed' ? 'wine'
            : bucket === 'in_flight' ? 'muted'
            : 'accent';
        const span = document.createElement('span');
        span.className = 'op-backend-dot op-backend-dot-' + cls;
        return span;
    }

    function backendChip(name) {
        const span = document.createElement('span');
        span.className = 'op-backend-chip mono';
        span.textContent = (name || 'unknown').toUpperCase();
        return span;
    }

    /* Success-rate as data — one of the brief's allowed colour exceptions.
     * Returns a flex row: bar (width = pct, color by tier) + percent text.
     */
    function successRateBar(rate, opts) {
        const wrap = document.createElement('div');
        wrap.className = 'op-srate-row';
        if (rate == null) {
            wrap.innerHTML = '<span class="mono muted">—</span>';
            return wrap;
        }
        const pct = Math.max(0, Math.min(1, rate));
        const tier = pct >= 0.8 ? 'good' : pct >= 0.5 ? 'warn' : 'bad';
        const bar = document.createElement('div');
        bar.className = 'op-srate-bar';
        const fill = document.createElement('div');
        fill.className = 'op-srate-fill op-srate-' + tier;
        fill.style.width = (pct * 100).toFixed(1) + '%';
        bar.appendChild(fill);
        wrap.appendChild(bar);
        const txt = document.createElement('span');
        txt.className = 'op-srate-txt mono op-srate-txt-' + tier;
        const decimals = opts && opts.decimals != null ? opts.decimals : 1;
        txt.textContent = (pct * 100).toFixed(decimals) + '%';
        wrap.appendChild(txt);
        return wrap;
    }

    /* Vertical monochrome bar chart — used for the Lidarr Backfill throughput
     * panel. rows: [{label, value}], opts: { height, accentLast }
     */
    function verticalBarChart(rows, opts) {
        const cfg = Object.assign({ height: 110 }, opts || {});
        const wrap = document.createElement('div');
        wrap.className = 'op-vchart';
        if (!rows || !rows.length) {
            wrap.innerHTML = '<div class="empty-row">No data.</div>';
            return wrap;
        }
        const max = Math.max(1, rows.reduce((m, r) => Math.max(m, r.value || 0), 0));
        const bars = document.createElement('div');
        bars.className = 'op-vchart-bars';
        bars.style.height = cfg.height + 'px';
        rows.forEach((r, i) => {
            const col = document.createElement('div');
            col.className = 'op-vchart-col';
            const fill = document.createElement('div');
            const pct = (r.value || 0) / max * 100;
            fill.className = 'op-vchart-fill';
            if (cfg.accentLast && i === rows.length - 1) fill.classList.add('op-vchart-accent');
            fill.style.height = pct.toFixed(1) + '%';
            fill.title = r.label + ' · ' + (r.value || 0);
            col.appendChild(fill);
            const lbl = document.createElement('div');
            lbl.className = 'op-vchart-lbl mono';
            lbl.textContent = r.label;
            col.appendChild(lbl);
            bars.appendChild(col);
        });
        wrap.appendChild(bars);
        return wrap;
    }

    /* Auto-refresh indicator — pulsing dot + label. Click to pause.
     * Returns { el, set(active), refresh, stop }.
     */
    function autoRefreshIndicator(opts) {
        const cfg = Object.assign({ label: 'Auto-refresh', intervalMs: 3000 }, opts || {});
        const el = document.createElement('button');
        el.type = 'button';
        el.className = 'op-autorefresh active';
        let active = true;
        let timer = null;

        function rebuild() {
            el.innerHTML = '';
            const dot = document.createElement('span');
            dot.className = active ? 'op-autorefresh-dot pulse' : 'op-autorefresh-dot';
            el.appendChild(dot);
            const span = document.createElement('span');
            span.className = 'op-autorefresh-text mono';
            const seconds = Math.round(cfg.intervalMs / 1000);
            span.textContent = (active ? 'auto · ' : 'paused · ') + seconds + 's';
            el.appendChild(span);
            el.classList.toggle('active', active);
            el.title = active ? 'Click to pause auto-refresh' : 'Click to resume auto-refresh';
        }
        rebuild();

        el.addEventListener('click', () => {
            active = !active;
            rebuild();
            if (active) {
                if (cfg.onStart) cfg.onStart();
            } else {
                if (cfg.onStop) cfg.onStop();
            }
        });

        return {
            el,
            isActive: () => active,
            set(a) { active = !!a; rebuild(); },
        };
    }

    /* =====================================================================
     * Monitor → Downloads
     * ===================================================================== */

    function renderDownloadsMonitor(root) {
        const state = {
            range: '24h',
            queue: null,
            stats: null,
            history: null,
            historyOpen: true,
            recentOpen: true,
            error: null,
        };
        const RANGE_TO_DAYS = { '1h': 1, '24h': 1, '7d': 7, '30d': 30 };

        let queueTimer = null;
        let bgTimer = null;

        const range = GIQ.components.rangeToggle({
            values: ['1h', '24h', '7d', '30d'],
            current: state.range,
            onChange: v => { state.range = v; loadStats(); },
        });

        const auto = autoRefreshIndicator({
            intervalMs: 3000,
            onStart: () => { startQueuePolling(); },
            onStop: () => { stopQueuePolling(); },
        });

        const headerRight = document.createElement('div');
        headerRight.className = 'op-head-right';
        headerRight.appendChild(range);
        headerRight.appendChild(auto.el);

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'MONITOR',
            title: 'Downloads',
            right: headerRight,
        }));

        const body = document.createElement('div');
        body.className = 'op-page-body';
        root.appendChild(body);

        const inflightHost = document.createElement('div');
        const recentHost = document.createElement('div');
        const telemetryHost = document.createElement('div');
        const historyHost = document.createElement('div');
        body.appendChild(inflightHost);
        body.appendChild(recentHost);
        body.appendChild(telemetryHost);
        body.appendChild(historyHost);

        loadAll();
        startQueuePolling();
        bgTimer = setInterval(loadBackground, 30000);

        function loadAll() {
            return Promise.all([loadQueue(), loadStats(), loadHistory()]);
        }

        function loadBackground() {
            loadStats();
            loadHistory();
        }

        function loadQueue() {
            return GIQ.api.get('/v1/downloads/queue?recent_limit=10&in_flight_limit=50').then(d => {
                state.queue = d;
                state.error = null;
                renderInflight();
                renderRecent();
            }).catch(e => {
                state.error = e.message || 'Queue unavailable';
                renderInflight();
            });
        }

        function loadStats() {
            const days = RANGE_TO_DAYS[state.range] || 30;
            return GIQ.api.get('/v1/downloads/stats?days=' + days).then(d => {
                state.stats = d;
                renderTelemetry();
            }).catch(() => {
                state.stats = { backends: [] };
                renderTelemetry();
            });
        }

        function loadHistory() {
            return GIQ.api.get('/v1/downloads?limit=10').then(d => {
                state.history = d;
                renderHistory();
            }).catch(() => {
                state.history = { downloads: [], total: 0 };
                renderHistory();
            });
        }

        function startQueuePolling() {
            stopQueuePolling();
            queueTimer = setInterval(loadQueue, 3000);
        }

        function stopQueuePolling() {
            if (queueTimer) { clearInterval(queueTimer); queueTimer = null; }
        }

        function renderInflight() {
            inflightHost.innerHTML = '';
            const inFlight = (state.queue && state.queue.in_flight) || [];
            const sub = inFlight.length
                ? inFlight.length + (inFlight.length === 1 ? ' download' : ' downloads')
                : 'idle';
            const body = document.createElement('div');
            body.className = 'op-inflight-list';
            if (state.error && !state.queue) {
                body.innerHTML = '<div class="empty-row wine">Queue unavailable: ' + GIQ.fmt.esc(state.error) + '</div>';
            } else if (!inFlight.length) {
                body.innerHTML = '<div class="empty-row">No downloads in flight. Trigger one from Actions → Downloads.</div>';
            } else {
                inFlight.forEach(row => body.appendChild(buildInflightRow(row)));
            }
            inflightHost.appendChild(GIQ.components.panel({
                title: 'In flight',
                sub: sub,
                badge: inFlight.length ? 'LIVE' : null,
                children: body,
            }));
        }

        function buildInflightRow(row) {
            const r = document.createElement('div');
            r.className = 'op-dl-row op-dl-row-active';
            const left = document.createElement('div');
            left.className = 'op-dl-row-left';
            left.appendChild(backendDot('in_flight'));
            left.appendChild(backendChip(row.source || 'unknown'));
            r.appendChild(left);

            const main = document.createElement('div');
            main.className = 'op-dl-row-main';
            const label = (row.artist_name || '') + (row.artist_name && row.track_title ? ' — ' : '') + (row.track_title || '(unnamed)');
            const lbl = document.createElement('div');
            lbl.className = 'op-dl-row-title';
            lbl.textContent = label;
            main.appendChild(lbl);

            const sub = document.createElement('div');
            sub.className = 'op-dl-row-sub mono muted';
            const status = row.live_status || row.status || 'queued';
            sub.textContent = status + ' · ' + fmtElapsed(row.elapsed_s);
            main.appendChild(sub);
            r.appendChild(main);

            const progress = document.createElement('div');
            progress.className = 'op-dl-row-progress';
            const p = row.progress;
            if (typeof p === 'number' && p >= 0) {
                const pct = Math.max(0, Math.min(100, Math.round(p * 100)));
                progress.innerHTML = '<div class="op-dl-bar"><div class="op-dl-bar-fill" style="width:' + pct + '%"></div></div>'
                    + '<span class="op-dl-pct mono">' + pct + '%</span>';
            } else {
                progress.innerHTML = '<div class="op-dl-bar op-dl-bar-shimmer"></div>'
                    + '<span class="op-dl-pct mono muted">…</span>';
            }
            r.appendChild(progress);
            return r;
        }

        function renderRecent() {
            recentHost.innerHTML = '';
            const completed = (state.queue && state.queue.recent_completed) || [];
            const failed = (state.queue && state.queue.recent_failed) || [];
            const recent = completed.concat(failed)
                .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))
                .slice(0, 10);

            const head = document.createElement('button');
            head.type = 'button';
            head.className = 'op-collapse-toggle';
            const arrow = state.recentOpen ? '▾' : '▸';
            head.textContent = arrow + ' Recent activity (' + completed.length + ' ✓ / ' + failed.length + ' ✗)';
            head.addEventListener('click', () => {
                state.recentOpen = !state.recentOpen;
                renderRecent();
            });

            const body = document.createElement('div');
            body.className = 'op-recent-list';
            if (state.recentOpen) {
                if (!recent.length) {
                    body.innerHTML = '<div class="empty-row">No recent terminal downloads.</div>';
                } else {
                    recent.forEach(row => {
                        const isFailed = (row.status === 'error' || row.status === 'failed' || row.status === 'stalled' || row.status === 'cancelled');
                        body.appendChild(buildRecentRow(row, isFailed ? 'failed' : 'completed'));
                    });
                }
            }

            const wrap = document.createElement('div');
            wrap.appendChild(head);
            wrap.appendChild(body);

            recentHost.appendChild(GIQ.components.panel({
                title: 'Recent activity',
                sub: 'last 10 terminal · interleaved by updated_at',
                children: wrap,
            }));
        }

        function buildRecentRow(row, bucket) {
            const r = document.createElement('div');
            r.className = 'op-dl-row op-dl-row-' + bucket;

            const left = document.createElement('div');
            left.className = 'op-dl-row-left';
            left.appendChild(backendDot(bucket));
            left.appendChild(backendChip(row.source || 'unknown'));
            r.appendChild(left);

            const main = document.createElement('div');
            main.className = 'op-dl-row-main';
            const label = (row.artist_name || '') + (row.artist_name && row.track_title ? ' — ' : '') + (row.track_title || '(unnamed)');
            const lbl = document.createElement('div');
            lbl.className = 'op-dl-row-title';
            lbl.textContent = label;
            main.appendChild(lbl);
            const sub = document.createElement('div');
            sub.className = 'op-dl-row-sub mono muted';
            sub.textContent = (row.status || 'unknown') + ' · ' + GIQ.fmt.timeAgo(row.updated_at || row.created_at);
            if (row.error_message) {
                sub.textContent += ' · ' + row.error_message;
                sub.classList.add('wine');
            }
            main.appendChild(sub);
            r.appendChild(main);

            const status = document.createElement('div');
            status.className = 'op-dl-row-status';
            status.appendChild(buildDlStatusBadge(row, bucket));
            r.appendChild(status);

            return r;
        }

        function buildDlStatusBadge(row, bucket) {
            const badge = document.createElement('span');
            badge.className = 'op-status-badge mono';
            const status = row.status || 'unknown';
            if (bucket === 'completed' || status === 'completed' || status === 'complete' || status === 'done') {
                badge.classList.add('good');
                badge.textContent = '✓ done';
            } else if (status === 'duplicate') {
                badge.classList.add('warn');
                badge.textContent = 'duplicate';
            } else if (bucket === 'failed') {
                badge.classList.add('bad');
                badge.textContent = '✗ ' + status;
            } else {
                badge.classList.add('muted');
                badge.textContent = status;
            }
            return badge;
        }

        function renderTelemetry() {
            telemetryHost.innerHTML = '';
            const backends = (state.stats && state.stats.backends) || [];
            const days = RANGE_TO_DAYS[state.range] || 30;

            const body = document.createElement('div');
            body.className = 'op-telemetry-wrap';
            if (!backends.length) {
                body.innerHTML = '<div class="empty-row">No backend activity in the last ' + days + (days === 1 ? ' day' : ' days') + '.</div>';
            } else {
                const tbl = document.createElement('table');
                tbl.className = 'op-telemetry-table';
                tbl.innerHTML = ''
                    + '<thead><tr>'
                    + '<th class="eyebrow">Backend</th>'
                    + '<th class="eyebrow op-num">Total</th>'
                    + '<th class="eyebrow op-num">Success</th>'
                    + '<th class="eyebrow op-num">Failure</th>'
                    + '<th class="eyebrow op-num">In-flight</th>'
                    + '<th class="eyebrow op-srate-col">Success rate</th>'
                    + '</tr></thead>';
                const tb = document.createElement('tbody');
                backends.forEach(b => {
                    const tr = document.createElement('tr');
                    const tdName = document.createElement('td');
                    tdName.className = 'op-telemetry-name';
                    tdName.appendChild(backendChip(b.backend));
                    tr.appendChild(tdName);
                    tr.innerHTML += ''
                        + '<td class="op-num mono">' + GIQ.fmt.fmtNumber(b.total || 0) + '</td>'
                        + '<td class="op-num mono">' + GIQ.fmt.fmtNumber(b.success || 0) + '</td>'
                        + '<td class="op-num mono">' + GIQ.fmt.fmtNumber(b.failure || 0) + '</td>'
                        + '<td class="op-num mono">' + GIQ.fmt.fmtNumber(b.in_flight || 0) + '</td>';
                    const tdSr = document.createElement('td');
                    tdSr.className = 'op-srate-col';
                    tdSr.appendChild(successRateBar(b.success_rate));
                    tr.appendChild(tdSr);
                    tb.appendChild(tr);
                });
                tbl.appendChild(tb);
                body.appendChild(tbl);
            }

            telemetryHost.appendChild(GIQ.components.panel({
                title: 'Per-backend telemetry',
                sub: 'last ' + (days === 1 ? '24 hours' : days + ' days') + ' · success vs failure',
                children: body,
            }));
        }

        function renderHistory() {
            historyHost.innerHTML = '';
            const rows = (state.history && state.history.downloads) || [];
            const total = (state.history && state.history.total) || 0;

            const body = document.createElement('div');
            body.className = 'op-history-list';
            if (!rows.length) {
                body.innerHTML = '<div class="empty-row">No persisted download requests yet.</div>';
            } else {
                rows.forEach(r => body.appendChild(buildHistoryRow(r)));
            }

            historyHost.appendChild(GIQ.components.panel({
                title: 'Recent ad-hoc requests',
                sub: 'last 10 · of ' + GIQ.fmt.fmtNumber(total) + ' total',
                children: body,
            }));
        }

        function buildHistoryRow(row) {
            const r = document.createElement('div');
            r.className = 'op-dl-history-row';

            const ts = document.createElement('div');
            ts.className = 'op-dl-history-ts mono muted';
            ts.textContent = GIQ.fmt.timeAgo(row.created_at);
            r.appendChild(ts);

            const back = document.createElement('div');
            back.className = 'op-dl-history-backend';
            back.appendChild(backendChip(row.source || 'unknown'));
            r.appendChild(back);

            const track = document.createElement('div');
            track.className = 'op-dl-history-track';
            const label = (row.artist_name || '') + (row.artist_name && row.track_title ? ' — ' : '') + (row.track_title || '(unnamed)');
            track.textContent = label;
            track.title = label;
            r.appendChild(track);

            const status = document.createElement('div');
            status.className = 'op-dl-history-status';
            const isFailed = (row.status === 'error' || row.status === 'failed' || row.status === 'stalled' || row.status === 'cancelled');
            const isDone = (row.status === 'completed' || row.status === 'complete' || row.status === 'done');
            status.appendChild(buildDlStatusBadge(row, isFailed ? 'failed' : isDone ? 'completed' : 'in_flight'));
            r.appendChild(status);

            return r;
        }

        function fmtElapsed(s) {
            if (typeof s !== 'number' || s < 0) return '—';
            if (s < 60) return s + 's';
            if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
            return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
        }

        return () => {
            stopQueuePolling();
            if (bgTimer) clearInterval(bgTimer);
        };
    }

    /* =====================================================================
     * Monitor → Lidarr Backfill
     * ===================================================================== */

    function renderLidarrBackfillMonitor(root) {
        const state = {
            stats: null,
            requests: null,
            checkedAt: null,
        };
        let pollTimer = null;

        const liveBadge = document.createElement('div');
        liveBadge.className = 'op-live-pill';
        liveBadge.innerHTML = '<span class="op-live-dot pulse"></span><span class="mono">live</span>'
            + '<span class="op-last-checked mono muted">last checked · —</span>';

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'MONITOR',
            title: 'Lidarr Backfill Stats',
            right: liveBadge,
        }));

        root.appendChild(GIQ.components.relatedRail({
            label: 'related →',
            links: [
                { prefix: 'Settings', label: 'Edit config', href: '#/settings/lidarr-backfill' },
                { prefix: 'Actions', label: 'Manage queue', href: '#/actions/discovery' },
            ],
        }));

        const body = document.createElement('div');
        body.className = 'op-page-body';
        root.appendChild(body);

        const statsHost = document.createElement('div');
        statsHost.className = 'lbf-stats-grid';
        body.appendChild(statsHost);

        const throughputHost = document.createElement('div');
        body.appendChild(throughputHost);

        const serviceHost = document.createElement('div');
        body.appendChild(serviceHost);

        load();
        pollTimer = setInterval(load, 30000);

        function load() {
            return Promise.all([
                GIQ.api.get('/v1/lidarr-backfill/stats').catch(() => null),
                GIQ.api.get('/v1/lidarr-backfill/requests?limit=200').catch(() => null),
            ]).then(([stats, reqs]) => {
                state.stats = stats || {};
                state.requests = (reqs && Array.isArray(reqs.items)) ? reqs.items : [];
                state.checkedAt = Math.floor(Date.now() / 1000);
                liveBadge.querySelector('.op-last-checked').textContent =
                    'last checked · ' + GIQ.fmt.timeAgo(state.checkedAt);
                renderStats();
                renderThroughput();
                renderServices();
            });
        }

        function renderStats() {
            statsHost.innerHTML = '';
            const st = state.stats || {};

            const missing = st.missing_total != null ? st.missing_total : '—';
            const cutoff = st.cutoff_total != null ? st.cutoff_total : '—';

            statsHost.appendChild(GIQ.components.statTile({
                label: 'Missing',
                value: GIQ.fmt.fmtNumber(missing),
                delta: st.cutoff_total != null ? '+ ' + GIQ.fmt.fmtNumber(cutoff) + ' cutoff' : null,
                deltaKind: 'flat',
            }));

            const completeTotal = st.complete != null ? st.complete : 0;
            statsHost.appendChild(GIQ.components.statTile({
                label: 'Complete',
                value: GIQ.fmt.fmtNumber(completeTotal),
                delta: '+' + (st.complete_24h || 0) + ' / 24h',
                deltaKind: 'good',
            }));

            const failedTotal = st.failed != null ? st.failed : 0;
            const totalAttempts = (completeTotal + failedTotal + (st.permanently_skipped || 0)) || 1;
            const failPct = (failedTotal / totalAttempts * 100).toFixed(1);
            statsHost.appendChild(GIQ.components.statTile({
                label: 'Failed',
                value: GIQ.fmt.fmtNumber(failedTotal),
                delta: '+' + (st.failed_24h || 0) + ' / 24h · ' + failPct + '%',
                deltaKind: failedTotal > 0 ? 'bad' : 'flat',
            }));

            const used = (st.max_per_hour || 0) - (st.capacity_remaining != null ? st.capacity_remaining : 0);
            const capPct = st.max_per_hour ? Math.round(used / st.max_per_hour * 100) : 0;
            statsHost.appendChild(GIQ.components.statTile({
                label: 'Capacity',
                value: capPct + '%',
                delta: (st.capacity_remaining != null ? st.capacity_remaining : '—') + ' / ' + (st.max_per_hour != null ? st.max_per_hour : '—') + ' · this hour',
                deltaKind: 'flat',
            }));

            let etaTxt = '—';
            if (st.eta_days != null && st.eta_days >= 1) etaTxt = '~' + st.eta_days + ' d';
            else if (st.eta_hours != null) etaTxt = '~' + st.eta_hours + ' h';
            statsHost.appendChild(GIQ.components.statTile({
                label: 'ETA',
                value: etaTxt,
                delta: 'at current rate',
                deltaKind: 'flat',
            }));

            const tickAt = st.last_tick_at ? GIQ.fmt.timeAgo(st.last_tick_at) : '—';
            const status = st.tick_in_progress ? 'running' : (st.enabled ? 'idle' : 'paused');
            statsHost.appendChild(GIQ.components.statTile({
                label: 'Status',
                value: status.toUpperCase(),
                delta: 'last tick · ' + tickAt,
                deltaKind: status === 'running' ? 'good' : status === 'paused' ? 'bad' : 'flat',
            }));
        }

        function renderThroughput() {
            throughputHost.innerHTML = '';
            const reqs = state.requests || [];

            // Build 7-day buckets keyed by YYYY-MM-DD (UTC). Counts only
            // status === 'complete' rows whose updated_at falls in the window.
            const now = Math.floor(Date.now() / 1000);
            const dayMs = 86400;
            const buckets = [];
            for (let i = 6; i >= 0; i--) {
                const dayStart = Math.floor(now / dayMs) * dayMs - i * dayMs;
                buckets.push({ start: dayStart, end: dayStart + dayMs, value: 0 });
            }
            const oldest = buckets[0].start;
            reqs.forEach(r => {
                if (r.status !== 'complete') return;
                const ts = r.updated_at || r.created_at;
                if (!ts || ts < oldest) return;
                const idx = buckets.findIndex(b => ts >= b.start && ts < b.end);
                if (idx >= 0) buckets[idx].value++;
            });

            const rows = buckets.map(b => {
                const d = new Date(b.start * 1000);
                return {
                    label: (d.getMonth() + 1) + '/' + d.getDate(),
                    value: b.value,
                };
            });

            const noData = rows.every(r => r.value === 0);
            const sub = noData
                ? 'no completed downloads in the last 7 days'
                : 'derived from /v1/lidarr-backfill/requests · status = complete';

            throughputHost.appendChild(GIQ.components.panel({
                title: 'Throughput · last 7 days',
                sub: sub,
                action: 'albums / day',
                children: verticalBarChart(rows, { height: 110, accentLast: !noData }),
            }));
        }

        function renderServices() {
            serviceHost.innerHTML = '';
            const reqs = state.requests || [];

            // Group recent (any status) by picked_service. Skip rows without
            // a picked_service so we only count actual download attempts.
            const byService = {};
            reqs.forEach(r => {
                const s = r.picked_service;
                if (!s) return;
                const b = byService[s] || (byService[s] = { service: s, complete: 0, failed: 0, in_flight: 0, total: 0 });
                b.total++;
                if (r.status === 'complete') b.complete++;
                else if (r.status === 'failed' || r.status === 'no_match' || r.status === 'permanently_skipped') b.failed++;
                else b.in_flight++;
            });

            const order = ['qobuz', 'tidal', 'deezer', 'soundcloud'];
            const services = Object.values(byService).sort((a, b) => {
                const ai = order.indexOf(a.service);
                const bi = order.indexOf(b.service);
                if (ai === -1 && bi === -1) return a.service.localeCompare(b.service);
                if (ai === -1) return 1;
                if (bi === -1) return -1;
                return ai - bi;
            });

            const body = document.createElement('div');
            body.className = 'op-telemetry-wrap';
            if (!services.length) {
                body.innerHTML = '<div class="empty-row">No service-tagged attempts yet. Service stats appear once the engine has dispatched downloads via streamrip.</div>';
            } else {
                const tbl = document.createElement('table');
                tbl.className = 'op-telemetry-table';
                tbl.innerHTML = ''
                    + '<thead><tr>'
                    + '<th class="eyebrow">Service</th>'
                    + '<th class="eyebrow op-num">Attempts</th>'
                    + '<th class="eyebrow op-num">Complete</th>'
                    + '<th class="eyebrow op-num">Failed</th>'
                    + '<th class="eyebrow op-srate-col">Success rate</th>'
                    + '</tr></thead>';
                const tb = document.createElement('tbody');
                services.forEach(s => {
                    const tr = document.createElement('tr');
                    const tdName = document.createElement('td');
                    tdName.className = 'op-telemetry-name';
                    tdName.appendChild(backendChip(s.service));
                    tr.appendChild(tdName);
                    const terminal = s.complete + s.failed;
                    const rate = terminal > 0 ? s.complete / terminal : null;
                    tr.innerHTML += ''
                        + '<td class="op-num mono">' + s.total + '</td>'
                        + '<td class="op-num mono">' + s.complete + '</td>'
                        + '<td class="op-num mono">' + s.failed + '</td>';
                    const tdSr = document.createElement('td');
                    tdSr.className = 'op-srate-col';
                    tdSr.appendChild(successRateBar(rate));
                    tr.appendChild(tdSr);
                    tb.appendChild(tr);
                });
                tbl.appendChild(tb);
                body.appendChild(tbl);
            }

            serviceHost.appendChild(GIQ.components.panel({
                title: 'Per-service success rate',
                sub: 'derived from picked_service across recent attempts',
                children: body,
            }));
        }

        return () => { if (pollTimer) clearInterval(pollTimer); };
    }

    /* =====================================================================
     * Monitor → Discovery
     * ===================================================================== */

    function renderDiscoveryMonitor(root) {
        const state = {
            mode: 'lidarr', // 'lidarr' | 'fill' | 'soulseek'
            lidarr: null,
            fill: null,
            soulseek: null,
        };
        let pollTimer = null;

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'MONITOR',
            title: 'Discovery',
        }));

        const subnav = document.createElement('div');
        subnav.className = 'op-subnav';
        const SUBS = [
            { id: 'lidarr', label: 'Lidarr Discovery' },
            { id: 'fill', label: 'Fill Library' },
            { id: 'soulseek', label: 'Soulseek Bulk' },
        ];
        function buildSubnav() {
            subnav.innerHTML = '';
            SUBS.forEach(s => {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'op-subnav-btn' + (state.mode === s.id ? ' active' : '');
                btn.textContent = s.label;
                btn.addEventListener('click', () => {
                    if (state.mode === s.id) return;
                    state.mode = s.id;
                    buildSubnav();
                    loadCurrent();
                    schedulePolling();
                });
                subnav.appendChild(btn);
            });
        }
        buildSubnav();
        root.appendChild(subnav);

        const body = document.createElement('div');
        body.className = 'op-page-body';
        root.appendChild(body);

        loadCurrent();
        schedulePolling();

        function schedulePolling() {
            if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
            const interval = state.mode === 'soulseek' ? 3000 : 30000;
            pollTimer = setInterval(loadCurrent, interval);
        }

        function loadCurrent() {
            if (state.mode === 'lidarr') return loadLidarr();
            if (state.mode === 'fill') return loadFill();
            return loadSoulseek();
        }

        function loadLidarr() {
            return Promise.all([
                GIQ.api.get('/v1/discovery/stats').catch(() => null),
                GIQ.api.get('/v1/discovery?limit=50').catch(() => null),
            ]).then(([stats, list]) => {
                state.lidarr = { stats: stats || {}, list: list || { requests: [], total: 0 } };
                renderLidarr();
            });
        }

        function loadFill() {
            return Promise.all([
                GIQ.api.get('/v1/fill-library/stats').catch(() => null),
                GIQ.api.get('/v1/fill-library?limit=50').catch(() => null),
            ]).then(([stats, list]) => {
                state.fill = { stats: stats || {}, list: list || { requests: [], total: 0 } };
                renderFill();
            });
        }

        function loadSoulseek() {
            return GIQ.api.get('/v1/soulseek/bulk-download/status').then(job => {
                state.soulseek = job || { status: 'no_job' };
                renderSoulseek();
            }).catch(() => {
                state.soulseek = { status: 'no_job' };
                renderSoulseek();
            });
        }

        function renderLidarr() {
            body.innerHTML = '';
            const st = state.lidarr.stats || {};
            const list = state.lidarr.list || {};
            const requests = list.requests || [];

            if (!st.enabled) {
                body.appendChild(notConfiguredPanel(
                    'Music Discovery is not configured.',
                    ['LASTFM_API_KEY', 'LIDARR_URL', 'LIDARR_API_KEY'],
                ));
                return;
            }

            const sent = (st.by_status?.sent || 0) + (st.by_status?.in_lidarr || 0);
            const stats = document.createElement('div');
            stats.className = 'op-stat-row';
            stats.appendChild(GIQ.components.statTile({
                label: 'Artists discovered', value: GIQ.fmt.fmtNumber(st.total || 0),
            }));
            stats.appendChild(GIQ.components.statTile({
                label: 'Sent to Lidarr', value: GIQ.fmt.fmtNumber(sent),
                delta: 'success', deltaKind: 'good',
            }));
            stats.appendChild(GIQ.components.statTile({
                label: 'Pending', value: GIQ.fmt.fmtNumber(st.by_status?.pending || 0),
            }));
            stats.appendChild(GIQ.components.statTile({
                label: 'Today',
                value: (st.today_count || 0) + ' / ' + (st.daily_limit || '—'),
                delta: 'daily limit',
            }));
            body.appendChild(stats);

            const tableBody = document.createElement('div');
            if (!requests.length) {
                tableBody.innerHTML = '<div class="empty-row">No discovery requests yet. Run from Actions → Discovery.</div>';
            } else {
                tableBody.appendChild(buildDiscoveryTable(requests));
            }
            body.appendChild(GIQ.components.panel({
                title: 'Discovery history',
                sub: GIQ.fmt.fmtNumber(list.total || 0) + ' total · last ' + requests.length,
                children: tableBody,
            }));
        }

        function buildDiscoveryTable(requests) {
            const tbl = document.createElement('table');
            tbl.className = 'op-history-table';
            tbl.innerHTML = ''
                + '<thead><tr>'
                + '<th class="eyebrow">Artist</th>'
                + '<th class="eyebrow">Source</th>'
                + '<th class="eyebrow">Seed</th>'
                + '<th class="eyebrow">Similarity</th>'
                + '<th class="eyebrow">Status</th>'
                + '<th class="eyebrow">When</th>'
                + '</tr></thead>';
            const tb = document.createElement('tbody');
            requests.forEach(r => {
                const tr = document.createElement('tr');
                const seed = r.seed_artist
                    ? GIQ.fmt.esc(r.seed_artist)
                    : (r.seed_genre ? '<span class="muted">' + GIQ.fmt.esc(r.seed_genre) + '</span>' : '—');
                const simBar = r.similarity_score != null
                    ? '<div class="op-mini-bar"><div class="op-mini-bar-fill" style="width:' + (r.similarity_score * 100).toFixed(0) + '%"></div></div>'
                    + '<span class="mono muted op-mini-bar-pct">' + (r.similarity_score * 100).toFixed(0) + '%</span>'
                    : '<span class="mono muted">—</span>';
                tr.innerHTML = ''
                    + '<td><strong>' + GIQ.fmt.esc(r.artist_name || '') + '</strong>'
                    + (r.artist_mbid ? '<div class="mono muted op-mbid">' + GIQ.fmt.esc(r.artist_mbid).slice(0, 16) + '…</div>' : '')
                    + '</td>'
                    + '<td><span class="op-source-chip mono">' + GIQ.fmt.esc((r.source || '').replace('_', ' ')) + '</span></td>'
                    + '<td>' + seed + '</td>'
                    + '<td><div class="op-sim-cell">' + simBar + '</div></td>'
                    + '<td>' + buildDiscoveryStatusChip(r.status) + (r.error_message ? '<div class="mono wine op-err-snip" title="' + GIQ.fmt.esc(r.error_message) + '">' + GIQ.fmt.esc(r.error_message).slice(0, 40) + '…</div>' : '') + '</td>'
                    + '<td class="mono muted">' + GIQ.fmt.timeAgo(r.created_at) + '</td>';
                tb.appendChild(tr);
            });
            tbl.appendChild(tb);
            return tbl;
        }

        function buildDiscoveryStatusChip(status) {
            const cls = (status === 'in_lidarr' || status === 'sent') ? 'good'
                : status === 'failed' ? 'bad'
                : 'muted';
            return '<span class="op-status-badge mono ' + cls + '">' + GIQ.fmt.esc(status || 'unknown') + '</span>';
        }

        function renderFill() {
            body.innerHTML = '';
            const st = state.fill.stats || {};
            const list = state.fill.list || {};
            const requests = list.requests || [];

            if (!st.enabled) {
                body.appendChild(notConfiguredPanel(
                    'Fill Library is not configured.',
                    ['FILL_LIBRARY_ENABLED=true', 'AB_LOOKUP_URL', 'LIDARR_URL', 'LIDARR_API_KEY'],
                ));
                return;
            }

            const sent = st.by_status?.sent || 0;
            const avgMatch = st.avg_distance_sent != null
                ? ((1 - st.avg_distance_sent) * 100).toFixed(0) + '%'
                : '—';

            const stats = document.createElement('div');
            stats.className = 'op-stat-row';
            stats.appendChild(GIQ.components.statTile({
                label: 'Albums queued', value: GIQ.fmt.fmtNumber(sent),
                delta: 'success', deltaKind: 'good',
            }));
            stats.appendChild(GIQ.components.statTile({
                label: 'Total processed', value: GIQ.fmt.fmtNumber(st.total || 0),
            }));
            stats.appendChild(GIQ.components.statTile({
                label: 'Avg match', value: avgMatch,
                delta: '1 - distance',
            }));
            stats.appendChild(GIQ.components.statTile({
                label: 'Today',
                value: (st.today_count || 0) + ' / ' + (st.max_per_run || '—'),
                delta: 'max per run',
            }));
            body.appendChild(stats);

            const tableBody = document.createElement('div');
            if (!requests.length) {
                tableBody.innerHTML = '<div class="empty-row">No fill-library requests yet. Run from Actions → Discovery.</div>';
            } else {
                tableBody.appendChild(buildFillTable(requests));
            }
            body.appendChild(GIQ.components.panel({
                title: 'Fill Library history',
                sub: GIQ.fmt.fmtNumber(list.total || 0) + ' total · last ' + requests.length,
                children: tableBody,
            }));
        }

        function buildFillTable(requests) {
            const tbl = document.createElement('table');
            tbl.className = 'op-history-table';
            tbl.innerHTML = ''
                + '<thead><tr>'
                + '<th class="eyebrow">Artist</th>'
                + '<th class="eyebrow">Album</th>'
                + '<th class="eyebrow op-num">Tracks</th>'
                + '<th class="eyebrow">Match</th>'
                + '<th class="eyebrow">Status</th>'
                + '<th class="eyebrow">When</th>'
                + '</tr></thead>';
            const tb = document.createElement('tbody');
            requests.forEach(fl => {
                const tr = document.createElement('tr');
                const matchPct = fl.avg_distance != null ? ((1 - fl.avg_distance) * 100).toFixed(0) : null;
                const bestPct = fl.best_distance != null ? ((1 - fl.best_distance) * 100).toFixed(0) : null;
                const matchCell = matchPct != null
                    ? '<div class="op-sim-cell">'
                        + '<div class="op-mini-bar"><div class="op-mini-bar-fill" style="width:' + matchPct + '%"></div></div>'
                        + '<span class="mono muted op-mini-bar-pct">' + matchPct + '%' + (bestPct != null ? ' (best ' + bestPct + '%)' : '') + '</span>'
                    + '</div>'
                    : '<span class="mono muted">—</span>';
                const albumCell = fl.album_name
                    ? GIQ.fmt.esc(fl.album_name)
                    : (fl.album_mbid
                        ? '<span class="mono muted">' + GIQ.fmt.esc(fl.album_mbid).slice(0, 16) + '…</span>'
                        : '<span class="muted">—</span>');
                tr.innerHTML = ''
                    + '<td><strong>' + GIQ.fmt.esc(fl.artist_name || '') + '</strong></td>'
                    + '<td>' + albumCell + '</td>'
                    + '<td class="op-num mono">' + (fl.matched_tracks != null ? fl.matched_tracks : '—') + '</td>'
                    + '<td>' + matchCell + '</td>'
                    + '<td>' + buildDiscoveryStatusChip(fl.status) + (fl.error_message ? '<div class="mono wine op-err-snip" title="' + GIQ.fmt.esc(fl.error_message) + '">' + GIQ.fmt.esc(fl.error_message).slice(0, 40) + '…</div>' : '') + '</td>'
                    + '<td class="mono muted">' + GIQ.fmt.timeAgo(fl.created_at) + '</td>';
                tb.appendChild(tr);
            });
            tbl.appendChild(tb);
            return tbl;
        }

        function renderSoulseek() {
            body.innerHTML = '';
            const job = state.soulseek || {};

            if (!job.status || job.status === 'no_job') {
                const empty = document.createElement('div');
                empty.innerHTML = '<div class="empty-row">No Soulseek bulk job has run yet. Start one from Actions → Discovery → Soulseek.</div>';
                body.appendChild(GIQ.components.panel({
                    title: 'Soulseek bulk download',
                    sub: 'no active job',
                    children: empty,
                }));
                return;
            }

            const isRunning = job.status === 'running';
            const total = job.total_artists || 0;
            const done = job.artists_processed || 0;
            const pct = total > 0 ? Math.round(done / total * 100) : 0;

            const stats = document.createElement('div');
            stats.className = 'op-stat-row';
            stats.appendChild(GIQ.components.statTile({
                label: 'Tracks found', value: GIQ.fmt.fmtNumber(job.total_tracks || 0),
            }));
            stats.appendChild(GIQ.components.statTile({
                label: 'Queued', value: GIQ.fmt.fmtNumber(job.tracks_queued || 0),
                delta: 'sent to slskd', deltaKind: 'good',
            }));
            stats.appendChild(GIQ.components.statTile({
                label: 'Searched', value: GIQ.fmt.fmtNumber(job.tracks_searched || 0),
            }));
            stats.appendChild(GIQ.components.statTile({
                label: 'Skipped', value: GIQ.fmt.fmtNumber(job.tracks_skipped || 0),
            }));
            stats.appendChild(GIQ.components.statTile({
                label: 'Failed', value: GIQ.fmt.fmtNumber(job.tracks_failed || 0),
                deltaKind: (job.tracks_failed || 0) > 0 ? 'bad' : 'flat',
            }));
            const elapsed = job.started_at
                ? ((job.finished_at || Math.floor(Date.now() / 1000)) - job.started_at)
                : null;
            stats.appendChild(GIQ.components.statTile({
                label: 'Elapsed',
                value: elapsed != null ? fmtElapsedSeconds(elapsed) : '—',
                delta: job.started_at ? 'started ' + GIQ.fmt.timeAgo(job.started_at) : null,
            }));
            body.appendChild(stats);

            const progBody = document.createElement('div');
            progBody.className = 'op-soulseek-progress';
            progBody.innerHTML = ''
                + '<div class="op-soulseek-progress-row">'
                + '<span class="mono">artists</span>'
                + '<div class="op-dl-bar"><div class="op-dl-bar-fill" style="width:' + pct + '%"></div></div>'
                + '<span class="mono">' + done + ' / ' + total + ' · ' + pct + '%</span>'
                + '</div>'
                + (job.current_artist && isRunning
                    ? '<div class="op-soulseek-current mono muted">currently · <strong class="op-soulseek-artist">' + GIQ.fmt.esc(job.current_artist) + '</strong></div>'
                    : '');

            body.appendChild(GIQ.components.panel({
                title: 'Soulseek bulk download',
                sub: 'status · ' + job.status,
                badge: isRunning ? 'LIVE' : null,
                children: progBody,
            }));

            if (job.errors && job.errors.length) {
                const errBody = document.createElement('div');
                errBody.className = 'op-soulseek-errors mono';
                for (let i = job.errors.length - 1; i >= Math.max(0, job.errors.length - 20); i--) {
                    const row = document.createElement('div');
                    row.className = 'op-soulseek-error-row wine';
                    row.textContent = job.errors[i];
                    errBody.appendChild(row);
                }
                body.appendChild(GIQ.components.panel({
                    title: 'Recent errors',
                    sub: GIQ.fmt.fmtNumber(job.errors.length) + ' total',
                    children: errBody,
                }));
            }
        }

        function notConfiguredPanel(title, envVars) {
            const wrap = document.createElement('div');
            wrap.className = 'op-not-configured';
            wrap.innerHTML = '<div class="op-not-configured-title">' + GIQ.fmt.esc(title) + '</div>'
                + '<div class="op-not-configured-sub">Set the following in your <span class="mono">.env</span>:</div>'
                + '<ul class="op-not-configured-list mono">'
                + envVars.map(v => '<li>' + GIQ.fmt.esc(v) + '</li>').join('')
                + '</ul>';
            return wrap;
        }

        function fmtElapsedSeconds(s) {
            if (s == null) return '—';
            const m = Math.floor(s / 60);
            const sec = s % 60;
            if (m === 0) return sec + 's';
            const h = Math.floor(m / 60);
            if (h === 0) return m + 'm ' + sec + 's';
            return h + 'h ' + (m % 60) + 'm';
        }

        return () => { if (pollTimer) clearInterval(pollTimer); };
    }

    /* =====================================================================
     * Monitor → Charts
     * ===================================================================== */

    function renderChartsMonitor(root) {
        const state = {
            stats: null,
            charts: null,
        };
        let pollTimer = null;

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'MONITOR',
            title: 'Charts Stats',
        }));

        const banner = document.createElement('div');
        banner.className = 'op-build-banner';
        root.appendChild(banner);

        const body = document.createElement('div');
        body.className = 'op-page-body';
        root.appendChild(body);

        const statsHost = document.createElement('div');
        statsHost.className = 'op-stat-row op-stat-row-six';
        body.appendChild(statsHost);

        const breakdownHost = document.createElement('div');
        body.appendChild(breakdownHost);

        load();
        pollTimer = setInterval(load, 30000);

        function load() {
            return Promise.all([
                GIQ.api.get('/v1/charts/stats').catch(() => null),
                GIQ.api.get('/v1/charts').catch(() => null),
            ]).then(([stats, charts]) => {
                state.stats = stats;
                state.charts = charts;
                renderBanner();
                renderStats();
                renderBreakdown();
            });
        }

        function renderBanner() {
            banner.innerHTML = '';
            banner.className = 'op-build-banner';
            const st = state.stats;
            if (!st) {
                banner.classList.add('warn');
                banner.innerHTML = '<span class="op-build-dot"></span>'
                    + '<span class="mono"><strong>UNAVAILABLE</strong></span>'
                    + '<span class="op-build-msg">Could not load /v1/charts/stats. Charts may not be enabled.</span>';
                return;
            }
            const auto = !!st.auto_rebuild_enabled;
            const last = st.last_fetched_at;
            const interval = st.interval_hours || 24;
            const ageH = last ? (Math.floor(Date.now() / 1000) - last) / 3600 : null;

            let kind = 'warn';
            let title = 'STALE';
            let msg = '';
            if (last == null) {
                title = 'NOT YET BUILT';
                msg = 'No charts have been built. Trigger a build from Actions → Charts.';
            } else if (ageH < interval * 1.5) {
                kind = 'good';
                title = 'FRESH';
                msg = 'Built ' + GIQ.fmt.timeAgo(last) + (auto ? ' · auto-rebuild every ' + interval + 'h' : ' · auto-rebuild OFF');
            } else if (ageH < interval * 3) {
                kind = 'warn';
                title = 'AGING';
                msg = 'Built ' + GIQ.fmt.timeAgo(last) + ' · expected within ' + interval + 'h';
            } else {
                kind = 'bad';
                title = 'STALE';
                msg = 'Last built ' + GIQ.fmt.timeAgo(last) + ' · expected every ' + interval + 'h';
            }
            if (!auto) {
                msg += ' · set CHARTS_ENABLED=true in your .env to enable scheduled builds';
            }

            banner.classList.add(kind);
            banner.innerHTML = '<span class="op-build-dot"></span>'
                + '<span class="mono op-build-state"><strong>' + title + '</strong></span>'
                + '<span class="op-build-msg">' + GIQ.fmt.esc(msg) + '</span>';
        }

        function renderStats() {
            statsHost.innerHTML = '';
            const st = state.stats || {};

            const lastValue = st.last_fetched_at ? GIQ.fmt.timeAgo(st.last_fetched_at) : 'Never';
            statsHost.appendChild(GIQ.components.statTile({
                label: 'Last build',
                value: lastValue,
                delta: st.last_fetched_at ? new Date(st.last_fetched_at * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC' : null,
            }));
            statsHost.appendChild(GIQ.components.statTile({
                label: 'Total charts',
                value: GIQ.fmt.fmtNumber(st.chart_count || 0),
            }));
            statsHost.appendChild(GIQ.components.statTile({
                label: 'Total entries',
                value: GIQ.fmt.fmtNumber(st.total_entries || 0),
            }));
            statsHost.appendChild(GIQ.components.statTile({
                label: 'Library matches',
                value: GIQ.fmt.fmtNumber(st.library_matches || 0),
                delta: 'in your library',
                deltaKind: 'good',
            }));

            const matchRate = st.match_rate != null ? (st.match_rate * 100).toFixed(1) + '%' : '—';
            statsHost.appendChild(GIQ.components.statTile({
                label: 'Match rate',
                value: matchRate,
            }));

            const unmatched = (st.total_entries || 0) - (st.library_matches || 0);
            statsHost.appendChild(GIQ.components.statTile({
                label: 'Unmatched',
                value: GIQ.fmt.fmtNumber(unmatched),
                delta: unmatched > 0 ? 'candidates for download' : null,
                deltaKind: 'flat',
            }));
        }

        function renderBreakdown() {
            breakdownHost.innerHTML = '';
            const charts = (state.charts && state.charts.charts) || [];

            if (!charts.length) {
                const body = document.createElement('div');
                body.innerHTML = '<div class="empty-row">No charts built yet. Trigger from Actions → Charts.</div>';
                breakdownHost.appendChild(GIQ.components.panel({
                    title: 'Per-scope breakdown',
                    sub: 'no charts available',
                    children: body,
                }));
                return;
            }

            // Group charts by scope.
            const groups = {};
            charts.forEach(c => {
                const scope = c.scope || 'global';
                let groupKey = 'Global';
                if (scope.indexOf('tag:') === 0) groupKey = 'Tags';
                else if (scope.indexOf('geo:') === 0) groupKey = 'Countries';
                const g = groups[groupKey] || (groups[groupKey] = []);
                g.push(c);
            });

            const tbody = document.createElement('div');
            tbody.className = 'op-charts-breakdown';

            Object.keys(groups).forEach(k => {
                const items = groups[k];
                const groupSection = document.createElement('div');
                groupSection.className = 'op-charts-group';
                groupSection.innerHTML = '<div class="op-charts-group-head"><span class="mono">' + GIQ.fmt.esc(k.toUpperCase()) + '</span><span class="op-charts-group-count mono muted">' + items.length + ' chart' + (items.length === 1 ? '' : 's') + '</span></div>';

                const inner = document.createElement('div');
                inner.className = 'op-charts-group-list';
                items.forEach(c => {
                    const scopeLabel = c.scope === 'global' ? 'global'
                        : c.scope.indexOf('tag:') === 0 ? c.scope.slice(4)
                        : c.scope.indexOf('geo:') === 0 ? c.scope.slice(4)
                        : c.scope;
                    const row = document.createElement('div');
                    row.className = 'op-charts-row';
                    row.innerHTML = ''
                        + '<span class="op-charts-row-name">' + GIQ.fmt.esc(scopeLabel) + '</span>'
                        + '<span class="op-source-chip mono">' + GIQ.fmt.esc((c.chart_type || '').replace('_', ' ')) + '</span>'
                        + '<span class="op-charts-row-meta mono muted">' + GIQ.fmt.fmtNumber(c.entry_count || c.total || 0) + ' entries</span>'
                        + (c.fetched_at
                            ? '<span class="op-charts-row-meta mono muted">' + GIQ.fmt.timeAgo(c.fetched_at) + '</span>'
                            : '<span class="op-charts-row-meta mono muted">—</span>');
                    inner.appendChild(row);
                });
                groupSection.appendChild(inner);
                tbody.appendChild(groupSection);
            });

            breakdownHost.appendChild(GIQ.components.panel({
                title: 'Per-scope breakdown',
                sub: charts.length + ' charts grouped by scope',
                children: tbody,
            }));
        }

        return () => { if (pollTimer) clearInterval(pollTimer); };
    }

})();
