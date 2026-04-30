/* core.js — GIQ namespace, formatters, API wrappers, toast.
 * Loaded first; populates window.GIQ.
 */

window.GIQ = window.GIQ || {};

GIQ.state = GIQ.state || {
    apiKey: null,
    apiKeyValid: false,
    sseConnected: false,
    sidebarCollapsed: false,
};

GIQ.pages = GIQ.pages || {
    explore: {},
    actions: {},
    monitor: {},
    settings: {},
};

GIQ.handle = GIQ.handle || {};

/* ── Formatters ───────────────────────────────────────────────────── */

const _escDiv = document.createElement('div');

GIQ.fmt = {
    esc(s) {
        if (s == null) return '';
        _escDiv.textContent = String(s);
        return _escDiv.innerHTML;
    },

    timeAgo(ts) {
        if (!ts) return '—';
        const diff = Math.floor(Date.now() / 1000) - ts;
        if (diff < 0) return 'in ' + GIQ.fmt._absDur(-diff);
        if (diff < 60) return diff + 's ago';
        if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
        if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
        return Math.floor(diff / 86400) + 'd ago';
    },

    fmtTime(ts) {
        return ts ? new Date(ts * 1000).toLocaleString() : '—';
    },

    fmtDuration(secs) {
        if (secs == null) return '—';
        if (secs < 60) return Math.round(secs) + 's';
        if (secs < 3600) return Math.floor(secs / 60) + 'm ' + Math.round(secs % 60) + 's';
        return Math.floor(secs / 3600) + 'h ' + Math.floor((secs % 3600) / 60) + 'm';
    },

    fmtNumber(n) {
        if (n == null) return '—';
        if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
        if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
        return String(n);
    },

    _absDur(secs) {
        if (secs < 60) return secs + 's';
        if (secs < 3600) return Math.floor(secs / 60) + 'm';
        if (secs < 86400) return Math.floor(secs / 3600) + 'h';
        return Math.floor(secs / 86400) + 'd';
    },
};

/* ── API wrappers ─────────────────────────────────────────────────── */

const BASE = window.location.origin;

function _headers() {
    const h = { 'Content-Type': 'application/json' };
    if (GIQ.state.apiKey) h['Authorization'] = 'Bearer ' + GIQ.state.apiKey;
    return h;
}

async function _parse(res) {
    if (res.status === 204) return null;
    const ct = res.headers.get('content-type') || '';
    if (ct.includes('application/json')) return res.json();
    return res.text();
}

async function _request(method, path, body) {
    const opts = { method, headers: _headers() };
    if (body !== undefined) opts.body = JSON.stringify(body);
    let res;
    try {
        res = await fetch(BASE + path, opts);
    } catch (e) {
        throw new Error('Network error: ' + e.message);
    }
    if (!res.ok) {
        let detail = res.status + ' ' + res.statusText;
        try {
            const data = await res.json();
            if (data && data.detail) detail = data.detail;
        } catch (_) { /* not JSON */ }
        const err = new Error(detail);
        err.status = res.status;
        throw err;
    }
    return _parse(res);
}

GIQ.api = {
    get(path) { return _request('GET', path); },
    post(path, body) { return _request('POST', path, body || {}); },
    put(path, body) { return _request('PUT', path, body || {}); },
    patch(path, body) { return _request('PATCH', path, body || {}); },
    del(path) { return _request('DELETE', path); },

    /* Validate the current key against /health (no auth required, but we
     * include it so the server records the key was used). Returns a bool.
     */
    async validateKey() {
        try {
            const res = await fetch(BASE + '/health', { headers: _headers() });
            return res.ok;
        } catch (_) {
            return false;
        }
    },
};

/* ── API key persistence (sessionStorage) ────────────────────────── */

GIQ.apiKey = {
    KEY: 'giq.apiKey',
    load() {
        try {
            const v = sessionStorage.getItem(this.KEY);
            if (v) GIQ.state.apiKey = v;
        } catch (_) { /* sessionStorage may be disabled */ }
        return GIQ.state.apiKey;
    },
    save(k) {
        GIQ.state.apiKey = k || null;
        try {
            if (k) sessionStorage.setItem(this.KEY, k);
            else sessionStorage.removeItem(this.KEY);
        } catch (_) { /* ignore */ }
    },
    clear() { this.save(null); },
};

/* ── Toast ────────────────────────────────────────────────────────── */

GIQ.toast = function (message, kind, duration) {
    /* Accepts either a string + (kind, duration) or a single object form:
     *   GIQ.toast({ message, kind?, duration?, jump?: { hash, label } }). */
    let opts;
    if (message && typeof message === 'object' && !Array.isArray(message)) {
        opts = message;
    } else {
        opts = { message: String(message), kind, duration };
    }
    const k = opts.kind || 'info';
    const dur = opts.duration != null ? opts.duration : (k === 'error' ? 7000 : 4000);
    let stack = document.getElementById('toast-stack');
    if (!stack) {
        stack = document.createElement('div');
        stack.id = 'toast-stack';
        document.body.appendChild(stack);
    }
    const icons = { success: '✓', error: '!', warning: '!', info: 'i' };
    const t = document.createElement('div');
    t.className = 'toast toast-' + k + (opts.jump ? ' toast-with-jump' : '');
    t.innerHTML = '<span class="toast-icon">' + (icons[k] || icons.info) + '</span>'
                + '<div class="toast-body"></div>';
    t.querySelector('.toast-body').textContent = String(opts.message != null ? opts.message : '');
    if (opts.jump && opts.jump.hash) {
        const a = document.createElement('a');
        a.className = 'toast-jump';
        a.href = opts.jump.hash;
        a.textContent = opts.jump.label || 'View →';
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
    if (dur > 0) setTimeout(() => GIQ._dismissToast(t), dur);
    return t;
};

GIQ._dismissToast = function (t) {
    if (!t || !t.parentNode || t.classList.contains('toast-out')) return;
    t.classList.add('toast-out');
    setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, 240);
};
