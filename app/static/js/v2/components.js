/* components.js — shared components used across pages.
 * Built session 02. Each component returns a DOM Element (not an HTML string).
 * Names: GIQ.components.{statTile, panel, liveBadge, pageHeader, rangeToggle, areaChart, sparkline}.
 * Plus GIQ.sse — single SSE bus subscribed to /v1/pipeline/stream.
 */

GIQ.components = GIQ.components || {};

/* ── Stat tile ────────────────────────────────────────────────────── */

GIQ.components.statTile = function statTile(opts) {
    const { label, value, delta, deltaKind } = opts || {};
    const el = document.createElement('div');
    el.className = 'stat-tile';
    let arrow = '';
    if (deltaKind === 'good') arrow = '↑';
    else if (deltaKind === 'bad') arrow = '↓';
    const deltaCls = deltaKind === 'good' ? 'good'
        : deltaKind === 'bad' ? 'bad'
        : 'flat';
    el.innerHTML = '<div class="eyebrow">' + GIQ.fmt.esc(label || '') + '</div>'
        + '<div class="stat-value">' + GIQ.fmt.esc(value == null ? '—' : value) + '</div>'
        + (delta
            ? '<div class="stat-delta delta-' + deltaCls + '">'
                + (arrow ? '<span class="stat-arrow">' + arrow + '</span>' : '')
                + GIQ.fmt.esc(delta) + '</div>'
            : '<div class="stat-delta delta-flat">&nbsp;</div>');
    return el;
};

/* ── Panel ────────────────────────────────────────────────────────── */

GIQ.components.panel = function panel(opts) {
    const { title, sub, action, badge, children } = opts || {};
    const el = document.createElement('section');
    el.className = 'panel';

    const head = document.createElement('div');
    head.className = 'panel-head';

    const left = document.createElement('div');
    left.className = 'panel-head-left';
    const titleRow = document.createElement('div');
    titleRow.className = 'panel-title-row';
    const titleEl = document.createElement('div');
    titleEl.className = 'panel-title';
    titleEl.textContent = title || '';
    titleRow.appendChild(titleEl);
    if (badge) {
        if (typeof badge === 'string' && badge.toUpperCase() === 'LIVE') {
            titleRow.appendChild(GIQ.components.liveBadge());
        } else if (badge instanceof Element) {
            titleRow.appendChild(badge);
        } else {
            const span = document.createElement('span');
            span.className = 'panel-badge';
            span.textContent = String(badge);
            titleRow.appendChild(span);
        }
    }
    left.appendChild(titleRow);
    if (sub) {
        const subEl = document.createElement('div');
        subEl.className = 'panel-sub';
        subEl.textContent = sub;
        left.appendChild(subEl);
    }
    head.appendChild(left);

    if (action) {
        if (action instanceof Element) {
            head.appendChild(action);
        } else if (typeof action === 'object' && action.label) {
            const a = document.createElement('button');
            a.className = 'panel-action';
            a.type = 'button';
            a.textContent = action.label;
            if (action.onClick) a.addEventListener('click', action.onClick);
            head.appendChild(a);
        } else if (typeof action === 'string') {
            const a = document.createElement('div');
            a.className = 'panel-action';
            a.textContent = action;
            head.appendChild(a);
        }
    }

    el.appendChild(head);

    const body = document.createElement('div');
    body.className = 'panel-body';
    if (children instanceof Element) body.appendChild(children);
    else if (Array.isArray(children)) children.forEach(c => { if (c instanceof Element) body.appendChild(c); });
    else if (typeof children === 'string') body.innerHTML = children;
    el.appendChild(body);
    return el;
};

/* ── LIVE badge ───────────────────────────────────────────────────── */

GIQ.components.liveBadge = function liveBadge() {
    const el = document.createElement('span');
    el.className = 'live-badge';
    el.innerHTML = '<span class="live-dot"></span>LIVE';
    return el;
};

/* ── Page header ──────────────────────────────────────────────────── */

GIQ.components.pageHeader = function pageHeader(opts) {
    const { eyebrow, title, right } = opts || {};
    const el = document.createElement('header');
    el.className = 'page-header';

    const left = document.createElement('div');
    if (eyebrow) {
        const eb = document.createElement('div');
        eb.className = 'eyebrow';
        eb.textContent = eyebrow;
        left.appendChild(eb);
    }
    if (title) {
        const t = document.createElement('h1');
        t.className = 'page-title';
        t.textContent = title;
        left.appendChild(t);
    }
    el.appendChild(left);

    if (right instanceof Element) {
        const r = document.createElement('div');
        r.className = 'page-header-right';
        r.appendChild(right);
        el.appendChild(r);
    } else if (right) {
        const r = document.createElement('div');
        r.className = 'page-header-right';
        if (Array.isArray(right)) right.forEach(c => { if (c instanceof Element) r.appendChild(c); });
        el.appendChild(r);
    }
    return el;
};

/* ── Range toggle ─────────────────────────────────────────────────── */

GIQ.components.rangeToggle = function rangeToggle(opts) {
    const { values, current, onChange } = opts || {};
    const el = document.createElement('div');
    el.className = 'range-toggle';
    let active = current;
    (values || []).forEach(v => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'range-toggle-btn' + (v === active ? ' active' : '');
        btn.textContent = v;
        btn.dataset.value = v;
        btn.addEventListener('click', () => {
            if (active === v) return;
            active = v;
            el.querySelectorAll('.range-toggle-btn').forEach(b =>
                b.classList.toggle('active', b.dataset.value === v));
            if (typeof onChange === 'function') onChange(v);
        });
        el.appendChild(btn);
    });
    return el;
};

/* ── Smooth area chart (cubic Bezier) ─────────────────────────────── */

/* Returns an inline-block element with an SVG area chart, x-axis labels,
 * and a legend. opts:
 *   series: [{ name, color, values:[number, ...], strokeWidth, fillOpacity }]
 *   labels: array of axis labels (strings) — typically ~7 items
 *   height: SVG height in user units (default 180)
 */
GIQ.components.areaChart = function areaChart(opts) {
    const series = (opts && opts.series) || [];
    const labels = (opts && opts.labels) || [];
    const w = 720;
    const h = (opts && opts.height) || 180;

    const wrap = document.createElement('div');
    wrap.className = 'area-chart';

    // empty-state
    const allEmpty = series.every(s => !s.values || s.values.length === 0
        || s.values.every(v => !v));
    if (!series.length || allEmpty) {
        const empty = document.createElement('div');
        empty.className = 'chart-empty';
        empty.textContent = (opts && opts.emptyText) || 'No data in window';
        wrap.appendChild(empty);
        return wrap;
    }

    let maxV = 0;
    for (const s of series) {
        for (const v of (s.values || [])) if (v > maxV) maxV = v;
    }
    if (maxV <= 0) maxV = 1;

    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.classList.add('area-chart-svg');

    const defs = document.createElementNS(svgNS, 'defs');
    series.forEach((s, idx) => {
        const grad = document.createElementNS(svgNS, 'linearGradient');
        const id = 'area-grad-' + idx + '-' + Math.random().toString(36).slice(2, 7);
        s._gradId = id;
        grad.setAttribute('id', id);
        grad.setAttribute('x1', '0'); grad.setAttribute('y1', '0');
        grad.setAttribute('x2', '0'); grad.setAttribute('y2', '1');
        const stop1 = document.createElementNS(svgNS, 'stop');
        stop1.setAttribute('offset', '0%');
        stop1.setAttribute('stop-color', s.color || '#a887ce');
        stop1.setAttribute('stop-opacity', String(s.fillOpacity != null ? s.fillOpacity : 0.45));
        const stop2 = document.createElementNS(svgNS, 'stop');
        stop2.setAttribute('offset', '100%');
        stop2.setAttribute('stop-color', s.color || '#a887ce');
        stop2.setAttribute('stop-opacity', '0');
        grad.appendChild(stop1);
        grad.appendChild(stop2);
        defs.appendChild(grad);
    });
    svg.appendChild(defs);

    [0.25, 0.5, 0.75].forEach(g => {
        const line = document.createElementNS(svgNS, 'line');
        line.setAttribute('x1', '0');
        line.setAttribute('y1', String(h * g));
        line.setAttribute('x2', String(w));
        line.setAttribute('y2', String(h * g));
        line.setAttribute('stroke', 'rgba(236,232,242,0.04)');
        line.setAttribute('stroke-dasharray', '2 4');
        svg.appendChild(line);
    });

    function toPath(vals) {
        const n = vals.length;
        if (n === 0) return '';
        const parts = [];
        for (let i = 0; i < n; i++) {
            const x = (i / Math.max(1, n - 1)) * w;
            const y = h - (vals[i] / maxV) * (h - 20) - 10;
            if (i === 0) {
                parts.push('M ' + x.toFixed(1) + ' ' + y.toFixed(1));
            } else {
                const px = ((i - 1) / Math.max(1, n - 1)) * w;
                const py = h - (vals[i - 1] / maxV) * (h - 20) - 10;
                const cx1 = px + (x - px) / 2;
                const cx2 = px + (x - px) / 2;
                parts.push('C ' + cx1.toFixed(1) + ' ' + py.toFixed(1)
                    + ', ' + cx2.toFixed(1) + ' ' + y.toFixed(1)
                    + ', ' + x.toFixed(1) + ' ' + y.toFixed(1));
            }
        }
        return parts.join(' ');
    }

    series.forEach(s => {
        const vals = s.values || [];
        if (!vals.length) return;
        const stroke = s.color || '#a887ce';
        const sw = s.strokeWidth != null ? s.strokeWidth : 2;
        const so = s.strokeOpacity != null ? s.strokeOpacity : 1;
        const path = toPath(vals);
        const fill = path + ' L ' + w + ' ' + h + ' L 0 ' + h + ' Z';

        const pFill = document.createElementNS(svgNS, 'path');
        pFill.setAttribute('d', fill);
        pFill.setAttribute('fill', 'url(#' + s._gradId + ')');
        svg.appendChild(pFill);

        const pStroke = document.createElementNS(svgNS, 'path');
        pStroke.setAttribute('d', path);
        pStroke.setAttribute('stroke', stroke);
        pStroke.setAttribute('stroke-width', String(sw));
        pStroke.setAttribute('stroke-opacity', String(so));
        pStroke.setAttribute('fill', 'none');
        svg.appendChild(pStroke);
    });

    wrap.appendChild(svg);

    if (labels.length) {
        const xa = document.createElement('div');
        xa.className = 'area-chart-axis';
        labels.forEach(l => {
            const sp = document.createElement('span');
            sp.textContent = l;
            xa.appendChild(sp);
        });
        wrap.appendChild(xa);
    }

    if (series.some(s => s.name)) {
        const lg = document.createElement('div');
        lg.className = 'area-chart-legend';
        series.forEach(s => {
            if (!s.name) return;
            const item = document.createElement('span');
            item.className = 'area-chart-legend-item';
            const sw = document.createElement('span');
            sw.className = 'area-chart-legend-swatch';
            sw.style.background = s.color || '#a887ce';
            item.appendChild(sw);
            item.appendChild(document.createTextNode(s.name));
            lg.appendChild(item);
        });
        wrap.appendChild(lg);
    }

    return wrap;
};

/* ── SSE bus ──────────────────────────────────────────────────────── */

(function () {
    const handlers = {};
    let abortCtrl = null;
    let connecting = false;
    let backoffMs = 1000;
    let manualDisconnect = false;

    function emit(eventName, payload) {
        const list = handlers[eventName];
        if (!list) return;
        list.slice().forEach(fn => {
            try { fn(payload); } catch (e) { console.error('SSE handler error', e); }
        });
    }

    function setConnected(v) {
        const prev = GIQ.state.sseConnected;
        GIQ.state.sseConnected = !!v;
        if (prev !== !!v) {
            emit(v ? 'connected' : 'disconnected', null);
            if (typeof GIQ.shell?.renderTopbar === 'function') GIQ.shell.renderTopbar();
        }
    }

    async function connectInner() {
        if (!GIQ.state.apiKey) {
            setConnected(false);
            return;
        }
        if (connecting) return;
        connecting = true;
        manualDisconnect = false;
        const ac = new AbortController();
        abortCtrl = ac;
        try {
            const res = await fetch(window.location.origin + '/v1/pipeline/stream', {
                headers: { 'Authorization': 'Bearer ' + GIQ.state.apiKey },
                signal: ac.signal,
            });
            if (!res.ok || !res.body) {
                setConnected(false);
                connecting = false;
                scheduleReconnect();
                return;
            }
            setConnected(true);
            backoffMs = 1000;
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buf = '';
            let eventName = 'message';
            const dataLines = [];

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buf += decoder.decode(value, { stream: true });
                const lines = buf.split('\n');
                buf = lines.pop();
                for (const line of lines) {
                    if (line.startsWith('event: ')) {
                        eventName = line.slice(7).trim();
                    } else if (line.startsWith('data: ')) {
                        dataLines.push(line.slice(6));
                    } else if (line === '' && dataLines.length) {
                        let payload = null;
                        try { payload = JSON.parse(dataLines.join('\n')); } catch (_) { /* */ }
                        emit(eventName, payload);
                        eventName = 'message';
                        dataLines.length = 0;
                    }
                }
            }
            setConnected(false);
            connecting = false;
            if (!manualDisconnect) scheduleReconnect();
        } catch (e) {
            connecting = false;
            setConnected(false);
            if (!manualDisconnect) scheduleReconnect();
        }
    }

    let reconnectTimer = null;
    function scheduleReconnect() {
        if (reconnectTimer || manualDisconnect) return;
        const delay = Math.min(30000, backoffMs);
        backoffMs = Math.min(30000, backoffMs * 2);
        reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            if (!manualDisconnect && GIQ.state.apiKey) connectInner();
        }, delay);
    }

    GIQ.sse = {
        subscribe(eventName, handler) {
            if (!handlers[eventName]) handlers[eventName] = [];
            handlers[eventName].push(handler);
            return () => {
                const list = handlers[eventName];
                if (!list) return;
                const i = list.indexOf(handler);
                if (i >= 0) list.splice(i, 1);
            };
        },
        connect() {
            manualDisconnect = false;
            if (!GIQ.state.sseConnected) connectInner();
        },
        disconnect() {
            manualDisconnect = true;
            if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
            if (abortCtrl) { try { abortCtrl.abort(); } catch (_) { } }
            abortCtrl = null;
            connecting = false;
            setConnected(false);
        },
        isConnected() { return !!GIQ.state.sseConnected; },
    };
})();

/* ── Modal ────────────────────────────────────────────────────────── */

/* Lightweight modal dialog. Returns { overlay, close }.
 *   title:  string
 *   body:   Element | string (treated as escaped text if string)
 *   footer: Element[] (buttons / links) — optional
 *   onClose: callback fired after dispose
 *   width:  'sm' | 'md' | 'lg' (default 'md')
 */
GIQ.components.modal = function modal(opts) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    const dialog = document.createElement('div');
    dialog.className = 'modal modal-' + ((opts && opts.width) || 'md');

    const head = document.createElement('div');
    head.className = 'modal-head';
    const title = document.createElement('div');
    title.className = 'modal-title';
    title.textContent = (opts && opts.title) || '';
    head.appendChild(title);

    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'modal-close';
    closeBtn.setAttribute('aria-label', 'Close');
    closeBtn.textContent = '×';
    head.appendChild(closeBtn);
    dialog.appendChild(head);

    const body = document.createElement('div');
    body.className = 'modal-body';
    if (opts && opts.body instanceof Element) {
        body.appendChild(opts.body);
    } else if (opts && typeof opts.body === 'string') {
        body.textContent = opts.body;
    }
    dialog.appendChild(body);

    if (opts && Array.isArray(opts.footer) && opts.footer.length) {
        const foot = document.createElement('div');
        foot.className = 'modal-foot';
        opts.footer.forEach(el => { if (el instanceof Element) foot.appendChild(el); });
        dialog.appendChild(foot);
    }

    overlay.appendChild(dialog);

    let closed = false;
    function close() {
        if (closed) return;
        closed = true;
        document.removeEventListener('keydown', escHandler);
        overlay.removeEventListener('click', outsideHandler);
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        if (opts && typeof opts.onClose === 'function') {
            try { opts.onClose(); } catch (e) { console.error(e); }
        }
    }
    function escHandler(e) { if (e.key === 'Escape') close(); }
    function outsideHandler(e) { if (e.target === overlay) close(); }
    closeBtn.addEventListener('click', close);
    document.addEventListener('keydown', escHandler);
    overlay.addEventListener('click', outsideHandler);

    document.body.appendChild(overlay);
    return { overlay, dialog, body, close };
};

/* ── Related rail (top of split pages) ────────────────────────────── */

GIQ.components.relatedRail = function relatedRail(opts) {
    const links = (opts && opts.links) || [];
    const el = document.createElement('div');
    el.className = 'related-rail';
    const lbl = document.createElement('span');
    lbl.className = 'related-rail-label';
    lbl.textContent = (opts && opts.label) || 'related →';
    el.appendChild(lbl);
    links.forEach(l => {
        if (!l) return;
        el.appendChild(GIQ.components.jumpLink({
            prefix: l.prefix,
            label: l.label,
            href: l.href,
        }));
    });
    return el;
};

/* ── Cross-link "jump" pill ───────────────────────────────────────── */

GIQ.components.jumpLink = function jumpLink(opts) {
    const { prefix, label, href } = opts || {};
    const a = document.createElement('a');
    a.className = 'jump-link';
    a.href = href || '#';
    if (prefix) {
        const pre = document.createElement('span');
        pre.className = 'jump-link-prefix';
        pre.textContent = prefix;
        a.appendChild(pre);
    }
    const main = document.createElement('span');
    main.className = 'jump-link-label';
    main.textContent = label || '';
    a.appendChild(main);
    const arrow = document.createElement('span');
    arrow.className = 'jump-link-arrow';
    arrow.textContent = '→';
    a.appendChild(arrow);
    return a;
};

/* ── Action card (Actions bucket — Shape B from design hand-off) ──── */

/* Renders one action trigger inside an Actions page. Pattern: name +
 * optional state dot / destructive chip on the left, "▶ Run" primary button
 * on the right; description below; optional mono sub-line ("last run · …").
 *
 * opts:
 *   name:        string (required)
 *   description: string
 *   lastRun:     string — sub-line below description (e.g. "last run · 14m ago · ok")
 *   state:       'good' | 'bad' | 'neutral' | null — left dot
 *   destructive: bool — replaces dot with a "destructive" chip
 *   busy:        bool — disables the Run button + shows "Running…"
 *   runLabel:    string — defaults to "▶ Run"
 *   onRun:       async () => any — called on Run click. Toast + monitor jump
 *                wired automatically based on its return value (any non-throw
 *                = success).
 *   monitorPath: string — hash to redirect to ~800ms after a successful run.
 *                Toast becomes "{name} triggered. View in Monitor →".
 *   monitorLabel:string — overrides "View in Monitor →" toast label.
 *   confirm:     string — if present, browser confirm() is shown before run.
 *   extras:      Element | Element[] — extra controls inserted above the
 *                description (used by Cleanup-stale dry-run toggle).
 *
 * The card re-renders itself in place when busy/last-run state changes —
 * consumer pages that want to refresh from the API can call .refresh(opts2)
 * on the returned wrapper to swap any subset of fields.
 */
GIQ.components.actionCard = function actionCard(opts) {
    const cfg = Object.assign({
        runLabel: '▶ Run',
        state: null,
        destructive: false,
        busy: false,
    }, opts || {});

    const card = document.createElement('section');
    card.className = 'action-card';

    function build() {
        card.innerHTML = '';

        const top = document.createElement('div');
        top.className = 'action-card-top';

        const left = document.createElement('div');
        left.className = 'action-card-name-row';

        if (cfg.destructive) {
            const chip = document.createElement('span');
            chip.className = 'action-card-chip action-card-chip-destructive';
            chip.textContent = 'destructive';
            left.appendChild(chip);
        } else if (cfg.state) {
            const dot = document.createElement('span');
            dot.className = 'action-card-dot action-card-dot-' + cfg.state;
            left.appendChild(dot);
        }

        const nameEl = document.createElement('span');
        nameEl.className = 'action-card-name';
        nameEl.textContent = cfg.name || '';
        left.appendChild(nameEl);

        top.appendChild(left);

        const runBtn = document.createElement('button');
        runBtn.type = 'button';
        runBtn.className = 'vc-btn vc-btn-primary action-card-run';
        runBtn.textContent = cfg.busy ? 'Running…' : cfg.runLabel;
        runBtn.disabled = !!cfg.busy;
        runBtn.addEventListener('click', handleRun);
        top.appendChild(runBtn);

        card.appendChild(top);

        if (cfg.extras) {
            const ex = document.createElement('div');
            ex.className = 'action-card-extras';
            const items = Array.isArray(cfg.extras) ? cfg.extras : [cfg.extras];
            items.forEach(el => { if (el instanceof Element) ex.appendChild(el); });
            card.appendChild(ex);
        }

        if (cfg.description) {
            const desc = document.createElement('div');
            desc.className = 'action-card-desc';
            desc.textContent = cfg.description;
            card.appendChild(desc);
        }

        if (cfg.lastRun) {
            const sub = document.createElement('div');
            sub.className = 'action-card-sub mono';
            sub.textContent = cfg.lastRun;
            card.appendChild(sub);
        }
    }

    async function handleRun() {
        if (cfg.busy) return;
        if (cfg.confirm && !window.confirm(cfg.confirm)) return;
        if (typeof cfg.onRun !== 'function') return;
        wrapper.refresh({ busy: true });
        try {
            await cfg.onRun();
            const monitorPath = cfg.monitorPath;
            if (monitorPath) {
                _actionToastWithJump(cfg.name + ' triggered.', monitorPath, cfg.monitorLabel || 'View in Monitor →');
                setTimeout(() => {
                    if (window.location.hash !== monitorPath) {
                        window.location.hash = monitorPath;
                    }
                }, 800);
            } else {
                GIQ.toast(cfg.name + ' triggered.', 'success');
            }
        } catch (e) {
            GIQ.toast((cfg.name || 'Action') + ' failed: ' + (e && e.message ? e.message : e), 'error');
        } finally {
            wrapper.refresh({ busy: false });
        }
    }

    build();

    const wrapper = {
        el: card,
        refresh(patch) {
            Object.assign(cfg, patch || {});
            build();
        },
    };
    return wrapper;
};

/* Internal helper: a toast with a trailing "→" jump link, mirrors the
 * versioned-config shell's save-toast pattern. Lives here so any page can
 * use it without depending on settings.js.
 */
function _actionToastWithJump(message, hash, jumpLabel) {
    let stack = document.getElementById('toast-stack');
    if (!stack) {
        stack = document.createElement('div');
        stack.id = 'toast-stack';
        document.body.appendChild(stack);
    }
    const t = document.createElement('div');
    t.className = 'toast toast-success toast-with-jump';
    t.innerHTML = '<span class="toast-icon">✓</span>'
                + '<div class="toast-body"></div>'
                + '<a class="toast-jump" href="#"></a>'
                + '<button class="toast-close" type="button" aria-label="Dismiss">×</button>';
    t.querySelector('.toast-body').textContent = message;
    const jump = t.querySelector('.toast-jump');
    jump.textContent = jumpLabel;
    jump.href = hash;
    jump.addEventListener('click', () => GIQ._dismissToast(t));
    t.querySelector('.toast-close').addEventListener('click', () => GIQ._dismissToast(t));
    stack.appendChild(t);
    setTimeout(() => GIQ._dismissToast(t), 6000);
    return t;
}
GIQ.components._actionToastWithJump = _actionToastWithJump;

/* ── Versioned-config shell ───────────────────────────────────────── */

/* Shared shell for Algorithm, Download Routing, Lidarr Backfill configs.
 * Builds the full UI inside a host element: header (eyebrow + title + button
 * row), optional retrain banner, collapsible groups of slider+input fields,
 * and modals for History / Diff / Import.
 *
 * opts:
 *   kind:           'algorithm' | 'downloads/routing' | 'lidarr-backfill'
 *   title:          string
 *   eyebrowPrefix:  string (e.g. 'VERSIONED CONFIG' — version is appended)
 *   retrainGroups:  string[] of group keys that trigger a retrain warning
 *   fieldMeta:      { [groupKey]: { [fieldKey]: { desc, min, max, step, integer } } }
 *   saveSideEffect: { label, onSave?: ()=>Promise, jumpHash, jumpLabel } (optional)
 *   paths:          override for default API paths (optional)
 *   exportName:     filename prefix for downloaded JSON (e.g. 'grooveiq-config')
 *   topRail:        Element | (ctx)=>Element (rendered above the header)
 *   headerExtras:   Element | (ctx)=>Element (rendered between header and groups)
 *   extraButtons:   Array of {label, kind, onClick} or (ctx)=>Array (between Discard and Save)
 *   renderGroupBody: (ctx)=>Element (replaces the default field grid for that group)
 *   bodyClass:      extra CSS class on the host (e.g. 'lbf-disabled')
 *
 * Returns { mount(host), refresh(), dispose() }.
 */
GIQ.components.versionedConfigShell = function versionedConfigShell(opts) {
    const cfg = Object.assign({
        retrainGroups: [],
        fieldMeta: {},
        eyebrowPrefix: 'VERSIONED CONFIG',
        saveButtonLabel: 'Save & Apply',
        exportName: 'grooveiq-config',
    }, opts || {});

    const paths = cfg.paths || _defaultConfigPaths(cfg.kind);

    const state = {
        active: null,    // { id, version, name, config, is_active, created_at, ... }
        working: null,   // working copy of active.config
        defaults: null,  // { config: {...defaults}, groups: [...] }
        history: [],
        loading: true,
        error: null,
        expandedGroups: new Set(),
        host: null,
        modalCleanups: [],
    };

    /* ---- Helpers --------------------------------------------------- */

    function configEqual(a, b) {
        if (a === b) return true;
        if (a == null || b == null) return false;
        try { return JSON.stringify(a) === JSON.stringify(b); } catch (_) { return false; }
    }

    function deepClone(v) { return JSON.parse(JSON.stringify(v)); }

    function fieldMeta(groupKey, fieldKey) {
        const g = cfg.fieldMeta[groupKey];
        if (g && g[fieldKey]) return g[fieldKey];
        const defVal = state.defaults?.config?.[groupKey]?.[fieldKey];
        if (typeof defVal === 'number' && Number.isInteger(defVal)) {
            return { desc: fieldKey.replace(/_/g, ' '), min: 0, max: Math.max(100, defVal * 10), step: 1, integer: true };
        }
        return { desc: fieldKey.replace(/_/g, ' '), min: 0, max: 10, step: 0.1 };
    }

    function isGroupRetrain(groupKey) {
        if (cfg.retrainGroups.includes(groupKey)) return true;
        const g = (state.defaults?.groups || []).find(x => x.key === groupKey);
        return !!(g && g.retrain_required);
    }

    function isFieldRetrain(groupKey, fieldKey) {
        const meta = fieldMeta(groupKey, fieldKey);
        return /\[RETRAIN\]/i.test(meta.desc || '');
    }

    function fieldsEqual(a, b) {
        if (typeof a === 'number' && typeof b === 'number') {
            return Math.abs(a - b) < 1e-9;
        }
        if (a === b) return true;
        if (a == null || b == null) return false;
        if (typeof a !== typeof b) return false;
        if (typeof a === 'object') {
            try { return JSON.stringify(a) === JSON.stringify(b); } catch (_) { return false; }
        }
        return false;
    }

    function groupDirty(groupKey) {
        if (!state.working || !state.active) return false;
        const w = state.working[groupKey] || {};
        const s = state.active.config[groupKey] || {};
        for (const k of Object.keys(w)) if (!fieldsEqual(w[k], s[k])) return true;
        return false;
    }

    function fieldDirty(groupKey, fieldKey) {
        return !fieldsEqual(
            state.working?.[groupKey]?.[fieldKey],
            state.active?.config?.[groupKey]?.[fieldKey],
        );
    }

    function fieldDeviatesFromDefault(groupKey, fieldKey) {
        return !fieldsEqual(
            state.working?.[groupKey]?.[fieldKey],
            state.defaults?.config?.[groupKey]?.[fieldKey],
        );
    }

    function anyDirty() {
        if (!state.working || !state.active) return false;
        return !configEqual(state.working, state.active.config);
    }

    function retrainTriggered() {
        if (!state.working || !state.active) return false;
        const groups = state.defaults?.groups || [];
        for (const g of groups) {
            if (!isGroupRetrain(g.key)) continue;
            if (!configEqual(state.working[g.key], state.active.config[g.key])) return true;
        }
        return false;
    }

    function roundToStep(value, step, isInt) {
        if (isInt) return Math.round(value);
        // Use toFixed with the step's decimal count to prevent FP drift
        const decimals = (String(step).split('.')[1] || '').length;
        return parseFloat(value.toFixed(Math.max(decimals, 0)));
    }

    /* Dotted-path nested getter / setter (used by structured configs).
     * Reject any segment that could traverse Object.prototype. */
    const _UNSAFE_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
    function pathGet(obj, path) {
        if (!path || obj == null) return obj;
        const parts = String(path).split('.');
        let cur = obj;
        for (let i = 0; i < parts.length; i++) {
            if (cur == null) return undefined;
            const k = parts[i];
            if (_UNSAFE_KEYS.has(k)) return undefined;
            // nosemgrep: javascript.lang.security.audit.prototype-pollution.prototype-pollution-loop.prototype-pollution-loop
            cur = cur[k];
        }
        return cur;
    }
    function pathSet(obj, path, value) {
        const parts = String(path).split('.');
        for (const k of parts) {
            if (_UNSAFE_KEYS.has(k)) return;
        }
        let cur = obj;
        for (let i = 0; i < parts.length - 1; i++) {
            const k = parts[i];
            if (cur[k] == null || typeof cur[k] !== 'object') cur[k] = {};
            // nosemgrep: javascript.lang.security.audit.prototype-pollution.prototype-pollution-loop.prototype-pollution-loop
            cur = cur[k];
        }
        cur[parts[parts.length - 1]] = value;
    }

    /* Build the ctx object passed to renderGroupBody / headerExtras / topRail / extraButtons.
     * Provides read-only state references and refresh callbacks. The renderer mutates
     * `working` directly via setWorking() (or by hand) and then calls refreshXxx().
     */
    function buildCtx(extra) {
        return Object.assign({
            working: state.working,
            saved: state.active && state.active.config,
            defaults: state.defaults && state.defaults.config,
            groupsMeta: (state.defaults && state.defaults.groups) || [],
            pathGet,
            pathSet,
            setWorking(path, value) {
                if (state.working == null) return;
                pathSet(state.working, path, value);
            },
            anyDirty,
            groupDirty,
            fieldDirty(gk, fk) { return fieldDirty(gk, fk); },
            refresh() { renderAll(); },
            refreshHeader: _refreshHeaderWithBanner,
            refreshGroup(gk) { _refreshGroupBody(gk); },
            refreshGroupBadge(gk) { _refreshGroupBadges(gk); },
        }, extra || {});
    }

    /* ---- Data loading ---------------------------------------------- */

    async function load() {
        state.loading = true;
        state.error = null;
        renderAll();
        try {
            const [defaults, active, history] = await Promise.all([
                GIQ.api.get(paths.defaults),
                GIQ.api.get(paths.active),
                GIQ.api.get(paths.history + '?limit=50'),
            ]);
            state.defaults = defaults;
            state.active = active;
            state.history = Array.isArray(history) ? history : (history?.items || []);
            // Expect active.config; some endpoints may return raw config under
            // a different key — fallback gracefully.
            const baseCfg = active.config || active;
            state.working = deepClone(baseCfg);
            // Default-expand the first group on first load.
            if (!state.expandedGroups.size && (defaults.groups || []).length) {
                state.expandedGroups.add(defaults.groups[0].key);
            }
            state.loading = false;
            renderAll();
        } catch (e) {
            state.loading = false;
            state.error = e.message || String(e);
            renderAll();
        }
    }

    async function refreshHistory() {
        try {
            const h = await GIQ.api.get(paths.history + '?limit=50');
            state.history = Array.isArray(h) ? h : (h?.items || []);
        } catch (_) { /* ignore */ }
    }

    /* ---- Actions --------------------------------------------------- */

    async function saveAndApply() {
        if (!anyDirty()) return;
        const retrainWarn = retrainTriggered();
        const baseMsg = retrainWarn
            ? 'Save these changes? They will trigger a full model retrain.'
            : 'Save these changes as a new config version?';
        if (!confirm(baseMsg)) return;
        const name = prompt('Version name (optional):', '') || null;
        try {
            const saved = await GIQ.api.put(paths.save, { name, config: state.working });
            state.active = saved;
            state.working = deepClone(saved.config || saved);
            await refreshHistory();
            // Optional side-effect (e.g. POST /v1/pipeline/reset).
            if (cfg.saveSideEffect && typeof cfg.saveSideEffect.onSave === 'function') {
                try { await cfg.saveSideEffect.onSave(); }
                catch (e) { GIQ.toast('Saved, but side-effect failed: ' + e.message, 'warning'); }
            }
            // Toast with optional jump link.
            const seLabel = (cfg.saveSideEffect && cfg.saveSideEffect.label)
                ? '. ' + cfg.saveSideEffect.label
                : '';
            const jumpHash = cfg.saveSideEffect && cfg.saveSideEffect.jumpHash;
            const jumpLabel = (cfg.saveSideEffect && cfg.saveSideEffect.jumpLabel) || 'View →';
            _toastWithJump('Saved' + seLabel, jumpHash, jumpLabel);
            renderAll();
        } catch (e) {
            GIQ.toast('Save failed: ' + e.message, 'error');
        }
    }

    async function discardChanges() {
        if (!anyDirty()) return;
        if (!confirm('Discard all unsaved changes?')) return;
        state.working = deepClone(state.active.config);
        renderAll();
    }

    async function resetToDefaults() {
        if (!confirm('Reset all values to their defaults? This creates a new config version.')) return;
        try {
            const saved = await GIQ.api.post(paths.reset, {});
            state.active = saved;
            state.working = deepClone(saved.config || saved);
            await refreshHistory();
            GIQ.toast('Reset to defaults · v' + saved.version, 'success');
            renderAll();
        } catch (e) {
            GIQ.toast('Reset failed: ' + e.message, 'error');
        }
    }

    async function exportConfig() {
        try {
            const res = await fetch(window.location.origin + paths.export, {
                headers: { 'Authorization': 'Bearer ' + (GIQ.state.apiKey || '') },
            });
            if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
            const blob = await res.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = cfg.exportName + '-v' + (state.active?.version || '0') + '.json';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setTimeout(() => URL.revokeObjectURL(a.href), 1000);
        } catch (e) {
            GIQ.toast('Export failed: ' + e.message, 'error');
        }
    }

    function showImport() {
        const wrap = document.createElement('div');
        wrap.innerHTML = ''
            + '<div class="form-field">'
            + '<label>JSON file</label>'
            + '<input type="file" accept=".json" id="vc-import-file">'
            + '</div>'
            + '<div class="form-field">'
            + '<label>Name (optional)</label>'
            + '<input type="text" id="vc-import-name" placeholder="e.g. backup-2026-04-29">'
            + '</div>';
        const cancelBtn = _btn('Cancel', 'ghost', () => modal.close());
        const importBtn = _btn('Import', 'primary', async () => {
            const fileInput = wrap.querySelector('#vc-import-file');
            const nameInput = wrap.querySelector('#vc-import-name');
            if (!fileInput || !fileInput.files.length) {
                GIQ.toast('Select a JSON file first', 'warning');
                return;
            }
            try {
                const text = await fileInput.files[0].text();
                const data = JSON.parse(text);
                const config = data.config || data;
                const name = (nameInput && nameInput.value) || data.name || 'Imported';
                const saved = await GIQ.api.post(paths.import, { name, config });
                state.active = saved;
                state.working = deepClone(saved.config || saved);
                await refreshHistory();
                GIQ.toast('Imported · v' + saved.version, 'success');
                modal.close();
                renderAll();
            } catch (e) {
                GIQ.toast('Import failed: ' + e.message, 'error');
            }
        });
        const modal = GIQ.components.modal({
            title: 'Import configuration',
            body: wrap,
            footer: [cancelBtn, importBtn],
            width: 'sm',
        });
        state.modalCleanups.push(modal.close);
    }

    function showHistory() {
        const body = document.createElement('div');
        body.className = 'vc-history';
        if (!state.history.length) {
            body.innerHTML = '<div class="vc-empty">No history yet.</div>';
        } else {
            const table = document.createElement('table');
            table.className = 'vc-table';
            table.innerHTML = '<thead><tr>'
                + '<th>Version</th><th>Name</th><th>Active</th><th>Created</th><th></th>'
                + '</tr></thead>';
            const tbody = document.createElement('tbody');
            state.history.forEach(v => {
                const tr = document.createElement('tr');
                tr.innerHTML = '<td class="mono">v' + GIQ.fmt.esc(v.version) + '</td>'
                    + '<td>' + GIQ.fmt.esc(v.name || '—') + '</td>'
                    + '<td>' + (v.is_active ? '<span class="badge badge-active">active</span>' : '') + '</td>'
                    + '<td class="mono muted">' + GIQ.fmt.esc(GIQ.fmt.timeAgo(v.created_at)) + '</td>';
                const actionsTd = document.createElement('td');
                actionsTd.className = 'vc-row-actions';
                if (!v.is_active) {
                    actionsTd.appendChild(_btn('Activate', 'ghost-sm', async () => {
                        if (!confirm('Activate v' + v.version + '? This rolls back to that version.')) return;
                        try {
                            const saved = await GIQ.api.post(paths.activate(v.version), {});
                            state.active = saved;
                            state.working = deepClone(saved.config || saved);
                            await refreshHistory();
                            GIQ.toast('Activated v' + saved.version, 'success');
                            modal.close();
                            renderAll();
                        } catch (e) {
                            GIQ.toast('Activate failed: ' + e.message, 'error');
                        }
                    }));
                }
                actionsTd.appendChild(_btn('Diff', 'ghost-sm', async () => {
                    try {
                        const ver = await GIQ.api.get(paths.version(v.version));
                        modal.close();
                        showDiff(ver.config || ver, 'v' + ver.version + (ver.name ? ' (' + ver.name + ')' : ''));
                    } catch (e) {
                        GIQ.toast('Failed to load v' + v.version + ': ' + e.message, 'error');
                    }
                }));
                tr.appendChild(actionsTd);
                tbody.appendChild(tr);
            });
            table.appendChild(tbody);
            body.appendChild(table);
        }
        const modal = GIQ.components.modal({
            title: 'Version history',
            body,
            footer: [_btn('Close', 'ghost', () => modal.close())],
            width: 'lg',
        });
        state.modalCleanups.push(modal.close);
    }

    function showDiff(compareConfig, compareLabel) {
        const groups = state.defaults?.groups || [];
        const body = document.createElement('div');
        body.className = 'vc-diff';
        let any = false;
        groups.forEach(g => {
            const gk = g.key;
            const w = state.working[gk] || {};
            const c = compareConfig[gk] || {};
            const diffs = [];
            for (const k of Object.keys(w)) if (!fieldsEqual(w[k], c[k])) diffs.push({ k, from: c[k], to: w[k] });
            if (!diffs.length) return;
            any = true;
            const block = document.createElement('div');
            block.className = 'vc-diff-group';
            block.innerHTML = '<div class="vc-diff-title">' + GIQ.fmt.esc(g.label) + '</div>';
            const tbl = document.createElement('table');
            tbl.className = 'vc-table vc-diff-table';
            tbl.innerHTML = '<thead><tr><th>Field</th><th>From</th><th>To</th></tr></thead>';
            const tbody = document.createElement('tbody');
            diffs.forEach(d => {
                const tr = document.createElement('tr');
                tr.innerHTML = '<td class="mono">' + GIQ.fmt.esc(d.k) + '</td>'
                    + '<td class="vc-diff-from">' + GIQ.fmt.esc(_fmt(d.from)) + '</td>'
                    + '<td class="vc-diff-to">' + GIQ.fmt.esc(_fmt(d.to)) + '</td>';
                tbody.appendChild(tr);
            });
            tbl.appendChild(tbody);
            block.appendChild(tbl);
            body.appendChild(block);
        });
        if (!any) {
            body.innerHTML = '<div class="vc-empty">No differences.</div>';
        }
        const modal = GIQ.components.modal({
            title: 'Diff · working copy vs ' + compareLabel,
            body,
            footer: [_btn('Close', 'ghost', () => modal.close())],
            width: 'lg',
        });
        state.modalCleanups.push(modal.close);
    }

    /* ---- Render ---------------------------------------------------- */

    function renderAll() {
        if (!state.host) return;
        const host = state.host;
        host.innerHTML = '';
        const dynBodyClass = (typeof cfg.bodyClass === 'function')
            ? (cfg.bodyClass(buildCtx()) || '')
            : (cfg.bodyClass || '');
        host.className = 'vc-shell' + (dynBodyClass ? ' ' + dynBodyClass : '');

        if (cfg.topRail) {
            const rail = (typeof cfg.topRail === 'function') ? cfg.topRail(buildCtx()) : cfg.topRail;
            if (rail instanceof Element) host.appendChild(rail);
        }
        host.appendChild(_buildHeader());
        if (state.loading) {
            host.appendChild(_loading());
            return;
        }
        if (state.error) {
            host.appendChild(_errorPanel());
            return;
        }
        if (cfg.headerExtras) {
            const extras = (typeof cfg.headerExtras === 'function') ? cfg.headerExtras(buildCtx()) : cfg.headerExtras;
            if (extras instanceof Element) {
                extras.classList.add('vc-header-extras');
                host.appendChild(extras);
            }
        }
        if (anyDirty() && retrainTriggered()) host.appendChild(_buildRetrainBanner());
        host.appendChild(_buildGroups());
    }

    function _buildHeader() {
        const head = document.createElement('header');
        head.className = 'vc-header';

        const left = document.createElement('div');
        left.className = 'vc-header-left';
        const eb = document.createElement('div');
        eb.className = 'eyebrow';
        const versionLabel = state.active?.version != null
            ? cfg.eyebrowPrefix + ' · v' + state.active.version
            : cfg.eyebrowPrefix;
        eb.textContent = versionLabel;
        left.appendChild(eb);
        const title = document.createElement('h1');
        title.className = 'vc-title';
        title.textContent = cfg.title;
        if (state.active?.name) {
            const subName = document.createElement('span');
            subName.className = 'vc-name';
            subName.textContent = ' · ' + state.active.name;
            title.appendChild(subName);
        }
        left.appendChild(title);
        head.appendChild(left);

        const right = document.createElement('div');
        right.className = 'vc-header-right';
        const dirty = !state.loading && !state.error && anyDirty();

        right.appendChild(_btn('History', 'ghost', showHistory, { disabled: state.loading }));
        right.appendChild(_btn('Diff', 'ghost', () => showDiff(state.active.config, 'v' + state.active.version), { disabled: state.loading || state.error }));
        right.appendChild(_btn('Reset', 'ghost', resetToDefaults, { disabled: state.loading }));
        if (dirty) right.appendChild(_btn('Discard', 'ghost', discardChanges));
        right.appendChild(_btn('Export', 'ghost', exportConfig, { disabled: state.loading || state.error }));
        right.appendChild(_btn('Import', 'ghost', showImport, { disabled: state.loading }));

        const extraBtns = (typeof cfg.extraButtons === 'function') ? cfg.extraButtons(buildCtx()) : cfg.extraButtons;
        if (Array.isArray(extraBtns)) {
            extraBtns.forEach(b => {
                if (!b || !b.label) return;
                right.appendChild(_btn(b.label, b.kind || 'ghost', b.onClick, { disabled: !!b.disabled || state.loading }));
            });
        }

        right.appendChild(_btn(cfg.saveButtonLabel, 'primary', saveAndApply, { disabled: !dirty }));

        head.appendChild(right);
        return head;
    }

    function _buildRetrainBanner() {
        const el = document.createElement('div');
        el.className = 'vc-retrain-banner';
        el.innerHTML = '<span class="vc-retrain-icon">!</span>'
            + '<span>Changes include parameters that trigger a full model retrain. Saving will rebuild the affected model on the next pipeline run.</span>';
        return el;
    }

    function _buildGroups() {
        const wrap = document.createElement('div');
        wrap.className = 'vc-groups';
        const groups = state.defaults?.groups || [];
        groups.forEach(g => wrap.appendChild(_buildGroup(g)));
        return wrap;
    }

    function _buildGroup(group) {
        const gk = group.key;
        const sect = document.createElement('section');
        sect.className = 'vc-group';
        sect.dataset.groupKey = gk;
        const expanded = state.expandedGroups.has(gk);
        if (expanded) sect.classList.add('expanded');

        const header = document.createElement('button');
        header.type = 'button';
        header.className = 'vc-group-head';
        header.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        header.addEventListener('click', () => {
            if (state.expandedGroups.has(gk)) state.expandedGroups.delete(gk);
            else state.expandedGroups.add(gk);
            const open = state.expandedGroups.has(gk);
            sect.classList.toggle('expanded', open);
            header.setAttribute('aria-expanded', open ? 'true' : 'false');
        });

        const titleRow = document.createElement('div');
        titleRow.className = 'vc-group-title-row';
        const chev = document.createElement('span');
        chev.className = 'vc-group-chev';
        chev.textContent = '▸';
        titleRow.appendChild(chev);
        const label = document.createElement('span');
        label.className = 'vc-group-label';
        label.textContent = group.label;
        titleRow.appendChild(label);
        if (isGroupRetrain(gk)) {
            const b = document.createElement('span');
            b.className = 'vc-badge vc-badge-retrain';
            b.textContent = 'RETRAIN';
            titleRow.appendChild(b);
        }
        if (groupDirty(gk)) {
            const b = document.createElement('span');
            b.className = 'vc-badge vc-badge-modified';
            b.textContent = 'MODIFIED';
            titleRow.appendChild(b);
        }
        header.appendChild(titleRow);

        if (group.description) {
            const sub = document.createElement('div');
            sub.className = 'vc-group-sub';
            sub.textContent = group.description;
            header.appendChild(sub);
        }
        sect.appendChild(header);

        const body = document.createElement('div');
        body.className = 'vc-group-body';
        if (typeof cfg.renderGroupBody === 'function') {
            const custom = cfg.renderGroupBody(buildCtx({ groupKey: gk, groupMeta: group }));
            if (custom instanceof Element) body.appendChild(custom);
        } else {
            const grid = document.createElement('div');
            grid.className = 'vc-fields';
            const defaults = state.defaults?.config?.[gk] || {};
            Object.keys(defaults).forEach(fk => grid.appendChild(_buildField(gk, fk)));
            body.appendChild(grid);
        }
        sect.appendChild(body);

        return sect;
    }

    /* Re-render just one group's body — used by renderGroupBody callers
     * after they mutate the working copy (e.g. chain reorder) so other
     * groups keep their open/scroll state. */
    function _refreshGroupBody(groupKey) {
        if (!state.host) return;
        const sect = state.host.querySelector('.vc-group[data-group-key="' + cssEscape(groupKey) + '"]');
        if (!sect) return;
        const body = sect.querySelector(':scope > .vc-group-body');
        if (!body) return;
        const groupMeta = (state.defaults?.groups || []).find(g => g.key === groupKey);
        body.innerHTML = '';
        if (typeof cfg.renderGroupBody === 'function') {
            const custom = cfg.renderGroupBody(buildCtx({ groupKey, groupMeta }));
            if (custom instanceof Element) body.appendChild(custom);
        } else {
            const grid = document.createElement('div');
            grid.className = 'vc-fields';
            const defaults = state.defaults?.config?.[groupKey] || {};
            Object.keys(defaults).forEach(fk => grid.appendChild(_buildField(groupKey, fk)));
            body.appendChild(grid);
        }
        _refreshGroupBadges(groupKey);
    }

    function _refreshHeaderWithBanner() {
        _refreshHeader();
        // Header refresh already handles the retrain banner; if headerExtras
        // depends on dirty state, also refresh it.
        if (state.host && cfg.headerExtras) {
            const oldExtras = state.host.querySelector(':scope > .vc-header-extras');
            const extras = (typeof cfg.headerExtras === 'function') ? cfg.headerExtras(buildCtx()) : cfg.headerExtras;
            if (extras instanceof Element) extras.classList.add('vc-header-extras');
            if (oldExtras && extras instanceof Element) {
                oldExtras.replaceWith(extras);
            } else if (oldExtras && !(extras instanceof Element)) {
                oldExtras.remove();
            } else if (!oldExtras && extras instanceof Element) {
                const oldHeader = state.host.querySelector(':scope > .vc-header');
                if (oldHeader) oldHeader.after(extras);
            }
        }
    }

    function _buildField(groupKey, fieldKey) {
        const meta = fieldMeta(groupKey, fieldKey);
        const row = document.createElement('div');
        row.className = 'vc-field';
        row.dataset.groupKey = groupKey;
        row.dataset.fieldKey = fieldKey;
        if (fieldDirty(groupKey, fieldKey)) row.classList.add('dirty');

        const headRow = document.createElement('div');
        headRow.className = 'vc-field-head';
        const lbl = document.createElement('label');
        lbl.className = 'vc-field-label';
        lbl.textContent = fieldKey.replace(/_/g, ' ');
        headRow.appendChild(lbl);
        if (isFieldRetrain(groupKey, fieldKey)) {
            const b = document.createElement('span');
            b.className = 'vc-badge vc-badge-retrain vc-badge-sm';
            b.textContent = 'RETRAIN';
            headRow.appendChild(b);
        }
        row.appendChild(headRow);

        const ctrls = document.createElement('div');
        ctrls.className = 'vc-field-controls';

        const value = state.working[groupKey][fieldKey];

        const slider = document.createElement('input');
        slider.type = 'range';
        slider.className = 'vc-slider';
        slider.min = String(meta.min);
        slider.max = String(meta.max);
        slider.step = String(meta.step);
        slider.value = String(value);
        ctrls.appendChild(slider);

        const numWrap = document.createElement('div');
        numWrap.className = 'vc-num-wrap';
        const minus = document.createElement('button');
        minus.type = 'button';
        minus.className = 'vc-spin vc-spin-down';
        minus.textContent = '−';
        const num = document.createElement('input');
        num.type = 'number';
        num.className = 'vc-num';
        num.min = String(meta.min);
        num.max = String(meta.max);
        num.step = String(meta.step);
        num.value = String(value);
        const plus = document.createElement('button');
        plus.type = 'button';
        plus.className = 'vc-spin vc-spin-up';
        plus.textContent = '+';
        numWrap.appendChild(minus);
        numWrap.appendChild(num);
        numWrap.appendChild(plus);
        ctrls.appendChild(numWrap);
        row.appendChild(ctrls);

        const info = document.createElement('div');
        info.className = 'vc-field-info';
        const desc = document.createElement('span');
        desc.className = 'vc-field-desc';
        desc.textContent = (meta.desc || '').replace(/\s*\[RETRAIN\]\s*/i, '');
        info.appendChild(desc);
        const defaultIndicator = document.createElement('span');
        defaultIndicator.className = 'vc-field-default';
        if (fieldDeviatesFromDefault(groupKey, fieldKey)) {
            defaultIndicator.textContent = 'default: ' + state.defaults.config[groupKey][fieldKey];
        }
        info.appendChild(defaultIndicator);
        row.appendChild(info);

        // Wiring
        function setValue(v, opts) {
            const isInt = !!meta.integer;
            let n = isInt ? Math.round(v) : v;
            if (n < meta.min) n = meta.min;
            if (n > meta.max) n = meta.max;
            n = roundToStep(n, meta.step, isInt);
            state.working[groupKey][fieldKey] = n;
            slider.value = String(n);
            num.value = String(n);
            row.classList.toggle('dirty', fieldDirty(groupKey, fieldKey));
            // Update default indicator
            if (fieldDeviatesFromDefault(groupKey, fieldKey)) {
                defaultIndicator.textContent = 'default: ' + state.defaults.config[groupKey][fieldKey];
            } else {
                defaultIndicator.textContent = '';
            }
            // Update group + page level affordances
            _refreshGroupBadges(groupKey);
            _refreshHeader();
        }

        slider.addEventListener('input', () => {
            const v = meta.integer ? parseInt(slider.value, 10) : parseFloat(slider.value);
            if (!Number.isNaN(v)) setValue(v);
        });
        num.addEventListener('change', () => {
            const v = meta.integer ? parseInt(num.value, 10) : parseFloat(num.value);
            if (Number.isNaN(v)) {
                num.value = String(state.working[groupKey][fieldKey]);
                return;
            }
            setValue(v);
        });
        minus.addEventListener('click', () => {
            const cur = state.working[groupKey][fieldKey];
            setValue(cur - meta.step);
        });
        plus.addEventListener('click', () => {
            const cur = state.working[groupKey][fieldKey];
            setValue(cur + meta.step);
        });

        return row;
    }

    function _refreshGroupBadges(groupKey) {
        if (!state.host) return;
        const sect = state.host.querySelector('.vc-group[data-group-key="' + cssEscape(groupKey) + '"]');
        if (!sect) return;
        const titleRow = sect.querySelector('.vc-group-title-row');
        if (!titleRow) return;
        const existing = titleRow.querySelector('.vc-badge-modified');
        const dirty = groupDirty(groupKey);
        if (dirty && !existing) {
            const b = document.createElement('span');
            b.className = 'vc-badge vc-badge-modified';
            b.textContent = 'MODIFIED';
            titleRow.appendChild(b);
        } else if (!dirty && existing) {
            existing.remove();
        }
    }

    function _refreshHeader() {
        if (!state.host) return;
        const oldHeader = state.host.querySelector(':scope > .vc-header');
        if (!oldHeader) return;
        const newHeader = _buildHeader();
        oldHeader.replaceWith(newHeader);
        const oldBanner = state.host.querySelector(':scope > .vc-retrain-banner');
        const wantBanner = anyDirty() && retrainTriggered();
        if (wantBanner && !oldBanner) {
            newHeader.after(_buildRetrainBanner());
        } else if (!wantBanner && oldBanner) {
            oldBanner.remove();
        }
    }

    function cssEscape(s) {
        if (window.CSS && CSS.escape) return CSS.escape(s);
        return String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
    }

    function _loading() {
        const el = document.createElement('div');
        el.className = 'vc-loading';
        el.textContent = 'Loading…';
        return el;
    }

    function _errorPanel() {
        const el = document.createElement('div');
        el.className = 'vc-error';
        el.innerHTML = '<div class="vc-error-title">Failed to load</div>'
            + '<div class="vc-error-msg"></div>'
            + '<button type="button" class="vc-btn vc-btn-ghost">Retry</button>';
        el.querySelector('.vc-error-msg').textContent = state.error || 'Unknown error';
        el.querySelector('.vc-btn').addEventListener('click', load);
        return el;
    }

    function _btn(label, kind, onClick, opts) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'vc-btn vc-btn-' + (kind || 'ghost');
        b.textContent = label;
        if (opts && opts.disabled) b.disabled = true;
        if (typeof onClick === 'function') b.addEventListener('click', onClick);
        return b;
    }

    function _toastWithJump(text, jumpHash, jumpLabel) {
        const stack = (() => {
            let s = document.getElementById('toast-stack');
            if (!s) {
                s = document.createElement('div');
                s.id = 'toast-stack';
                document.body.appendChild(s);
            }
            return s;
        })();
        const t = document.createElement('div');
        t.className = 'toast toast-success toast-with-jump';
        t.innerHTML = '<span class="toast-icon">✓</span>'
            + '<div class="toast-body"></div>';
        t.querySelector('.toast-body').textContent = text;
        if (jumpHash) {
            const a = document.createElement('a');
            a.className = 'toast-jump';
            a.href = jumpHash;
            a.textContent = jumpLabel || 'View →';
            a.addEventListener('click', () => GIQ._dismissToast(t));
            t.appendChild(a);
        }
        const close = document.createElement('button');
        close.className = 'toast-close';
        close.type = 'button';
        close.setAttribute('aria-label', 'Dismiss');
        close.textContent = '×';
        close.addEventListener('click', () => GIQ._dismissToast(t));
        t.appendChild(close);
        stack.appendChild(t);
        setTimeout(() => GIQ._dismissToast(t), 7000);
    }

    function _fmt(v) {
        if (v == null) return '—';
        if (typeof v === 'number') return String(v);
        if (typeof v === 'object') return JSON.stringify(v);
        return String(v);
    }

    /* ---- Public API ------------------------------------------------ */

    function mount(host) {
        state.host = host;
        load();
    }

    function refresh() { load(); }

    function dispose() {
        state.modalCleanups.forEach(fn => { try { fn(); } catch (_) { } });
        state.modalCleanups = [];
        state.host = null;
    }

    return { mount, refresh, dispose };
};

function _defaultConfigPaths(kind) {
    if (kind === 'downloads/routing') {
        const sub = '/v1/' + kind;
        return {
            active: sub,
            defaults: sub + '/defaults',
            history: sub + '/history',
            version: (v) => sub + '/' + v,
            save: sub,
            reset: sub + '/reset',
            activate: (v) => sub + '/activate/' + v,
            export: sub + '/export',
            import: sub + '/import',
        };
    }
    const sub = '/v1/' + kind + '/config';
    return {
        active: sub,
        defaults: sub + '/defaults',
        history: sub + '/history',
        version: (v) => sub + '/' + v,
        save: sub,
        reset: sub + '/reset',
        activate: (v) => sub + '/activate/' + v,
        export: sub + '/export',
        import: sub + '/import',
    };
}

/* ── Candidate panel (Recs Debug detail + Live Debug) ─────────────── */

/* Renders the shared "candidate sources / reranker actions / candidates
 * with feature vector inspector" set of panels, used by both Audit
 * Request Detail and Live Debug Recs.
 *
 * opts:
 *   candidatesByCount: { source: count } map
 *   candidates: array of audit candidates (each with track_id, title,
 *               artist, sources[], raw_score, final_score,
 *               pre_rerank_position, final_position, shown,
 *               reranker_actions[], feature_vector{})
 *   candidatesTotal: optional number — total considered (for header)
 *   limitRequested: optional number — original `?limit=`
 *
 * Returns a DOM container with the three panels appended.
 */
GIQ.components.candidatePanel = function candidatePanel(opts) {
    const cfg = opts || {};
    const wrap = document.createElement('div');
    wrap.className = 'candidate-panel-wrap';

    const grid = document.createElement('div');
    grid.className = 'candidate-panel-grid';

    // Sources panel
    const cbsKeys = Object.keys(cfg.candidatesByCount || {});
    const cbsTotal = cbsKeys.reduce((s, k) => s + (cfg.candidatesByCount[k] || 0), 0) || 1;
    const cbsRows = cbsKeys
        .sort((a, b) => (cfg.candidatesByCount[b] || 0) - (cfg.candidatesByCount[a] || 0))
        .map(k => ({ label: k, value: cfg.candidatesByCount[k] || 0 }));
    const cbsMax = cbsRows.reduce((m, r) => Math.max(m, r.value), 1);
    const cbsList = document.createElement('div');
    cbsList.className = 'bar-list';
    if (!cbsRows.length) {
        cbsList.innerHTML = '<div class="empty-row">No candidate sources captured.</div>';
    } else {
        cbsRows.forEach(r => {
            const pct = (r.value / cbsMax) * 100;
            const sharePct = (r.value / cbsTotal) * 100;
            const row = document.createElement('div');
            row.className = 'bar-row';
            row.innerHTML = '<div class="bar-label"><span class="rd-source-chip">' + GIQ.fmt.esc(r.label) + '</span></div>'
                + '<div class="bar-track"><div class="bar-fill bar-fill-accent" style="width:' + Math.max(2, pct).toFixed(1) + '%"></div></div>'
                + '<div class="bar-count mono">' + GIQ.fmt.esc(String(r.value)) + ' <span class="muted">· ' + sharePct.toFixed(0) + '%</span></div>';
            cbsList.appendChild(row);
        });
    }
    grid.appendChild(GIQ.components.panel({
        title: 'Candidate sources',
        sub: cbsKeys.length ? cbsKeys.length + ' sources' : 'no breakdown',
        children: cbsList,
    }));

    // Reranker actions panel
    const actionCounts = {};
    (cfg.candidates || []).forEach(c => {
        (c.reranker_actions || []).forEach(a => {
            const name = a.action || 'unknown';
            actionCounts[name] = (actionCounts[name] || 0) + 1;
        });
    });
    const actKeys = Object.keys(actionCounts);
    const actMax = actKeys.reduce((m, k) => Math.max(m, actionCounts[k]), 1);
    const actBody = document.createElement('div');
    if (!actKeys.length) {
        actBody.innerHTML = '<div class="empty-row">No reranker actions for this request.</div>';
    } else {
        const summary = document.createElement('div');
        summary.className = 'rd-action-summary';
        actKeys.forEach(k => {
            const chip = document.createElement('span');
            chip.className = 'rd-source-chip';
            chip.textContent = k + ' × ' + actionCounts[k];
            summary.appendChild(chip);
        });
        actBody.appendChild(summary);
        const list = document.createElement('div');
        list.className = 'bar-list';
        actKeys.sort((a, b) => actionCounts[b] - actionCounts[a]).forEach(k => {
            const pct = (actionCounts[k] / actMax) * 100;
            const colorClass = (k.indexOf('boost') >= 0 || k === 'exploration_slot') ? 'accent' : 'wine';
            const row = document.createElement('div');
            row.className = 'bar-row';
            row.innerHTML = '<div class="bar-label">' + GIQ.fmt.esc(k.replace(/_/g, ' ')) + '</div>'
                + '<div class="bar-track"><div class="bar-fill bar-fill-' + colorClass + '" style="width:' + Math.max(2, pct).toFixed(1) + '%"></div></div>'
                + '<div class="bar-count mono">' + actionCounts[k] + '</div>';
            list.appendChild(row);
        });
        actBody.appendChild(list);
    }
    let actSub = (cfg.candidatesTotal != null ? cfg.candidatesTotal : (cfg.candidates || []).length) + ' total';
    if (cfg.limitRequested != null) actSub += ' · limit ' + cfg.limitRequested;
    grid.appendChild(GIQ.components.panel({
        title: 'Reranker actions',
        sub: actSub,
        children: actBody,
    }));

    wrap.appendChild(grid);

    // Candidates table
    const candTable = document.createElement('div');
    candTable.className = 'rd-candidates-table';
    const head = document.createElement('div');
    head.className = 'rd-row rd-cand-head';
    head.innerHTML = '<span>Rank</span><span>Track</span><span>Sources</span><span>Raw</span><span>Final</span><span>Actions</span><span></span>';
    candTable.appendChild(head);

    (cfg.candidates || []).forEach((c, i) => {
        const name = (c.artist ? c.artist + ' — ' : '') + (c.title || c.track_id || '—');
        let rankCell;
        if (c.shown && c.final_position != null) {
            const delta = (c.pre_rerank_position != null) ? (c.pre_rerank_position - c.final_position) : 0;
            const arrow = delta > 0 ? '<span class="rd-up">↑' + delta + '</span>'
                : delta < 0 ? '<span class="rd-down">↓' + Math.abs(delta) + '</span>'
                : '';
            rankCell = '<strong class="mono">' + (c.final_position + 1) + '</strong> ' + arrow;
        } else {
            rankCell = '<span class="mono muted" title="filtered out">—</span>';
        }

        const sources = (c.sources || []).map(s => '<span class="rd-source-chip">' + GIQ.fmt.esc(s) + '</span>').join(' ');
        const actions = (c.reranker_actions || []).map(a => '<span class="rd-source-chip">' + GIQ.fmt.esc(a.action || '?') + '</span>').join(' ');

        const row = document.createElement('div');
        row.className = 'rd-row rd-cand-row';
        row.innerHTML = '<span class="rd-cand-rank">' + rankCell + '</span>'
            + '<span class="rd-truncate" title="' + GIQ.fmt.esc(c.track_id || '') + '">' + GIQ.fmt.esc(name) + '</span>'
            + '<span class="rd-cand-sources">' + (sources || '<span class="mono muted">—</span>') + '</span>'
            + '<span class="mono muted">' + (c.raw_score != null ? c.raw_score.toFixed(3) : '—') + '</span>'
            + '<span class="mono">' + (c.final_score != null ? c.final_score.toFixed(3) : '—') + '</span>'
            + '<span class="rd-cand-actions">' + (actions || '<span class="mono muted">—</span>') + '</span>'
            + '<span></span>';

        const whyBtn = document.createElement('button');
        whyBtn.type = 'button';
        whyBtn.className = 'vc-btn vc-btn-ghost vc-btn-sm';
        whyBtn.textContent = 'Why?';
        const whyRow = document.createElement('div');
        whyRow.className = 'rd-cand-why';
        whyRow.style.display = 'none';
        whyBtn.addEventListener('click', () => {
            const showing = whyRow.style.display !== 'none';
            if (showing) { whyRow.style.display = 'none'; return; }
            if (!whyRow.dataset.built) {
                whyRow.appendChild(buildFeatureInspector(c.feature_vector || {}));
                whyRow.dataset.built = '1';
            }
            whyRow.style.display = '';
        });
        row.lastElementChild.appendChild(whyBtn);
        candTable.appendChild(row);
        candTable.appendChild(whyRow);
    });

    wrap.appendChild(GIQ.components.panel({
        title: 'Candidates',
        sub: (cfg.candidates || []).length + ' rows',
        children: candTable,
    }));

    return wrap;
};

function buildFeatureInspector(fv) {
    const wrap = document.createElement('div');
    wrap.className = 'rd-feature-inspector';
    const keys = Object.keys(fv || {});
    if (!keys.length) {
        wrap.innerHTML = '<div class="empty-row">No feature vector persisted.</div>';
        return wrap;
    }
    keys.sort((a, b) => Math.abs(fv[b] || 0) - Math.abs(fv[a] || 0));
    const head = document.createElement('div');
    head.className = 'eyebrow';
    head.textContent = 'TOP FEATURES BY MAGNITUDE · ' + keys.length + ' TOTAL';
    wrap.appendChild(head);
    const grid = document.createElement('div');
    grid.className = 'rd-feature-grid';
    keys.forEach(k => {
        const v = fv[k];
        let vstr;
        if (typeof v === 'number') {
            vstr = Number.isInteger(v) ? String(v) : v.toFixed(4);
        } else {
            vstr = String(v);
        }
        const cell = document.createElement('div');
        cell.className = 'rd-feature-cell';
        cell.innerHTML = '<span class="mono muted">' + GIQ.fmt.esc(k) + '</span>'
            + '<span class="mono">' + GIQ.fmt.esc(vstr) + '</span>';
        grid.appendChild(cell);
    });
    wrap.appendChild(grid);
    return wrap;
}

/* ── Integration card ────────────────────────────────────────────────
 * Used by both Settings → Connections (mode='configured', snapshot)
 * and Monitor → Integrations (mode='live', live probe data).
 *
 * Props:
 *   name, icon, description: presentation
 *   mode: 'configured' | 'live' (default 'configured')
 *
 *   When mode='configured':
 *     configured (bool), type, version, details (label/value rows),
 *     snapshot (bool, shows footer note), configurePath (optional)
 *
 *   When mode='live':
 *     status: 'healthy' | 'probing' | 'error' | 'not_configured'
 *     type, version, error (string),
 *     latencyMs (number), checkedAt (unix epoch)
 */
GIQ.components.integrationCard = function integrationCard(opts) {
    const o = opts || {};
    const mode = o.mode === 'live' ? 'live' : 'configured';
    const card = document.createElement('section');
    card.className = 'conn-card conn-card-' + mode;

    if (mode === 'live') {
        card.classList.add('status-' + (o.status || 'probing'));
    } else if (o.configured) {
        card.classList.add('configured');
    } else {
        card.classList.add('unconfigured');
    }

    const head = document.createElement('div');
    head.className = 'conn-card-head';
    const iconEl = document.createElement('span');
    iconEl.className = 'conn-card-icon';
    iconEl.textContent = o.icon || '◇';
    head.appendChild(iconEl);

    const titleGroup = document.createElement('div');
    titleGroup.className = 'conn-card-title-group';
    const tName = document.createElement('div');
    tName.className = 'conn-card-name';
    tName.textContent = o.name || '';
    titleGroup.appendChild(tName);
    const subBits = [];
    if (o.type) subBits.push(o.type);
    if (o.version) subBits.push('v' + o.version);
    if (subBits.length) {
        const tSub = document.createElement('div');
        tSub.className = 'conn-card-meta muted mono';
        tSub.textContent = subBits.join(' · ');
        titleGroup.appendChild(tSub);
    }
    head.appendChild(titleGroup);

    const badge = document.createElement('span');
    if (mode === 'live') {
        badge.className = 'conn-status-badge status-' + (o.status || 'probing');
        const statusLabels = {
            healthy: 'Healthy',
            probing: 'Probing…',
            error: 'Error',
            not_configured: 'Not configured',
        };
        badge.textContent = statusLabels[o.status] || 'Unknown';
    } else {
        badge.className = 'vc-badge ' + (o.configured ? 'vc-badge-active' : 'vc-badge-modified');
        badge.textContent = o.configured ? 'configured' : 'not configured';
    }
    head.appendChild(badge);
    card.appendChild(head);

    if (o.description) {
        const desc = document.createElement('div');
        desc.className = 'conn-card-desc muted';
        desc.textContent = o.description;
        card.appendChild(desc);
    }

    if (mode === 'configured' && o.configured && o.details && o.details.length) {
        const dl = document.createElement('div');
        dl.className = 'conn-card-details';
        o.details.forEach(d => {
            if (!d) return;
            const row = document.createElement('div');
            row.className = 'conn-card-detail-row';
            const lbl = document.createElement('span');
            lbl.className = 'conn-card-detail-label muted';
            lbl.textContent = d.label;
            const val = document.createElement('span');
            val.className = 'conn-card-detail-value' + (d.mono ? ' mono' : '');
            val.textContent = d.value == null ? '—' : String(d.value);
            row.appendChild(lbl);
            row.appendChild(val);
            dl.appendChild(row);
        });
        card.appendChild(dl);
    }

    if (mode === 'live') {
        const meta = document.createElement('div');
        meta.className = 'conn-live-meta';
        const bits = [];
        if (o.latencyMs != null) {
            bits.push('<span class="conn-live-stat"><span class="conn-live-stat-label muted">latency</span>'
                + '<span class="conn-live-stat-value mono">' + Math.round(o.latencyMs) + 'ms</span></span>');
        }
        if (o.checkedAt) {
            bits.push('<span class="conn-live-stat"><span class="conn-live-stat-label muted">checked</span>'
                + '<span class="conn-live-stat-value mono">' + GIQ.fmt.esc(GIQ.fmt.timeAgo(o.checkedAt)) + '</span></span>');
        }
        if (o.status === 'not_configured') {
            bits.push('<span class="conn-live-stat conn-live-not-cfg muted">Not configured. See Settings → Connections.</span>');
        }
        if (bits.length) {
            meta.innerHTML = bits.join('');
            card.appendChild(meta);
        }
    }

    if (mode === 'configured' && !o.configured) {
        const hint = document.createElement('div');
        hint.className = 'conn-card-hint';
        hint.innerHTML = 'Set the required env vars in your <code>.env</code> file to enable this integration.';
        card.appendChild(hint);
    } else if (mode === 'configured' && o.snapshot) {
        const note = document.createElement('div');
        note.className = 'conn-card-snapshot-note muted';
        note.textContent = 'Live health probe lives on Monitor → Integrations.';
        card.appendChild(note);
    }

    if (o.error) {
        const errEl = document.createElement('div');
        errEl.className = 'conn-card-error';
        errEl.textContent = o.error;
        card.appendChild(errEl);
    }

    if (o.configurePath) {
        const link = GIQ.components.jumpLink({
            label: 'Configure',
            href: o.configurePath,
        });
        card.appendChild(link);
    }

    return card;
};

/* ── Track table ────────────────────────────────────────────────────
 *
 * Reusable across Recommendations, Tracks, Playlists, Charts, Audit.
 * Below 700px, renders a card list (same data, different layout).
 *
 * opts:
 *   columns: array of column ids — pick from
 *     'rank' | 'title' | 'artist' | 'album' | 'genre' | 'source' | 'score'
 *     | 'bpm' | 'key' | 'energy' | 'dance' | 'valence' | 'mood'
 *     | 'duration' | 'version' | 'id'
 *   rows:    array of track objects from the API. Common fields:
 *     position, track_id, title, artist, album, genre, source, score,
 *     bpm, key, mode, energy, danceability, valence, mood_tags,
 *     duration, analysis_version
 *   sort:        { field, dir } — current sort state, may be null
 *   sortable:    array of column ids that respond to header clicks
 *                (default: nothing — pages opt in)
 *   onSort:      (field) => void — called when user clicks sortable header
 *   pagination:  { offset, limit, total, onPage } | null
 *   rowAction:   (row, idx) => HTMLElement | null — extra cell at the end
 *                (e.g. debug→ link in Recommendations)
 *   maxScore:    optional number; if absent and 'score' column is present,
 *                computed from rows
 *   empty:       text to show when rows is empty (default 'No tracks.')
 */
GIQ.components.trackTable = function trackTable(opts) {
    const cfg = opts || {};
    const columns = cfg.columns || ['title', 'artist', 'bpm', 'key', 'energy', 'duration'];
    const rows = cfg.rows || [];
    const sort = cfg.sort || null;
    const sortable = cfg.sortable || [];
    const onSort = cfg.onSort || null;
    const pagination = cfg.pagination || null;
    const rowAction = cfg.rowAction || null;
    const empty = cfg.empty || 'No tracks.';

    let maxScore = cfg.maxScore;
    if (maxScore == null && columns.indexOf('score') >= 0) {
        maxScore = 0;
        rows.forEach(r => { if (typeof r.score === 'number' && r.score > maxScore) maxScore = r.score; });
        if (maxScore <= 0) maxScore = 1;
    }

    const wrap = document.createElement('div');
    wrap.className = 'track-table-wrap';

    if (!rows.length) {
        const e = document.createElement('div');
        e.className = 'track-table-empty';
        e.textContent = empty;
        wrap.appendChild(e);
        return wrap;
    }

    /* Table view (default; CSS collapses to cards <700px). */
    const table = document.createElement('div');
    table.className = 'track-table';
    table.style.gridTemplateColumns = _ttGridTemplate(columns, !!rowAction);

    const head = document.createElement('div');
    head.className = 'track-table-row track-table-head';
    columns.forEach(col => {
        const cell = document.createElement('div');
        cell.className = 'tt-h tt-h-' + col;
        const label = _ttHeader(col);
        const isSortable = sortable.indexOf(col) >= 0;
        if (isSortable && onSort) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'tt-sort-btn';
            const arrow = (sort && sort.field === col)
                ? (sort.dir === 'asc' ? ' ↑' : ' ↓')
                : '';
            btn.textContent = label + arrow;
            btn.addEventListener('click', () => onSort(col));
            cell.appendChild(btn);
        } else {
            cell.textContent = label;
        }
        head.appendChild(cell);
    });
    if (rowAction) {
        const cell = document.createElement('div');
        cell.className = 'tt-h tt-h-action';
        head.appendChild(cell);
    }
    table.appendChild(head);

    rows.forEach((r, idx) => {
        const row = document.createElement('div');
        row.className = 'track-table-row';
        columns.forEach(col => row.appendChild(_ttCell(col, r, idx, maxScore)));
        if (rowAction) {
            const cell = document.createElement('div');
            cell.className = 'tt-c tt-c-action';
            try {
                const el = rowAction(r, idx);
                if (el instanceof Element) cell.appendChild(el);
            } catch (e) { /* ignore action errors */ }
            row.appendChild(cell);
        }

        /* Card-mode layout (rendered below 700px via CSS — this just
         * builds the additional card content into a hidden element). */
        const card = document.createElement('div');
        card.className = 'tt-card';
        card.appendChild(_ttCardLayout(columns, r, idx, maxScore, rowAction));
        row.appendChild(card);

        table.appendChild(row);
    });

    wrap.appendChild(table);

    if (pagination) {
        const offset = pagination.offset || 0;
        const limit = pagination.limit || rows.length;
        const total = pagination.total || rows.length;
        const from = total > 0 ? offset + 1 : 0;
        const to = Math.min(offset + rows.length, total);
        const foot = document.createElement('div');
        foot.className = 'track-table-pagination';
        foot.innerHTML = '<span class="mono muted">Showing ' + from + '–' + to + ' of '
            + total.toLocaleString() + '</span>';

        const right = document.createElement('div');
        right.style.display = 'flex';
        right.style.gap = '6px';

        const prev = document.createElement('button');
        prev.type = 'button';
        prev.className = 'vc-btn vc-btn-sm';
        prev.textContent = '← Prev';
        prev.disabled = offset <= 0;
        prev.addEventListener('click', () => {
            if (typeof pagination.onPage === 'function') pagination.onPage(-1);
        });

        const next = document.createElement('button');
        next.type = 'button';
        next.className = 'vc-btn vc-btn-sm';
        next.textContent = 'Next →';
        next.disabled = offset + rows.length >= total;
        next.addEventListener('click', () => {
            if (typeof pagination.onPage === 'function') pagination.onPage(+1);
        });

        right.appendChild(prev);
        right.appendChild(next);
        foot.appendChild(right);
        wrap.appendChild(foot);
    }

    return wrap;
};

function _ttGridTemplate(columns, hasAction) {
    /* Per-column widths matched to _ttHeader / _ttCell. Use auto for the
     * elastic title column; everything else fixed. */
    const widths = {
        rank: '36px',
        title: 'minmax(180px, 2fr)',
        artist: 'minmax(120px, 1fr)',
        album: 'minmax(120px, 1fr)',
        genre: 'minmax(100px, 0.8fr)',
        source: '90px',
        score: '90px',
        bpm: '52px',
        key: '54px',
        energy: '90px',
        dance: '60px',
        valence: '60px',
        mood: '90px',
        duration: '60px',
        version: '64px',
        id: '120px',
    };
    const parts = columns.map(c => widths[c] || '1fr');
    if (hasAction) parts.push('66px');
    return parts.join(' ');
}

function _ttHeader(col) {
    return ({
        rank: '#',
        title: 'Title',
        artist: 'Artist',
        album: 'Album',
        genre: 'Genre',
        source: 'Source',
        score: 'Score',
        bpm: 'BPM',
        key: 'Key',
        energy: 'Energy',
        dance: 'Dance',
        valence: 'Valence',
        mood: 'Mood',
        duration: 'Dur',
        version: 'Version',
        id: 'Track ID',
    })[col] || col;
}

function _ttCell(col, r, idx, maxScore) {
    const cell = document.createElement('div');
    cell.className = 'tt-c tt-c-' + col;
    const esc = GIQ.fmt.esc;

    if (col === 'rank') {
        const n = (typeof r.position === 'number' ? r.position + 1 : idx + 1);
        cell.innerHTML = '<span class="tt-rank mono">' + n + '</span>';
        return cell;
    }
    if (col === 'title') {
        const title = r.title || _ttBasename(r.file_path) || r.track_id || '—';
        const sub = r.album || '';
        cell.innerHTML = '<span class="tt-title" title="' + esc(r.track_id || title) + '">'
            + esc(title) + '</span>'
            + (sub ? '<span class="tt-title-sub">' + esc(sub) + '</span>' : '');
        return cell;
    }
    if (col === 'artist') {
        cell.innerHTML = '<span class="tt-artist" title="' + esc(r.artist || '') + '">'
            + esc(r.artist || '—') + '</span>';
        return cell;
    }
    if (col === 'album') {
        cell.innerHTML = '<span class="tt-truncate" title="' + esc(r.album || '') + '">'
            + esc(r.album || '—') + '</span>';
        return cell;
    }
    if (col === 'genre') {
        cell.innerHTML = '<span class="tt-truncate" title="' + esc(r.genre || '') + '">'
            + esc(r.genre || '—') + '</span>';
        return cell;
    }
    if (col === 'source') {
        cell.innerHTML = r.source
            ? '<span class="rd-source-chip">' + esc(r.source) + '</span>'
            : '<span class="mono muted">—</span>';
        return cell;
    }
    if (col === 'score') {
        if (typeof r.score === 'number') {
            const pct = Math.max(2, Math.min(100, (r.score / (maxScore || 1)) * 100));
            cell.innerHTML = '<div class="tt-score">'
                + '<div class="tt-score-track"><div class="tt-score-fill" style="width:' + pct.toFixed(1) + '%"></div></div>'
                + '<span class="mono">' + r.score.toFixed(3) + '</span>'
                + '</div>';
        } else cell.innerHTML = '<span class="mono muted">—</span>';
        return cell;
    }
    if (col === 'bpm') {
        cell.innerHTML = (typeof r.bpm === 'number')
            ? '<span class="mono">' + r.bpm.toFixed(1) + '</span>'
            : '<span class="mono muted">—</span>';
        return cell;
    }
    if (col === 'key') {
        const key = r.key || '—';
        const mode = r.mode ? ' ' + r.mode.charAt(0) : '';
        cell.innerHTML = (key === '—')
            ? '<span class="mono muted">—</span>'
            : '<span class="mono">' + esc(key + mode) + '</span>';
        return cell;
    }
    if (col === 'energy') {
        if (typeof r.energy === 'number') {
            const pct = Math.max(2, Math.min(100, r.energy * 100));
            cell.innerHTML = '<div class="tt-energy">'
                + '<div class="tt-energy-track"><div class="tt-energy-fill" style="width:' + pct.toFixed(1) + '%"></div></div>'
                + '<span class="mono tt-energy-num">' + r.energy.toFixed(2) + '</span>'
                + '</div>';
        } else cell.innerHTML = '<span class="mono muted">—</span>';
        return cell;
    }
    if (col === 'dance') {
        cell.innerHTML = (typeof r.danceability === 'number')
            ? '<span class="mono">' + r.danceability.toFixed(2) + '</span>'
            : '<span class="mono muted">—</span>';
        return cell;
    }
    if (col === 'valence') {
        cell.innerHTML = (typeof r.valence === 'number')
            ? '<span class="mono">' + r.valence.toFixed(2) + '</span>'
            : '<span class="mono muted">—</span>';
        return cell;
    }
    if (col === 'mood') {
        cell.innerHTML = '<span class="tt-truncate">' + esc(_ttTopMood(r.mood_tags)) + '</span>';
        return cell;
    }
    if (col === 'duration') {
        cell.innerHTML = (typeof r.duration === 'number')
            ? '<span class="mono">' + _ttFmtDur(r.duration) + '</span>'
            : '<span class="mono muted">—</span>';
        return cell;
    }
    if (col === 'version') {
        cell.innerHTML = r.analysis_version
            ? '<span class="mono muted tt-version">' + esc(r.analysis_version) + '</span>'
            : '<span class="mono muted">—</span>';
        return cell;
    }
    if (col === 'id') {
        cell.innerHTML = '<span class="mono muted tt-id" title="' + esc(r.track_id || '') + '">'
            + esc(r.track_id || '—').slice(0, 12) + '</span>';
        return cell;
    }
    cell.textContent = '';
    return cell;
}

function _ttCardLayout(columns, r, idx, maxScore, rowAction) {
    /* Used in <700px card mode. Top line: title + artist. Sub-line:
     * BPM · key · mood · duration. Optional source chip + score bar +
     * trailing rowAction. */
    const wrap = document.createElement('div');
    wrap.className = 'tt-card-inner';
    const esc = GIQ.fmt.esc;

    const top = document.createElement('div');
    top.className = 'tt-card-top';
    const rank = (columns.indexOf('rank') >= 0)
        ? '<span class="tt-rank mono">' + (typeof r.position === 'number' ? r.position + 1 : idx + 1) + '</span>'
        : '';
    const title = r.title || _ttBasename(r.file_path) || r.track_id || '—';
    top.innerHTML = rank
        + '<div class="tt-card-title">'
        + '<div class="tt-card-name">' + esc(title) + '</div>'
        + '<div class="tt-card-artist">' + esc(r.artist || '') + '</div>'
        + '</div>';

    if (columns.indexOf('source') >= 0 && r.source) {
        top.innerHTML += '<span class="rd-source-chip">' + esc(r.source) + '</span>';
    }

    if (columns.indexOf('score') >= 0 && typeof r.score === 'number') {
        const pct = Math.max(2, Math.min(100, (r.score / (maxScore || 1)) * 100));
        top.innerHTML += '<div class="tt-score">'
            + '<div class="tt-score-track"><div class="tt-score-fill" style="width:' + pct.toFixed(1) + '%"></div></div>'
            + '<span class="mono">' + r.score.toFixed(3) + '</span>'
            + '</div>';
    }
    wrap.appendChild(top);

    const sub = document.createElement('div');
    sub.className = 'tt-card-sub mono muted';
    const subParts = [];
    if (typeof r.bpm === 'number') subParts.push(r.bpm.toFixed(0) + ' BPM');
    if (r.key) subParts.push(r.key + (r.mode ? ' ' + r.mode.charAt(0) : ''));
    if (typeof r.energy === 'number') subParts.push('E ' + r.energy.toFixed(2));
    const mood = _ttTopMood(r.mood_tags);
    if (mood && mood !== '—') subParts.push(mood);
    if (typeof r.duration === 'number') subParts.push(_ttFmtDur(r.duration));
    sub.textContent = subParts.join(' · ') || '—';
    wrap.appendChild(sub);

    if (rowAction) {
        const cell = document.createElement('div');
        cell.className = 'tt-card-action';
        try {
            const el = rowAction(r, idx);
            if (el instanceof Element) cell.appendChild(el);
        } catch (_) { /* ignore */ }
        wrap.appendChild(cell);
    }

    return wrap;
}

function _ttTopMood(moodTags) {
    if (!moodTags) return '—';
    if (typeof moodTags === 'string') return moodTags;
    if (!Array.isArray(moodTags) || !moodTags.length) return '—';
    const top = moodTags[0];
    if (top && typeof top === 'object') return top.label || '—';
    return String(top);
}

function _ttFmtDur(sec) {
    if (sec == null) return '—';
    const s = Math.max(0, Math.round(sec));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m + ':' + (r < 10 ? '0' + r : r);
}

function _ttBasename(p) {
    if (!p) return '';
    const i = p.lastIndexOf('/');
    return i >= 0 ? p.slice(i + 1) : p;
}

/* ── Generate Playlist modal (session 10; reused 09, 11) ──────────── */

/* opts.prefill: { strategy?, seed_track_id?, target_track_id?, prompt?,
 *                 mood?, curve?, name? }
 * opts.onCreated: optional fn(playlistDetail) called after a successful POST.
 *                 Default: navigate to #/explore/playlists/{id} on toast click.
 *
 * The modal owns its own state. Uses GIQ.components.modal for the shell.
 */
GIQ.components.generatePlaylistModal = function generatePlaylistModal(opts) {
    const prefill = (opts && opts.prefill) || {};
    const onCreated = (opts && opts.onCreated) || null;

    const STRATEGIES = [
        ['flow', 'Flow (smooth transitions)'],
        ['mood', 'Mood (by feeling)'],
        ['energy_curve', 'Energy Curve (shape)'],
        ['key_compatible', 'Key Compatible (harmonic)'],
        ['path', 'Song Path (A → B sonic bridge)'],
        ['text', 'Text Prompt (CLAP)'],
    ];
    const MOODS = [
        ['happy', 'Happy'], ['sad', 'Sad'], ['aggressive', 'Aggressive'],
        ['relaxed', 'Relaxed'], ['party', 'Party'],
    ];
    const CURVES = [
        ['ramp_up_cool_down', 'Ramp Up + Cool Down'],
        ['ramp_up', 'Ramp Up'],
        ['cool_down', 'Cool Down'],
        ['steady_high', 'Steady High'],
        ['steady_low', 'Steady Low'],
    ];

    const previousFocus = document.activeElement;
    const body = document.createElement('div');
    body.className = 'gp-form';

    function field(label, control, helperText) {
        const wrap = document.createElement('div');
        wrap.className = 'form-field gp-field';
        const lbl = document.createElement('label');
        lbl.textContent = label;
        wrap.appendChild(lbl);
        wrap.appendChild(control);
        if (helperText) {
            const help = document.createElement('div');
            help.className = 'gp-help';
            help.textContent = helperText;
            wrap.appendChild(help);
        }
        return wrap;
    }

    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'gp-input';
    nameInput.value = prefill.name || 'My Playlist';
    nameInput.maxLength = 255;

    const stratSel = document.createElement('select');
    stratSel.className = 'gp-input';
    STRATEGIES.forEach(([v, lbl]) => {
        const o = document.createElement('option');
        o.value = v; o.textContent = lbl;
        if (prefill.strategy === v) o.selected = true;
        stratSel.appendChild(o);
    });

    const seedInput = document.createElement('input');
    seedInput.type = 'text';
    seedInput.className = 'gp-input mono';
    seedInput.placeholder = 'paste a track_id';
    seedInput.value = prefill.seed_track_id || '';

    const targetInput = document.createElement('input');
    targetInput.type = 'text';
    targetInput.className = 'gp-input mono';
    targetInput.placeholder = 'paste a different track_id';
    targetInput.value = prefill.target_track_id || '';

    const promptInput = document.createElement('input');
    promptInput.type = 'text';
    promptInput.className = 'gp-input';
    promptInput.placeholder = 'e.g. upbeat summer night driving';
    promptInput.value = prefill.prompt || '';

    const moodSel = document.createElement('select');
    moodSel.className = 'gp-input';
    MOODS.forEach(([v, lbl]) => {
        const o = document.createElement('option');
        o.value = v; o.textContent = lbl;
        if (prefill.mood === v) o.selected = true;
        moodSel.appendChild(o);
    });

    const curveSel = document.createElement('select');
    curveSel.className = 'gp-input';
    CURVES.forEach(([v, lbl]) => {
        const o = document.createElement('option');
        o.value = v; o.textContent = lbl;
        if (prefill.curve === v) o.selected = true;
        curveSel.appendChild(o);
    });

    const maxInput = document.createElement('input');
    maxInput.type = 'number';
    maxInput.className = 'gp-input';
    maxInput.min = '5';
    maxInput.max = '100';
    maxInput.value = '25';

    body.appendChild(field('Name', nameInput));
    body.appendChild(field('Strategy', stratSel));
    const seedField = field('Seed Track ID', seedInput);
    const targetField = field('Target Track ID (destination)', targetInput);
    const promptField = field('Text Prompt', promptInput, 'Requires CLAP enabled and backfilled.');
    const moodField = field('Mood', moodSel);
    const curveField = field('Curve', curveSel);
    const maxField = field('Max Tracks (5–100)', maxInput);
    body.appendChild(seedField);
    body.appendChild(targetField);
    body.appendChild(promptField);
    body.appendChild(moodField);
    body.appendChild(curveField);
    body.appendChild(maxField);

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'vc-btn';
    cancelBtn.textContent = 'Cancel';

    const generateBtn = document.createElement('button');
    generateBtn.type = 'button';
    generateBtn.className = 'vc-btn vc-btn-primary';
    generateBtn.textContent = 'Generate';

    const handle = GIQ.components.modal({
        title: 'Generate Playlist',
        width: 'sm',
        body: body,
        footer: [cancelBtn, generateBtn],
        onClose() {
            if (previousFocus && typeof previousFocus.focus === 'function') {
                try { previousFocus.focus(); } catch (_) { /* ignore */ }
            }
        },
    });

    /* Show/hide conditional fields based on selected strategy. */
    function applyStrategy() {
        const s = stratSel.value;
        seedField.style.display = (s === 'flow' || s === 'key_compatible' || s === 'path') ? '' : 'none';
        targetField.style.display = (s === 'path') ? '' : 'none';
        promptField.style.display = (s === 'text') ? '' : 'none';
        moodField.style.display = (s === 'mood') ? '' : 'none';
        curveField.style.display = (s === 'energy_curve') ? '' : 'none';
        validate();
    }

    function validate() {
        const s = stratSel.value;
        const name = (nameInput.value || '').trim();
        const max = parseInt(maxInput.value, 10);
        let ok = name.length > 0 && !isNaN(max) && max >= 5 && max <= 100;
        if (s === 'flow' || s === 'key_compatible') ok = ok && seedInput.value.trim().length > 0;
        if (s === 'path') {
            const a = seedInput.value.trim(), b = targetInput.value.trim();
            ok = ok && a.length > 0 && b.length > 0 && a !== b && max >= 3;
        }
        if (s === 'text') ok = ok && promptInput.value.trim().length > 0;
        if (s === 'mood') ok = ok && !!moodSel.value;
        if (s === 'energy_curve') ok = ok && !!curveSel.value;
        generateBtn.disabled = !ok;
    }

    stratSel.addEventListener('change', applyStrategy);
    [nameInput, seedInput, targetInput, promptInput, moodSel, curveSel, maxInput].forEach(el =>
        el.addEventListener('input', validate));

    cancelBtn.addEventListener('click', () => handle.close());

    generateBtn.addEventListener('click', async () => {
        const s = stratSel.value;
        const max = parseInt(maxInput.value, 10) || 25;
        const payload = {
            name: (nameInput.value || '').trim() || 'My Playlist',
            strategy: s,
            max_tracks: Math.max(5, Math.min(100, max)),
        };
        if (s === 'flow' || s === 'key_compatible' || s === 'path') {
            payload.seed_track_id = seedInput.value.trim();
        }
        if (s === 'path') payload.params = { target_track_id: targetInput.value.trim() };
        if (s === 'text') payload.params = { prompt: promptInput.value.trim() };
        if (s === 'mood') payload.params = { mood: moodSel.value };
        if (s === 'energy_curve') payload.params = { curve: curveSel.value };

        generateBtn.disabled = true;
        const oldLabel = generateBtn.textContent;
        generateBtn.textContent = 'Generating…';

        try {
            const detail = await GIQ.api.post('/v1/playlists', payload);
            handle.close();
            const tn = (detail && detail.track_count != null) ? detail.track_count : (detail.tracks ? detail.tracks.length : 0);
            const pid = detail && detail.id;
            const t = GIQ.toast('Playlist generated · ' + tn + ' tracks', 'success', 6000);
            if (typeof onCreated === 'function') {
                try { onCreated(detail); } catch (e) { console.error(e); }
            } else if (pid != null) {
                /* Click the toast (or its body) to jump to the new playlist. */
                if (t) {
                    t.style.cursor = 'pointer';
                    t.title = 'Open playlist';
                    t.addEventListener('click', (ev) => {
                        if (ev.target && ev.target.classList && ev.target.classList.contains('toast-close')) return;
                        GIQ._dismissToast(t);
                        GIQ.router.navigate('explore', 'playlists/' + pid);
                    });
                }
            }
        } catch (e) {
            generateBtn.disabled = false;
            generateBtn.textContent = oldLabel;
            GIQ.toast('Failed to generate playlist: ' + e.message, 'error');
        }
    });

    /* Initial state: apply strategy gating, validate, focus first input. */
    applyStrategy();
    validate();
    setTimeout(() => {
        try {
            const first = body.querySelector('input, select');
            if (first) first.focus();
        } catch (_) { /* ignore */ }
    }, 30);

    return handle;
};
