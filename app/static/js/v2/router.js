/* router.js — hash-based routing.
 * Hash format: #/<bucket>/<subpage>. Empty hash redirects to #/monitor/overview.
 * Each bucket has a default subpage when only the bucket is present.
 */

(function () {
    const BUCKETS = ['explore', 'actions', 'monitor', 'settings'];

    const SUBPAGES = {
        explore: [
            'recommendations', 'radio', 'playlists', 'tracks',
            'text-search', 'music-map', 'charts', 'artists', 'news',
        ],
        actions: [
            'pipeline-ml', 'library', 'discovery', 'charts', 'downloads',
        ],
        monitor: [
            'overview', 'pipeline', 'models', 'system-health', 'recs-debug',
            'user-diagnostics', 'integrations', 'downloads', 'lidarr-backfill',
            'discovery', 'charts',
        ],
        settings: [
            'algorithm', 'download-routing', 'lidarr-backfill',
            'connections', 'users', 'onboarding',
        ],
    };

    const DEFAULTS = {
        explore: 'recommendations',
        actions: 'pipeline-ml',
        monitor: 'overview',
        settings: 'algorithm',
    };

    const SUBPAGE_LABELS = {
        // Explore
        'recommendations': 'Recommendations',
        'radio': 'Radio',
        'playlists': 'Playlists',
        'tracks': 'Tracks',
        'text-search': 'Text Search',
        'music-map': 'Music Map',
        'artists': 'Artists',
        'news': 'News',
        // Actions
        'pipeline-ml': 'Pipeline & ML',
        'library': 'Library',
        'discovery': 'Discovery',
        'downloads': 'Downloads',
        // Monitor (overrides above where collisions exist within a bucket)
        'overview': 'Overview',
        'pipeline': 'Pipeline',
        'models': 'Models',
        'system-health': 'System Health',
        'recs-debug': 'Recs Debug',
        'user-diagnostics': 'User Diagnostics',
        'integrations': 'Integrations',
        'lidarr-backfill': 'Lidarr Backfill',
        // Settings
        'algorithm': 'Algorithm',
        'download-routing': 'Download Routing',
        'connections': 'Connections',
        'users': 'Users',
        'onboarding': 'Onboarding',
        // Shared (Charts in Explore/Actions/Monitor)
        'charts': 'Charts',
    };

    const BUCKET_LABELS = {
        explore: 'Explore',
        actions: 'Actions',
        monitor: 'Monitor',
        settings: 'Settings',
    };

    // U+FE0E forces text presentation so glyphs with emoji defaults
    // (notably ⚡) render in the inherited text colour, not as colour emoji.
    const BUCKET_ICONS = {
        explore: '♪',
        actions: '⚡︎',
        monitor: '◉',
        settings: '⚙',
    };

    let _cleanup = null;

    function parseHash() {
        const raw = (window.location.hash || '').replace(/^#\/?/, '').trim();
        if (!raw) return null;
        const [pathPart, queryPart] = raw.split('?');
        const parts = pathPart.split('/').filter(Boolean);
        const bucket = parts[0];
        const subpage = parts[1];
        const tail = parts.slice(2);
        const params = {};
        if (queryPart) {
            queryPart.split('&').forEach(pair => {
                const [k, v] = pair.split('=');
                if (k) params[decodeURIComponent(k)] = v == null ? '' : decodeURIComponent(v.replace(/\+/g, ' '));
            });
        }
        if (tail.length) params._tail = tail.map(s => decodeURIComponent(s));
        return { bucket, subpage, params };
    }

    function resolve(parsed) {
        if (!parsed) return { bucket: 'monitor', subpage: 'overview' };
        const { bucket, subpage } = parsed;
        if (!BUCKETS.includes(bucket)) {
            return { bucket: 'monitor', subpage: 'overview' };
        }
        const list = SUBPAGES[bucket];
        const sp = subpage && list.includes(subpage) ? subpage : DEFAULTS[bucket];
        return { bucket, subpage: sp };
    }

    function dispatch() {
        const parsed = parseHash();
        const { bucket, subpage } = resolve(parsed);
        const params = (parsed && parsed.params) || {};
        GIQ.state.currentBucket = bucket;
        GIQ.state.currentSubpage = subpage;
        GIQ.state.currentParams = params;

        if (typeof _cleanup === 'function') {
            try { _cleanup(); } catch (e) { console.error('cleanup failed', e); }
            _cleanup = null;
        }

        if (typeof GIQ.shell?.render === 'function') GIQ.shell.render();

        const root = document.getElementById('page-root');
        if (!root) return;
        root.innerHTML = '';

        const renderer = GIQ.pages[bucket] && GIQ.pages[bucket][subpage];
        if (typeof renderer === 'function') {
            try {
                _cleanup = renderer(root, params) || null;
            } catch (e) {
                console.error('Page render error', e);
                root.innerHTML = '<div class="page-message"><h2>Render error</h2><p>'
                    + GIQ.fmt.esc(e.message) + '</p></div>';
            }
        } else {
            root.innerHTML = '<div class="page-stub"><div class="eyebrow">'
                + GIQ.fmt.esc(BUCKET_LABELS[bucket].toUpperCase()) + '</div>'
                + '<h1>' + GIQ.fmt.esc(SUBPAGE_LABELS[subpage] || subpage) + '</h1>'
                + '<p class="muted">Stub — no renderer registered.</p></div>';
        }
    }

    function navigate(bucket, subpage, params) {
        const sp = subpage || DEFAULTS[bucket] || '';
        /* `subpage` may include trailing path segments — e.g. "playlists/123" — passed
         * verbatim. We do not validate them against SUBPAGES; resolve() trims to the
         * first segment. */
        let next = '#/' + bucket + (sp ? '/' + sp : '');
        if (params && typeof params === 'object') {
            const qs = Object.keys(params)
                .filter(k => k !== '_tail' && params[k] != null && params[k] !== '')
                .map(k => encodeURIComponent(k) + '=' + encodeURIComponent(params[k]))
                .join('&');
            if (qs) next += '?' + qs;
        }
        if (window.location.hash === next) {
            dispatch();
            return;
        }
        window.location.hash = next;
    }

    GIQ.router = {
        BUCKETS, SUBPAGES, DEFAULTS,
        BUCKET_LABELS, BUCKET_ICONS, SUBPAGE_LABELS,
        parseHash, resolve, dispatch, navigate,
        get cleanup() { return _cleanup; },
    };

    window.addEventListener('hashchange', dispatch);
})();
