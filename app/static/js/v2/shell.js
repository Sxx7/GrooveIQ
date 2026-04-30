/* shell.js — sidebar (logo, nav, activity pill, API key block, search) and topbar (subnav + SSE pill).
 * Renders into #app. Re-renders the topbar on route changes; the sidebar persists.
 */

(function () {
    const COLLAPSE_KEY = 'groove.nav.collapsed';

    function loadCollapsed() {
        try { return localStorage.getItem(COLLAPSE_KEY) === '1'; }
        catch (_) { return false; }
    }

    function saveCollapsed(v) {
        try { localStorage.setItem(COLLAPSE_KEY, v ? '1' : '0'); }
        catch (_) { /* ignore */ }
    }

    function navItemHTML(bucket, active, collapsed) {
        const label = GIQ.router.BUCKET_LABELS[bucket];
        const icon = GIQ.router.BUCKET_ICONS[bucket];
        const count = GIQ.router.SUBPAGES[bucket].length;
        const cls = 'nav-item' + (active ? ' active' : '');
        return '<button class="' + cls + '" data-bucket="' + bucket + '"'
            + (collapsed ? ' title="' + GIQ.fmt.esc(label) + '"' : '')
            + '>'
            + '<span class="nav-icon">' + GIQ.fmt.esc(icon) + '</span>'
            + '<span class="nav-label">' + GIQ.fmt.esc(label) + '</span>'
            + '<span class="nav-count">' + count + '</span>'
            + '</button>';
    }

    function effectiveCollapsed() {
        if (typeof window !== 'undefined' && window.innerWidth < 1100) return true;
        return !!GIQ.state.sidebarCollapsed;
    }

    function renderSidebar() {
        const collapsed = effectiveCollapsed();
        const aside = document.querySelector('.sidebar');
        if (!aside) return;
        aside.classList.toggle('collapsed', collapsed);

        const current = GIQ.state.currentBucket || 'monitor';
        let html = '';

        // Header (logo + collapse toggle)
        html += '<div class="sidebar-head">';
        if (collapsed) {
            html += '<div class="logo">g</div>';
        } else {
            html += '<div class="logo">groove<span class="logo-accent">iq</span></div>';
            html += '<button class="sidebar-toggle" data-action="collapse" aria-label="Collapse sidebar">«</button>';
        }
        html += '</div>';
        if (collapsed) {
            html += '<button class="sidebar-toggle-collapsed" data-action="expand" aria-label="Expand sidebar">»</button>';
        }

        // Nav
        html += '<nav class="nav">';
        for (const b of GIQ.router.BUCKETS) {
            html += navItemHTML(b, b === current, collapsed);
        }
        html += '</nav>';

        // Spacer
        html += '<div class="sidebar-spacer"></div>';

        // Activity pill — content driven by GIQ.activity (session 02+)
        html += '<button class="activity-pill idle" data-count="0" aria-label="Activity">'
            + '<span class="idle-dot"></span>';
        if (!collapsed) {
            html += '<div class="activity-pill-body">'
                + '<div class="activity-pill-title">idle</div>'
                + '<div class="activity-pill-sub">no active jobs</div>'
                + '</div>'
                + '<span class="activity-pill-chevron">▾</span>';
        }
        html += '</button>';

        // API key block
        html += renderApiKeyBlock();

        // Search row (placeholder)
        html += '<button class="search-row" data-action="search" disabled>'
            + '<span class="search-icon">⌕</span>'
            + '<span class="search-label">Search</span>'
            + '<span class="search-kbd">⌘K</span>'
            + '</button>';

        aside.innerHTML = html;
        wireSidebar(aside);
        if (GIQ.activity?.rebind) GIQ.activity.rebind();
    }

    function renderApiKeyBlock() {
        const k = GIQ.state.apiKey || '';
        const valid = GIQ.state.apiKeyValid;
        const status = !k
            ? '<span class="apikey-status">not connected</span>'
            : (valid
                ? '<span class="apikey-status connected">connected</span>'
                : '<span class="apikey-status error">invalid</span>');
        return '<div class="apikey-block">'
            + '<div class="apikey-row">'
            + '<input type="password" class="apikey-input" placeholder="API key" value="'
                + GIQ.fmt.esc(k) + '" autocomplete="off" spellcheck="false">'
            + '<button class="apikey-btn" data-action="connect">Connect</button>'
            + '</div>'
            + status
            + '</div>';
    }

    function wireSidebar(aside) {
        aside.querySelectorAll('[data-bucket]').forEach(btn => {
            btn.addEventListener('click', () => {
                GIQ.router.navigate(btn.dataset.bucket);
            });
        });
        aside.querySelector('[data-action="collapse"]')?.addEventListener('click', () => {
            GIQ.state.sidebarCollapsed = true;
            saveCollapsed(true);
            renderSidebar();
        });
        aside.querySelector('[data-action="expand"]')?.addEventListener('click', () => {
            GIQ.state.sidebarCollapsed = false;
            saveCollapsed(false);
            renderSidebar();
        });
        aside.querySelector('[data-action="search"]')?.addEventListener('click', () => {
            GIQ.toast('Search palette — planned (⌘K)', 'info');
        });
        const input = aside.querySelector('.apikey-input');
        const btn = aside.querySelector('[data-action="connect"]');
        if (btn && input) {
            const submit = async () => {
                const k = input.value.trim();
                if (!k) {
                    GIQ.apiKey.clear();
                    GIQ.state.apiKeyValid = false;
                    if (GIQ.sse?.disconnect) GIQ.sse.disconnect();
                    if (GIQ.activity?.refresh) GIQ.activity.refresh();
                    GIQ.toast('API key cleared', 'info');
                    renderSidebar();
                    GIQ.router.dispatch();
                    return;
                }
                GIQ.apiKey.save(k);
                btn.disabled = true;
                btn.textContent = '…';
                const ok = await GIQ.api.validateKey();
                GIQ.state.apiKeyValid = ok;
                btn.disabled = false;
                btn.textContent = 'Connect';
                if (ok) {
                    GIQ.toast('Connected', 'success');
                    if (GIQ.sse?.connect) GIQ.sse.connect();
                    if (GIQ.activity?.refresh) GIQ.activity.refresh();
                } else {
                    GIQ.toast('Health check failed — server unreachable or down', 'error');
                    if (GIQ.sse?.disconnect) GIQ.sse.disconnect();
                }
                renderSidebar();
                GIQ.router.dispatch();
            };
            btn.addEventListener('click', submit);
            input.addEventListener('keydown', e => {
                if (e.key === 'Enter') submit();
            });
        }
    }

    function renderTopbar() {
        const top = document.querySelector('.topbar');
        if (!top) return;
        const bucket = GIQ.state.currentBucket || 'monitor';
        const sub = GIQ.state.currentSubpage;
        const subs = GIQ.router.SUBPAGES[bucket] || [];

        let html = '';
        for (const sp of subs) {
            const cls = 'subnav-tab' + (sp === sub ? ' active' : '');
            const label = GIQ.router.SUBPAGE_LABELS[sp] || sp;
            html += '<button class="' + cls + '" data-subpage="' + sp + '">'
                + GIQ.fmt.esc(label) + '</button>';
        }
        html += '<div class="topbar-spacer"></div>';
        html += '<div class="sse-pill' + (GIQ.state.sseConnected ? ' live' : '') + '">'
            + '<span class="sse-dot"></span>'
            + (GIQ.state.sseConnected ? 'SSE live' : 'SSE off')
            + '</div>';

        top.innerHTML = html;
        top.querySelectorAll('[data-subpage]').forEach(btn => {
            btn.addEventListener('click', () => {
                GIQ.router.navigate(bucket, btn.dataset.subpage);
            });
        });
    }

    function renderBottomTabbar() {
        let bar = document.querySelector('.bottom-tabbar');
        if (!bar) {
            bar = document.createElement('nav');
            bar.className = 'bottom-tabbar';
            document.body.appendChild(bar);
        }
        const current = GIQ.state.currentBucket || 'monitor';
        let html = '';
        for (const b of GIQ.router.BUCKETS) {
            const cls = 'bottom-tabbar-item' + (b === current ? ' active' : '');
            html += '<button class="' + cls + '" data-bucket="' + b + '">'
                + '<span class="bottom-tabbar-icon">' + GIQ.fmt.esc(GIQ.router.BUCKET_ICONS[b]) + '</span>'
                + '<span class="bottom-tabbar-label">' + GIQ.fmt.esc(GIQ.router.BUCKET_LABELS[b]) + '</span>'
                + '</button>';
        }
        bar.innerHTML = html;
        bar.querySelectorAll('[data-bucket]').forEach(btn => {
            btn.addEventListener('click', () => {
                GIQ.router.navigate(btn.dataset.bucket);
            });
        });
    }

    function renderActivityFab() {
        let fab = document.querySelector('.mobile-activity-fab');
        if (!fab) {
            fab = document.createElement('button');
            fab.className = 'mobile-activity-fab idle';
            fab.setAttribute('aria-label', 'Activity');
            fab.setAttribute('data-count', '0');
            fab.innerHTML = '<span class="idle-dot"></span>'
                + '<span class="pulse-dot"></span>'
                + '<span class="mobile-activity-fab-icon">⚡</span>'
                + '<span class="mobile-activity-fab-count">0</span>';
            document.body.appendChild(fab);
        }
        if (GIQ.activity?.rebind) GIQ.activity.rebind();
    }

    function applyResponsive() {
        const aside = document.querySelector('.sidebar');
        if (!aside) return;
        const wantsCollapsed = effectiveCollapsed();
        const showsExpandedLogo = !!aside.querySelector('.logo > .logo-accent');
        // Re-render only when the rendered state mismatches the desired state.
        if (wantsCollapsed && showsExpandedLogo) renderSidebar();
        else if (!wantsCollapsed && !showsExpandedLogo) renderSidebar();
        else aside.classList.toggle('collapsed', wantsCollapsed);
    }

    function renderShell() {
        const app = document.getElementById('app');
        if (!app) return;
        if (!app.querySelector('.sidebar')) {
            app.innerHTML = '<aside class="sidebar"></aside>'
                + '<div class="main">'
                + '<div class="topbar"></div>'
                + '<div class="page-scroll" id="page-root"></div>'
                + '</div>';
        }
        renderSidebar();
        renderTopbar();
        renderBottomTabbar();
        renderActivityFab();
        applyResponsive();
    }

    let _resizeBound = false;
    function bindResize() {
        if (_resizeBound) return;
        _resizeBound = true;
        let raf = 0;
        window.addEventListener('resize', () => {
            if (raf) cancelAnimationFrame(raf);
            raf = requestAnimationFrame(() => {
                applyResponsive();
                raf = 0;
            });
        });
    }

    GIQ.shell = {
        init() {
            GIQ.state.sidebarCollapsed = loadCollapsed();
            renderShell();
            bindResize();
        },
        render: renderShell,
        renderTopbar,
        renderSidebar,
        renderBottomTabbar,
        applyResponsive,
    };
})();
