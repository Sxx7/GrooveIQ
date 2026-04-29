/* monitor.js — Monitor bucket pages.
 * Overview: full-fidelity (session 02). Other sub-pages remain stubs and are
 * filled in by sessions 06–08.
 */

(function () {
    GIQ.pages.monitor = GIQ.pages.monitor || {};

    const STUBS = [
        'pipeline', 'models', 'system-health', 'recs-debug',
        'user-diagnostics', 'integrations', 'downloads', 'lidarr-backfill',
        'discovery', 'charts',
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

    /* ---- Constants ---------------------------------------------------- */

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
        { name: 'Backfill CLAP', href: '#/actions/library', defaultSub: 'fill missing CLAP embeddings' },
    ];

    /* ---- Page render -------------------------------------------------- */

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

        // Header
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

        // Scrollable content body
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

        // Initial loading skeletons
        renderStatRow(statRow, state);
        renderEventChart(eventChartHost, state);
        renderTopTracks(topTracksHost, state);
        renderEventTypes(eventTypesHost, state);
        renderRecentEvents(recentEventsHost, state);
        renderModels(modelsHost, state);
        renderScan(scanHost, state);
        renderQuickRun(quickRunHost, state);

        async function refreshAll() {
            if (state.destroyed) return;
            const apiKey = GIQ.state.apiKey;
            if (!apiKey) {
                renderStatRow(statRow, state);
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

            renderStatRow(statRow, state);
            renderEventChart(eventChartHost, state);
            renderTopTracks(topTracksHost, state);
            renderEventTypes(eventTypesHost, state);
            renderRecentEvents(recentEventsHost, state);
            renderModels(modelsHost, state);
            renderScan(scanHost, state);
            renderQuickRun(quickRunHost, state);

            lastUpdateEl.textContent = 'last update · just now';
        }

        // First load
        refreshAll();

        // Periodic refresh — full set every 15s, recent events every 5s.
        const fullTimer = setInterval(refreshAll, 15000);
        const recentTimer = setInterval(async () => {
            if (state.destroyed) return;
            if (!GIQ.state.apiKey) return;
            try {
                const recent = await GIQ.api.get('/v1/events?limit=4');
                state.recentEvents = Array.isArray(recent) ? recent : null;
                renderRecentEvents(recentEventsHost, state);
            } catch (_) { /* ignore */ }
        }, 5000);

        // Tick "last update" every second (relative time only)
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

        // SSE refresh hooks
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

    /* ---- Stat row ----------------------------------------------------- */

    function renderStatRow(host, state) {
        host.innerHTML = '';
        const stats = state.stats;
        const models = state.models;

        const tiles = [];

        // Events
        const totalEvents = stats?.total_events;
        const e24 = stats?.events_last_24h;
        tiles.push(GIQ.components.statTile({
            label: 'Events',
            value: totalEvents != null ? Number(totalEvents).toLocaleString() : '—',
            delta: e24 != null ? '+' + Number(e24).toLocaleString() + ' / 24h' : null,
            deltaKind: e24 > 0 ? 'good' : 'flat',
        }));
        // Users
        tiles.push(GIQ.components.statTile({
            label: 'Users',
            value: stats?.total_users != null ? String(stats.total_users) : '—',
            delta: null,
            deltaKind: 'flat',
        }));
        // Tracks
        tiles.push(GIQ.components.statTile({
            label: 'Tracks',
            value: stats?.total_tracks_analyzed != null
                ? Number(stats.total_tracks_analyzed).toLocaleString() : '—',
            delta: stats?.analysis_version ? 'analysis v' + stats.analysis_version : null,
            deltaKind: 'flat',
        }));
        // Playlists
        tiles.push(GIQ.components.statTile({
            label: 'Playlists',
            value: stats?.total_playlists != null ? String(stats.total_playlists) : '—',
            delta: null,
            deltaKind: 'flat',
        }));
        // Events / hr — compare to 24h average
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
            delta: perHrDelta,
            deltaKind: perHrKind,
        }));
        // Ranker
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

    /* ---- Event ingest area chart -------------------------------------- */

    function renderEventChart(host, state) {
        host.innerHTML = '';
        const evs = state.events;
        const buckets = (evs && evs.buckets) || [];

        let series = [];
        let labels = [];
        if (buckets.length) {
            const total = buckets.map(b => b.count || 0);
            // Engagement subset can be approximated as ~30% of total when we have no breakdown.
            // Keep a single-series chart honest until we have a richer endpoint (session 07).
            series = [
                {
                    name: 'All events',
                    color: '#a887ce',
                    values: total,
                    strokeWidth: 2,
                    fillOpacity: 0.45,
                },
            ];
            // Build seven evenly-spaced HH:MM x-axis labels.
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
            series,
            labels,
            height: 180,
            emptyText: state.events ? 'No events in window' : 'Loading…',
        });

        const action = document.createElement('a');
        action.className = 'panel-action';
        action.textContent = 'View full breakdown →';
        action.href = '#/monitor/system-health';

        host.appendChild(GIQ.components.panel({
            title: 'Event ingest',
            sub: 'play_end · like · skip · pause · etc · last 24h · 15m bins',
            action,
            children: chart,
        }));
    }

    /* ---- Top tracks panel --------------------------------------------- */

    function renderTopTracks(host, state) {
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

    /* ---- Event types panel -------------------------------------------- */

    function renderEventTypes(host, state) {
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

    /* ---- Recent events panel ------------------------------------------ */

    function renderRecentEvents(host, state) {
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

    /* ---- Models panel ------------------------------------------------- */

    function renderModels(host, state) {
        host.innerHTML = '';
        const models = state.models || {};

        const list = document.createElement('div');
        list.className = 'models-list';
        MODEL_ROWS.forEach((m, i) => {
            const data = models[m.key];
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

        const action = document.createElement('a');
        action.className = 'panel-action';
        action.textContent = 'See all →';
        action.href = '#/monitor/models';

        host.appendChild(GIQ.components.panel({
            title: 'Models',
            sub: 'readiness · 6 surfaces',
            action,
            children: list,
        }));
    }

    /* ---- Library scan panel ------------------------------------------- */

    function renderScan(host, state) {
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
            title: 'Library scan',
            sub,
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

    /* ---- Quick run panel ---------------------------------------------- */

    function renderQuickRun(host, state) {
        host.innerHTML = '';
        const list = document.createElement('div');
        list.className = 'quick-run-list';

        // Map most-recent pipeline run to "Run pipeline" sub-line.
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

        // Library scan sub-line.
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

        QUICK_RUN_ROWS.forEach((q, i) => {
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
})();
