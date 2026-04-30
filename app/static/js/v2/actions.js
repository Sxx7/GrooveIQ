/* actions.js — Actions bucket pages.
 * Session 05: 5 grouped pages following Shape B (page-actions.jsx ActionsGrouped).
 * Each page lists its triggers as cards. Discovery additionally hosts the
 * Lidarr Backfill Queue table; Downloads hosts the multi-agent search tool.
 */

(function () {
    GIQ.pages.actions = GIQ.pages.actions || {};
    const esc = GIQ.fmt.esc;
    const timeAgo = GIQ.fmt.timeAgo;

    /* ── shared helpers ──────────────────────────────────────────────── */

    function makePage(opts) {
        // Returns a [pageHeader, body] pair. body is a flex-column wrapper.
        const wrap = document.createElement('div');
        wrap.className = 'actions-page';

        const header = GIQ.components.pageHeader({
            eyebrow: 'ACTIONS',
            title: opts.title,
            right: opts.right,
        });
        wrap.appendChild(header);

        if (opts.subline) {
            const sub = document.createElement('div');
            sub.className = 'actions-subline';
            sub.textContent = opts.subline;
            wrap.appendChild(sub);
        }

        const body = document.createElement('div');
        body.className = 'actions-body';
        wrap.appendChild(body);

        return { wrap, body, header };
    }

    function renderError(host, message) {
        host.innerHTML = '';
        const div = document.createElement('div');
        div.className = 'actions-error';
        div.textContent = message;
        host.appendChild(div);
    }

    function renderLoading(host, message) {
        host.innerHTML = '';
        const div = document.createElement('div');
        div.className = 'actions-loading';
        div.textContent = message || 'Loading…';
        host.appendChild(div);
    }

    function describePipelineStatus(run) {
        if (!run) return 'no recent run';
        const when = timeAgo(run.started_at || run.created_at || run.ended_at);
        const status = run.status || 'unknown';
        return 'last run · ' + when + ' · ' + status;
    }

    /* ===================================================================
     * Page B — Pipeline & ML
     * =================================================================== */

    GIQ.pages.actions['pipeline-ml'] = function (root) {
        const { wrap, body } = makePage({
            title: 'Pipeline & ML',
            subline: 'Trigger pipeline runs, model retraining, and one-shot maintenance jobs.',
        });
        root.appendChild(wrap);

        // Initial cards (no last-run data yet — refreshed below)
        const runPipeline = GIQ.components.actionCard({
            name: 'Run Pipeline',
            description: 'Triggers the full 10-step recommendation pipeline (sessionizer through music_map). Auto-redirects to Monitor → Pipeline.',
            state: 'good',
            monitorPath: '#/monitor/pipeline',
            monitorLabel: 'Pipeline →',
            onRun: () => GIQ.api.post('/v1/pipeline/run', {}),
        });
        const resetPipeline = GIQ.components.actionCard({
            name: 'Reset Pipeline',
            description: 'Clears all sessions, interactions, and taste profiles, then rebuilds from raw events. Required after large algorithm changes.',
            destructive: true,
            confirm: 'Reset clears all pipeline state and rebuilds from raw events. Continue?',
            monitorPath: '#/monitor/pipeline',
            monitorLabel: 'Pipeline →',
            onRun: () => GIQ.api.post('/v1/pipeline/reset', {}),
        });
        const backfillClap = GIQ.components.actionCard({
            name: 'Backfill CLAP',
            description: 'Generates CLAP audio embeddings for tracks scanned before CLAP was enabled. Runs in the background. Requires CLAP_ENABLED=true.',
            onRun: () => GIQ.api.post('/v1/tracks/clap/backfill', {}),
        });

        // Cleanup Stale — has an inline dry-run toggle that flows into onRun
        const dryRunWrap = document.createElement('label');
        dryRunWrap.className = 'action-card-toggle';
        const dryRunCb = document.createElement('input');
        dryRunCb.type = 'checkbox';
        dryRunCb.checked = true; // default to dry-run for safety
        const dryRunLbl = document.createElement('span');
        dryRunLbl.textContent = 'dry-run (preview only — uncheck to actually delete)';
        dryRunWrap.appendChild(dryRunCb);
        dryRunWrap.appendChild(dryRunLbl);

        const cleanupStale = GIQ.components.actionCard({
            name: 'Cleanup Stale Tracks',
            description: 'Removes legacy 16-hex TrackFeatures rows whose underlying files are gone. Defaults to dry-run; uncheck the toggle to actually delete.',
            destructive: true,
            extras: dryRunWrap,
            confirm: 'Cleanup will scan for orphaned TrackFeatures rows and either preview or delete depending on the dry-run toggle. Continue?',
            onRun: async () => {
                const dryRun = !!dryRunCb.checked;
                const url = '/v1/library/cleanup-stale?dry_run=' + (dryRun ? 'true' : 'false') + '&pattern=legacy_hex';
                const res = await GIQ.api.post(url, {});
                const counts = res ? (res.deleted != null ? res.deleted : (res.matched || 0)) : 0;
                GIQ.toast(
                    (dryRun ? 'Dry-run: ' : 'Deleted: ') + counts + ' stale row(s).',
                    dryRun ? 'info' : 'success'
                );
                return res;
            },
        });

        body.appendChild(runPipeline.el);
        body.appendChild(resetPipeline.el);
        body.appendChild(backfillClap.el);
        body.appendChild(cleanupStale.el);

        // Async hydrate: pipeline last-run + clap pending count
        if (GIQ.state.apiKey) {
            GIQ.api.get('/v1/pipeline/status?limit=1').then(data => {
                const run = data && (data.current || (data.history && data.history[0]));
                runPipeline.refresh({ lastRun: describePipelineStatus(run) });
                resetPipeline.refresh({ lastRun: run ? ('last run · ' + timeAgo(run.started_at || run.created_at)) : null });
            }).catch(() => { /* leave blank */ });

            GIQ.api.get('/v1/tracks/clap/stats').then(stats => {
                if (!stats) return;
                if (stats.enabled === false) {
                    backfillClap.refresh({ lastRun: 'CLAP disabled — set CLAP_ENABLED=true to enable' });
                    return;
                }
                const pending = (stats.total || 0) - (stats.with_clap || 0);
                const cov = stats.coverage != null ? Math.round(stats.coverage * 100) : null;
                backfillClap.refresh({
                    lastRun: pending > 0
                        ? pending.toLocaleString() + ' tracks pending · coverage ' + (cov != null ? cov + '%' : '—')
                        : 'all tracks covered (' + (stats.with_clap || 0).toLocaleString() + ')',
                });
            }).catch(() => { /* leave blank */ });
        } else {
            const note = document.createElement('div');
            note.className = 'actions-empty';
            note.textContent = 'Connect API key to load last-run / pending-count details. Triggers below still work once connected.';
            body.insertBefore(note, body.firstChild);
        }
    };

    /* ===================================================================
     * Page C — Library
     * =================================================================== */

    GIQ.pages.actions['library'] = function (root) {
        const { wrap, body } = makePage({
            title: 'Library',
            subline: 'Re-scan files on disk and reconcile track IDs with the connected media server.',
        });
        root.appendChild(wrap);

        const scan = GIQ.components.actionCard({
            name: 'Scan Library',
            description: 'Walks MUSIC_LIBRARY_PATH for new or changed files and queues analysis. Only one scan runs at a time.',
            monitorPath: '#/monitor/system-health',
            monitorLabel: 'Scan progress →',
            onRun: () => GIQ.api.post('/v1/library/scan', {}),
        });
        const sync = GIQ.components.actionCard({
            name: 'Sync IDs',
            description: 'Maps track IDs between the local library and Navidrome/Plex by file path. Cascades updates across events, interactions, sessions, features, and playlists.',
            onRun: () => GIQ.api.post('/v1/library/sync', {}),
        });

        body.appendChild(scan.el);
        body.appendChild(sync.el);

        // Hydrate scan card with the latest scan state from /v1/stats
        if (GIQ.state.apiKey) {
            GIQ.api.get('/v1/stats').then(stats => {
                const latest = stats && stats.latest_scan;
                if (!latest) {
                    scan.refresh({ lastRun: 'no scans yet' });
                    return;
                }
                const status = latest.status || 'unknown';
                const when = latest.started_at ? timeAgo(latest.started_at) : (latest.completed_at ? timeAgo(latest.completed_at) : '—');
                scan.refresh({
                    lastRun: 'last scan · ' + when + ' · ' + status + (latest.files_analyzed != null ? ' · ' + latest.files_analyzed + ' files' : ''),
                    state: status === 'running' ? 'good' : null,
                });
            }).catch(() => { /* leave blank */ });
        }
    };

    /* ===================================================================
     * Page D — Discovery (4 trigger cards + Lidarr Backfill Queue)
     * =================================================================== */

    GIQ.pages.actions['discovery'] = function (root) {
        const { wrap, body } = makePage({
            title: 'Discovery',
            subline: 'Pull new music into the library and manage the Lidarr backfill queue.',
        });
        root.appendChild(wrap);

        // ---- 4 trigger cards ----
        const triggers = document.createElement('div');
        triggers.className = 'actions-triggers';
        body.appendChild(triggers);

        const lidarrDisc = GIQ.components.actionCard({
            name: 'Lidarr Discovery',
            description: 'Last.fm similar-artist crawl that auto-adds new artists to Lidarr for download.',
            monitorPath: '#/monitor/discovery',
            monitorLabel: 'Discovery stats →',
            onRun: () => GIQ.api.post('/v1/discovery/run', {}),
        });
        const fillLib = GIQ.components.actionCard({
            name: 'Fill Library',
            description: 'Queries AcousticBrainz for tracks matching each user\'s taste profile, groups by album, and queues them via Lidarr.',
            monitorPath: '#/monitor/discovery',
            monitorLabel: 'Discovery stats →',
            onRun: () => GIQ.api.post('/v1/fill-library/run', {}),
        });

        // Soulseek Bulk — has its own trigger UI (max-artists / tracks-per-artist)
        const slskWrap = document.createElement('div');
        slskWrap.className = 'action-card-form';

        const slskArtists = _numField('Top artists', 500, 1, 1000);
        const slskTracks = _numField('Tracks/artist', 20, 1, 50);
        slskWrap.appendChild(slskArtists.field);
        slskWrap.appendChild(slskTracks.field);

        const slskEstimate = document.createElement('div');
        slskEstimate.className = 'action-card-sub mono';
        function updateEstimate() {
            const a = parseInt(slskArtists.input.value, 10) || 0;
            const t = parseInt(slskTracks.input.value, 10) || 0;
            slskEstimate.textContent = 'up to ' + (a * t).toLocaleString() + ' tracks (' + a + ' × ' + t + ')';
        }
        slskArtists.input.addEventListener('input', updateEstimate);
        slskTracks.input.addEventListener('input', updateEstimate);
        updateEstimate();
        slskWrap.appendChild(slskEstimate);

        const slsk = GIQ.components.actionCard({
            name: 'Soulseek Bulk',
            description: 'Fetches the top N artists from Last.fm global charts and downloads their top tracks via Soulseek (slskd). Skips tracks already in the library.',
            extras: slskWrap,
            runLabel: '▶ Start',
            confirm: 'Start a bulk Soulseek download? It will run in the background and may take hours.',
            onRun: async () => {
                const a = Math.min(1000, Math.max(1, parseInt(slskArtists.input.value, 10) || 500));
                const t = Math.min(50, Math.max(1, parseInt(slskTracks.input.value, 10) || 20));
                return GIQ.api.post('/v1/soulseek/bulk-download?max_artists=' + a + '&tracks_per_artist=' + t, {});
            },
        });

        const lbfNow = GIQ.components.actionCard({
            name: 'Run Lidarr Backfill (now)',
            description: 'Force one tick of the Lidarr backfill engine — picks the next batch from /wanted/missing and dispatches via streamrip.',
            monitorPath: '#/monitor/lidarr-backfill',
            monitorLabel: 'Backfill stats →',
            onRun: () => GIQ.api.post('/v1/lidarr-backfill/run', {}),
        });

        triggers.appendChild(lidarrDisc.el);
        triggers.appendChild(fillLib.el);
        triggers.appendChild(slsk.el);
        triggers.appendChild(lbfNow.el);

        // ---- Lidarr Backfill Queue (operator surface) ----
        const queueSection = document.createElement('section');
        queueSection.className = 'lbf-queue-section';
        body.appendChild(queueSection);

        // Cross-link rail
        queueSection.appendChild(GIQ.components.relatedRail({
            links: [
                { prefix: 'Settings', label: 'Edit backfill config →', href: '#/settings/lidarr-backfill' },
                { prefix: 'Monitor', label: 'Live stats & ETA →', href: '#/monitor/lidarr-backfill' },
            ],
        }));

        // Queue header (title + Pause / Run-now)
        const qHead = document.createElement('div');
        qHead.className = 'lbf-queue-head';
        const qTitle = document.createElement('div');
        qTitle.className = 'lbf-queue-title';
        qTitle.textContent = 'Lidarr Backfill Queue';
        qHead.appendChild(qTitle);

        const qBtns = document.createElement('div');
        qBtns.className = 'lbf-queue-btns';
        const pauseBtn = document.createElement('button');
        pauseBtn.type = 'button';
        pauseBtn.className = 'vc-btn';
        pauseBtn.textContent = 'Pause';
        pauseBtn.title = 'Toggle the backfill engine off via the Lidarr Backfill config.';
        pauseBtn.addEventListener('click', toggleBackfill);
        const runNowBtn = document.createElement('button');
        runNowBtn.type = 'button';
        runNowBtn.className = 'vc-btn vc-btn-primary';
        runNowBtn.textContent = '▶ Run now';
        runNowBtn.addEventListener('click', () => {
            GIQ.api.post('/v1/lidarr-backfill/run', {})
                .then(() => { GIQ.toast('Backfill tick triggered.', 'success'); reloadQueue(); })
                .catch(e => GIQ.toast('Run failed: ' + e.message, 'error'));
        });
        qBtns.appendChild(pauseBtn);
        qBtns.appendChild(runNowBtn);
        qHead.appendChild(qBtns);
        queueSection.appendChild(qHead);

        // Filter chips (All / Queued / In flight / Failed)
        const FILTER_OPTS = [
            { key: '', label: 'All', counts: ['queued_total', 'in_flight_total', 'failed_24h', 'complete_24h'] },
            { key: 'queued', label: 'Queued', counts: ['queued_total'] },
            { key: 'downloading', label: 'In flight', counts: ['in_flight_total'] },
            { key: 'failed', label: 'Failed', counts: ['failed_24h'] },
        ];
        const chipsBar = document.createElement('div');
        chipsBar.className = 'lbf-queue-chips';
        queueSection.appendChild(chipsBar);

        // Bulk actions row
        const bulkBar = document.createElement('div');
        bulkBar.className = 'lbf-queue-bulk';
        ['failed', 'no_match', 'permanently_skipped'].forEach(scope => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'vc-btn';
            btn.textContent = 'Clear ' + (scope === 'permanently_skipped' ? 'skipped' : scope.replace('_', ' '));
            btn.addEventListener('click', () => bulkReset(scope));
            bulkBar.appendChild(btn);
        });
        queueSection.appendChild(bulkBar);

        // Queue table host
        const tableHost = document.createElement('div');
        tableHost.className = 'lbf-queue-table-host';
        queueSection.appendChild(tableHost);

        let currentFilter = '';
        let stats = null;
        let backfillEnabled = null;

        function renderChips() {
            chipsBar.innerHTML = '';
            FILTER_OPTS.forEach(opt => {
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.className = 'lbf-chip' + (opt.key === currentFilter ? ' active' : '');
                let count = 0;
                if (stats) opt.counts.forEach(k => { if (typeof stats[k] === 'number') count += stats[k]; });
                chip.innerHTML = esc(opt.label) + ' <span class="lbf-chip-count mono">' + count + '</span>';
                chip.addEventListener('click', () => {
                    currentFilter = opt.key;
                    renderChips();
                    reloadQueue();
                });
                chipsBar.appendChild(chip);
            });
        }

        function reloadStats() {
            return GIQ.api.get('/v1/lidarr-backfill/stats').then(s => {
                stats = s || {};
                backfillEnabled = !!stats.enabled;
                pauseBtn.textContent = backfillEnabled ? 'Pause' : '▶ Resume';
                renderChips();
            }).catch(() => { stats = null; renderChips(); });
        }

        function reloadQueue() {
            renderLoading(tableHost, 'Loading queue…');
            const url = '/v1/lidarr-backfill/requests?limit=50' + (currentFilter ? '&status=' + encodeURIComponent(currentFilter) : '');
            GIQ.api.get(url).then(r => {
                renderTable(tableHost, (r && r.items) || []);
            }).catch(e => renderError(tableHost, 'Failed to load queue: ' + e.message));
        }

        function renderTable(host, items) {
            host.innerHTML = '';
            if (!items.length) {
                host.innerHTML = '<div class="actions-empty">No rows match the current filter.</div>';
                return;
            }
            const tbl = document.createElement('table');
            tbl.className = 'lbf-queue-table';
            tbl.innerHTML = '<thead><tr><th>State</th><th>Artist</th><th>Album</th><th class="num">Score</th><th>Service</th><th class="num">Attempts</th><th>Last attempt</th><th></th></tr></thead>';
            const tb = document.createElement('tbody');
            items.forEach(r => {
                const tr = document.createElement('tr');
                const stateChip = '<span class="lbf-state-chip lbf-state-' + esc(r.status) + '">' + esc(r.status) + '</span>';
                const score = r.match_score != null ? (r.match_score * 100).toFixed(0) + '%' : '—';
                tr.innerHTML =
                      '<td>' + stateChip + '</td>'
                    + '<td>' + esc(r.artist || '?') + '</td>'
                    + '<td>' + esc(r.album_title || '?') + '</td>'
                    + '<td class="num mono">' + score + '</td>'
                    + '<td class="mono">' + esc(r.picked_service || '—') + '</td>'
                    + '<td class="num mono">' + (r.attempt_count || 0) + '</td>'
                    + '<td class="mono">' + (r.last_attempt_at ? esc(timeAgo(r.last_attempt_at)) : '—') + '</td>';
                const td = document.createElement('td');
                td.className = 'lbf-row-actions';
                if (r.status === 'failed' || r.status === 'no_match') {
                    td.appendChild(_iconBtn('Retry', () => rowAction(r.id, 'retry')));
                }
                if (r.status !== 'permanently_skipped' && r.status !== 'complete') {
                    td.appendChild(_iconBtn('Skip', () => rowAction(r.id, 'skip')));
                }
                td.appendChild(_iconBtn('Forget', () => rowAction(r.id, 'forget')));
                tr.appendChild(td);
                if (r.last_error) tr.title = r.last_error;
                tb.appendChild(tr);
            });
            tbl.appendChild(tb);
            host.appendChild(tbl);
        }

        function rowAction(id, action) {
            let p;
            if (action === 'retry') p = GIQ.api.post('/v1/lidarr-backfill/requests/' + id + '/retry', {});
            else if (action === 'skip') p = GIQ.api.post('/v1/lidarr-backfill/requests/' + id + '/skip', {});
            else if (action === 'forget') {
                if (!window.confirm('Forget this row? It will be re-picked from Lidarr on the next tick.')) return;
                p = GIQ.api.del('/v1/lidarr-backfill/requests/' + id);
            } else return;
            p.then(() => { GIQ.toast('Row ' + action + ' applied.', 'success'); reloadQueue(); reloadStats(); })
             .catch(e => GIQ.toast('Action failed: ' + e.message, 'error'));
        }

        function bulkReset(scope) {
            const human = scope.replace('_', ' ');
            if (!window.confirm('Bulk-delete all rows with status="' + human + '"?')) return;
            GIQ.api.post('/v1/lidarr-backfill/requests/reset', { scope })
                .then(res => { GIQ.toast('Deleted ' + (res.deleted || 0) + ' row(s).', 'success'); reloadQueue(); reloadStats(); })
                .catch(e => GIQ.toast('Bulk reset failed: ' + e.message, 'error'));
        }

        function toggleBackfill() {
            // Pause/resume mutates the active config. We confirm to avoid surprises.
            if (backfillEnabled === null) { GIQ.toast('Backfill state still loading.', 'info'); return; }
            const next = !backfillEnabled;
            const verb = next ? 'enable' : 'pause';
            if (!window.confirm('This will ' + verb + ' the Lidarr backfill engine via a new config version. Continue?')) return;
            GIQ.api.get('/v1/lidarr-backfill/config').then(cur => {
                const newCfg = JSON.parse(JSON.stringify(cur.config || {}));
                newCfg.enabled = next;
                return GIQ.api.put('/v1/lidarr-backfill/config', { name: cur.name || null, config: newCfg });
            }).then(() => {
                GIQ.toast('Backfill ' + (next ? 'resumed' : 'paused') + '.', 'success');
                reloadStats();
            }).catch(e => GIQ.toast('Toggle failed: ' + e.message, 'error'));
        }

        renderChips();
        if (GIQ.state.apiKey) {
            reloadStats().then(reloadQueue);
        } else {
            renderError(tableHost, 'Connect API key to load the queue.');
        }
    };

    /* ===================================================================
     * Page E — Charts
     * =================================================================== */

    GIQ.pages.actions['charts'] = function (root) {
        const { wrap, body } = makePage({
            title: 'Charts',
            subline: 'Rebuild Last.fm chart snapshots and (optionally) auto-add missing artists / tracks.',
        });
        root.appendChild(wrap);

        const build = GIQ.components.actionCard({
            name: 'Build Charts',
            description: 'Refreshes every configured chart (CHARTS_TAGS / CHARTS_COUNTRIES). If CHARTS_LIDARR_AUTO_ADD or CHARTS_SPOTIZERR_AUTO_ADD is on, auto-queues missing entries.',
            monitorPath: '#/monitor/charts',
            monitorLabel: 'Charts stats →',
            onRun: () => GIQ.api.post('/v1/charts/build', {}),
        });
        body.appendChild(build.el);

        if (GIQ.state.apiKey) {
            GIQ.api.get('/v1/charts/stats').then(stats => {
                if (!stats) return;
                const last = stats.last_built_at;
                const total = stats.total_entries;
                const matchRate = stats.match_rate;
                const parts = [];
                if (last) parts.push('last build · ' + timeAgo(last));
                if (typeof total === 'number') parts.push(total.toLocaleString() + ' entries');
                if (typeof matchRate === 'number') parts.push(Math.round(matchRate * 100) + '% match');
                build.refresh({ lastRun: parts.length ? parts.join(' · ') : 'no builds yet' });
            }).catch(() => { /* leave blank */ });
        }
    };

    /* ===================================================================
     * Page F — Downloads (multi-agent search + recent)
     * =================================================================== */

    GIQ.pages.actions['downloads'] = function (root) {
        const { wrap, body } = makePage({
            title: 'Downloads',
            subline: 'Operator tool: query every configured backend in parallel and download a specific result.',
        });
        root.appendChild(wrap);

        // ---- search panel ----
        const panel = document.createElement('section');
        panel.className = 'actions-search-panel';
        body.appendChild(panel);

        const form = document.createElement('form');
        form.className = 'actions-search-form';

        const queryInput = document.createElement('input');
        queryInput.type = 'text';
        queryInput.className = 'actions-search-input';
        queryInput.placeholder = 'e.g. Radiohead Creep';
        form.appendChild(queryInput);

        const limitInput = document.createElement('input');
        limitInput.type = 'number';
        limitInput.className = 'actions-search-num';
        limitInput.value = '25';
        limitInput.min = '1';
        limitInput.max = '100';
        limitInput.title = 'Result limit per backend';
        form.appendChild(limitInput);

        const timeoutInput = document.createElement('input');
        timeoutInput.type = 'number';
        timeoutInput.className = 'actions-search-num';
        timeoutInput.value = '5000';
        timeoutInput.min = '500';
        timeoutInput.max = '30000';
        timeoutInput.step = '500';
        timeoutInput.title = 'Per-backend timeout (ms)';
        form.appendChild(timeoutInput);

        const submitBtn = document.createElement('button');
        submitBtn.type = 'submit';
        submitBtn.className = 'vc-btn vc-btn-primary';
        submitBtn.textContent = 'Search';
        form.appendChild(submitBtn);

        panel.appendChild(form);

        // Backend checkboxes (loaded from /v1/downloads/routing parallel_search_backends)
        const backendsRow = document.createElement('div');
        backendsRow.className = 'actions-search-backends';
        backendsRow.innerHTML = '<span class="actions-search-label">backends:</span>';
        panel.appendChild(backendsRow);

        const knownBackends = ['spotdl', 'streamrip', 'spotizerr', 'slskd'];
        const enabled = new Set(); // populated after we read routing

        function renderBackends() {
            // Wipe everything except the label
            backendsRow.querySelectorAll('label').forEach(n => n.remove());
            knownBackends.forEach(b => {
                const lbl = document.createElement('label');
                lbl.className = 'actions-backend-cb';
                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.checked = enabled.has(b);
                cb.addEventListener('change', () => { if (cb.checked) enabled.add(b); else enabled.delete(b); });
                const dot = document.createElement('span');
                dot.className = 'dl-backend-dot';
                dot.style.background = ({
                    spotdl: '#4ade80', streamrip: '#fbbf24', spotizerr: '#60a5fa', slskd: '#c084fc',
                })[b] || 'var(--ink-3)';
                const txt = document.createElement('span');
                txt.textContent = b;
                lbl.appendChild(cb);
                lbl.appendChild(dot);
                lbl.appendChild(txt);
                backendsRow.appendChild(lbl);
            });
        }
        renderBackends();

        // Results host
        const results = document.createElement('section');
        results.className = 'actions-search-results';
        body.appendChild(results);

        // Recent ad-hoc downloads (bottom panel)
        const recentSection = document.createElement('section');
        recentSection.className = 'actions-recent-downloads';
        body.appendChild(recentSection);

        function renderRecent(items) {
            recentSection.innerHTML = '';
            const title = document.createElement('div');
            title.className = 'panel-title';
            title.textContent = 'Recent ad-hoc downloads';
            recentSection.appendChild(title);

            if (!items || !items.length) {
                const empty = document.createElement('div');
                empty.className = 'actions-empty';
                empty.textContent = 'No recent downloads. Search and pick a result above.';
                recentSection.appendChild(empty);
                return;
            }
            const tbl = document.createElement('table');
            tbl.className = 'actions-recent-table';
            tbl.innerHTML = '<thead><tr><th>When</th><th>Backend</th><th>Track</th><th>Status</th></tr></thead>';
            const tb = document.createElement('tbody');
            items.slice(0, 10).forEach(r => {
                const tr = document.createElement('tr');
                tr.innerHTML =
                      '<td class="mono">' + esc(timeAgo(r.created_at || r.requested_at || r.queued_at)) + '</td>'
                    + '<td class="mono">' + esc(r.backend || r.client || '—') + '</td>'
                    + '<td>' + esc((r.artist_name || r.artist || '') + (r.track_title ? ' — ' + r.track_title : '')) + '</td>'
                    + '<td class="mono">' + esc(r.status || '—') + '</td>';
                tb.appendChild(tr);
            });
            tbl.appendChild(tb);
            recentSection.appendChild(tbl);
        }

        function reloadRecent() {
            if (!GIQ.state.apiKey) { renderRecent([]); return; }
            GIQ.api.get('/v1/downloads?limit=10').then(data => {
                renderRecent((data && (data.items || data.recent || [])) || []);
            }).catch(() => renderRecent([]));
        }

        function runSearch() {
            const q = queryInput.value.trim();
            if (!q) return;
            const lim = Math.min(100, Math.max(1, parseInt(limitInput.value, 10) || 25));
            const tmo = Math.min(30000, Math.max(500, parseInt(timeoutInput.value, 10) || 5000));
            const backendsList = Array.from(enabled);

            renderLoading(results, 'Querying configured backends in parallel…');

            let url = '/v1/downloads/search/multi?q=' + encodeURIComponent(q) + '&limit=' + lim + '&timeout_ms=' + tmo;
            if (backendsList.length) url += '&backends=' + encodeURIComponent(backendsList.join(','));

            GIQ.api.get(url).then(res => {
                renderResults(res);
            }).catch(e => renderError(results, 'Search failed: ' + e.message));
        }

        function renderResults(res) {
            results.innerHTML = '';
            const groups = (res && res.groups) || [];
            if (!groups.length) {
                results.innerHTML = '<div class="actions-empty">No backends responded.</div>';
                return;
            }
            groups.forEach(grp => {
                const groupEl = document.createElement('div');
                groupEl.className = 'actions-search-group';

                const head = document.createElement('div');
                head.className = 'actions-search-group-head';
                const dot = document.createElement('span');
                dot.className = 'dl-backend-dot';
                dot.style.background = ({
                    spotdl: '#4ade80', streamrip: '#fbbf24', spotizerr: '#60a5fa', slskd: '#c084fc',
                })[grp.backend] || 'var(--ink-3)';
                head.appendChild(dot);
                const lbl = document.createElement('b');
                lbl.textContent = grp.backend;
                head.appendChild(lbl);
                const sub = document.createElement('span');
                sub.className = 'actions-search-group-sub mono';
                if (grp.ok) {
                    sub.textContent = '(' + ((grp.results || []).length) + ' results)';
                } else {
                    sub.textContent = grp.error || 'failed';
                    sub.classList.add('actions-search-group-error');
                }
                head.appendChild(sub);
                groupEl.appendChild(head);

                const list = document.createElement('div');
                list.className = 'actions-search-list';
                if (grp.ok && grp.results && grp.results.length) {
                    grp.results.forEach(item => list.appendChild(_renderResultRow(grp.backend, item, reloadRecent)));
                } else if (grp.ok) {
                    const empty = document.createElement('div');
                    empty.className = 'actions-empty';
                    empty.textContent = 'No matches.';
                    list.appendChild(empty);
                }
                groupEl.appendChild(list);
                results.appendChild(groupEl);
            });
        }

        form.addEventListener('submit', (ev) => { ev.preventDefault(); runSearch(); });

        // Hydrate parallel-search backend defaults from routing config
        if (GIQ.state.apiKey) {
            GIQ.api.get('/v1/downloads/routing').then(routing => {
                const cfg = routing && routing.config;
                const psbs = (cfg && cfg.parallel_search_backends) || ['spotdl', 'streamrip', 'spotizerr'];
                const tmo = (cfg && cfg.parallel_search_timeout_ms) || 5000;
                enabled.clear();
                psbs.forEach(b => enabled.add(b));
                timeoutInput.value = tmo;
                renderBackends();
            }).catch(() => {
                ['spotdl', 'streamrip', 'spotizerr'].forEach(b => enabled.add(b));
                renderBackends();
            });
        } else {
            ['spotdl', 'streamrip', 'spotizerr'].forEach(b => enabled.add(b));
            renderBackends();
        }
        reloadRecent();
    };

    /* ── tiny helpers ────────────────────────────────────────────────── */

    function _numField(label, defaultVal, min, max) {
        const field = document.createElement('label');
        field.className = 'actions-num-field';
        const lbl = document.createElement('span');
        lbl.className = 'actions-num-label';
        lbl.textContent = label;
        const input = document.createElement('input');
        input.type = 'number';
        input.value = String(defaultVal);
        input.min = String(min);
        input.max = String(max);
        field.appendChild(lbl);
        field.appendChild(input);
        return { field, input };
    }

    function _iconBtn(label, onClick) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'vc-btn vc-btn-ghost-sm';
        b.textContent = label;
        b.addEventListener('click', onClick);
        return b;
    }

    function _renderResultRow(backend, item, onAfterDownload) {
        const row = document.createElement('div');
        row.className = 'actions-search-row';
        const inLib = !!item.in_library;
        if (inLib) row.classList.add('actions-search-row-inlib');

        const main = document.createElement('div');
        main.className = 'actions-search-row-main';
        const titleLine = document.createElement('div');
        titleLine.className = 'actions-search-row-title';
        titleLine.textContent = item.title || '(no title)';
        if (inLib) {
            const lib = document.createElement('span');
            lib.className = 'actions-search-row-lib mono';
            lib.textContent = 'in library' + (item.library_format ? ' · ' + item.library_format : '');
            titleLine.appendChild(lib);
        }
        const meta = document.createElement('div');
        meta.className = 'actions-search-row-meta';
        meta.textContent = (item.artist || '') + (item.album ? ' — ' + item.album : '');
        const sub = document.createElement('div');
        sub.className = 'actions-search-row-sub mono';
        const subParts = [];
        if (item.quality) subParts.push(item.quality);
        if (item.bitrate_kbps) subParts.push(item.bitrate_kbps + ' kbps');
        if (item.duration_ms) subParts.push(Math.round(item.duration_ms / 1000) + 's');
        if (item.album_id) subParts.push('album_id ' + String(item.album_id).slice(0, 12) + '…');
        sub.textContent = subParts.join(' · ');
        main.appendChild(titleLine);
        main.appendChild(meta);
        if (subParts.length) main.appendChild(sub);

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'vc-btn vc-btn-primary actions-search-row-btn';
        btn.textContent = (inLib ? 'Re-download via ' : 'Download via ') + backend;
        if (inLib) btn.title = 'You already have this — clicking will queue another copy.';
        btn.addEventListener('click', () => {
            btn.disabled = true;
            btn.textContent = 'Queueing…';
            const body = {
                handle: item.download_handle || {},
                track_title: item.title || null,
                artist_name: item.artist || null,
                album_name: item.album || null,
            };
            GIQ.api.post('/v1/downloads/from-handle', body).then(rec => {
                const status = rec && rec.status;
                if (status === 'duplicate') {
                    GIQ.toast('Already downloaded via ' + backend + ' — no new file written.', 'info');
                } else {
                    GIQ.components._actionToastWithJump(
                        'Queued via ' + backend + '.',
                        '#/monitor/downloads',
                        'View in Monitor →'
                    );
                    setTimeout(() => {
                        if (window.location.hash !== '#/monitor/downloads') {
                            window.location.hash = '#/monitor/downloads';
                        }
                    }, 800);
                }
                if (typeof onAfterDownload === 'function') onAfterDownload();
            }).catch(e => {
                GIQ.toast('Download via ' + backend + ' failed: ' + e.message, 'error');
                btn.disabled = false;
                btn.textContent = (inLib ? 'Re-download via ' : 'Download via ') + backend;
            });
        });

        row.appendChild(main);
        row.appendChild(btn);
        return row;
    }
})();
