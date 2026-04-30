/* activity.js — Activity pill: client-side aggregator that polls
 * pipeline / scan / downloads / lidarr-backfill, plus a 320px popover
 * with deep-links to Monitor surfaces.
 */

(function () {
    let pollTimer = null;
    let lastSnap = { jobs: [], at: 0 };
    let openPopover = null;
    let outsideHandler = null;
    let escHandler = null;
    let renderToken = 0;
    let sseUnsubs = [];

    const POLL_MS = 5000;

    function summarizeJobs(jobs) {
        if (!jobs.length) return { title: 'idle', sub: 'no active jobs' };
        const parts = [];
        for (const j of jobs) parts.push(j.shortLabel);
        const sub = parts.slice(0, 4).join(' · ') + (parts.length > 4 ? ' …' : '');
        return {
            title: jobs.length + (jobs.length === 1 ? ' active' : ' active'),
            sub,
        };
    }

    async function poll() {
        const token = ++renderToken;
        const jobs = [];
        const apiKey = GIQ.state.apiKey;
        if (!apiKey) {
            lastSnap = { jobs: [], at: Date.now() };
            renderPill();
            return;
        }

        const tasks = [
            GIQ.api.get('/v1/pipeline/status?limit=1').catch(() => null),
            GIQ.api.get('/v1/stats').catch(() => null),
            GIQ.api.get('/v1/downloads/queue?recent_limit=0&in_flight_limit=50').catch(() => null),
            GIQ.api.get('/v1/lidarr-backfill/stats').catch(() => null),
        ];
        const [pipeline, stats, dlq, lbf] = await Promise.all(tasks);
        if (token !== renderToken) return;

        if (pipeline && pipeline.current && pipeline.current.status === 'running') {
            const r = pipeline.current;
            const steps = r.steps || [];
            const total = steps.length || 10;
            let runningStep = null;
            let completed = 0;
            for (const s of steps) {
                if (s.status === 'completed') completed++;
                if (s.status === 'running') runningStep = s;
            }
            const stepIdx = runningStep
                ? steps.findIndex(s => s.name === runningStep.name) + 1
                : completed + 1;
            const stepLabel = runningStep
                ? (runningStep.name || 'running')
                : 'preparing';
            jobs.push({
                key: 'pipeline',
                icon: '◉',
                label: 'Pipeline run',
                sub: 'step ' + stepIdx + ' of ' + total + ' · ' + stepLabel,
                shortLabel: 'pipeline',
                live: true,
                href: '#/monitor/pipeline',
            });
        }

        if (stats && stats.latest_scan && stats.latest_scan.status === 'running') {
            const s = stats.latest_scan;
            const pct = (s.percent_complete || 0).toFixed(0);
            const proc = (s.files_analyzed || 0) + (s.files_skipped || 0) + (s.files_failed || 0);
            jobs.push({
                key: 'scan',
                icon: '⌕',
                label: 'Library scan',
                sub: pct + '% · ' + proc + ' / ' + (s.files_found || 0),
                shortLabel: 'scan',
                live: true,
                href: '#/monitor/system-health',
            });
        }

        const inFlight = dlq && Array.isArray(dlq.in_flight) ? dlq.in_flight.length : 0;
        if (inFlight > 0) {
            jobs.push({
                key: 'downloads',
                icon: '↓',
                label: inFlight + (inFlight === 1 ? ' download' : ' downloads'),
                sub: 'in flight',
                shortLabel: inFlight + ' dl',
                live: false,
                href: '#/monitor/downloads',
            });
        }

        if (lbf && lbf.enabled && lbf.tick_in_progress) {
            jobs.push({
                key: 'backfill',
                icon: '⚡',
                label: 'Lidarr backfill',
                sub: 'tick in progress',
                shortLabel: 'backfill',
                live: true,
                href: '#/monitor/lidarr-backfill',
            });
        }

        lastSnap = { jobs, at: Date.now() };
        renderPill();
        if (openPopover) renderPopover();
    }

    function renderPill() {
        const pill = document.querySelector('.activity-pill');
        const fab = document.querySelector('.mobile-activity-fab');

        const { jobs } = lastSnap;
        const collapsed = GIQ.state.sidebarCollapsed;
        const count = jobs.length;
        const sum = summarizeJobs(jobs);

        if (pill) {
            const dot = pill.querySelector('.pulse-dot, .idle-dot');
            if (count > 0) {
                if (dot) {
                    dot.classList.remove('idle-dot');
                    dot.classList.add('pulse-dot');
                }
                pill.classList.remove('idle');
            } else {
                if (dot) {
                    dot.classList.remove('pulse-dot');
                    dot.classList.add('idle-dot');
                }
                pill.classList.add('idle');
            }

            pill.setAttribute('data-count', String(count));

            if (!collapsed) {
                const titleEl = pill.querySelector('.activity-pill-title');
                const subEl = pill.querySelector('.activity-pill-sub');
                if (titleEl) titleEl.textContent = sum.title;
                if (subEl) subEl.textContent = sum.sub;
            }
        }

        if (fab) {
            fab.classList.toggle('idle', count === 0);
            fab.setAttribute('data-count', String(count));
            const countEl = fab.querySelector('.mobile-activity-fab-count');
            if (countEl) countEl.textContent = String(count);
        }
    }

    function bind() {
        const pill = document.querySelector('.activity-pill');
        if (pill && pill.dataset.bound !== '1') {
            pill.dataset.bound = '1';
            pill.addEventListener('click', (e) => {
                e.stopPropagation();
                togglePopover();
            });
        }
        const fab = document.querySelector('.mobile-activity-fab');
        if (fab && fab.dataset.bound !== '1') {
            fab.dataset.bound = '1';
            fab.addEventListener('click', (e) => {
                e.stopPropagation();
                togglePopover();
            });
        }
    }

    function togglePopover() {
        if (openPopover) closePopover();
        else openPopoverEl();
    }

    function getAnchor() {
        // Prefer the visible FAB on mobile, fallback to the sidebar pill.
        const fab = document.querySelector('.mobile-activity-fab');
        if (fab && fab.offsetParent !== null) return { el: fab, mobile: true };
        const pill = document.querySelector('.activity-pill');
        if (pill && pill.offsetParent !== null) return { el: pill, mobile: false };
        return null;
    }

    function openPopoverEl() {
        const anchor = getAnchor();
        if (!anchor) return;
        const pop = document.createElement('div');
        pop.className = 'activity-popover';
        if (anchor.mobile) pop.classList.add('mobile-popover');
        document.body.appendChild(pop);
        openPopover = pop;
        positionPopover(pop, anchor);
        renderPopover();

        outsideHandler = (e) => {
            if (!openPopover) return;
            if (openPopover.contains(e.target)) return;
            if (anchor.el.contains(e.target)) return;
            closePopover();
        };
        escHandler = (e) => {
            if (e.key === 'Escape') closePopover();
        };
        setTimeout(() => {
            document.addEventListener('mousedown', outsideHandler, true);
            document.addEventListener('keydown', escHandler);
        }, 0);

        window.addEventListener('resize', repositionPopover);
        window.addEventListener('scroll', repositionPopover, true);
    }

    function repositionPopover() {
        const anchor = getAnchor();
        if (!anchor || !openPopover) return;
        positionPopover(openPopover, anchor);
    }

    function positionPopover(pop, anchor) {
        if (anchor.mobile) {
            // CSS handles positioning via .mobile-popover.
            pop.style.position = '';
            pop.style.width = '';
            pop.style.top = '';
            pop.style.left = '';
            pop.style.transform = '';
            return;
        }
        const r = anchor.el.getBoundingClientRect();
        pop.style.position = 'fixed';
        pop.style.width = '320px';
        const top = r.top - 8;
        pop.style.left = (r.left) + 'px';
        pop.style.top = top + 'px';
        pop.style.transform = 'translateY(-100%)';
        const margin = 8;
        const vw = window.innerWidth;
        if (r.left + 320 + margin > vw) {
            pop.style.left = (vw - 320 - margin) + 'px';
        }
    }

    function renderPopover() {
        if (!openPopover) return;
        const { jobs } = lastSnap;
        let html = '<div class="activity-popover-head">'
            + '<div class="activity-popover-title">Active jobs</div>'
            + '<button class="activity-popover-close" type="button" aria-label="Close">×</button>'
            + '</div>';
        if (!jobs.length) {
            html += '<div class="activity-popover-empty">No jobs running.</div>';
        } else {
            html += '<div class="activity-popover-list">';
            for (const j of jobs) {
                html += '<a class="activity-popover-row" href="' + GIQ.fmt.esc(j.href) + '">'
                    + '<span class="activity-popover-icon">' + GIQ.fmt.esc(j.icon) + '</span>'
                    + '<div class="activity-popover-row-body">'
                    + '<div class="activity-popover-row-title">'
                    + GIQ.fmt.esc(j.label)
                    + (j.live
                        ? '<span class="live-badge"><span class="live-dot"></span>LIVE</span>'
                        : '')
                    + '</div>'
                    + '<div class="activity-popover-row-sub">' + GIQ.fmt.esc(j.sub) + '</div>'
                    + '</div>'
                    + '<span class="activity-popover-arrow">View →</span>'
                    + '</a>';
            }
            html += '</div>';
        }
        openPopover.innerHTML = html;
        openPopover.querySelector('.activity-popover-close')?.addEventListener('click', closePopover);
        openPopover.querySelectorAll('.activity-popover-row').forEach(a => {
            a.addEventListener('click', () => {
                setTimeout(closePopover, 0);
            });
        });
    }

    function closePopover() {
        if (!openPopover) return;
        if (openPopover.parentNode) openPopover.parentNode.removeChild(openPopover);
        openPopover = null;
        if (outsideHandler) {
            document.removeEventListener('mousedown', outsideHandler, true);
            outsideHandler = null;
        }
        if (escHandler) {
            document.removeEventListener('keydown', escHandler);
            escHandler = null;
        }
        window.removeEventListener('resize', repositionPopover);
        window.removeEventListener('scroll', repositionPopover, true);
    }

    function start() {
        bind();
        if (pollTimer) return;
        poll();
        pollTimer = setInterval(poll, POLL_MS);
        if (!sseUnsubs.length) {
            const refresh = () => poll();
            sseUnsubs.push(GIQ.sse.subscribe('pipeline_start', refresh));
            sseUnsubs.push(GIQ.sse.subscribe('pipeline_end', refresh));
            sseUnsubs.push(GIQ.sse.subscribe('step_complete', refresh));
            sseUnsubs.push(GIQ.sse.subscribe('step_failed', refresh));
        }
    }

    function stop() {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = null;
        sseUnsubs.forEach(u => u && u());
        sseUnsubs = [];
        closePopover();
    }

    function refresh() { poll(); }
    function rebind() { bind(); renderPill(); }

    GIQ.activity = { start, stop, refresh, poll, rebind, _snap: () => lastSnap };
})();
