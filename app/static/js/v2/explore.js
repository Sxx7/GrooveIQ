/* explore.js — Explore bucket pages.
 *
 * Session 09 lands:
 *   - GIQ.pages.explore.recommendations
 *   - GIQ.pages.explore.tracks
 * Session 10 lands:
 *   - GIQ.pages.explore.playlists       (list + #/explore/playlists/{id} detail)
 *   - GIQ.pages.explore['text-search']
 *   - GIQ.pages.explore['music-map']
 * Session 11 lands:
 *   - GIQ.pages.explore.radio
 *   - GIQ.pages.explore.charts
 *   - GIQ.pages.explore.artists
 *   - GIQ.pages.explore.news
 */

(function () {
    GIQ.pages.explore = GIQ.pages.explore || {};

    /* ── Persistent state across navigations ───────────────────────── */

    GIQ.state.recoState = GIQ.state.recoState || {
        userId: null,
        seedTrackId: '',
        limit: 25,
        result: null,        /* last response */
        contextOpen: false,
        ctx: {},             /* device_type / output_type / etc. */
    };

    GIQ.state.trackList = GIQ.state.trackList || {
        offset: 0,
        limit: 50,
        sortBy: 'bpm',
        sortDir: 'asc',
        search: '',
        total: 0,
        tracks: [],
        loading: false,
    };

    /* ── Recommendations ───────────────────────────────────────────── */

    GIQ.pages.explore.recommendations = function renderRecommendations(root, params) {
        const state = GIQ.state.recoState;
        if (params && params.user) state.userId = decodeURIComponent(params.user);

        const headerRight = document.createElement('div');
        headerRight.className = 'reco-header-controls';

        const userSel = document.createElement('select');
        userSel.className = 'reco-select';
        userSel.innerHTML = '<option value="">Select user…</option>';

        const seedInput = document.createElement('input');
        seedInput.type = 'text';
        seedInput.className = 'reco-input';
        seedInput.placeholder = 'Seed track id (optional)';
        seedInput.value = state.seedTrackId || '';
        seedInput.style.width = '180px';

        const limitInput = document.createElement('input');
        limitInput.type = 'number';
        limitInput.min = '1';
        limitInput.max = '100';
        limitInput.className = 'reco-input';
        limitInput.value = String(state.limit || 25);
        limitInput.style.width = '60px';

        const ctxBtn = document.createElement('button');
        ctxBtn.type = 'button';
        ctxBtn.className = 'vc-btn vc-btn-sm';
        ctxBtn.textContent = state.contextOpen ? 'Hide context' : 'Add context';

        const goBtn = document.createElement('button');
        goBtn.type = 'button';
        goBtn.className = 'vc-btn vc-btn-primary vc-btn-sm';
        goBtn.textContent = 'Get Recs';

        headerRight.appendChild(userSel);
        headerRight.appendChild(seedInput);
        headerRight.appendChild(limitInput);
        headerRight.appendChild(ctxBtn);
        headerRight.appendChild(goBtn);

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'EXPLORE',
            title: 'Recommendations',
            right: headerRight,
        }));

        const body = document.createElement('div');
        body.className = 'reco-body';
        root.appendChild(body);

        const ctxRow = document.createElement('div');
        ctxRow.className = 'reco-context-row';
        ctxRow.style.display = state.contextOpen ? '' : 'none';
        body.appendChild(ctxRow);

        const banner = document.createElement('div');
        banner.className = 'reco-banner';
        banner.style.display = 'none';
        body.appendChild(banner);

        const results = document.createElement('div');
        results.className = 'reco-results';
        body.appendChild(results);

        function buildContextRow() {
            ctxRow.innerHTML = '';
            const fields = [
                ['device_type', 'Device', ['mobile', 'desktop', 'speaker', 'car', 'web']],
                ['output_type', 'Output', ['headphones', 'speaker', 'bluetooth_speaker', 'car_audio', 'built_in', 'airplay']],
                ['context_type', 'Context', ['playlist', 'album', 'radio', 'search', 'home_shelf']],
                ['location_label', 'Location', ['home', 'work', 'gym', 'commute']],
                ['hour_of_day', 'Hour', _hours()],
                ['day_of_week', 'Day', [['1', 'Mon'], ['2', 'Tue'], ['3', 'Wed'], ['4', 'Thu'], ['5', 'Fri'], ['6', 'Sat'], ['7', 'Sun']]],
            ];
            fields.forEach(([key, label, opts]) => {
                const fld = document.createElement('div');
                fld.className = 'reco-ctx-field';
                fld.innerHTML = '<div class="eyebrow">' + GIQ.fmt.esc(label) + '</div>';
                const sel = document.createElement('select');
                sel.className = 'reco-select';
                sel.innerHTML = '<option value="">—</option>';
                opts.forEach(opt => {
                    const o = document.createElement('option');
                    if (Array.isArray(opt)) { o.value = opt[0]; o.textContent = opt[1]; }
                    else { o.value = opt; o.textContent = opt; }
                    if (state.ctx[key] === o.value) o.selected = true;
                    sel.appendChild(o);
                });
                sel.addEventListener('change', () => { state.ctx[key] = sel.value || undefined; });
                fld.appendChild(sel);
                ctxRow.appendChild(fld);
            });
        }
        buildContextRow();

        ctxBtn.addEventListener('click', () => {
            state.contextOpen = !state.contextOpen;
            ctxRow.style.display = state.contextOpen ? '' : 'none';
            ctxBtn.textContent = state.contextOpen ? 'Hide context' : 'Add context';
        });

        /* Load users for the dropdown — admin-only endpoint, may 401 */
        const usersPromise = (window.cachedUsers && Array.isArray(window.cachedUsers) && window.cachedUsers.length)
            ? Promise.resolve(window.cachedUsers)
            : GIQ.api.get('/v1/users').then(r => {
                const list = Array.isArray(r) ? r : (r?.users || []);
                window.cachedUsers = list;
                return list;
            }).catch(() => []);

        usersPromise.then(users => {
            users.forEach(u => {
                const o = document.createElement('option');
                o.value = u.user_id;
                o.textContent = u.user_id + (u.display_name ? ' (' + u.display_name + ')' : '');
                if (u.user_id === state.userId) o.selected = true;
                userSel.appendChild(o);
            });
            if (!users.length && state.userId) {
                /* deep-linked but couldn't load users — still allow a fetch */
                const o = document.createElement('option');
                o.value = state.userId; o.textContent = state.userId; o.selected = true;
                userSel.appendChild(o);
            }
            /* If we loaded the page with cached results for this user, re-render. */
            if (state.result && state.result.user_id === state.userId) renderResults();
        });

        userSel.addEventListener('change', () => {
            state.userId = userSel.value || null;
            /* Reflect in URL so reloads land in the same place. */
            if (state.userId) {
                const next = '#/explore/recommendations?user=' + encodeURIComponent(state.userId);
                if (window.location.hash !== next) {
                    history.replaceState(null, '', next);
                }
            }
        });
        seedInput.addEventListener('input', () => { state.seedTrackId = seedInput.value.trim(); });
        limitInput.addEventListener('input', () => {
            const v = parseInt(limitInput.value, 10);
            state.limit = (isNaN(v) || v < 1) ? 25 : Math.min(100, v);
        });

        goBtn.addEventListener('click', fetchRecs);
        seedInput.addEventListener('keydown', e => { if (e.key === 'Enter') fetchRecs(); });
        limitInput.addEventListener('keydown', e => { if (e.key === 'Enter') fetchRecs(); });

        async function fetchRecs() {
            if (!state.userId) {
                GIQ.toast('Pick a user first.', 'warning');
                return;
            }
            goBtn.disabled = true;
            const oldLabel = goBtn.textContent;
            goBtn.textContent = 'Loading…';
            results.innerHTML = '<div class="vc-loading">Loading recommendations…</div>';
            banner.style.display = 'none';

            const qs = ['limit=' + state.limit];
            if (state.seedTrackId) qs.push('seed_track_id=' + encodeURIComponent(state.seedTrackId));
            Object.keys(state.ctx).forEach(k => {
                const v = state.ctx[k];
                if (v != null && v !== '') qs.push(k + '=' + encodeURIComponent(v));
            });
            const url = '/v1/recommend/' + encodeURIComponent(state.userId) + '?' + qs.join('&');

            try {
                const data = await GIQ.api.get(url);
                state.result = data;
                renderResults();
            } catch (e) {
                results.innerHTML = '';
                const err = document.createElement('div');
                err.className = 'reco-error';
                err.textContent = 'Error: ' + e.message;
                results.appendChild(err);
            } finally {
                goBtn.disabled = false;
                goBtn.textContent = oldLabel;
            }
        }

        function renderResults() {
            const data = state.result;
            results.innerHTML = '';
            if (!data) return;

            /* Cross-link banner */
            banner.innerHTML = '';
            banner.style.display = '';
            const lbl = document.createElement('span');
            lbl.className = 'eyebrow';
            lbl.textContent = 'request_id';
            const id = document.createElement('span');
            id.className = 'mono';
            id.textContent = (data.request_id || '').slice(0, 18) + '…';
            id.title = data.request_id || '';
            const grow = document.createElement('span');
            grow.style.flexGrow = '1';
            const jump = GIQ.components.jumpLink({
                label: 'Debug this request',
                href: '#/monitor/recs-debug?debug=' + encodeURIComponent(data.request_id || ''),
            });
            banner.appendChild(lbl);
            banner.appendChild(id);
            banner.appendChild(grow);
            banner.appendChild(jump);

            const tracks = data.tracks || [];
            const meta = document.createElement('div');
            meta.className = 'reco-meta';
            const modelChip = '<span class="rd-source-chip">model · ' + GIQ.fmt.esc(data.model_version || '?') + '</span>';
            meta.innerHTML = '<span class="mono muted">' + tracks.length + ' tracks · '
                + GIQ.fmt.esc(data.user_id || '') + '</span>'
                + ' <span style="margin-left:10px">' + modelChip + '</span>';
            results.appendChild(meta);

            const tableHost = document.createElement('section');
            tableHost.className = 'panel reco-table-panel';
            const head = document.createElement('div');
            head.className = 'panel-head';
            head.innerHTML = '<div class="panel-head-left"><div class="panel-title-row">'
                + '<div class="panel-title">Results</div></div>'
                + '<div class="panel-sub">' + tracks.length + ' candidates · click '
                + '<span class="mono" style="color:var(--accent)">debug→</span> on any row to inspect</div></div>';
            tableHost.appendChild(head);

            const body2 = document.createElement('div');
            body2.className = 'panel-body';
            const debugRid = data.request_id || '';
            const tableEl = GIQ.components.trackTable({
                columns: ['rank', 'title', 'artist', 'source', 'score', 'bpm', 'key', 'energy', 'mood', 'duration'],
                rows: tracks,
                empty: 'No recommendations available. Try a different user or remove filters.',
                rowAction: (row) => {
                    const a = document.createElement('a');
                    a.className = 'reco-debug-link';
                    a.textContent = 'debug→';
                    const tid = row.track_id || '';
                    a.href = '#/monitor/recs-debug?debug=' + encodeURIComponent(debugRid)
                        + (tid ? '&track=' + encodeURIComponent(tid) : '');
                    return a;
                },
            });
            body2.appendChild(tableEl);
            tableHost.appendChild(body2);
            results.appendChild(tableHost);
        }

        /* If we already had results cached and the user matches, re-render
         * after the user list has been wired in (above). */
        if (state.result && state.userId === (state.result.user_id || '')) {
            renderResults();
        } else {
            results.innerHTML = '<div class="reco-empty">'
                + 'Pick a user, optionally a seed track, then click <strong>Get Recs</strong>.</div>';
        }

        return function cleanup() { /* nothing to tear down */ };
    };

    function _hours() {
        const out = [];
        for (let h = 0; h < 24; h++) {
            out.push([String(h), (h < 10 ? '0' + h : h) + ':00']);
        }
        return out;
    }

    /* ── Tracks ─────────────────────────────────────────────────────── */

    GIQ.pages.explore.tracks = function renderTracks(root) {
        const s = GIQ.state.trackList;

        const right = document.createElement('div');
        right.className = 'tracks-header-controls';

        const searchWrap = document.createElement('div');
        searchWrap.className = 'tracks-search-wrap';
        const search = document.createElement('input');
        search.type = 'text';
        search.className = 'reco-input tracks-search';
        search.placeholder = 'Search title, artist, ID…';
        search.value = s.search || '';
        searchWrap.appendChild(search);

        const clearX = document.createElement('button');
        clearX.type = 'button';
        clearX.className = 'tracks-search-clear';
        clearX.textContent = '×';
        clearX.title = 'Clear search';
        clearX.style.display = s.search ? '' : 'none';
        searchWrap.appendChild(clearX);

        const searchBtn = document.createElement('button');
        searchBtn.type = 'button';
        searchBtn.className = 'vc-btn vc-btn-sm';
        searchBtn.textContent = 'Search';

        const genBtn = document.createElement('button');
        genBtn.type = 'button';
        genBtn.className = 'vc-btn vc-btn-primary vc-btn-sm';
        genBtn.textContent = 'Generate Playlist';

        right.appendChild(searchWrap);
        right.appendChild(searchBtn);
        right.appendChild(genBtn);

        const header = GIQ.components.pageHeader({
            eyebrow: 'EXPLORE',
            title: 'Tracks (' + (s.total ? s.total.toLocaleString() : '…') + ')',
            right: right,
        });
        root.appendChild(header);

        const body = document.createElement('div');
        body.className = 'tracks-body';
        root.appendChild(body);

        const tableHost = document.createElement('section');
        tableHost.className = 'panel tracks-panel';
        body.appendChild(tableHost);

        function setHeaderTitle() {
            const t = header.querySelector('.page-title');
            if (t) t.textContent = 'Tracks (' + (s.total ? s.total.toLocaleString() : '0') + ')';
        }

        function renderTable() {
            tableHost.innerHTML = '';
            const head = document.createElement('div');
            head.className = 'panel-head';
            head.innerHTML = '<div class="panel-head-left"><div class="panel-title-row">'
                + '<div class="panel-title">Library</div></div>'
                + '<div class="panel-sub">'
                + (s.search ? 'matching “' + GIQ.fmt.esc(s.search) + '” · ' : '')
                + s.total.toLocaleString() + ' total · sorted by '
                + GIQ.fmt.esc(s.sortBy) + ' ' + (s.sortDir === 'asc' ? '▲︎' : '▼︎')
                + '</div></div>';
            tableHost.appendChild(head);

            const panelBody = document.createElement('div');
            panelBody.className = 'panel-body';
            tableHost.appendChild(panelBody);

            if (s.loading) {
                panelBody.innerHTML = '<div class="vc-loading">Loading tracks…</div>';
                return;
            }

            const table = GIQ.components.trackTable({
                columns: ['title', 'artist', 'genre', 'bpm', 'key', 'energy', 'dance', 'valence', 'mood', 'duration', 'version', 'id'],
                rows: s.tracks,
                sort: { field: s.sortBy, dir: s.sortDir },
                sortable: ['bpm', 'energy', 'dance', 'valence', 'duration', 'version'],
                onSort: (field) => {
                    if (s.sortBy === field) s.sortDir = (s.sortDir === 'asc') ? 'desc' : 'asc';
                    else { s.sortBy = field; s.sortDir = 'desc'; }
                    s.offset = 0;
                    load();
                },
                pagination: {
                    offset: s.offset,
                    limit: s.limit,
                    total: s.total,
                    onPage: (delta) => {
                        s.offset = Math.max(0, s.offset + delta * s.limit);
                        load();
                    },
                },
                empty: s.search
                    ? 'No tracks match “' + s.search + '”.'
                    : 'No tracks. Run a Library scan from Actions → Library to populate.',
            });
            panelBody.appendChild(table);
        }

        async function load() {
            s.loading = true;
            renderTable();

            const qs = [
                'limit=' + s.limit,
                'offset=' + s.offset,
                'sort_by=' + encodeURIComponent(s.sortBy),
                'sort_dir=' + encodeURIComponent(s.sortDir),
            ];
            if (s.search) qs.push('search=' + encodeURIComponent(s.search));

            try {
                const data = await GIQ.api.get('/v1/tracks?' + qs.join('&'));
                s.total = data.total || 0;
                s.tracks = data.tracks || [];
                s.loading = false;
                setHeaderTitle();
                renderTable();
            } catch (e) {
                s.loading = false;
                tableHost.innerHTML = '';
                const err = document.createElement('div');
                err.className = 'reco-error';
                err.textContent = 'Failed to load tracks: ' + e.message;
                tableHost.appendChild(err);
            }
        }

        function doSearch() {
            s.search = (search.value || '').trim();
            clearX.style.display = s.search ? '' : 'none';
            s.offset = 0;
            load();
        }

        searchBtn.addEventListener('click', doSearch);
        search.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
        clearX.addEventListener('click', () => {
            search.value = '';
            s.search = '';
            clearX.style.display = 'none';
            s.offset = 0;
            load();
        });
        genBtn.addEventListener('click', () => {
            GIQ.components.generatePlaylistModal({
                prefill: { strategy: 'flow' },
            });
        });

        load();

        return function cleanup() { /* state persists in GIQ.state.trackList */ };
    };

    /* ── Playlists ──────────────────────────────────────────────────── */

    function _strategyChip(strategy) {
        const span = document.createElement('span');
        span.className = 'rd-source-chip pl-strategy';
        span.textContent = String(strategy || 'unknown').replace(/_/g, ' ');
        return span;
    }

    function _fmtPlaylistDuration(secs) {
        if (secs == null) return '—';
        const s = Math.max(0, Math.round(secs));
        const m = Math.floor(s / 60);
        if (m < 60) return m + 'm';
        const h = Math.floor(m / 60);
        const rm = m % 60;
        return h + 'h ' + (rm < 10 ? '0' + rm : rm) + 'm';
    }

    GIQ.pages.explore.playlists = function renderPlaylists(root, params) {
        const tail = (params && params._tail) || [];
        if (tail.length && tail[0]) {
            const id = parseInt(tail[0], 10);
            if (!isNaN(id)) return _renderPlaylistDetail(root, id);
        }
        return _renderPlaylistList(root);
    };

    function _renderPlaylistList(root) {
        const right = document.createElement('div');
        right.className = 'pl-header-controls';
        const genBtn = document.createElement('button');
        genBtn.type = 'button';
        genBtn.className = 'vc-btn vc-btn-primary vc-btn-sm';
        genBtn.textContent = 'Generate Playlist';
        right.appendChild(genBtn);

        const header = GIQ.components.pageHeader({
            eyebrow: 'EXPLORE',
            title: 'Playlists (…)',
            right: right,
        });
        root.appendChild(header);

        const body = document.createElement('div');
        body.className = 'pl-body';
        root.appendChild(body);

        const grid = document.createElement('div');
        grid.className = 'pl-grid';
        body.appendChild(grid);

        function setTitle(n) {
            const t = header.querySelector('.page-title');
            if (t) t.textContent = 'Playlists (' + n + ')';
        }

        async function load() {
            grid.innerHTML = '<div class="vc-loading">Loading playlists…</div>';
            try {
                const data = await GIQ.api.get('/v1/playlists?limit=50');
                const list = Array.isArray(data) ? data : (data && data.playlists) || [];
                setTitle(list.length);
                grid.innerHTML = '';
                if (!list.length) {
                    const empty = document.createElement('div');
                    empty.className = 'reco-empty';
                    empty.innerHTML = 'No playlists yet. Click <strong>Generate Playlist</strong> to create one.';
                    grid.appendChild(empty);
                    return;
                }
                list.forEach(p => grid.appendChild(_renderPlaylistCard(p)));
            } catch (e) {
                grid.innerHTML = '';
                const err = document.createElement('div');
                err.className = 'reco-error';
                err.textContent = 'Failed to load playlists: ' + e.message;
                grid.appendChild(err);
            }
        }

        genBtn.addEventListener('click', () => {
            GIQ.components.generatePlaylistModal({
                onCreated(detail) {
                    if (detail && detail.id != null) {
                        GIQ.router.navigate('explore', 'playlists/' + detail.id);
                    } else {
                        load();
                    }
                },
            });
        });

        load();
        return function cleanup() { /* nothing */ };
    }

    function _renderPlaylistCard(p) {
        const esc = GIQ.fmt.esc;
        const card = document.createElement('a');
        card.className = 'pl-card';
        card.href = '#/explore/playlists/' + encodeURIComponent(p.id);

        const name = document.createElement('div');
        name.className = 'pl-card-name';
        name.textContent = p.name || ('Playlist #' + p.id);
        card.appendChild(name);

        const meta = document.createElement('div');
        meta.className = 'pl-card-meta';
        const chip = _strategyChip(p.strategy);
        meta.appendChild(chip);
        const stats = document.createElement('span');
        stats.className = 'mono muted';
        stats.textContent = (p.track_count || 0) + ' tracks · '
            + _fmtPlaylistDuration(p.total_duration) + ' · '
            + GIQ.fmt.timeAgo(p.created_at);
        meta.appendChild(stats);
        card.appendChild(meta);

        if (p.seed_track_id) {
            const seed = document.createElement('div');
            seed.className = 'pl-card-seed mono muted';
            seed.textContent = 'seed: ' + esc(String(p.seed_track_id).slice(0, 16));
            seed.title = p.seed_track_id;
            card.appendChild(seed);
        }
        return card;
    }

    function _renderPlaylistDetail(root, playlistId) {
        const right = document.createElement('div');
        right.className = 'pl-header-controls';

        const backBtn = document.createElement('a');
        backBtn.className = 'vc-btn vc-btn-sm';
        backBtn.href = '#/explore/playlists';
        backBtn.textContent = '← Back';
        backBtn.style.textDecoration = 'none';

        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.className = 'vc-btn vc-btn-sm pl-delete';
        delBtn.textContent = 'Delete';
        delBtn.disabled = true;

        right.appendChild(backBtn);
        right.appendChild(delBtn);

        const header = GIQ.components.pageHeader({
            eyebrow: 'EXPLORE',
            title: 'Playlist',
            right: right,
        });
        root.appendChild(header);

        /* Subtitle bar (strategy chip · count · duration) sits below the header. */
        const sub = document.createElement('div');
        sub.className = 'pl-subtitle';
        root.appendChild(sub);

        const body = document.createElement('div');
        body.className = 'pl-detail-body';
        root.appendChild(body);

        const tableHost = document.createElement('section');
        tableHost.className = 'panel pl-detail-panel';
        body.appendChild(tableHost);

        let loaded = null;

        async function load() {
            tableHost.innerHTML = '<div class="vc-loading">Loading playlist…</div>';
            try {
                const p = await GIQ.api.get('/v1/playlists/' + encodeURIComponent(playlistId));
                loaded = p;
                const t = header.querySelector('.page-title');
                if (t) t.textContent = p.name || ('Playlist #' + p.id);
                sub.innerHTML = '';
                sub.appendChild(_strategyChip(p.strategy));
                const meta = document.createElement('span');
                meta.className = 'mono muted pl-subtitle-meta';
                meta.textContent = (p.track_count || 0) + ' tracks · '
                    + _fmtPlaylistDuration(p.total_duration) + ' · '
                    + 'created ' + GIQ.fmt.timeAgo(p.created_at);
                sub.appendChild(meta);

                delBtn.disabled = false;

                tableHost.innerHTML = '';
                const head = document.createElement('div');
                head.className = 'panel-head';
                head.innerHTML = '<div class="panel-head-left"><div class="panel-title-row">'
                    + '<div class="panel-title">Tracks</div></div></div>';
                tableHost.appendChild(head);

                const panelBody = document.createElement('div');
                panelBody.className = 'panel-body';
                const table = GIQ.components.trackTable({
                    columns: ['rank', 'title', 'artist', 'bpm', 'key', 'energy', 'mood', 'duration'],
                    rows: p.tracks || [],
                    empty: 'This playlist has no tracks.',
                });
                panelBody.appendChild(table);
                tableHost.appendChild(panelBody);
            } catch (e) {
                tableHost.innerHTML = '';
                const err = document.createElement('div');
                err.className = 'reco-error';
                err.textContent = (e.status === 404)
                    ? 'Playlist not found.'
                    : 'Failed to load playlist: ' + e.message;
                tableHost.appendChild(err);
                delBtn.disabled = true;
            }
        }

        delBtn.addEventListener('click', async () => {
            if (!loaded) return;
            const ok = window.confirm('Delete playlist "' + (loaded.name || ('#' + loaded.id)) + '"? This cannot be undone.');
            if (!ok) return;
            delBtn.disabled = true;
            const oldText = delBtn.textContent;
            delBtn.textContent = 'Deleting…';
            try {
                await GIQ.api.del('/v1/playlists/' + encodeURIComponent(loaded.id));
                GIQ.toast('Playlist deleted', 'success');
                GIQ.router.navigate('explore', 'playlists');
            } catch (e) {
                delBtn.disabled = false;
                delBtn.textContent = oldText;
                GIQ.toast('Failed to delete: ' + e.message, 'error');
            }
        });

        load();
        return function cleanup() { /* nothing */ };
    }

    /* ── Text Search (CLAP) ─────────────────────────────────────────── */

    GIQ.state.textSearch = GIQ.state.textSearch || {
        prompt: '',
        limit: 50,
        result: null,
        clapStats: null,
    };

    const TEXT_SEARCH_EXAMPLES = [
        'upbeat summer night driving',
        'chill lofi study session',
        'aggressive workout metal',
        'rainy coffee shop jazz',
    ];

    GIQ.pages.explore['text-search'] = function renderTextSearch(root) {
        const s = GIQ.state.textSearch;

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'EXPLORE',
            title: 'Text Search',
        }));

        const body = document.createElement('div');
        body.className = 'ts-body';
        root.appendChild(body);

        const gateHost = document.createElement('div');
        body.appendChild(gateHost);

        const panel = document.createElement('section');
        panel.className = 'panel ts-search-panel';
        body.appendChild(panel);

        const panelBody = document.createElement('div');
        panelBody.className = 'panel-body';
        panel.appendChild(panelBody);

        const controls = document.createElement('div');
        controls.className = 'ts-controls';
        panelBody.appendChild(controls);

        const promptInput = document.createElement('input');
        promptInput.type = 'text';
        promptInput.className = 'ts-prompt';
        promptInput.placeholder = 'Describe what you want to hear — e.g. melancholic piano at 2am';
        promptInput.value = s.prompt || '';
        controls.appendChild(promptInput);

        const limitInput = document.createElement('input');
        limitInput.type = 'number';
        limitInput.min = '5';
        limitInput.max = '200';
        limitInput.className = 'reco-input ts-limit';
        limitInput.value = String(s.limit || 50);
        controls.appendChild(limitInput);

        const searchBtn = document.createElement('button');
        searchBtn.type = 'button';
        searchBtn.className = 'vc-btn vc-btn-primary vc-btn-sm';
        searchBtn.textContent = 'Search';
        controls.appendChild(searchBtn);

        const chipsRow = document.createElement('div');
        chipsRow.className = 'ts-examples';
        const chipsLbl = document.createElement('span');
        chipsLbl.className = 'eyebrow';
        chipsLbl.textContent = 'Try';
        chipsRow.appendChild(chipsLbl);
        TEXT_SEARCH_EXAMPLES.forEach(ex => {
            const c = document.createElement('button');
            c.type = 'button';
            c.className = 'ts-example-chip';
            c.textContent = ex;
            c.addEventListener('click', () => {
                promptInput.value = ex;
                s.prompt = ex;
                runSearch();
            });
            chipsRow.appendChild(c);
        });
        panelBody.appendChild(chipsRow);

        const results = document.createElement('div');
        results.className = 'ts-results';
        body.appendChild(results);

        function disableControls(hide) {
            panel.style.display = hide ? 'none' : '';
            results.style.display = hide ? 'none' : '';
        }

        function gateMessage(text) {
            gateHost.innerHTML = '';
            const g = document.createElement('div');
            g.className = 'ts-gate';
            g.innerHTML = text;
            gateHost.appendChild(g);
        }

        async function checkClap() {
            try {
                const stats = s.clapStats || await GIQ.api.get('/v1/tracks/clap/stats');
                s.clapStats = stats;
                if (!stats || stats.enabled === false) {
                    gateMessage('CLAP is disabled. Enable <code>CLAP_ENABLED=true</code> in <code>.env</code> '
                        + 'and run a CLAP backfill from <a href="#/actions/pipeline-ml">Actions → Pipeline &amp; ML</a>.');
                    disableControls(true);
                    return false;
                }
                if (!stats.with_clap_embedding) {
                    gateMessage('No tracks have CLAP embeddings yet. Run a CLAP backfill from '
                        + '<a href="#/actions/pipeline-ml">Actions → Pipeline &amp; ML</a>.');
                    disableControls(true);
                    return false;
                }
                gateHost.innerHTML = '';
                const cov = document.createElement('div');
                cov.className = 'ts-coverage mono muted';
                cov.textContent = 'CLAP index: ' + stats.with_clap_embedding + ' / '
                    + stats.total_tracks + ' tracks ('
                    + Math.round((stats.coverage || 0) * 100) + '%)';
                gateHost.appendChild(cov);
                disableControls(false);
                return true;
            } catch (e) {
                gateMessage('Failed to check CLAP availability: ' + GIQ.fmt.esc(e.message));
                disableControls(true);
                return false;
            }
        }

        async function runSearch() {
            const q = (promptInput.value || '').trim();
            if (!q) {
                GIQ.toast('Type a prompt first.', 'warning');
                return;
            }
            s.prompt = q;
            const lim = Math.max(5, Math.min(200, parseInt(limitInput.value, 10) || 50));
            s.limit = lim;
            results.innerHTML = '<div class="vc-loading">Searching…</div>';
            searchBtn.disabled = true;
            try {
                const data = await GIQ.api.get('/v1/tracks/text-search?q='
                    + encodeURIComponent(q) + '&limit=' + lim);
                s.result = data;
                renderResults();
            } catch (e) {
                results.innerHTML = '';
                const err = document.createElement('div');
                err.className = 'reco-error';
                err.textContent = 'Search failed: ' + e.message;
                results.appendChild(err);
            } finally {
                searchBtn.disabled = false;
            }
        }

        function renderResults() {
            const data = s.result;
            results.innerHTML = '';
            if (!data) return;
            const tracks = data.tracks || [];
            tracks.forEach(t => {
                if (typeof t.similarity === 'number' && typeof t.score !== 'number') t.score = t.similarity;
            });

            const tableHost = document.createElement('section');
            tableHost.className = 'panel ts-results-panel';
            const head = document.createElement('div');
            head.className = 'panel-head';
            const subParts = [
                tracks.length + ' results',
                'q="' + GIQ.fmt.esc(data.query || s.prompt) + '"',
            ];
            if (data.model_version) subParts.push('model ' + GIQ.fmt.esc(data.model_version));
            if (data.request_id) subParts.push('req ' + GIQ.fmt.esc(String(data.request_id).slice(0, 12)));
            head.innerHTML = '<div class="panel-head-left"><div class="panel-title-row">'
                + '<div class="panel-title">Results</div></div>'
                + '<div class="panel-sub mono">' + subParts.join(' · ') + '</div></div>';
            const genWrap = document.createElement('div');
            const genBtn = document.createElement('button');
            genBtn.type = 'button';
            genBtn.className = 'vc-btn vc-btn-primary vc-btn-sm';
            genBtn.textContent = 'Generate Playlist';
            genBtn.addEventListener('click', () => {
                GIQ.components.generatePlaylistModal({
                    prefill: {
                        strategy: 'text',
                        prompt: s.prompt,
                        name: 'Prompt: ' + s.prompt.slice(0, 60),
                    },
                });
            });
            genWrap.appendChild(genBtn);
            head.appendChild(genWrap);
            tableHost.appendChild(head);

            const pBody = document.createElement('div');
            pBody.className = 'panel-body';
            const table = GIQ.components.trackTable({
                columns: ['rank', 'title', 'artist', 'score', 'bpm', 'key', 'energy', 'mood', 'duration'],
                rows: tracks,
                empty: 'No matches for that prompt. Try different words.',
            });
            pBody.appendChild(table);
            tableHost.appendChild(pBody);
            results.appendChild(tableHost);
        }

        promptInput.addEventListener('input', () => { s.prompt = promptInput.value; });
        promptInput.addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });
        limitInput.addEventListener('input', () => {
            const v = parseInt(limitInput.value, 10);
            s.limit = (isNaN(v) || v < 5) ? 5 : Math.min(200, v);
        });
        searchBtn.addEventListener('click', runSearch);

        checkClap().then(ok => { if (ok && s.result) renderResults(); });

        return function cleanup() { /* state persists in GIQ.state.textSearch */ };
    };

    /* ── Music Map ──────────────────────────────────────────────────── */

    GIQ.state.musicMap = GIQ.state.musicMap || {
        tracks: null,
        colorBy: 'by_energy',
        selectedA: null,
        selectedB: null,
        loading: false,
        error: null,
    };

    const MAP_COLOR_OPTIONS = [
        ['by_energy', 'Color: Energy'],
        ['by_danceability', 'Color: Danceability'],
        ['by_valence', 'Color: Valence'],
        ['by_acousticness', 'Color: Acousticness'],
        ['by_key', 'Color: Key'],
        ['by_mood', 'Color: Mood'],
    ];

    /* by_mood / by_key are categorical; everything else is monochrome ramp
     * from --paper-2 to --accent (perceptually-uniform-ish; doesn't claim
     * to be viridis but reads cleanly on the dark background). */
    const MOOD_COLOR = {
        happy: '#e6c77a', sad: '#7fb0d6', aggressive: '#c66f6f', relaxed: '#8fc6a8',
        party: '#d49ec0', acoustic: '#b8a489', electronic: '#7fbac6',
    };
    const KEY_COLOR = {
        'C': '#a887ce', 'C#': '#b48fcf', 'D': '#c098cf', 'D#': '#cba0d0',
        'E': '#d4a8c5', 'F': '#d4a8b0', 'F#': '#d4b09a', 'G': '#cdb98a',
        'G#': '#bcc187', 'A': '#a4c293', 'A#': '#88c1a6', 'B': '#82bdc0',
    };
    /* Lavender ramp endpoints in linear-ish RGB space — tweaked to match the
     * palette's --paper-2 (#4d3e50) and --accent (#a887ce). */
    const RAMP = { lo: [77, 62, 80], hi: [168, 135, 206] };
    function rampColor(t) {
        const c = Math.max(0, Math.min(1, t));
        const r = Math.round(RAMP.lo[0] + (RAMP.hi[0] - RAMP.lo[0]) * c);
        const g = Math.round(RAMP.lo[1] + (RAMP.hi[1] - RAMP.lo[1]) * c);
        const b = Math.round(RAMP.lo[2] + (RAMP.hi[2] - RAMP.lo[2]) * c);
        return 'rgb(' + r + ',' + g + ',' + b + ')';
    }

    GIQ.pages.explore['music-map'] = function renderMusicMap(root) {
        const s = GIQ.state.musicMap;

        const right = document.createElement('div');
        right.className = 'mm-header-controls';
        const colorSel = document.createElement('select');
        colorSel.className = 'reco-select';
        MAP_COLOR_OPTIONS.forEach(([v, lbl]) => {
            const o = document.createElement('option');
            o.value = v; o.textContent = lbl;
            if (v === s.colorBy) o.selected = true;
            colorSel.appendChild(o);
        });
        const reloadBtn = document.createElement('button');
        reloadBtn.type = 'button';
        reloadBtn.className = 'vc-btn vc-btn-sm';
        reloadBtn.textContent = 'Reload';
        right.appendChild(colorSel);
        right.appendChild(reloadBtn);

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'EXPLORE',
            title: 'Music Map',
            right: right,
        }));

        const body = document.createElement('div');
        body.className = 'mm-body';
        root.appendChild(body);

        const explainer = document.createElement('div');
        explainer.className = 'mm-explainer';
        explainer.innerHTML = ''
            + '<strong>What is this?</strong> Each dot is a track in your library. '
            + 'Their position is a UMAP projection of the 64-dim audio embedding GrooveIQ '
            + 'computes during analysis — so dots near each other <em>sound similar</em>, '
            + 'even when their genre tags or BPM differ. '
            + '<strong>Color</strong> overlays one audio dimension on top (Energy, Mood, …); '
            + 'switching it just repaints, the layout itself doesn\'t move. '
            + '<strong>How to use it:</strong> click any track to pin it as <em>A</em>, '
            + 'then click another to pin <em>B</em> and build a Song Path playlist that '
            + 'sonically interpolates from A → B.';
        body.appendChild(explainer);

        // CSS handles the visibility toggle at <700px. Always insert.
        const notice = document.createElement('div');
        notice.className = 'mm-mobile-notice';
        notice.textContent = 'Music Map is best viewed on desktop. '
            + 'Tracks are densely packed; tap two to build a path.';
        body.appendChild(notice);

        const stage = document.createElement('div');
        stage.className = 'mm-stage';
        body.appendChild(stage);

        const canvas = document.createElement('canvas');
        canvas.className = 'mm-canvas';
        canvas.width = 1200;
        canvas.height = 720;
        stage.appendChild(canvas);

        const tooltip = document.createElement('div');
        tooltip.className = 'mm-tooltip';
        tooltip.style.display = 'none';
        stage.appendChild(tooltip);

        const selBar = document.createElement('div');
        selBar.className = 'mm-selection';
        body.appendChild(selBar);

        const status = document.createElement('div');
        status.className = 'mm-status mono muted';
        body.appendChild(status);

        let bounds = null;
        let cleanedUp = false;

        function fmtSel(t) {
            const esc = GIQ.fmt.esc;
            return '<strong>' + esc(t.title || t.track_id || '—') + '</strong>'
                + (t.artist ? ' <span class="muted">' + esc(t.artist) + '</span>' : '');
        }

        function renderSelection() {
            selBar.innerHTML = '';
            if (!s.selectedA && !s.selectedB) return;

            const tagA = document.createElement('span');
            tagA.className = 'mm-pin mm-pin-a';
            tagA.textContent = 'A';
            selBar.appendChild(tagA);
            const a = document.createElement('span');
            a.className = 'mm-sel-name';
            a.innerHTML = fmtSel(s.selectedA);
            selBar.appendChild(a);

            if (s.selectedB) {
                const arrow = document.createElement('span');
                arrow.className = 'muted'; arrow.textContent = ' → ';
                selBar.appendChild(arrow);
                const tagB = document.createElement('span');
                tagB.className = 'mm-pin mm-pin-b';
                tagB.textContent = 'B';
                selBar.appendChild(tagB);
                const b = document.createElement('span');
                b.className = 'mm-sel-name';
                b.innerHTML = fmtSel(s.selectedB);
                selBar.appendChild(b);

                const buildBtn = document.createElement('button');
                buildBtn.type = 'button';
                buildBtn.className = 'vc-btn vc-btn-primary vc-btn-sm mm-build';
                buildBtn.textContent = 'Build Path';
                buildBtn.addEventListener('click', () => {
                    GIQ.components.generatePlaylistModal({
                        prefill: {
                            strategy: 'path',
                            seed_track_id: s.selectedA.track_id,
                            target_track_id: s.selectedB.track_id,
                            name: 'Path: ' + ((s.selectedA.title || 'A') + ' → ' + (s.selectedB.title || 'B')).slice(0, 80),
                        },
                        onCreated(detail) {
                            if (detail && detail.id != null) {
                                GIQ.router.navigate('explore', 'playlists/' + detail.id);
                            }
                        },
                    });
                });
                selBar.appendChild(buildBtn);
            } else {
                const hint = document.createElement('span');
                hint.className = 'muted';
                hint.textContent = ' — click another track for B';
                selBar.appendChild(hint);
            }

            const clearBtn = document.createElement('button');
            clearBtn.type = 'button';
            clearBtn.className = 'vc-btn vc-btn-sm mm-clear';
            clearBtn.textContent = 'Clear';
            clearBtn.addEventListener('click', () => {
                s.selectedA = null; s.selectedB = null;
                renderSelection(); paint();
            });
            selBar.appendChild(clearBtn);
        }

        function computeBounds(tracks) {
            if (!tracks.length) return { xmin: 0, xmax: 1, ymin: 0, ymax: 1 };
            let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
            for (let i = 0; i < tracks.length; i++) {
                const t = tracks[i];
                if (t.x < xmin) xmin = t.x;
                if (t.x > xmax) xmax = t.x;
                if (t.y < ymin) ymin = t.y;
                if (t.y > ymax) ymax = t.y;
            }
            if (xmax === xmin) xmax = xmin + 1;
            if (ymax === ymin) ymax = ymin + 1;
            return { xmin, xmax, ymin, ymax };
        }

        function project(t) {
            const pad = 22;
            const w = canvas.width, h = canvas.height;
            const fx = (t.x - bounds.xmin) / (bounds.xmax - bounds.xmin);
            const fy = (t.y - bounds.ymin) / (bounds.ymax - bounds.ymin);
            return [pad + fx * (w - 2 * pad), h - pad - fy * (h - 2 * pad)];
        }

        function colorFor(t) {
            const mode = s.colorBy;
            if (mode === 'by_mood') return MOOD_COLOR[t.mood] || '#7b6e7f';
            if (mode === 'by_key') return KEY_COLOR[t.key] || '#7b6e7f';
            let v = null;
            if (mode === 'by_energy') v = t.energy;
            else if (mode === 'by_danceability') v = t.danceability;
            else if (mode === 'by_valence') v = t.valence;
            else if (mode === 'by_acousticness') v = t.acousticness;
            if (v == null) return '#4d3e50';
            return rampColor(Math.max(0, Math.min(1, v)));
        }

        function paint() {
            if (cleanedUp) return;
            const ctx = canvas.getContext('2d');
            const w = canvas.width, h = canvas.height;
            ctx.clearRect(0, 0, w, h);
            ctx.fillStyle = 'rgba(15, 14, 22, 0.6)';
            ctx.fillRect(0, 0, w, h);
            if (!s.tracks || !s.tracks.length) return;

            ctx.globalAlpha = 0.78;
            for (let i = 0; i < s.tracks.length; i++) {
                const t = s.tracks[i];
                const xy = project(t);
                ctx.fillStyle = colorFor(t);
                ctx.beginPath();
                ctx.arc(xy[0], xy[1], 2.4, 0, Math.PI * 2);
                ctx.fill();
            }
            ctx.globalAlpha = 1;

            const pins = [['#a887ce', s.selectedA], ['#9c526d', s.selectedB]];
            for (let i = 0; i < pins.length; i++) {
                const [color, p] = pins[i];
                if (!p) continue;
                const xy = project(p);
                ctx.strokeStyle = color;
                ctx.lineWidth = 2.5;
                ctx.beginPath();
                ctx.arc(xy[0], xy[1], 9, 0, Math.PI * 2);
                ctx.stroke();
            }
            if (s.selectedA && s.selectedB) {
                const a = project(s.selectedA), b = project(s.selectedB);
                ctx.strokeStyle = 'rgba(168, 135, 206, 0.65)';
                ctx.setLineDash([5, 4]);
                ctx.lineWidth = 1.6;
                ctx.beginPath();
                ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]);
                ctx.stroke();
                ctx.setLineDash([]);
            }
        }

        function nearest(ev) {
            if (!s.tracks || !s.tracks.length) return null;
            const rect = canvas.getBoundingClientRect();
            const sx = canvas.width / rect.width;
            const sy = canvas.height / rect.height;
            const mx = (ev.clientX - rect.left) * sx;
            const my = (ev.clientY - rect.top) * sy;
            let best = null, bestD = 400;
            for (let i = 0; i < s.tracks.length; i++) {
                const t = s.tracks[i];
                const xy = project(t);
                const dx = xy[0] - mx, dy = xy[1] - my, d = dx * dx + dy * dy;
                if (d < bestD) { bestD = d; best = { track: t, x: xy[0], y: xy[1], rx: rect, sx, sy }; }
            }
            return best;
        }

        function onMove(ev) {
            const hit = nearest(ev);
            if (!hit) { tooltip.style.display = 'none'; return; }
            const t = hit.track;
            tooltip.style.display = 'block';
            tooltip.style.left = ((hit.x / hit.sx) + 12) + 'px';
            tooltip.style.top = ((hit.y / hit.sy) + 12) + 'px';
            const esc = GIQ.fmt.esc;
            const metricVal = (s.colorBy === 'by_mood') ? (t.mood || '—')
                : (s.colorBy === 'by_key') ? (t.key || '—')
                : (s.colorBy === 'by_energy' && t.energy != null) ? ('E ' + t.energy.toFixed(2))
                : (s.colorBy === 'by_danceability' && t.danceability != null) ? ('D ' + t.danceability.toFixed(2))
                : (s.colorBy === 'by_valence' && t.valence != null) ? ('V ' + t.valence.toFixed(2))
                : (s.colorBy === 'by_acousticness' && t.acousticness != null) ? ('A ' + t.acousticness.toFixed(2))
                : '—';
            tooltip.innerHTML = '<div class="mm-tt-title">' + esc(t.title || t.track_id || '—') + '</div>'
                + '<div class="mm-tt-artist muted">' + esc(t.artist || '') + '</div>'
                + '<div class="mm-tt-meta mono muted">' + (t.bpm ? Math.round(t.bpm) + ' BPM · ' : '') + esc(metricVal) + '</div>';
        }

        function onClick(ev) {
            const hit = nearest(ev);
            if (!hit) return;
            const t = hit.track;
            const isShift = !!ev.shiftKey;
            if (!s.selectedA) {
                s.selectedA = t;
            } else if (!s.selectedB && t.track_id !== s.selectedA.track_id && (isShift || true)) {
                /* Either shift+click for explicit B, or any second click — match old dashboard. */
                s.selectedB = t;
            } else {
                s.selectedA = t;
                s.selectedB = null;
            }
            renderSelection();
            paint();
        }

        canvas.addEventListener('mousemove', onMove);
        canvas.addEventListener('mouseleave', () => { tooltip.style.display = 'none'; });
        canvas.addEventListener('click', onClick);
        colorSel.addEventListener('change', () => {
            s.colorBy = colorSel.value;
            paint();
        });
        reloadBtn.addEventListener('click', () => {
            s.tracks = null;
            load();
        });

        async function load() {
            s.loading = true;
            s.error = null;
            status.textContent = 'Loading map…';
            try {
                const data = await GIQ.api.get('/v1/tracks/map?limit=10000');
                const list = (data && data.tracks) || [];
                s.tracks = list;
                if (list.length < 50) {
                    status.innerHTML = '';
                    body.innerHTML = '';
                    if (isMobile) {
                        const notice = document.createElement('div');
                        notice.className = 'mm-mobile-notice';
                        notice.textContent = 'Music Map is best viewed on desktop.';
                        body.appendChild(notice);
                    }
                    const empty = document.createElement('div');
                    empty.className = 'mm-empty';
                    empty.innerHTML = 'Music Map needs at least 50 analysed tracks (got '
                        + list.length + '). '
                        + '<a href="#/actions/library" class="jump-link"><span class="jump-link-label">Run a library scan</span><span class="jump-link-arrow">→</span></a>';
                    body.appendChild(empty);
                    return;
                }
                bounds = computeBounds(list);
                status.textContent = list.length.toLocaleString() + ' tracks plotted · click two for Build Path';
                paint();
            } catch (e) {
                s.error = e.message;
                status.innerHTML = '<span style="color:var(--wine)">Failed to load map: '
                    + GIQ.fmt.esc(e.message) + '</span>';
            } finally {
                s.loading = false;
            }
        }

        if (s.tracks && s.tracks.length >= 50) {
            bounds = computeBounds(s.tracks);
            status.textContent = s.tracks.length.toLocaleString() + ' tracks plotted · click two for Build Path';
            paint();
            renderSelection();
        } else {
            load();
        }

        return function cleanup() {
            cleanedUp = true;
        };
    };

    /* ── Users dropdown helper ─────────────────────────────────────── */

    function _loadUsersList() {
        if (window.cachedUsers && Array.isArray(window.cachedUsers) && window.cachedUsers.length) {
            return Promise.resolve(window.cachedUsers);
        }
        return GIQ.api.get('/v1/users').then(r => {
            const list = Array.isArray(r) ? r : (r?.users || []);
            window.cachedUsers = list;
            return list;
        }).catch(() => []);
    }

    function _userSelect(currentId, onChange, opts) {
        const sel = document.createElement('select');
        sel.className = 'reco-select';
        const placeholder = (opts && opts.placeholder) || 'Select user…';
        sel.innerHTML = '<option value="">' + GIQ.fmt.esc(placeholder) + '</option>';
        _loadUsersList().then(users => {
            users.forEach(u => {
                const o = document.createElement('option');
                o.value = u.user_id;
                o.textContent = u.user_id + (u.display_name ? ' (' + u.display_name + ')' : '');
                if (u.user_id === currentId) o.selected = true;
                sel.appendChild(o);
            });
            if (!users.length && currentId) {
                const o = document.createElement('option');
                o.value = currentId; o.textContent = currentId; o.selected = true;
                sel.appendChild(o);
            }
            if (typeof onChange === 'function') onChange(sel.value, true);
        });
        sel.addEventListener('change', () => {
            if (typeof onChange === 'function') onChange(sel.value, false);
        });
        return sel;
    }

    /* ── Radio ──────────────────────────────────────────────────────── */

    GIQ.state.radio = GIQ.state.radio || {
        userId: null,
        seedType: 'track',
        seedValue: '',
        contextOpen: false,
        ctx: {},
        sessionId: null,
        seedDisplayName: '',
        seedTypeLoaded: '',
        tracks: [],
        totalServed: 0,
        sessions: [],
        feedback: {},   /* trackId -> 'like'|'skip'|'dislike' */
    };

    GIQ.pages.explore.radio = function renderRadio(root, params) {
        if (!GIQ.state.radio) {
            GIQ.state.radio = {
                userId: null, seedType: 'track', seedValue: '', contextOpen: false, ctx: {},
                sessionId: null, seedDisplayName: '', seedTypeLoaded: '',
                tracks: [], totalServed: 0, sessions: [], feedback: {},
            };
        }
        const state = GIQ.state.radio;

        /* Allow deep-link from Artists / others — ?seed_type=artist&seed_value=name. */
        if (params && params.seed_type) state.seedType = params.seed_type;
        if (params && params.seed_value) state.seedValue = params.seed_value;
        if (params && params.user) state.userId = params.user;

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'EXPLORE',
            title: 'Radio',
        }));

        const body = document.createElement('div');
        body.className = 'radio-body';
        root.appendChild(body);

        /* Top row: start panel + active sessions (2-col). */
        const topRow = document.createElement('div');
        topRow.className = 'radio-top-row';
        body.appendChild(topRow);

        /* ── Start panel ─────────────────────────────────────────── */
        const startPanel = document.createElement('section');
        startPanel.className = 'panel radio-start-panel';
        topRow.appendChild(startPanel);

        const startHead = document.createElement('div');
        startHead.className = 'panel-head';
        startHead.innerHTML = '<div class="panel-head-left"><div class="panel-title-row">'
            + '<div class="panel-title">Start Radio</div></div>'
            + '<div class="panel-sub">Seed from a track, artist, or playlist</div></div>';
        startPanel.appendChild(startHead);

        const startBody = document.createElement('div');
        startBody.className = 'panel-body radio-start-body';
        startPanel.appendChild(startBody);

        /* User select */
        const userField = document.createElement('div');
        userField.className = 'radio-field';
        userField.innerHTML = '<div class="eyebrow">User</div>';
        const userSel = _userSelect(state.userId, (val) => {
            state.userId = val || null;
        });
        userField.appendChild(userSel);
        startBody.appendChild(userField);

        /* Seed type radio group */
        const seedTypeField = document.createElement('div');
        seedTypeField.className = 'radio-field';
        seedTypeField.innerHTML = '<div class="eyebrow">Seed Type</div>';
        const seedTypeWrap = document.createElement('div');
        seedTypeWrap.className = 'radio-seed-type-row';
        ['track', 'artist', 'playlist'].forEach(st => {
            const lbl = document.createElement('label');
            lbl.className = 'radio-seed-type-opt';
            const r = document.createElement('input');
            r.type = 'radio';
            r.name = 'radio-seed-type';
            r.value = st;
            if (state.seedType === st) r.checked = true;
            r.addEventListener('change', () => {
                if (r.checked) {
                    state.seedType = st;
                    state.seedValue = '';
                    rebuildSeedInput();
                }
            });
            lbl.appendChild(r);
            const span = document.createElement('span');
            span.textContent = st.charAt(0).toUpperCase() + st.slice(1);
            lbl.appendChild(span);
            seedTypeWrap.appendChild(lbl);
        });
        seedTypeField.appendChild(seedTypeWrap);
        startBody.appendChild(seedTypeField);

        /* Seed value input — text or select depending on seed type */
        const seedValueField = document.createElement('div');
        seedValueField.className = 'radio-field';
        seedValueField.innerHTML = '<div class="eyebrow">Seed Value</div>';
        const seedValueWrap = document.createElement('div');
        seedValueField.appendChild(seedValueWrap);
        startBody.appendChild(seedValueField);

        function rebuildSeedInput() {
            seedValueWrap.innerHTML = '';
            if (state.seedType === 'playlist') {
                const sel = document.createElement('select');
                sel.className = 'reco-select radio-seed-input';
                sel.innerHTML = '<option value="">Loading playlists…</option>';
                seedValueWrap.appendChild(sel);
                GIQ.api.get('/v1/playlists?limit=200').then(data => {
                    const list = Array.isArray(data) ? data : (data && data.playlists) || [];
                    sel.innerHTML = '<option value="">Select playlist…</option>';
                    list.forEach(p => {
                        const o = document.createElement('option');
                        o.value = String(p.id);
                        o.textContent = (p.name || ('Playlist #' + p.id)) + ' (' + (p.track_count || 0) + ' tracks)';
                        if (String(state.seedValue) === String(p.id)) o.selected = true;
                        sel.appendChild(o);
                    });
                }).catch(() => {
                    sel.innerHTML = '<option value="">Failed to load playlists</option>';
                });
                sel.addEventListener('change', () => { state.seedValue = sel.value; });
            } else {
                const inp = document.createElement('input');
                inp.type = 'text';
                inp.className = 'reco-input radio-seed-input';
                inp.placeholder = state.seedType === 'track'
                    ? 'Track ID'
                    : 'Artist name';
                inp.value = state.seedValue || '';
                inp.addEventListener('input', () => { state.seedValue = inp.value.trim(); });
                inp.addEventListener('keydown', e => {
                    if (e.key === 'Enter') startBtn.click();
                });
                seedValueWrap.appendChild(inp);
            }
        }
        rebuildSeedInput();

        /* Advanced context section (collapsible) */
        const ctxToggle = document.createElement('button');
        ctxToggle.type = 'button';
        ctxToggle.className = 'vc-btn vc-btn-sm radio-ctx-toggle';
        ctxToggle.textContent = state.contextOpen ? 'Hide context' : 'Add context';
        startBody.appendChild(ctxToggle);

        const ctxRow = document.createElement('div');
        ctxRow.className = 'reco-context-row radio-ctx-row';
        ctxRow.style.display = state.contextOpen ? '' : 'none';
        startBody.appendChild(ctxRow);

        function buildCtxRow() {
            ctxRow.innerHTML = '';
            const fields = [
                ['device_type', 'Device', ['mobile', 'desktop', 'speaker', 'car', 'web']],
                ['output_type', 'Output', ['headphones', 'speaker', 'bluetooth_speaker', 'car_audio', 'built_in', 'airplay']],
                ['context_type', 'Context', ['playlist', 'album', 'radio', 'search', 'home_shelf']],
                ['location_label', 'Location', ['home', 'work', 'gym', 'commute']],
                ['hour_of_day', 'Hour', _hours()],
                ['day_of_week', 'Day', [['1', 'Mon'], ['2', 'Tue'], ['3', 'Wed'], ['4', 'Thu'], ['5', 'Fri'], ['6', 'Sat'], ['7', 'Sun']]],
            ];
            fields.forEach(([key, label, opts]) => {
                const fld = document.createElement('div');
                fld.className = 'reco-ctx-field';
                fld.innerHTML = '<div class="eyebrow">' + GIQ.fmt.esc(label) + '</div>';
                const sel = document.createElement('select');
                sel.className = 'reco-select';
                sel.innerHTML = '<option value="">—</option>';
                opts.forEach(opt => {
                    const o = document.createElement('option');
                    if (Array.isArray(opt)) { o.value = opt[0]; o.textContent = opt[1]; }
                    else { o.value = opt; o.textContent = opt; }
                    if (state.ctx[key] === o.value) o.selected = true;
                    sel.appendChild(o);
                });
                sel.addEventListener('change', () => { state.ctx[key] = sel.value || undefined; });
                fld.appendChild(sel);
                ctxRow.appendChild(fld);
            });
        }
        buildCtxRow();

        ctxToggle.addEventListener('click', () => {
            state.contextOpen = !state.contextOpen;
            ctxRow.style.display = state.contextOpen ? '' : 'none';
            ctxToggle.textContent = state.contextOpen ? 'Hide context' : 'Add context';
        });

        /* Start button */
        const startBtn = document.createElement('button');
        startBtn.type = 'button';
        startBtn.className = 'vc-btn vc-btn-primary radio-start-btn';
        startBtn.textContent = '▶ Start Radio';
        startBody.appendChild(startBtn);

        /* ── Active sessions panel ──────────────────────────────── */
        const sessPanel = document.createElement('section');
        sessPanel.className = 'panel radio-sess-panel';
        topRow.appendChild(sessPanel);

        const sessHead = document.createElement('div');
        sessHead.className = 'panel-head';
        sessHead.innerHTML = '<div class="panel-head-left"><div class="panel-title-row">'
            + '<div class="panel-title">Active Sessions</div></div>'
            + '<div class="panel-sub">Resume or stop existing radio sessions</div></div>';
        sessPanel.appendChild(sessHead);

        const sessBody = document.createElement('div');
        sessBody.className = 'panel-body radio-sess-body';
        sessBody.innerHTML = '<div class="vc-loading">Loading sessions…</div>';
        sessPanel.appendChild(sessBody);

        /* Now Playing host (below the top row) */
        const nowPlaying = document.createElement('div');
        nowPlaying.className = 'radio-now-playing';
        body.appendChild(nowPlaying);

        let cleanedUp = false;

        async function loadSessions() {
            if (cleanedUp) return;
            try {
                const url = state.userId
                    ? '/v1/radio?user_id=' + encodeURIComponent(state.userId)
                    : '/v1/radio';
                const data = await GIQ.api.get(url);
                state.sessions = (data && data.sessions) || [];
                renderSessions();
            } catch (e) {
                if (cleanedUp) return;
                /* /v1/radio (no user_id) requires admin — surface a friendly message.
                 * Also handle the static-server / unimplemented case (404). */
                if (!state.userId && (e.status === 403 || e.status === 404)) {
                    sessBody.innerHTML = '<div class="reco-empty">Pick a user above to see their active sessions.</div>';
                    return;
                }
                sessBody.innerHTML = '<div class="reco-error">Failed to load sessions: ' + GIQ.fmt.esc(e.message) + '</div>';
            }
        }

        function renderSessions() {
            sessBody.innerHTML = '';
            const list = state.sessions || [];
            if (!list.length) {
                const empty = document.createElement('div');
                empty.className = 'reco-empty';
                empty.textContent = 'No active radio sessions.';
                sessBody.appendChild(empty);
                return;
            }
            list.forEach(s => {
                const card = document.createElement('div');
                card.className = 'radio-sess-card';
                const isActive = state.sessionId && state.sessionId === s.session_id;
                if (isActive) card.classList.add('radio-sess-card-active');

                const left = document.createElement('div');
                left.className = 'radio-sess-card-left';
                const name = document.createElement('div');
                name.className = 'radio-sess-name';
                name.innerHTML = '<strong>' + GIQ.fmt.esc(s.seed_display_name || s.seed_value || '—')
                    + '</strong> <span class="rd-source-chip">' + GIQ.fmt.esc(s.seed_type) + '</span>';
                left.appendChild(name);
                const meta = document.createElement('div');
                meta.className = 'radio-sess-meta mono muted';
                meta.textContent = (s.user_id || '?') + ' · ' + (s.total_served || 0) + ' served · '
                    + (s.tracks_played || 0) + ' played · '
                    + (s.tracks_skipped || 0) + ' skipped · '
                    + (s.tracks_liked || 0) + ' liked · '
                    + GIQ.fmt.timeAgo(s.last_active);
                left.appendChild(meta);
                const id = document.createElement('div');
                id.className = 'radio-sess-id mono muted';
                id.textContent = String(s.session_id || '').slice(0, 16);
                id.title = s.session_id;
                left.appendChild(id);
                card.appendChild(left);

                const right = document.createElement('div');
                right.className = 'radio-sess-card-right';
                const resumeBtn = document.createElement('button');
                resumeBtn.type = 'button';
                resumeBtn.className = 'vc-btn vc-btn-sm vc-btn-primary';
                resumeBtn.textContent = isActive ? 'Active' : 'Resume';
                resumeBtn.disabled = isActive;
                resumeBtn.addEventListener('click', () => resumeSession(s));
                right.appendChild(resumeBtn);
                const stopBtn = document.createElement('button');
                stopBtn.type = 'button';
                stopBtn.className = 'vc-btn vc-btn-sm radio-stop-btn';
                stopBtn.textContent = 'Stop';
                stopBtn.addEventListener('click', () => stopSession(s.session_id));
                right.appendChild(stopBtn);
                card.appendChild(right);

                sessBody.appendChild(card);
            });
        }

        async function startRadio() {
            if (!state.userId) {
                GIQ.toast('Pick a user first.', 'warning');
                return;
            }
            if (!state.seedValue) {
                GIQ.toast('Enter a seed value.', 'warning');
                return;
            }
            startBtn.disabled = true;
            const oldText = startBtn.textContent;
            startBtn.textContent = 'Starting…';
            try {
                const body = {
                    user_id: state.userId,
                    seed_type: state.seedType,
                    seed_value: state.seedValue,
                    count: 10,
                };
                Object.keys(state.ctx).forEach(k => {
                    if (state.ctx[k] != null && state.ctx[k] !== '') body[k] = state.ctx[k];
                });
                const data = await GIQ.api.post('/v1/radio/start', body);
                state.sessionId = data.session_id;
                state.seedTypeLoaded = data.seed_type;
                state.seedDisplayName = data.seed_display_name || data.seed_value;
                state.tracks = data.tracks || [];
                state.totalServed = data.tracks ? data.tracks.length : 0;
                state.feedback = {};
                renderNowPlaying();
                loadSessions();
            } catch (e) {
                GIQ.toast('Failed to start radio: ' + e.message, 'error');
            } finally {
                startBtn.disabled = false;
                startBtn.textContent = oldText;
            }
        }

        startBtn.addEventListener('click', startRadio);

        async function resumeSession(s) {
            state.sessionId = s.session_id;
            state.seedTypeLoaded = s.seed_type;
            state.seedDisplayName = s.seed_display_name || s.seed_value;
            state.userId = s.user_id;
            state.tracks = [];
            state.totalServed = s.total_served || 0;
            state.feedback = {};
            await fetchNext(10, /*append=*/false);
            loadSessions();
        }

        async function stopSession(sid) {
            try {
                await GIQ.api.del('/v1/radio/' + encodeURIComponent(sid));
                if (state.sessionId === sid) {
                    state.sessionId = null;
                    state.tracks = [];
                    nowPlaying.innerHTML = '';
                }
                GIQ.toast('Radio session stopped', 'success');
                loadSessions();
            } catch (e) {
                GIQ.toast('Failed to stop session: ' + e.message, 'error');
            }
        }

        async function fetchNext(count, append) {
            if (!state.sessionId) return;
            const npLoading = nowPlaying.querySelector('.radio-loading');
            if (npLoading) npLoading.style.display = 'block';
            try {
                const data = await GIQ.api.get('/v1/radio/' + encodeURIComponent(state.sessionId) + '/next?count=' + (count || 10));
                state.totalServed = data.total_served || 0;
                if (append) {
                    state.tracks = state.tracks.concat(data.tracks || []);
                } else {
                    state.tracks = data.tracks || [];
                }
                renderNowPlaying();
            } catch (e) {
                if (e.status === 404) {
                    state.sessionId = null;
                    state.tracks = [];
                    nowPlaying.innerHTML = '<section class="panel"><div class="panel-body"><div class="reco-empty">Radio session expired.</div></div></section>';
                } else {
                    GIQ.toast('Failed to fetch next tracks: ' + e.message, 'error');
                }
            }
        }

        async function sendFeedback(trackId, action) {
            if (!state.sessionId || !state.userId) return;
            try {
                await GIQ.api.post('/v1/events', {
                    user_id: state.userId,
                    track_id: trackId,
                    event_type: action,
                    context_type: 'radio',
                    context_id: state.sessionId,
                });
                state.feedback[trackId] = action;
                renderNowPlaying();
            } catch (e) {
                GIQ.toast('Feedback failed: ' + e.message, 'error');
            }
        }

        function renderNowPlaying() {
            nowPlaying.innerHTML = '';
            if (!state.sessionId) return;

            const tracks = state.tracks || [];
            const current = tracks[0];

            /* ── Now Playing header card ─────────────────────────── */
            const npPanel = document.createElement('section');
            npPanel.className = 'panel radio-np-panel';
            nowPlaying.appendChild(npPanel);

            const npHead = document.createElement('div');
            npHead.className = 'panel-head radio-np-head';
            const headLeft = document.createElement('div');
            headLeft.className = 'panel-head-left';
            headLeft.innerHTML = '<div class="panel-title-row">'
                + '<div class="panel-title">Now Playing</div>'
                + '<span class="rd-source-chip">' + GIQ.fmt.esc(state.seedTypeLoaded || state.seedType) + ' radio</span>'
                + '</div>'
                + '<div class="panel-sub mono">'
                + 'seed: <strong>' + GIQ.fmt.esc(state.seedDisplayName || '—') + '</strong>'
                + ' · ' + state.totalServed + ' served'
                + ' · drift +' + Math.max(0, state.totalServed - (state.tracks ? state.tracks.length : 0)) + ' since seed'
                + '</div>';
            npHead.appendChild(headLeft);

            const headRight = document.createElement('div');
            headRight.className = 'radio-np-head-right';
            const next10 = document.createElement('button');
            next10.type = 'button';
            next10.className = 'vc-btn vc-btn-sm';
            next10.textContent = 'Next 10';
            next10.addEventListener('click', () => fetchNext(10, false));
            const next25 = document.createElement('button');
            next25.type = 'button';
            next25.className = 'vc-btn vc-btn-sm';
            next25.textContent = 'Next 25';
            next25.addEventListener('click', () => fetchNext(25, false));
            const stopBtn = document.createElement('button');
            stopBtn.type = 'button';
            stopBtn.className = 'vc-btn vc-btn-sm radio-stop-btn';
            stopBtn.textContent = 'Stop';
            stopBtn.addEventListener('click', () => stopSession(state.sessionId));
            headRight.appendChild(next10);
            headRight.appendChild(next25);
            headRight.appendChild(stopBtn);
            npHead.appendChild(headRight);
            npPanel.appendChild(npHead);

            const npBody = document.createElement('div');
            npBody.className = 'panel-body radio-np-body';
            npPanel.appendChild(npBody);

            if (!current) {
                npBody.innerHTML = '<div class="reco-empty">No tracks. Try Next 10 or adjust the seed.</div>';
                return;
            }

            /* Current track block */
            const currWrap = document.createElement('div');
            currWrap.className = 'radio-current';
            currWrap.innerHTML = '<div class="radio-current-title">' + GIQ.fmt.esc(current.title || current.track_id || '—') + '</div>'
                + '<div class="radio-current-artist">' + GIQ.fmt.esc(current.artist || '—')
                + (current.album ? ' <span class="muted">· ' + GIQ.fmt.esc(current.album) + '</span>' : '')
                + '</div>'
                + '<div class="radio-current-meta mono muted">'
                + (current.bpm ? Math.round(current.bpm) + ' BPM · ' : '')
                + (current.key ? GIQ.fmt.esc(current.key) + (current.mode ? ' ' + current.mode.charAt(0) : '') + ' · ' : '')
                + (current.energy != null ? 'E ' + current.energy.toFixed(2) + ' · ' : '')
                + (current.duration ? GIQ.fmt.fmtDuration(current.duration) : '')
                + '</div>';

            /* Position / duration timeline (decorative — no playback in browser). */
            const timeline = document.createElement('div');
            timeline.className = 'radio-timeline';
            timeline.innerHTML = '<div class="radio-timeline-track"><div class="radio-timeline-fill" style="width:0%"></div></div>'
                + '<div class="radio-timeline-meta mono muted">'
                + '0:00 / ' + (current.duration ? GIQ.fmt.fmtDuration(current.duration) : '—')
                + '</div>';
            currWrap.appendChild(timeline);
            npBody.appendChild(currWrap);

            /* Feedback row */
            const fbRow = document.createElement('div');
            fbRow.className = 'radio-feedback-row';
            const fb = state.feedback[current.track_id];
            const likeBtn = _radioFbBtn('♥ Like', 'like', fb === 'like', () => sendFeedback(current.track_id, 'like'));
            const skipBtn = _radioFbBtn('▶ Skip', 'skip', fb === 'skip', () => {
                sendFeedback(current.track_id, 'skip').then(() => {
                    /* Auto-advance: drop the head, fetch more if running low. */
                    state.tracks.shift();
                    if (state.tracks.length < 3) fetchNext(10, true);
                    else renderNowPlaying();
                });
            });
            const dislikeBtn = _radioFbBtn('✕ Dislike', 'dislike', fb === 'dislike', () => sendFeedback(current.track_id, 'dislike'));
            fbRow.appendChild(likeBtn);
            fbRow.appendChild(skipBtn);
            fbRow.appendChild(dislikeBtn);
            npBody.appendChild(fbRow);

            /* Queue panel */
            const queueTracks = tracks.slice(1);
            const queuePanel = document.createElement('section');
            queuePanel.className = 'panel radio-queue-panel';
            nowPlaying.appendChild(queuePanel);

            const queueHead = document.createElement('div');
            queueHead.className = 'panel-head';
            queueHead.innerHTML = '<div class="panel-head-left"><div class="panel-title-row">'
                + '<div class="panel-title">Up Next (' + queueTracks.length + ')</div></div>'
                + '<div class="panel-sub">Click a track\'s feedback button to influence the next batch</div></div>';
            queuePanel.appendChild(queueHead);

            const queueBody = document.createElement('div');
            queueBody.className = 'panel-body';
            if (!queueTracks.length) {
                queueBody.innerHTML = '<div class="reco-empty">Queue empty — fetch more with Next 10.</div>';
            } else {
                /* Re-number queue rows so the rank starts at 2 (after current). */
                queueTracks.forEach((t, idx) => { t._radioRank = idx + 2; });
                const tableEl = GIQ.components.trackTable({
                    columns: ['rank', 'title', 'artist', 'source', 'score', 'bpm', 'key', 'energy', 'mood', 'duration'],
                    rows: queueTracks.map(t => Object.assign({}, t, { position: t._radioRank - 1 })),
                    empty: 'No upcoming tracks.',
                    rowAction: (row) => {
                        const wrap = document.createElement('span');
                        wrap.className = 'radio-row-fb';
                        const liked = state.feedback[row.track_id] === 'like';
                        const disliked = state.feedback[row.track_id] === 'dislike';
                        const skipped = state.feedback[row.track_id] === 'skip';
                        const lk = document.createElement('button');
                        lk.type = 'button';
                        lk.className = 'radio-row-fb-btn radio-row-fb-like' + (liked ? ' is-active' : '');
                        lk.textContent = '♥';
                        lk.title = 'Like';
                        lk.addEventListener('click', e => { e.stopPropagation(); sendFeedback(row.track_id, 'like'); });
                        const sk = document.createElement('button');
                        sk.type = 'button';
                        sk.className = 'radio-row-fb-btn radio-row-fb-skip' + (skipped ? ' is-active' : '');
                        sk.textContent = '▶';
                        sk.title = 'Skip';
                        sk.addEventListener('click', e => { e.stopPropagation(); sendFeedback(row.track_id, 'skip'); });
                        const dk = document.createElement('button');
                        dk.type = 'button';
                        dk.className = 'radio-row-fb-btn radio-row-fb-dislike' + (disliked ? ' is-active' : '');
                        dk.textContent = '✕';
                        dk.title = 'Dislike';
                        dk.addEventListener('click', e => { e.stopPropagation(); sendFeedback(row.track_id, 'dislike'); });
                        wrap.appendChild(lk); wrap.appendChild(sk); wrap.appendChild(dk);
                        return wrap;
                    },
                });
                queueBody.appendChild(tableEl);
            }
            queuePanel.appendChild(queueBody);

            /* Source distribution chips */
            const counts = {};
            tracks.forEach(t => { const k = t.source || 'unknown'; counts[k] = (counts[k] || 0) + 1; });
            const srcKeys = Object.keys(counts);
            if (srcKeys.length) {
                const srcRow = document.createElement('div');
                srcRow.className = 'radio-sources-row';
                const lbl = document.createElement('span');
                lbl.className = 'eyebrow';
                lbl.textContent = 'Sources';
                srcRow.appendChild(lbl);
                srcKeys.forEach(k => {
                    const chip = document.createElement('span');
                    chip.className = 'rd-source-chip';
                    chip.textContent = String(k).replace(/^radio_/, '') + ' ' + counts[k];
                    srcRow.appendChild(chip);
                });
                queueBody.appendChild(srcRow);
            }

            /* Hidden loading shim for fetchNext */
            const loading = document.createElement('div');
            loading.className = 'radio-loading mono muted';
            loading.style.display = 'none';
            loading.textContent = 'Loading next batch…';
            nowPlaying.appendChild(loading);
        }

        loadSessions();
        if (state.sessionId && state.tracks && state.tracks.length) {
            renderNowPlaying();
        }

        return function cleanup() {
            cleanedUp = true;
        };
    };

    function _radioFbBtn(label, kind, active, onClick) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'radio-fb-btn radio-fb-' + kind + (active ? ' is-active' : '');
        b.textContent = label;
        b.addEventListener('click', onClick);
        return b;
    }

    /* ── Charts ─────────────────────────────────────────────────────── */

    GIQ.state.charts = GIQ.state.charts || {
        scope: 'global',
        chartType: 'top_tracks',
        offset: 0,
        limit: 100,
        available: null,
        stats: null,
        chart: null,
        loading: false,
    };

    GIQ.pages.explore.charts = function renderCharts(root) {
        if (!GIQ.state.charts) {
            GIQ.state.charts = {
                scope: 'global', chartType: 'top_tracks', offset: 0, limit: 100,
                available: null, stats: null, chart: null, loading: false,
            };
        }
        const s = GIQ.state.charts;

        const right = document.createElement('div');
        right.className = 'charts-header-controls';
        const cronBadge = document.createElement('span');
        cronBadge.className = 'charts-cron-badge mono';
        cronBadge.textContent = '…';
        right.appendChild(cronBadge);

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'EXPLORE',
            title: 'Charts',
            right: right,
        }));

        const body = document.createElement('div');
        body.className = 'charts-body';
        root.appendChild(body);

        /* Filter bar */
        const filterBar = document.createElement('div');
        filterBar.className = 'charts-filter-bar';
        body.appendChild(filterBar);

        const scopeWrap = document.createElement('div');
        scopeWrap.className = 'charts-filter-field';
        scopeWrap.innerHTML = '<div class="eyebrow">Scope</div>';
        const scopeSel = document.createElement('select');
        scopeSel.className = 'reco-select';
        scopeSel.innerHTML = '<option value="global">Global</option>';
        scopeWrap.appendChild(scopeSel);
        filterBar.appendChild(scopeWrap);

        const typeWrap = document.createElement('div');
        typeWrap.className = 'charts-filter-field';
        typeWrap.innerHTML = '<div class="eyebrow">Type</div>';
        const typeSel = document.createElement('select');
        typeSel.className = 'reco-select';
        typeSel.innerHTML = '<option value="top_tracks">Top Tracks</option>'
            + '<option value="top_artists">Top Artists</option>';
        typeSel.value = s.chartType;
        typeWrap.appendChild(typeSel);
        filterBar.appendChild(typeWrap);

        scopeSel.addEventListener('change', () => { s.scope = scopeSel.value; s.offset = 0; loadChart(); });
        typeSel.addEventListener('change', () => { s.chartType = typeSel.value; s.offset = 0; loadChart(); });

        /* Chart panel */
        const panel = document.createElement('section');
        panel.className = 'panel charts-panel';
        body.appendChild(panel);

        const panelHead = document.createElement('div');
        panelHead.className = 'panel-head';
        panel.appendChild(panelHead);

        const panelBody = document.createElement('div');
        panelBody.className = 'panel-body charts-panel-body';
        panel.appendChild(panelBody);

        function setCronBadge(stats) {
            if (!stats) { cronBadge.textContent = ''; return; }
            if (stats.auto_rebuild_enabled) {
                const interval = stats.interval_hours || 24;
                cronBadge.textContent = '✓ Auto-rebuild every ' + interval + 'h';
                cronBadge.classList.add('charts-cron-on');
                cronBadge.classList.remove('charts-cron-off');
                cronBadge.title = 'Periodic chart build is registered'
                    + (stats.next_run_at ? ' — next ' + GIQ.fmt.timeAgo(stats.next_run_at) : '');
            } else {
                cronBadge.textContent = '⚠ Auto-rebuild OFF — set CHARTS_ENABLED=true';
                cronBadge.classList.add('charts-cron-off');
                cronBadge.classList.remove('charts-cron-on');
                cronBadge.title = 'Set CHARTS_ENABLED=true (and LASTFM_API_KEY) in your .env to enable the periodic build';
            }
        }

        function buildScopeOptions() {
            const charts = (s.available && s.available.charts) || [];
            const scopes = [];
            const seen = {};
            charts.forEach(c => {
                if (!seen[c.scope]) { seen[c.scope] = true; scopes.push(c.scope); }
            });
            if (!scopes.includes('global')) scopes.unshift('global');
            scopeSel.innerHTML = '';
            scopes.forEach(sc => {
                const o = document.createElement('option');
                o.value = sc;
                o.textContent = _scopeLabel(sc);
                if (sc === s.scope) o.selected = true;
                scopeSel.appendChild(o);
            });
        }

        async function loadAvailable() {
            try {
                const [available, stats] = await Promise.all([
                    GIQ.api.get('/v1/charts'),
                    GIQ.api.get('/v1/charts/stats').catch(() => null),
                ]);
                s.available = available;
                s.stats = stats;
                setCronBadge(stats);
                buildScopeOptions();
                loadChart();
            } catch (e) {
                panelBody.innerHTML = '<div class="reco-error">Failed to load charts: ' + GIQ.fmt.esc(e.message) + '</div>';
            }
        }

        async function loadChart() {
            s.loading = true;
            renderHead();
            panelBody.innerHTML = '<div class="vc-loading">Loading chart…</div>';
            try {
                const url = '/v1/charts/' + encodeURIComponent(s.chartType)
                    + '?scope=' + encodeURIComponent(s.scope)
                    + '&limit=' + s.limit + '&offset=' + s.offset;
                const data = await GIQ.api.get(url);
                s.chart = data;
                s.loading = false;
                renderHead();
                renderTable();
            } catch (e) {
                s.loading = false;
                renderHead();
                if (e.status === 404) {
                    panelBody.innerHTML = '<div class="reco-empty">' + GIQ.fmt.esc(e.message)
                        + '<br><br>Build charts via Actions → Charts.</div>';
                } else {
                    panelBody.innerHTML = '<div class="reco-error">Failed to load chart: ' + GIQ.fmt.esc(e.message) + '</div>';
                }
            }
        }

        function renderHead() {
            const c = s.chart;
            const isTrack = s.chartType === 'top_tracks';
            const subParts = [];
            if (c) {
                subParts.push((c.total || 0) + ' entries');
                if (c.fetched_at) subParts.push('updated ' + GIQ.fmt.timeAgo(c.fetched_at));
            }
            panelHead.innerHTML = '<div class="panel-head-left"><div class="panel-title-row">'
                + '<div class="panel-title">' + GIQ.fmt.esc(_scopeLabel(s.scope)) + ' — '
                + (isTrack ? 'Top Tracks' : 'Top Artists') + '</div></div>'
                + '<div class="panel-sub mono">' + subParts.join(' · ') + '</div></div>';
        }

        function renderTable() {
            const c = s.chart;
            panelBody.innerHTML = '';
            if (!c || !c.entries || !c.entries.length) {
                panelBody.innerHTML = '<div class="reco-empty">No entries.</div>';
                return;
            }
            const isTrack = s.chartType === 'top_tracks';

            const table = document.createElement('div');
            table.className = 'charts-table';
            table.style.gridTemplateColumns = isTrack
                ? '40px 56px minmax(180px, 2fr) minmax(120px, 1fr) 80px 80px minmax(140px, 1.2fr)'
                : '40px 56px minmax(180px, 2fr) 80px 80px 80px minmax(140px, 1.2fr)';

            const head = document.createElement('div');
            head.className = 'charts-row charts-row-head';
            const headers = isTrack
                ? ['#', '', 'Title', 'Artist', 'Plays', 'Listeners', 'Status']
                : ['#', '', 'Artist', 'Plays', 'Listeners', 'Tracks', 'Status'];
            headers.forEach(h => {
                const cell = document.createElement('div');
                cell.className = 'charts-h';
                cell.textContent = h;
                head.appendChild(cell);
            });
            table.appendChild(head);

            c.entries.forEach((entry, idx) => {
                const row = document.createElement('div');
                row.className = 'charts-row';

                const numCell = document.createElement('div');
                numCell.className = 'charts-c charts-c-num mono';
                numCell.textContent = (entry.position + 1);
                row.appendChild(numCell);

                const thumbCell = document.createElement('div');
                thumbCell.className = 'charts-c charts-c-thumb';
                thumbCell.appendChild(_chartsThumbnail(entry));
                row.appendChild(thumbCell);

                if (isTrack) {
                    const titleCell = document.createElement('div');
                    titleCell.className = 'charts-c charts-c-title';
                    titleCell.innerHTML = '<strong>' + GIQ.fmt.esc(entry.track_title || '—') + '</strong>';
                    row.appendChild(titleCell);
                }
                const artistCell = document.createElement('div');
                artistCell.className = 'charts-c charts-c-artist';
                artistCell.innerHTML = isTrack
                    ? GIQ.fmt.esc(entry.artist_name || '—')
                    : '<strong>' + GIQ.fmt.esc(entry.artist_name || '—') + '</strong>';
                row.appendChild(artistCell);

                const playsCell = document.createElement('div');
                playsCell.className = 'charts-c mono';
                playsCell.textContent = GIQ.fmt.fmtNumber(entry.playcount);
                row.appendChild(playsCell);

                const listenersCell = document.createElement('div');
                listenersCell.className = 'charts-c mono';
                listenersCell.textContent = GIQ.fmt.fmtNumber(entry.listeners);
                row.appendChild(listenersCell);

                if (!isTrack) {
                    const tracksCell = document.createElement('div');
                    tracksCell.className = 'charts-c mono';
                    tracksCell.textContent = String(entry.library_track_count || 0);
                    row.appendChild(tracksCell);
                }

                const statusCell = document.createElement('div');
                statusCell.className = 'charts-c charts-c-status';
                _chartsStatus(entry, statusCell, idx);
                row.appendChild(statusCell);

                table.appendChild(row);
            });
            panelBody.appendChild(table);

            /* Pagination */
            const offset = s.offset || 0;
            const total = c.total || 0;
            const from = total > 0 ? offset + 1 : 0;
            const to = Math.min(offset + (c.entries.length || 0), total);
            const foot = document.createElement('div');
            foot.className = 'track-table-pagination';
            foot.innerHTML = '<span class="mono muted">Showing ' + from + '–' + to + ' of ' + total + '</span>';
            const right2 = document.createElement('div');
            right2.style.display = 'flex';
            right2.style.gap = '6px';
            const prev = document.createElement('button');
            prev.type = 'button';
            prev.className = 'vc-btn vc-btn-sm';
            prev.textContent = '← Prev';
            prev.disabled = offset <= 0;
            prev.addEventListener('click', () => {
                s.offset = Math.max(0, s.offset - s.limit);
                loadChart();
            });
            const next = document.createElement('button');
            next.type = 'button';
            next.className = 'vc-btn vc-btn-sm';
            next.textContent = 'Next →';
            next.disabled = offset + (c.entries.length || 0) >= total;
            next.addEventListener('click', () => {
                s.offset += s.limit;
                loadChart();
            });
            right2.appendChild(prev);
            right2.appendChild(next);
            foot.appendChild(right2);
            panelBody.appendChild(foot);
        }

        loadAvailable();

        return function cleanup() { /* state persists in GIQ.state.charts */ };
    };

    function _scopeLabel(scope) {
        if (scope === 'global') return 'Global';
        if (scope.indexOf('tag:') === 0) return 'Genre: ' + scope.substring(4);
        if (scope.indexOf('geo:') === 0) return 'Country: ' + scope.substring(4);
        return scope;
    }

    function _chartsThumbnail(entry) {
        const wrap = document.createElement('div');
        wrap.className = 'charts-thumb';
        const tile = document.createElement('span');
        tile.className = 'charts-thumb-tile';
        tile.textContent = '♫';
        wrap.appendChild(tile);
        const primary = (entry.library && entry.library.cover_url) || '';
        const fallback = entry.image_url || '';
        const src = primary || fallback;
        if (src) {
            const img = document.createElement('img');
            img.alt = '';
            img.loading = 'lazy';
            img.className = 'charts-thumb-img';
            if (primary && fallback && primary !== fallback) {
                img.dataset.fallback = fallback;
            }
            img.addEventListener('error', () => {
                const fb = img.dataset.fallback;
                if (fb) {
                    img.dataset.fallback = '';
                    img.src = fb;
                } else {
                    img.remove();
                }
            });
            img.src = src;
            wrap.appendChild(img);
        }
        return wrap;
    }

    function _chartsStatus(entry, cell, _idx) {
        cell.innerHTML = '';
        const ls = entry.lidarr_status;
        if (entry.in_library) {
            const chip = document.createElement('span');
            chip.className = 'charts-status-chip charts-status-in-lib';
            chip.textContent = 'in library';
            cell.appendChild(chip);
            if (ls === 'in_lidarr' || ls === 'downloading') {
                const c2 = document.createElement('span');
                c2.className = 'charts-status-chip charts-status-via-lidarr';
                c2.textContent = 'via lidarr';
                cell.appendChild(c2);
            }
            return;
        }

        if (ls === 'downloading') {
            const c = document.createElement('span');
            c.className = 'charts-status-chip charts-status-dl';
            c.textContent = '⬇ lidarr';
            cell.appendChild(c);
            return;
        }
        if (ls === 'pending') {
            const c = document.createElement('span');
            c.className = 'charts-status-chip charts-status-pending';
            c.textContent = '⏳ lidarr';
            cell.appendChild(c);
            return;
        }
        if (ls === 'in_lidarr') {
            const c = document.createElement('span');
            c.className = 'charts-status-chip charts-status-via-lidarr';
            c.textContent = '⬇ lidarr';
            cell.appendChild(c);
            return;
        }
        if (ls === 'failed') {
            const c = document.createElement('span');
            c.className = 'charts-status-chip charts-status-failed';
            c.textContent = '✗ lidarr';
            cell.appendChild(c);
            return;
        }

        /* Not in library, not in lidarr — show "not in library" + per-row "⬇ get" button. */
        const chip = document.createElement('span');
        chip.className = 'charts-status-chip charts-status-none';
        chip.textContent = 'not in library';
        cell.appendChild(chip);
        if (entry.track_title || entry.artist_name) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'charts-get-btn';
            btn.textContent = '⬇ get';
            btn.title = 'Download this track';
            btn.addEventListener('click', () => _chartsDownload(btn, entry));
            cell.appendChild(btn);
        }
    }

    function _chartsDownload(btn, entry) {
        btn.disabled = true;
        btn.textContent = '…';
        const s = GIQ.state.charts;
        GIQ.api.post('/v1/charts/download', {
            chart_type: s.chartType,
            scope: s.scope,
            position: entry.position,
        }).then(data => {
            if (data && (data.status === 'downloading' || data.status === 'duplicate')) {
                btn.outerHTML = '<span class="charts-status-chip charts-status-dl">⬇ queued</span>';
                GIQ.toast({
                    message: 'Queued — track sent to download cascade',
                    kind: 'success',
                    jump: { hash: '#/monitor/downloads', label: 'View queue →' },
                });
            } else {
                btn.textContent = (data && data.status) || 'sent';
                btn.disabled = false;
            }
        }).catch(e => {
            btn.outerHTML = '<span class="charts-status-chip charts-status-failed" title="'
                + GIQ.fmt.esc(e.message || 'failed') + '">✗ failed</span>';
            GIQ.toast('Download failed: ' + (e.message || 'unknown'), 'error');
        });
    }

    /* ── Artists ────────────────────────────────────────────────────── */

    GIQ.state.artists = GIQ.state.artists || {
        userId: null,
        source: 'listening',
        result: null,
        loading: false,
    };

    const ARTISTS_SOURCES = [
        ['listening', 'Listening history'],
        ['lastfm_similar', 'Last.fm similar'],
        ['lastfm_top', 'Last.fm top'],
    ];

    GIQ.pages.explore.artists = function renderArtists(root, params) {
        if (!GIQ.state.artists) {
            GIQ.state.artists = { userId: null, source: 'listening', result: null, loading: false };
        }
        const s = GIQ.state.artists;
        if (params && params.user) s.userId = params.user;

        const right = document.createElement('div');
        right.className = 'artists-header-controls';
        const userSel = _userSelect(s.userId, (val) => {
            s.userId = val || null;
            if (s.userId) {
                const next = '#/explore/artists?user=' + encodeURIComponent(s.userId);
                if (window.location.hash !== next) history.replaceState(null, '', next);
                load();
            }
        });
        right.appendChild(userSel);
        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'EXPLORE',
            title: 'Artists',
            right: right,
        }));

        const body = document.createElement('div');
        body.className = 'artists-body';
        root.appendChild(body);

        /* Source segmented toggle */
        const toggleBar = document.createElement('div');
        toggleBar.className = 'artists-source-toggle';
        ARTISTS_SOURCES.forEach(([val, label]) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'artists-source-btn' + (s.source === val ? ' is-active' : '');
            b.textContent = label;
            b.dataset.value = val;
            b.addEventListener('click', () => {
                s.source = val;
                Array.from(toggleBar.children).forEach(c => c.classList.toggle('is-active', c.dataset.value === val));
                renderGrid();
            });
            toggleBar.appendChild(b);
        });
        body.appendChild(toggleBar);

        const status = document.createElement('div');
        status.className = 'artists-status mono muted';
        body.appendChild(status);

        const grid = document.createElement('div');
        grid.className = 'artists-grid';
        body.appendChild(grid);

        async function load() {
            if (!s.userId) {
                status.textContent = '';
                grid.innerHTML = '<div class="reco-empty">Pick a user above to see recommended artists.</div>';
                return;
            }
            s.loading = true;
            status.textContent = 'Loading…';
            grid.innerHTML = '<div class="vc-loading">Loading recommended artists…</div>';
            try {
                const data = await GIQ.api.get('/v1/recommend/'
                    + encodeURIComponent(s.userId) + '/artists?limit=50');
                s.result = data;
                s.loading = false;
                renderGrid();
            } catch (e) {
                s.loading = false;
                grid.innerHTML = '<div class="reco-error">Failed to load artists: ' + GIQ.fmt.esc(e.message) + '</div>';
                status.textContent = '';
            }
        }

        function renderGrid() {
            const r = s.result;
            grid.innerHTML = '';
            if (!r) return;
            const all = r.artists || [];
            const filtered = all.filter(a => (a.source || 'listening') === s.source);
            status.textContent = filtered.length + ' / ' + all.length
                + ' (filter: ' + s.source + ')';
            if (!filtered.length) {
                grid.innerHTML = '<div class="reco-empty">No artists for this source. '
                    + 'Try another tab — listening history depends on play data; Last.fm sources require a connected account.</div>';
                return;
            }
            filtered.forEach(a => grid.appendChild(_artistCard(a)));
        }

        load();

        return function cleanup() { /* state persists */ };
    };

    function _artistCard(a) {
        const card = document.createElement('button');
        card.type = 'button';
        card.className = 'artist-card';

        const cover = document.createElement('div');
        cover.className = 'artist-card-cover';
        const fallbackLetter = (a.name || '').charAt(0).toUpperCase() || '?';
        const useEmptyFallback = () => {
            cover.innerHTML = '';
            cover.classList.add('artist-card-cover-empty');
            cover.textContent = fallbackLetter;
        };
        if (a.image_url) {
            const img = document.createElement('img');
            img.src = a.image_url;
            img.alt = '';
            img.loading = 'lazy';
            img.addEventListener('error', useEmptyFallback);
            cover.appendChild(img);
        } else {
            useEmptyFallback();
        }
        card.appendChild(cover);

        const inner = document.createElement('div');
        inner.className = 'artist-card-inner';
        card.appendChild(inner);

        const name = document.createElement('div');
        name.className = 'artist-card-name';
        name.textContent = a.name || '—';
        inner.appendChild(name);

        const audio = a.audio || {};
        const audioParts = [];
        if (audio.bpm != null) audioParts.push('BPM ' + Math.round(audio.bpm));
        if (audio.energy != null) audioParts.push('energy ' + audio.energy.toFixed(2));
        if (audio.valence != null) audioParts.push('valence ' + audio.valence.toFixed(2));
        if (audioParts.length) {
            const stats = document.createElement('div');
            stats.className = 'artist-card-stats mono muted';
            stats.textContent = audioParts.join(' · ');
            inner.appendChild(stats);
        }

        const presence = document.createElement('div');
        presence.className = 'artist-card-presence';
        if (a.in_library) {
            presence.innerHTML = '<span class="artist-presence-chip in-lib">✓ in library</span>'
                + ' <span class="mono muted">' + (a.track_count || 0) + ' tracks</span>';
        } else {
            presence.innerHTML = '<span class="artist-presence-chip not-lib">+ via Lidarr</span>';
        }
        inner.appendChild(presence);

        if (a.source === 'listening') {
            const counts = document.createElement('div');
            counts.className = 'artist-card-counts mono muted';
            counts.textContent = (a.plays || 0) + ' plays · ' + (a.likes || 0) + ' likes';
            inner.appendChild(counts);
        }

        if (a.source === 'lastfm_similar' && a.similar_to && a.similar_to.length) {
            const sim = document.createElement('div');
            sim.className = 'artist-card-similar mono muted';
            sim.textContent = 'similar to ' + a.similar_to.slice(0, 2).join(', ');
            inner.appendChild(sim);
        }

        if (a.top_tracks && a.top_tracks.length) {
            const ttHead = document.createElement('div');
            ttHead.className = 'artist-card-tt-head eyebrow';
            ttHead.textContent = 'Top tracks';
            inner.appendChild(ttHead);
            const ttList = document.createElement('div');
            ttList.className = 'artist-card-tt-list';
            a.top_tracks.slice(0, 5).forEach(t => {
                const row = document.createElement('div');
                row.className = 'artist-card-tt-row mono';
                row.innerHTML = '<span class="artist-card-tt-title">' + GIQ.fmt.esc(t.title || '—') + '</span>'
                    + (t.satisfaction_score != null
                        ? ' <span class="muted">' + t.satisfaction_score.toFixed(2) + '</span>'
                        : '');
                ttList.appendChild(row);
            });
            inner.appendChild(ttList);
        }

        card.addEventListener('click', () => _openArtistDetail(a));
        return card;
    }

    function _openArtistDetail(artist) {
        const body = document.createElement('div');
        body.className = 'artist-detail-body';
        body.innerHTML = '<div class="vc-loading">Loading artist…</div>';

        const handle = GIQ.components.modal({
            title: artist.name || 'Artist',
            body,
            width: 'lg',
        });

        GIQ.api.get('/v1/artists/' + encodeURIComponent(artist.name) + '/meta')
            .then(meta => _renderArtistDetail(body, artist, meta, handle))
            .catch(e => {
                body.innerHTML = '';
                if (e.status === 503) {
                    const empty = document.createElement('div');
                    empty.className = 'reco-empty';
                    empty.innerHTML = 'Last.fm integration is not configured. '
                        + 'Set <code>LASTFM_API_KEY</code> in <code>.env</code> for rich artist metadata.';
                    body.appendChild(empty);
                } else if (e.status === 404) {
                    const empty = document.createElement('div');
                    empty.className = 'reco-empty';
                    empty.textContent = 'No Last.fm data found for ' + (artist.name || 'this artist') + '.';
                    body.appendChild(empty);
                } else {
                    const err = document.createElement('div');
                    err.className = 'reco-error';
                    err.textContent = 'Failed to load: ' + e.message;
                    body.appendChild(err);
                }
                /* Always show the action buttons even when meta fails. */
                body.appendChild(_artistDetailActions(artist, null, handle));
            });
    }

    function _renderArtistDetail(body, artist, meta, handle) {
        const esc = GIQ.fmt.esc;
        body.innerHTML = '';

        /* Hero */
        const hero = document.createElement('div');
        hero.className = 'artist-detail-hero';
        if (meta.image_url) {
            const img = document.createElement('img');
            img.src = meta.image_url;
            img.alt = '';
            img.loading = 'lazy';
            img.className = 'artist-detail-img';
            img.addEventListener('error', () => img.remove());
            hero.appendChild(img);
        }
        const heroText = document.createElement('div');
        heroText.className = 'artist-detail-hero-text';
        heroText.innerHTML = '<div class="artist-detail-name">' + esc(meta.name || artist.name) + '</div>';
        const stats = [];
        if (meta.listeners != null) stats.push(GIQ.fmt.fmtNumber(meta.listeners) + ' listeners');
        if (meta.playcount != null) stats.push(GIQ.fmt.fmtNumber(meta.playcount) + ' plays');
        if (meta.in_library) stats.push((meta.library_track_count || 0) + ' tracks in library');
        if (stats.length) {
            const sub = document.createElement('div');
            sub.className = 'artist-detail-stats mono muted';
            sub.textContent = stats.join(' · ');
            heroText.appendChild(sub);
        }
        hero.appendChild(heroText);
        body.appendChild(hero);

        /* Action buttons row */
        body.appendChild(_artistDetailActions(artist, meta, handle));

        /* Tags */
        if (meta.tags && meta.tags.length) {
            const tagWrap = document.createElement('div');
            tagWrap.className = 'artist-detail-tags';
            meta.tags.slice(0, 16).forEach(t => {
                const chip = document.createElement('span');
                chip.className = 'rd-source-chip';
                chip.textContent = t;
                tagWrap.appendChild(chip);
            });
            body.appendChild(tagWrap);
        }

        /* Bio */
        if (meta.bio) {
            const bioPanel = document.createElement('section');
            bioPanel.className = 'panel artist-detail-panel';
            const bh = document.createElement('div');
            bh.className = 'panel-head';
            bh.innerHTML = '<div class="panel-head-left"><div class="panel-title-row">'
                + '<div class="panel-title">Bio</div></div></div>';
            bioPanel.appendChild(bh);
            const bb = document.createElement('div');
            bb.className = 'panel-body';
            const text = document.createElement('div');
            text.className = 'artist-detail-bio';
            /* The Last.fm bio summary is plain text or HTML — strip tags to be safe. */
            text.textContent = String(meta.bio || '').replace(/<[^>]*>/g, '');
            bb.appendChild(text);
            bioPanel.appendChild(bb);
            body.appendChild(bioPanel);
        }

        /* Top tracks */
        if (meta.top_tracks && meta.top_tracks.length) {
            const ttPanel = document.createElement('section');
            ttPanel.className = 'panel artist-detail-panel';
            const th = document.createElement('div');
            th.className = 'panel-head';
            th.innerHTML = '<div class="panel-head-left"><div class="panel-title-row">'
                + '<div class="panel-title">Top Tracks</div></div>'
                + '<div class="panel-sub mono">from Last.fm · matched against local library</div></div>';
            ttPanel.appendChild(th);
            const tb = document.createElement('div');
            tb.className = 'panel-body';
            const list = document.createElement('div');
            list.className = 'artist-detail-tt-list';
            meta.top_tracks.slice(0, 12).forEach(t => {
                const row = document.createElement('div');
                row.className = 'artist-detail-tt-row';
                const inLib = t.in_library;
                row.innerHTML = '<span class="artist-detail-tt-name">' + esc(t.title) + '</span>'
                    + ' <span class="mono muted">' + GIQ.fmt.fmtNumber(t.playcount || 0) + '</span>'
                    + ' <span class="artist-detail-tt-chip ' + (inLib ? 'in-lib' : 'not-lib') + '">'
                    + (inLib ? '✓ in library' : '— not in library') + '</span>';
                list.appendChild(row);
            });
            tb.appendChild(list);
            ttPanel.appendChild(tb);
            body.appendChild(ttPanel);
        }

        /* Similar artists */
        if (meta.similar && meta.similar.length) {
            const simPanel = document.createElement('section');
            simPanel.className = 'panel artist-detail-panel';
            const sh = document.createElement('div');
            sh.className = 'panel-head';
            sh.innerHTML = '<div class="panel-head-left"><div class="panel-title-row">'
                + '<div class="panel-title">Similar Artists</div></div></div>';
            simPanel.appendChild(sh);
            const sb = document.createElement('div');
            sb.className = 'panel-body';
            const grid = document.createElement('div');
            grid.className = 'artist-detail-sim-grid';
            meta.similar.slice(0, 12).forEach(sim => {
                const chip = document.createElement('a');
                chip.className = 'artist-detail-sim-chip';
                chip.href = '#';
                const avatar = document.createElement('span');
                avatar.className = 'artist-detail-sim-avatar';
                if (sim.image_url) {
                    const img = document.createElement('img');
                    img.src = sim.image_url;
                    img.alt = '';
                    img.loading = 'lazy';
                    img.addEventListener('error', () => {
                        img.remove();
                        avatar.classList.add('artist-detail-sim-avatar-empty');
                        avatar.textContent = (sim.name || '').charAt(0).toUpperCase() || '?';
                    });
                    avatar.appendChild(img);
                } else {
                    avatar.classList.add('artist-detail-sim-avatar-empty');
                    avatar.textContent = (sim.name || '').charAt(0).toUpperCase() || '?';
                }
                chip.appendChild(avatar);
                const text = document.createElement('span');
                text.className = 'artist-detail-sim-text';
                text.innerHTML = esc(sim.name)
                    + (sim.match != null ? ' <span class="mono muted">' + sim.match.toFixed(2) + '</span>' : '')
                    + (sim.in_library ? ' <span class="artist-detail-tt-chip in-lib">in library</span>' : '');
                chip.appendChild(text);
                chip.addEventListener('click', e => {
                    e.preventDefault();
                    handle.close();
                    _openArtistDetail({ name: sim.name });
                });
                grid.appendChild(chip);
            });
            sb.appendChild(grid);
            simPanel.appendChild(sb);
            body.appendChild(simPanel);
        }
    }

    function _artistDetailActions(artist, meta, handle) {
        const row = document.createElement('div');
        row.className = 'artist-detail-actions';

        const radioBtn = document.createElement('button');
        radioBtn.type = 'button';
        radioBtn.className = 'vc-btn vc-btn-primary vc-btn-sm';
        radioBtn.textContent = '▶ Play radio from this artist';
        radioBtn.addEventListener('click', () => {
            handle.close();
            const params = {
                seed_type: 'artist',
                seed_value: artist.name,
            };
            const cur = GIQ.state.artists && GIQ.state.artists.userId;
            if (cur) params.user = cur;
            GIQ.router.navigate('explore', 'radio', params);
        });
        row.appendChild(radioBtn);

        const inLib = (meta && meta.in_library) || (artist && artist.in_library);
        if (!inLib) {
            const lidarrBtn = document.createElement('button');
            lidarrBtn.type = 'button';
            lidarrBtn.className = 'vc-btn vc-btn-sm';
            lidarrBtn.textContent = '+ Add to Lidarr';
            lidarrBtn.title = 'Discovery → Lidarr (manual artist add not yet wired up)';
            lidarrBtn.addEventListener('click', () => {
                GIQ.toast(
                    'Manual single-artist Lidarr add isn\'t wired into the v2 dashboard yet — use Actions → Discovery to run a full discovery pass.',
                    'info', 6000,
                );
            });
            row.appendChild(lidarrBtn);
        }
        return row;
    }

    /* ── News ───────────────────────────────────────────────────────── */

    GIQ.state.news = GIQ.state.news || {
        userId: null,
        tag: '',
        result: null,
        loading: false,
        unavailable: null, /* null = unknown, 'disabled' = stub, 'other' = error */
    };

    GIQ.pages.explore.news = function renderNews(root, params) {
        if (!GIQ.state.news) {
            GIQ.state.news = { userId: null, tag: '', result: null, loading: false, unavailable: null };
        }
        const s = GIQ.state.news;
        if (params && params.user) s.userId = params.user;

        /* If we already know the news endpoint isn't implemented, skip the fetch. */
        if (s.unavailable === 'disabled') {
            _renderNewsStub(root);
            /* Still render the page header so the user can change user / refresh. */
            return function cleanup() { };
        }

        const right = document.createElement('div');
        right.className = 'news-header-controls';
        const userSel = _userSelect(s.userId, (val, isInit) => {
            s.userId = val || null;
            if (s.userId) {
                if (!isInit) load();
                else load();
            }
        });
        right.appendChild(userSel);

        const tagSel = document.createElement('select');
        tagSel.className = 'reco-select';
        tagSel.innerHTML = '<option value="">All Posts</option>'
            + '<option value="FRESH">FRESH</option>'
            + '<option value="NEWS">NEWS</option>'
            + '<option value="DISCUSSION">DISCUSSION</option>';
        tagSel.value = s.tag || '';
        tagSel.addEventListener('change', () => { s.tag = tagSel.value; load(); });
        right.appendChild(tagSel);

        const refreshBtn = document.createElement('button');
        refreshBtn.type = 'button';
        refreshBtn.className = 'vc-btn vc-btn-sm';
        refreshBtn.textContent = 'Refresh Feed';
        refreshBtn.addEventListener('click', load);
        right.appendChild(refreshBtn);

        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'EXPLORE',
            title: 'News',
            right: right,
        }));

        const body = document.createElement('div');
        body.className = 'news-body';
        root.appendChild(body);

        const cacheLine = document.createElement('div');
        cacheLine.className = 'news-cache-line mono muted';
        body.appendChild(cacheLine);

        const list = document.createElement('div');
        list.className = 'news-list';
        body.appendChild(list);

        async function load() {
            if (!s.userId) {
                cacheLine.textContent = '';
                list.innerHTML = '<div class="reco-empty">Pick a user above to load their personalized feed.</div>';
                return;
            }
            cacheLine.textContent = '';
            list.innerHTML = '<div class="vc-loading">Loading news…</div>';
            try {
                let url = '/v1/news/' + encodeURIComponent(s.userId) + '?limit=50';
                if (s.tag) url += '&tag=' + encodeURIComponent(s.tag);
                const data = await GIQ.api.get(url);
                s.result = data;
                s.unavailable = null;
                renderList();
            } catch (e) {
                if (e.status === 404 || e.status === 503 || /not implemented|not enabled|disabled/i.test(e.message || '')) {
                    s.unavailable = 'disabled';
                    /* Replace whole page with stub */
                    root.innerHTML = '';
                    _renderNewsStub(root);
                    return;
                }
                list.innerHTML = '<div class="reco-error">Failed to load news: ' + GIQ.fmt.esc(e.message) + '</div>';
            }
        }

        function renderList() {
            const data = s.result;
            list.innerHTML = '';
            cacheLine.textContent = '';
            if (!data) return;
            if (data.cache_age_minutes != null) {
                cacheLine.textContent = 'cache age ' + Math.round(data.cache_age_minutes) + ' min'
                    + (data.cache_stale ? ' (stale)' : '');
                cacheLine.classList.toggle('news-cache-stale', !!data.cache_stale);
            }
            const items = data.items || [];
            if (!items.length) {
                list.innerHTML = '<div class="reco-empty">No news articles found.'
                    + (s.tag ? ' Try removing the tag filter.' : '') + '</div>';
                return;
            }
            items.forEach(it => list.appendChild(_newsCard(it)));
        }

        load();

        return function cleanup() { };
    };

    function _renderNewsStub(root) {
        root.appendChild(GIQ.components.pageHeader({
            eyebrow: 'EXPLORE',
            title: 'News',
        }));
        const body = document.createElement('div');
        body.className = 'news-body';
        root.appendChild(body);

        const card = document.createElement('div');
        card.className = 'news-stub';
        card.innerHTML = '<div class="news-stub-eyebrow eyebrow">COMING SOON</div>'
            + '<h2 class="news-stub-title">Personalized music news from Reddit</h2>'
            + '<p class="news-stub-body">'
            + 'GrooveIQ can pull music news from Reddit (r/Music, r/hiphopheads, r/indieheads, …) '
            + 'and rank each post by your taste profile. Posts are scored on artist matches, '
            + 'genre overlap, and recency.</p>'
            + '<p class="news-stub-body">'
            + 'To enable, set <code>NEWS_ENABLED=true</code> in <code>.env</code> and configure '
            + '<code>NEWS_DEFAULT_SUBREDDITS</code>. The endpoint is not yet implemented in this build.</p>';
        body.appendChild(card);
    }

    function _newsCard(item) {
        const esc = GIQ.fmt.esc;
        const card = document.createElement('div');
        card.className = 'news-card';

        const head = document.createElement('div');
        head.className = 'news-card-head';
        const a = document.createElement('a');
        a.className = 'news-card-title';
        a.href = item.reddit_url || '#';
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = item.title || '—';
        head.appendChild(a);
        card.appendChild(head);

        const meta = document.createElement('div');
        meta.className = 'news-card-meta mono muted';
        const parts = [];
        if (item.subreddit) parts.push('r/' + item.subreddit);
        if (item.score != null) parts.push(item.score + ' pts');
        if (item.num_comments != null) parts.push(item.num_comments + ' comments');
        if (item.age_hours != null) parts.push(item.age_hours + 'h ago');
        if (item.domain) parts.push(item.domain);
        meta.textContent = parts.join(' · ');
        card.appendChild(meta);

        /* Tags */
        if (item.is_fresh || item.parsed_tag) {
            const tags = document.createElement('div');
            tags.className = 'news-card-tags';
            if (item.is_fresh) {
                const c = document.createElement('span');
                c.className = 'news-tag news-tag-fresh';
                c.textContent = 'FRESH';
                tags.appendChild(c);
            }
            if (item.parsed_tag && item.parsed_tag !== 'FRESH') {
                const c = document.createElement('span');
                c.className = 'news-tag';
                c.textContent = item.parsed_tag;
                tags.appendChild(c);
            }
            card.appendChild(tags);
        }

        /* Relevance reasons */
        if (item.relevance_reasons && item.relevance_reasons.length) {
            const reasons = document.createElement('div');
            reasons.className = 'news-card-reasons';
            item.relevance_reasons.forEach(r => {
                const chip = document.createElement('span');
                chip.className = 'news-reason';
                const label = r === 'artist_match' ? 'Artist you like'
                    : r === 'genre_match' ? 'Genre match'
                    : r === 'fresh' ? 'New release'
                    : r === 'high_engagement' ? 'Trending'
                    : r;
                chip.textContent = label;
                reasons.appendChild(chip);
            });
            card.appendChild(reasons);
        }

        /* Footer: relevance score + external link */
        const foot = document.createElement('div');
        foot.className = 'news-card-foot';
        if (item.relevance_score != null) {
            const sc = document.createElement('span');
            sc.className = 'mono muted';
            sc.textContent = 'relevance ' + Number(item.relevance_score).toFixed(2);
            foot.appendChild(sc);
        }
        if (item.url && item.url !== item.reddit_url) {
            const ext = document.createElement('a');
            ext.className = 'vc-btn vc-btn-sm';
            ext.href = item.url;
            ext.target = '_blank';
            ext.rel = 'noopener';
            ext.textContent = 'Open ↗';
            foot.appendChild(ext);
        }
        card.appendChild(foot);
        return card;
    }
})();
