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

    /* Dotted-path nested getter / setter (used by structured configs). */
    function pathGet(obj, path) {
        if (!path || obj == null) return obj;
        const parts = String(path).split('.');
        let cur = obj;
        for (let i = 0; i < parts.length; i++) {
            if (cur == null) return undefined;
            cur = cur[parts[i]];
        }
        return cur;
    }
    function pathSet(obj, path, value) {
        const parts = String(path).split('.');
        let cur = obj;
        for (let i = 0; i < parts.length - 1; i++) {
            if (cur[parts[i]] == null || typeof cur[parts[i]] !== 'object') cur[parts[i]] = {};
            cur = cur[parts[i]];
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
