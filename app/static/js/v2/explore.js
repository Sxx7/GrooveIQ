/* explore.js — Explore bucket pages.
 *
 * Session 09 lands:
 *   - GIQ.pages.explore.recommendations
 *   - GIQ.pages.explore.tracks
 * Session 10 lands:
 *   - GIQ.pages.explore.playlists       (list + #/explore/playlists/{id} detail)
 *   - GIQ.pages.explore['text-search']
 *   - GIQ.pages.explore['music-map']
 *
 * Other Explore sub-pages (radio, charts, artists, news) keep stub renderers —
 * session 11 fills them.
 */

(function () {
    GIQ.pages.explore = GIQ.pages.explore || {};

    const STUBS = ['radio', 'charts', 'artists', 'news'];
    STUBS.forEach(sp => {
        GIQ.pages.explore[sp] = function (root) {
            const label = GIQ.router.SUBPAGE_LABELS[sp] || sp;
            root.innerHTML = '<div class="page-stub">'
                + '<div class="eyebrow">EXPLORE</div>'
                + '<h1>' + GIQ.fmt.esc(label) + '</h1>'
                + '<p class="muted">Page: explore → ' + GIQ.fmt.esc(sp) + ' — TBD</p>'
                + '</div>';
        };
    });

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
                + GIQ.fmt.esc(s.sortBy) + ' ' + (s.sortDir === 'asc' ? '↑' : '↓')
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

        const isMobile = window.innerWidth < 700;
        if (isMobile) {
            const notice = document.createElement('div');
            notice.className = 'mm-mobile-notice';
            notice.textContent = 'Music Map is best viewed on desktop. '
                + 'Pinch-zoom and tap-to-select are supported but limited.';
            body.appendChild(notice);
        }

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
})();
