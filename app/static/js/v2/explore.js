/* explore.js — Explore bucket pages.
 *
 * Session 09 lands:
 *   - GIQ.pages.explore.recommendations
 *   - GIQ.pages.explore.tracks
 *
 * Other Explore sub-pages (radio, playlists, text-search, music-map,
 * charts, artists, news) keep stub renderers — sessions 10/11 fill them.
 */

(function () {
    GIQ.pages.explore = GIQ.pages.explore || {};

    const STUBS = ['radio', 'playlists', 'text-search', 'music-map', 'charts', 'artists', 'news'];
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
            GIQ.toast('Generate Playlist modal — session 10', 'info');
        });

        load();

        return function cleanup() { /* state persists in GIQ.state.trackList */ };
    };
})();
