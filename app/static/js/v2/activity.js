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
    let sseUnsubs = [];

    const POLL_MS = 5000;
    // Per-task client timeout: if any one endpoint takes longer than this,
    // the slot keeps its previous value rather than blocking the whole pill.
    // Server-side endpoints all aim for sub-200 ms; 4 s leaves headroom for
    // a degraded backend without flickering during normal hiccups.
    const TASK_TIMEOUT_MS = 4000;

    // Persistent per-slot state across poll cycles. A new poll's responses
    // overwrite their slot; if a poll's task times out without responding,
    // the slot retains its previous value so a single slow endpoint can't
    // blank the tile (issue #97 dashboard-resilience follow-up).
    const slotState = { pipeline: null, scan: null, downloads: null, backfill: null };
    // Per-slot inflight token: if a newer poll has issued a request for the
    // same slot, drop the older response when it eventually arrives so it
    // can't overwrite fresher state.
    const slotInflight = { pipeline: 0, scan: 0, downloads: 0, backfill: 0 };

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

    function commitSnapshot() {
        const jobs = [
            slotState.pipeline,
            slotState.scan,
            slotState.downloads,
            slotState.backfill,
        ].filter(Boolean);
        lastSnap = { jobs, at: Date.now() };
        renderPill();
        if (openPopover) renderPopover();
    }

    function runTask(key, fn) {
        const myId = ++slotInflight[key];
        let settled = false;
        const timer = setTimeout(() => {
            // Timeout: leave slotState[key] untouched. The previous value
            // (last successful response) keeps showing rather than the slot
            // blinking to empty during a transient stall.
            if (slotInflight[key] === myId) settled = true;
        }, TASK_TIMEOUT_MS);
        Promise.resolve().then(fn).then(
            (job) => {
                if (settled) return;
                if (slotInflight[key] !== myId) return;  // a newer poll won the race
                settled = true;
                clearTimeout(timer);
                slotState[key] = job || null;
                commitSnapshot();
            },
            () => {
                if (settled) return;
                if (slotInflight[key] !== myId) return;
                settled = true;
                clearTimeout(timer);
                slotState[key] = null;
                commitSnapshot();
            },
        );
    }

    function poll() {
        const apiKey = GIQ.state.apiKey;
        if (!apiKey) {
            slotState.pipeline = null;
            slotState.scan = null;
            slotState.downloads = null;
            slotState.backfill = null;
            commitSnapshot();
            return;
        }

        runTask('pipeline', async () => {
            const pipeline = await GIQ.api.get('/v1/pipeline/status?limit=1');
            if (!pipeline || !pipeline.current || pipeline.current.status !== 'running') return null;
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
            const stepLabel = runningStep ? (runningStep.name || 'running') : 'preparing';
            return {
                key: 'pipeline',
                icon: '◉',
                label: 'Pipeline run',
                sub: 'step ' + stepIdx + ' of ' + total + ' · ' + stepLabel,
                shortLabel: 'pipeline',
                live: true,
                href: '#/monitor/pipeline',
            };
        });

        runTask('scan', async () => {
            const stats = await GIQ.api.get('/v1/stats');
            if (!stats || !stats.latest_scan || stats.latest_scan.status !== 'running') return null;
            const s = stats.latest_scan;
            const pct = (s.percent_complete || 0).toFixed(0);
            const proc = (s.files_analyzed || 0) + (s.files_skipped || 0) + (s.files_failed || 0);
            return {
                key: 'scan',
                icon: '⌕',
                label: 'Library scan',
                sub: pct + '% · ' + proc + ' / ' + (s.files_found || 0),
                shortLabel: 'scan',
                live: true,
                href: '#/monitor/system-health',
            };
        });

        runTask('downloads', async () => {
            const dlq = await GIQ.api.get('/v1/downloads/queue?recent_limit=0&in_flight_limit=50');
            const inFlight = dlq && Array.isArray(dlq.in_flight) ? dlq.in_flight.length : 0;
            if (inFlight === 0) return null;
            return {
                key: 'downloads',
                icon: '↓',
                label: inFlight + (inFlight === 1 ? ' download' : ' downloads'),
                sub: 'in flight',
                shortLabel: inFlight + ' dl',
                live: false,
                href: '#/monitor/downloads',
            };
        });

        runTask('backfill', async () => {
            const lbf = await GIQ.api.get('/v1/lidarr-backfill/stats');
            if (!lbf || !lbf.enabled || !lbf.tick_in_progress) return null;
            return {
                key: 'backfill',
                icon: '⚡',
                label: 'Lidarr backfill',
                sub: 'tick in progress',
                shortLabel: 'backfill',
                live: true,
                href: '#/monitor/lidarr-backfill',
            };
        });
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
