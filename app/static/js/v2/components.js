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
