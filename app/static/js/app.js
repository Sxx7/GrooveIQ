// =========================================================================
// Global Error Handler
// =========================================================================
window.onerror = function(msg, src, line) {
  var el = document.getElementById('js-errors');
  if (el) { el.style.display = 'block'; el.innerHTML += '<div style="padding:6px 12px"><b>JS Error:</b> ' + esc(msg) + ' (line ' + line + ')</div>'; }
  return false;
};
window.addEventListener('unhandledrejection', function(e) {
  var el = document.getElementById('js-errors');
  if (el) { el.style.display = 'block'; el.innerHTML += '<div style="padding:6px 12px"><b>Promise:</b> ' + esc(e.reason ? (e.reason.message || e.reason) : 'unknown') + '</div>'; }
});

// =========================================================================
// Core State
// =========================================================================
var $ = function(s) { return document.querySelector(s); };
var BASE = window.location.origin;
var KEY = '';
var refreshTimer = null;
var scanPollTimer = null;
var scanLogLastId = 0;
var currentView = 'dashboard';
var trackState = { offset: 0, limit: 50, sort: 'bpm', dir: 'asc', total: 0, search: '' };
var userDetailState = { userId: '', historyOffset: 0, historyLimit: 25 };
var cachedUsers = [];

// Pipeline state
var pipelineSSE = null;
var pipelineData = { current: null, history: [], models: null };
var pipelineSelectedStep = null;
var pipelineSelectedRun = null;

// Charts state
var chartsCurrentScope = 'global';
var chartsCurrentType = 'top_tracks';

// =========================================================================
// Utilities
// =========================================================================
var _escDiv = document.createElement('div');
function esc(s) { if (s == null) return ''; _escDiv.textContent = String(s); return _escDiv.innerHTML; }

function headers() { return { 'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json' }; }

function api(path) {
  return fetch(BASE + path, { headers: headers() }).then(function(res) {
    if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
    return res.json();
  });
}

function apiPost(path, body) {
  return fetch(BASE + path, { method: 'POST', headers: headers(), body: JSON.stringify(body || {}) }).then(function(res) {
    if (!res.ok) return res.json().then(function(d) { throw new Error(d.detail || res.statusText); });
    return res.json();
  });
}

function apiPatch(path, body) {
  return fetch(BASE + path, { method: 'PATCH', headers: headers(), body: JSON.stringify(body) }).then(function(res) {
    if (!res.ok) return res.json().then(function(d) { throw new Error(d.detail || res.statusText); });
    return res.json();
  });
}

function apiPut(path, body) {
  return fetch(BASE + path, { method: 'PUT', headers: headers(), body: JSON.stringify(body) }).then(function(res) {
    if (!res.ok) return res.json().then(function(d) { throw new Error(d.detail || res.statusText); });
    return res.json();
  });
}

function apiDelete(path) {
  return fetch(BASE + path, { method: 'DELETE', headers: headers() }).then(function(res) {
    if (!res.ok) throw new Error(res.status);
  });
}

function timeAgo(ts) {
  if (!ts) return '\u2014';
  var diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleString() : '\u2014'; }

function fmtDuration(secs) {
  if (secs == null) return '\u2014';
  if (secs < 60) return Math.round(secs) + 's';
  if (secs < 3600) return Math.floor(secs / 60) + 'm ' + Math.round(secs % 60) + 's';
  return Math.floor(secs / 3600) + 'h ' + Math.floor((secs % 3600) / 60) + 'm';
}

function fmtTrackDur(secs) {
  if (!secs) return '\u2014';
  var m = Math.floor(secs / 60), s = Math.round(secs % 60);
  return m + ':' + (s < 10 ? '0' : '') + s;
}

function fmtMs(ms) {
  if (ms == null) return '\u2014';
  if (ms < 1000) return ms + 'ms';
  if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
  return Math.floor(ms / 60000) + 'm ' + Math.round((ms % 60000) / 1000) + 's';
}

function fmtNumber(n) {
  if (n == null) return '\u2014';
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return '' + n;
}

function topMood(tags) {
  if (!tags || !tags.length) return '\u2014';
  var best = tags[0];
  for (var i = 1; i < tags.length; i++) { if (tags[i].confidence > best.confidence) best = tags[i]; }
  return best.confidence > 0.4 ? esc(best.label) : '\u2014';
}

function basename(path) {
  if (!path) return '\u2014';
  return esc(path.split('/').pop());
}

function trackName(t) {
  if (t.title && t.artist) return esc(t.artist) + ' \u2014 ' + esc(t.title);
  if (t.title) return esc(t.title);
  return basename(t.file_path);
}

function trackTooltip(t) {
  return esc([t.artist, t.title, t.album, t.file_path].filter(Boolean).join(' | '));
}

// --- Badge generators ---
function eventBadge(type) {
  var colors = { play_end:'success',like:'success',playlist_add:'success',repeat:'success',seek_back:'success',queue_add:'success',play_start:'success', skip:'danger',dislike:'danger', volume_up:'info',volume_down:'info', pause:'warning',resume:'warning',rating:'warning',seek_forward:'warning',reco_impression:'primary' };
  return '<span class="badge badge-' + (colors[type] || 'primary') + '">' + esc(type) + '</span>';
}

function strategyBadge(s) {
  var colors = { flow: 'primary', mood: 'success', energy_curve: 'warning', key_compatible: 'info' };
  return '<span class="badge badge-' + (colors[s] || 'primary') + '">' + esc(s).replace('_', ' ') + '</span>';
}

function sourceBadge(s) {
  var cls = 'badge ';
  if (s && s.indexOf('content') === 0) cls += 'source-content';
  else if (s === 'cf') cls += 'source-cf';
  else if (s && s.indexOf('artist') === 0) cls += 'source-artist';
  else if (s === 'popular') cls += 'source-popular';
  else cls += 'badge-primary';
  return '<span class="' + cls + '">' + esc(s || 'unknown') + '</span>';
}

function scoreBar(score, maxScore) {
  if (score == null) return '\u2014';
  var pct = maxScore > 0 ? Math.min(score / maxScore * 100, 100) : 0;
  var color = pct > 70 ? 'var(--color-success)' : pct > 40 ? 'var(--color-warning)' : 'var(--color-danger)';
  return score.toFixed(3) + '<div class="score-bar"><div class="score-fill" style="width:' + pct + '%;background:' + color + '"></div></div>';
}

function stepStatusColor(s) {
  if (s === 'completed') return 'success'; if (s === 'failed') return 'danger';
  if (s === 'running') return 'warning'; if (s === 'skipped') return 'info'; return 'primary';
}

function fmtMetricLabel(k) { return k.replace(/_/g, ' '); }

function fmtMetricVal(val) {
  if (val === true) return '\u2713'; if (val === false) return '\u2717';
  if (typeof val === 'number') return Number.isInteger(val) ? val.toLocaleString() : val.toFixed(2);
  return esc(String(val));
}

function firstMetricKey(metrics) {
  if (!metrics) return null;
  var preferred = ['sessions_created','interactions_created','users_updated','training_samples','vocab_size','seeds_cached','trained'];
  for (var i = 0; i < preferred.length; i++) { if (metrics[preferred[i]] != null) return preferred[i]; }
  var keys = Object.keys(metrics);
  return keys.length ? keys[0] : null;
}

// =========================================================================
// Navigation & Tab Switching
// =========================================================================
document.getElementById('nav-links').addEventListener('click', function(e) {
  var btn = e.target.closest('.nav-link');
  if (!btn) return;
  // Close mobile menu on tab select
  document.getElementById('nav-links').classList.remove('open');
  switchTab(btn.getAttribute('data-tab'));
});

// Hamburger toggle for mobile
document.getElementById('nav-toggle').addEventListener('click', function() {
  document.getElementById('nav-links').classList.toggle('open');
});

var contentSubTab = 'recommendations';

function switchTab(view) {
  // Map legacy tab names to content sub-tabs
  var contentSubs = { recommendations:1, tracks:1, playlists:1, radio:1, charts:1, discovery:1, news:1 };
  if (contentSubs[view]) { contentSubTab = view; view = 'content'; }

  currentView = view;
  var links = document.querySelectorAll('.nav-link');
  for (var i = 0; i < links.length; i++) {
    links[i].classList.toggle('active', links[i].getAttribute('data-tab') === view);
  }
  // Disconnect pipeline SSE when leaving pipeline tab
  if (view !== 'pipeline') pipelineDisconnectSSE();

  if (view === 'dashboard') loadDashboard();
  else if (view === 'pipeline') loadPipeline();
  else if (view === 'content') loadContent(contentSubTab);
  else if (view === 'users') loadUsers();
  else if (view === 'connections') loadConnections();
  else if (view === 'algorithm') loadAlgorithm();
}

function contentSubTabBar() {
  var subs = [
    { id: 'recommendations', label: 'Recommendations' },
    { id: 'tracks', label: 'Tracks' },
    { id: 'playlists', label: 'Playlists' },
    { id: 'radio', label: 'Radio' },
    { id: 'charts', label: 'Charts' },
    { id: 'discovery', label: 'Discovery' },
    { id: 'news', label: 'News' }
  ];
  var bar = '<div class="subtab-bar">';
  for (var i = 0; i < subs.length; i++) {
    bar += '<button class="subtab' + (subs[i].id === contentSubTab ? ' active' : '') + '" onclick="loadContent(\'' + subs[i].id + '\')">' + subs[i].label + '</button>';
  }
  return bar + '</div>';
}

function loadContent(sub) {
  contentSubTab = sub || contentSubTab;
  if (contentSubTab === 'recommendations') loadRecommendations();
  else if (contentSubTab === 'tracks') loadTracks();
  else if (contentSubTab === 'playlists') loadPlaylists();
  else if (contentSubTab === 'radio') loadRadio();
  else if (contentSubTab === 'charts') loadCharts();
  else if (contentSubTab === 'discovery') loadDiscovery();
  else if (contentSubTab === 'news') loadNews();
}

// Inject sub-tab bar before content for content sub-views
function setAppContent(html) {
  $('#app').innerHTML = (currentView === 'content' ? contentSubTabBar() : '') + html;
}

// =========================================================================
// Connection
// =========================================================================
function connect() {
  KEY = $('#api-key').value.trim();
  if (!KEY) return;
  sessionStorage.setItem('grooveiq_key', KEY);
  api('/v1/users?limit=200').then(function(u) { cachedUsers = u; }).catch(function(){});
  fetch(BASE + '/health').then(function(r) { return r.json(); }).then(function(d) {
    var b = document.getElementById('auth-banner');
    if (b && d.auth_disabled) b.style.display = 'block';
  }).catch(function(){});
  loadDashboard().then(function() {
    $('#refresh-info').style.display = 'flex';
    $('#nav-links').classList.add('connected');
    startAutoRefresh();
  }).catch(function(e) {
    $('#app').innerHTML = '<div class="empty" style="color:var(--color-danger)">Connection failed: ' + esc(e.message) + '</div>';
  });
}

function startAutoRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(function() { if (currentView === 'dashboard') loadDashboard(); }, 10000);
}

// =========================================================================
// Dashboard View
// =========================================================================
function loadDashboard() {
  return Promise.all([api('/v1/stats'), api('/v1/users?limit=20'), api('/v1/events?limit=30'), api('/v1/stats/model').catch(function() { return null; })])
    .then(function(results) { renderDashboard(results[0], results[1], results[2], results[3]); });
}

function scanPhaseLabel(scan) {
  var phase = scan.phase || scan.status;
  if (phase === 'discovering') return 'Discovering files\u2026';
  if (phase === 'processing') {
    if (scan.files_analyzed > 0) return 'Analyzing new/changed files';
    return 'Checking existing files';
  }
  if (phase === 'finalizing') return 'Rebuilding indexes\u2026';
  if (phase === 'completed') return 'Completed';
  if (phase === 'failed') return 'Failed';
  if (phase === 'interrupted') return 'Interrupted';
  return phase;
}

function renderScanPanel(scan) {
  if (!scan) return '<div class="empty">No scans yet. Click "Scan Now" to analyze your library.</div>';
  var cls = scan.status === 'completed' ? 'success' : scan.status === 'running' ? 'warning' : scan.status === 'interrupted' ? 'info' : 'danger';
  var h = '';

  // Phase indicator (prominent, only while running)
  if (scan.status === 'running') {
    var phaseText = scanPhaseLabel(scan);
    h += '<div style="padding:var(--space-2) var(--space-5) var(--space-1);display:flex;align-items:center;gap:var(--space-2);font-size:0.875rem;font-weight:600" class="text-warning">';
    h += '<span class="pulse" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:currentColor"></span> ';
    h += esc(phaseText);
    h += '</div>';
  }

  h += '<div style="display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-3);padding:var(--space-3) var(--space-5)">';
  h += '<span>Status: <span class="badge badge-' + cls + '">' + esc(scan.status).toUpperCase() + '</span></span>';
  if (scan.elapsed_seconds) h += '<span class="text-sm">Elapsed: <strong>' + fmtDuration(scan.elapsed_seconds) + '</strong></span>';
  if (scan.status === 'running' && scan.eta_seconds != null) h += '<span class="text-sm">ETA: <strong class="text-warning">' + fmtDuration(scan.eta_seconds) + '</strong></span>';
  if (scan.check_rate) h += '<span class="text-sm">Check: <strong>' + scan.check_rate + '</strong>/s</span>';
  if (scan.analyze_rate) h += '<span class="text-sm">Analyze: <strong>' + scan.analyze_rate + '</strong>/s</span>';
  h += '</div>';
  if (scan.files_found > 0) {
    var pct = scan.percent_complete || 0;
    var proc = scan.files_analyzed + (scan.files_skipped || 0) + scan.files_failed;
    h += '<div style="padding:var(--space-1) var(--space-5) var(--space-3)"><div class="progress-bar" style="height:28px"><div class="progress-fill" style="width:' + pct + '%"></div><div class="progress-text">' + pct + '%  \u2014  ' + proc + ' / ' + scan.files_found + ' files</div></div></div>';
  }
  h += '<div style="display:flex;gap:var(--space-6);padding:var(--space-1) var(--space-5) var(--space-2);font-size:0.8125rem">';
  h += '<span class="text-success">\u2713 Analyzed: <strong>' + scan.files_analyzed + '</strong></span>';
  h += '<span style="color:var(--color-info)">\u21bb Skipped: <strong>' + (scan.files_skipped || 0) + '</strong></span>';
  h += '<span class="text-danger">\u2717 Failed: <strong>' + scan.files_failed + '</strong></span>';
  h += '<span class="text-muted">Found: <strong>' + scan.files_found + '</strong></span></div>';
  if (scan.status === 'running' && scan.current_file) {
    h += '<div style="padding:var(--space-1) var(--space-5) var(--space-2);font-size:0.75rem" class="text-muted">\u25b6 Processing: <span class="font-mono" style="color:var(--text-primary)">' + esc(scan.current_file) + '</span></div>';
  }
  h += '<div style="padding:var(--space-1) var(--space-5) var(--space-2);font-size:0.75rem" class="text-muted">Started: ' + fmtTime(scan.started_at);
  if (scan.ended_at) h += ' &nbsp;\u2022&nbsp; Ended: ' + fmtTime(scan.ended_at);
  h += '</div>';
  return h;
}

function renderDashboard(stats, users, events, model) {
  var maxEvt = 1;
  var evtKeys = Object.keys(stats.event_types_24h || {});
  for (var i = 0; i < evtKeys.length; i++) { if (stats.event_types_24h[evtKeys[i]] > maxEvt) maxEvt = stats.event_types_24h[evtKeys[i]]; }

  var h = '<div class="stats-grid">';
  h += '<div class="stat-card"><div class="stat-label">Total Events</div><div class="stat-value">' + (stats.total_events||0).toLocaleString() + '</div><div class="stat-sub">' + stats.events_last_24h + ' in last 24h</div></div>';
  h += '<div class="stat-card"><div class="stat-label">Active Users</div><div class="stat-value">' + stats.total_users + '</div></div>';
  h += '<div class="stat-card"><div class="stat-label">Total Tracks</div><div class="stat-value" id="stat-tracks">' + stats.total_tracks_analyzed + '</div><div class="stat-sub">analysis v' + (stats.analysis_version||'?') + '</div></div>';
  h += '<div class="stat-card"><div class="stat-label">Playlists</div><div class="stat-value">' + (stats.total_playlists||0) + '</div></div>';
  h += '<div class="stat-card"><div class="stat-label">Events / Hour</div><div class="stat-value">' + stats.events_last_1h + '</div><div class="stat-sub">last 60 minutes</div></div>';
  if (model && model.ranker) {
    var r = model.ranker;
    h += '<div class="stat-card"><div class="stat-label">Ranker Model</div><div class="stat-value" style="font-size:1rem">' + esc(r.model_type || 'N/A') + '</div><div class="stat-sub">' + (r.n_samples ? r.n_samples + ' samples' : 'not trained');
    if (r.trained_at) h += ' \u00B7 ' + timeAgo(r.trained_at);
    h += '</div></div>';
  }
  h += '</div>';

  h += '<div class="grid-2">';
  // Model evaluation panel
  if (model && (model.latest_evaluation || model.impressions)) {
    h += '<div class="card"><div class="card-header">Recommendation Model</div><div class="card-body">';
    if (model.latest_evaluation) {
      var ev = model.latest_evaluation;
      h += '<div class="profile-grid">';
      if (ev.ndcg_at_10 != null) h += '<div class="profile-item"><div class="pval">' + ev.ndcg_at_10.toFixed(4) + '</div><div class="plbl">NDCG@10</div></div>';
      if (ev.ndcg_at_50 != null) h += '<div class="profile-item"><div class="pval">' + ev.ndcg_at_50.toFixed(4) + '</div><div class="plbl">NDCG@50</div></div>';
      if (ev.evaluated_users != null) h += '<div class="profile-item"><div class="pval">' + ev.evaluated_users + '</div><div class="plbl">Test Users</div></div>';
      if (ev.baseline_ndcg_at_10 != null) h += '<div class="profile-item"><div class="pval">' + ev.baseline_ndcg_at_10.toFixed(4) + '</div><div class="plbl">Baseline NDCG@10</div></div>';
      h += '</div>';
    }
    if (model.impressions) {
      var imp = model.impressions;
      h += '<div style="padding:var(--space-2) var(--space-4);font-size:0.8125rem;border-top:1px solid var(--border)">Impressions: <strong>' + (imp.impressions || 0) + '</strong> &nbsp;\u00B7&nbsp; Streams: <strong>' + (imp.streams_from_reco || 0) + '</strong>';
      if (imp.i2s_rate != null) h += ' &nbsp;\u00B7&nbsp; I2S Rate: <strong class="text-success">' + (imp.i2s_rate * 100).toFixed(1) + '%</strong>';
      h += '</div>';
    }
    h += '</div></div>';
  }
  // Event types
  h += '<div class="card"><div class="card-header">Event Types (24h)</div><div class="card-body">';
  if (!evtKeys.length) { h += '<div class="empty">No events in the last 24 hours</div>'; }
  else { for (var i = 0; i < evtKeys.length; i++) { var k = evtKeys[i], c = stats.event_types_24h[k]; h += '<div class="bar-row"><div class="bar-label">' + eventBadge(k) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (c/maxEvt*100).toFixed(1) + '%"></div></div><div class="bar-count">' + c + '</div></div>'; } }
  h += '</div></div>';
  // Users
  h += '<div class="card"><div class="card-header">Users</div><div class="card-body">';
  if (!users.length) { h += '<div class="empty">No users yet</div>'; }
  else { h += '<table><tr><th>UID</th><th>User</th><th>Events</th><th>Last Seen</th></tr>'; for (var i = 0; i < users.length; i++) { var u = users[i]; h += '<tr class="clickable" onclick="switchTab(\'users\');setTimeout(function(){viewUser(\'' + esc(u.user_id) + '\')},100)"><td class="mono muted">' + (u.uid||'') + '</td><td><strong>' + esc(u.user_id) + '</strong></td><td>' + u.event_count + '</td><td>' + timeAgo(u.last_seen) + '</td></tr>'; } h += '</table>'; }
  h += '</div></div>';
  // Top tracks
  h += '<div class="card"><div class="card-header">Top Tracks (24h)</div><div class="card-body">';
  var tt = stats.top_tracks_24h || [];
  if (!tt.length) { h += '<div class="empty">No track activity in the last 24 hours</div>'; }
  else { h += '<table><tr><th>Track</th><th>Events</th></tr>'; for (var i = 0; i < tt.length; i++) { var name = (tt[i].artist && tt[i].title) ? esc(tt[i].artist) + ' \u2014 ' + esc(tt[i].title) : tt[i].title ? esc(tt[i].title) : esc(tt[i].track_id); h += '<tr><td class="truncate" title="ID: ' + esc(tt[i].track_id) + '">' + name + '</td><td>' + tt[i].events + '</td></tr>'; } h += '</table>'; }
  h += '</div></div>';
  h += '</div>'; // close grid-2

  // Library scan
  h += '<div class="card" style="margin-top:var(--space-4)"><div class="card-header">Library Scan <div class="page-actions"><button class="btn btn-secondary btn-sm" onclick="triggerSync()">Sync IDs</button><button class="btn btn-secondary btn-sm" onclick="triggerScan()">Scan Now</button></div></div>';
  h += '<div class="card-body" id="scan-panel">' + renderScanPanel(stats.latest_scan) + '</div>';
  h += '<div style="border-top:1px solid var(--border)"><div style="padding:var(--space-3) var(--space-5);font-size:0.75rem;font-weight:600;color:var(--text-muted);display:flex;justify-content:space-between;align-items:center"><span>Activity Log</span>';
  if (stats.latest_scan && stats.latest_scan.status === 'running') h += '<span class="badge badge-warning" style="animation:pulse 1.5s infinite">LIVE</span>';
  h += '</div><div class="scan-log" id="scan-log"><div style="padding:var(--space-2) var(--space-3);font-size:0.6875rem;color:var(--text-muted);font-style:italic">Waiting for scan activity...</div></div></div></div>';

  // Recent events
  h += '<div class="card" style="margin-top:var(--space-4)"><div class="card-header">Recent Events</div><div class="card-body" style="overflow-x:auto">';
  if (!events.length) { h += '<div class="empty">No events recorded yet</div>'; }
  else {
    h += '<table><tr><th>Time</th><th>User</th><th>Track</th><th>Event</th><th>Value</th><th>Source</th><th>Device</th></tr>';
    for (var i = 0; i < events.length; i++) {
      var e = events[i];
      var src = [e.context_type, e.surface].filter(Boolean).join(' / ') || e.client_id || '\u2014';
      var dev = [e.device_type, e.device_id ? e.device_id.slice(0,12) : null].filter(Boolean).join(' ') || '\u2014';
      var val = '\u2014';
      if (e.value != null) {
        if (e.event_type === 'play_end' || e.event_type === 'pause' || e.event_type === 'resume' || e.event_type === 'skip') val = (e.value * 100).toFixed(0) + '%';
        else if (e.event_type === 'rating') val = e.value.toFixed(1);
        else val = e.value;
      } else if (e.dwell_ms != null && e.dwell_ms > 0) { val = fmtDuration(Math.round(e.dwell_ms / 1000)); }
      h += '<tr><td class="nowrap">' + timeAgo(e.timestamp) + '</td><td><strong>' + esc(e.user_id) + '</strong></td><td class="mono text-sm">' + esc(e.track_id) + '</td><td>' + eventBadge(e.event_type) + '</td><td>' + val + '</td><td>' + esc(src) + '</td><td>' + esc(dev) + '</td></tr>';
    }
    h += '</table>';
  }
  h += '</div></div>';

  // Health panels placeholder
  h += '<div id="health-panels"></div>';
  $('#app').innerHTML = h;

  // Scan log handling
  if (stats.latest_scan) {
    if (stats.latest_scan.status === 'running') {
      startScanPolling(stats.latest_scan.scan_id);
    } else { stopScanPolling(); }
    loadScanLogs(stats.latest_scan.scan_id, stats.latest_scan.status === 'running' ? 100 : 50);
  }

  // Load health panels async
  loadHealthPanels(stats);
}

function loadScanLogs(scanId, limit) {
  api('/v1/library/scan/' + scanId + '/logs?limit=' + limit).then(function(logs) {
    var el = document.getElementById('scan-log');
    if (!el || !logs.length) return;
    el.innerHTML = '';
    for (var i = 0; i < logs.length; i++) appendLogLine(el, logs[i]);
    if (logs.length) scanLogLastId = logs[logs.length - 1].id;
    el.scrollTop = el.scrollHeight;
  }).catch(function(){});
}

function appendLogLine(el, l) {
  var cls = l.level === 'ok' ? 'color:var(--color-success)' : l.level === 'fail' ? 'color:var(--color-danger)' : 'color:var(--text-muted)';
  var icon = l.level === 'ok' ? '\u2713' : l.level === 'fail' ? '\u2717' : '\u2022';
  var line = document.createElement('div');
  line.style.cssText = 'padding:3px var(--space-3);font-size:0.75rem;font-family:var(--font-mono);border-bottom:1px solid var(--divider);' + cls;
  line.textContent = icon + ' ' + (l.filename || '') + (l.message ? '  ' + l.message : '');
  el.appendChild(line);
}

function startScanPolling(scanId) {
  if (scanPollTimer) clearInterval(scanPollTimer);
  scanLogLastId = 0;
  scanPollTimer = setInterval(function() {
    if (currentView !== 'dashboard') return;
    api('/v1/stats').then(function(stats) {
      var panel = document.getElementById('scan-panel');
      if (panel) panel.innerHTML = renderScanPanel(stats.latest_scan);
      var tc = document.getElementById('stat-tracks');
      if (tc) tc.textContent = stats.total_tracks_analyzed;
      if (!stats.latest_scan || stats.latest_scan.status !== 'running') { stopScanPolling(); loadDashboard(); }
    });
    api('/v1/library/scan/' + scanId + '/logs?limit=50&after_id=' + scanLogLastId).then(function(logs) {
      if (!logs.length) return;
      scanLogLastId = logs[logs.length - 1].id;
      var el = document.getElementById('scan-log');
      if (!el) return;
      for (var i = 0; i < logs.length; i++) appendLogLine(el, logs[i]);
      el.scrollTop = el.scrollHeight;
    });
  }, 3000);
}

function stopScanPolling() { if (scanPollTimer) { clearInterval(scanPollTimer); scanPollTimer = null; } }

function loadHealthPanels(stats) {
  Promise.all([
    api('/v1/pipeline/stats/events').catch(function(){return null;}),
    api('/v1/pipeline/stats/activity?days=7').catch(function(){return null;}),
    api('/v1/pipeline/stats/engagement').catch(function(){return null;})
  ]).then(function(results) {
    var html = '';
    if (results[0] && results[0].buckets && results[0].buckets.length > 0) html += renderEventSparkline(results[0]);
    if (stats.library_coverage) html += renderLibraryCoverage(stats.library_coverage);
    if (results[1] && results[1].buckets && results[1].buckets.length > 0) html += renderActivityTimeline(results[1]);
    if (results[2] && results[2].users && results[2].users.length > 0) html += renderEngagementTable(results[2]);
    var el = document.getElementById('health-panels');
    if (el && html) el.innerHTML = '<h2 style="font-size:1rem;margin:var(--space-6) 0 var(--space-4);color:var(--text-secondary)">System Health & Trends</h2>' + html;
  });
}

function renderEventSparkline(data) {
  var buckets = data.buckets, w = 800, h = 60, pad = 4;
  var maxCount = 1;
  for (var i = 0; i < buckets.length; i++) { if (buckets[i].count > maxCount) maxCount = buckets[i].count; }
  var bars = [], barW = Math.max(2, (w - pad * 2) / buckets.length - 1);
  for (var i = 0; i < buckets.length; i++) {
    var x = pad + i * ((w - pad * 2) / buckets.length), bh = (buckets[i].count / maxCount) * (h - pad * 2);
    bars.push('<rect x="' + x.toFixed(1) + '" y="' + (h - pad - bh).toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + bh.toFixed(1) + '" fill="var(--color-primary)" opacity="0.7" rx="1"/>');
  }
  var total = 0; for (var i = 0; i < buckets.length; i++) total += buckets[i].count;
  return '<div class="card" style="margin-bottom:var(--space-4)"><div class="card-header">Event Ingest Rate (24h) <span class="subtitle">' + total.toLocaleString() + ' events</span></div><div class="card-body chart-container"><svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" style="width:100%;height:auto">' + bars.join('') + '</svg></div></div>';
}

function renderLibraryCoverage(cov) {
  var total = cov.total_files || 0, analyzed = cov.total_analyzed || 0;
  var pct = total > 0 ? (analyzed / total * 100).toFixed(1) : 0;
  var vd = cov.version_distribution || {}, vKeys = Object.keys(vd);
  var failed = cov.failed_files || [];
  var html = '<div class="card" style="margin-bottom:var(--space-4)"><div class="card-header">Library Coverage</div><div class="card-body">';
  html += '<div style="padding:var(--space-3) var(--space-4)"><div style="font-size:0.8125rem;margin-bottom:var(--space-1)">Analyzed: <strong>' + analyzed + '</strong> / ' + total + ' files (' + pct + '%)</div>';
  html += '<div class="progress-bar"><div class="progress-fill" style="width:' + pct + '%"></div><div class="progress-text">' + pct + '%</div></div></div>';
  if (vKeys.length > 0) {
    var maxVer = 1; for (var i = 0; i < vKeys.length; i++) { if (vd[vKeys[i]] > maxVer) maxVer = vd[vKeys[i]]; }
    html += '<div style="border-top:1px solid var(--border);padding-top:var(--space-2)">';
    for (var i = 0; i < vKeys.length; i++) html += '<div class="bar-row"><div class="bar-label">v' + esc(vKeys[i]) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (vd[vKeys[i]]/maxVer*100).toFixed(1) + '%;background:var(--color-info)"></div></div><div class="bar-count">' + vd[vKeys[i]] + '</div></div>';
    html += '</div>';
  }
  if (failed.length > 0) {
    html += '<div style="border-top:1px solid var(--border);padding:var(--space-2) var(--space-4)"><div style="font-size:0.75rem;font-weight:600;color:var(--color-danger);margin-bottom:var(--space-1)">Failed Files (' + failed.length + ')</div>';
    for (var i = 0; i < Math.min(failed.length, 10); i++) html += '<div style="font-size:0.6875rem;font-family:var(--font-mono);color:var(--text-muted);padding:2px 0">\u2717 ' + esc(failed[i].filename || '') + (failed[i].message ? ' \u2014 ' + esc(failed[i].message) : '') + '</div>';
    html += '</div>';
  }
  html += '</div></div>';
  return html;
}

function renderActivityTimeline(data) {
  var buckets = data.buckets; if (!buckets.length) return '';
  var allTypes = {}; for (var i = 0; i < buckets.length; i++) { var keys = Object.keys(buckets[i]); for (var j = 0; j < keys.length; j++) { if (keys[j] !== 'timestamp') allTypes[keys[j]] = true; } }
  var types = Object.keys(allTypes);
  var typeColors = { play_start:'#22c55e',play_end:'#16a34a',skip:'#ef4444',like:'#0ea5e9',dislike:'#f97316',pause:'#f59e0b',resume:'#7c3aed',reco_impression:'#6366f1',playlist_add:'#14b8a6',rating:'#d946ef',repeat:'#38bdf8',queue_add:'#818cf8' };
  var defCols = ['#6366f1','#22c55e','#ef4444','#0ea5e9','#f59e0b','#7c3aed','#f97316'];
  var w = 800, h = 120, pad = 30, n = buckets.length;
  if (n < 2) return '';
  var dx = (w - pad * 2) / (n - 1);
  var maxStack = 1;
  for (var i = 0; i < n; i++) { var sum = 0; for (var t = 0; t < types.length; t++) sum += buckets[i][types[t]] || 0; if (sum > maxStack) maxStack = sum; }
  var svg = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" style="width:100%;height:auto">';
  for (var t = types.length - 1; t >= 0; t--) {
    var color = typeColors[types[t]] || defCols[t % defCols.length];
    var pts = pad + ',' + (h - pad);
    for (var i = 0; i < n; i++) { var x = pad + i * dx, stackSum = 0; for (var tt = 0; tt <= t; tt++) stackSum += buckets[i][types[tt]] || 0; pts += ' ' + x.toFixed(1) + ',' + ((h - pad) - (stackSum / maxStack) * (h - pad * 2)).toFixed(1); }
    pts += ' ' + (pad + (n - 1) * dx).toFixed(1) + ',' + (h - pad);
    svg += '<polygon points="' + pts + '" fill="' + color + '" opacity="0.6"/>';
  }
  svg += '</svg>';
  var legend = '<div class="legend-row">'; for (var t = 0; t < types.length; t++) { legend += '<div class="legend-item"><div class="legend-dot" style="background:' + (typeColors[types[t]] || defCols[t % defCols.length]) + '"></div> ' + esc(types[t]) + '</div>'; } legend += '</div>';
  return '<div class="card" style="margin-bottom:var(--space-4)"><div class="card-header">Listening Activity (' + data.days + ' days)</div><div class="card-body chart-container">' + svg + legend + '</div></div>';
}

function renderEngagementTable(data) {
  var html = '<div class="card" style="margin-bottom:var(--space-4)"><div class="card-header">User Engagement (30 days)</div><div class="card-body" style="overflow-x:auto"><table>';
  html += '<tr><th>User</th><th>Events</th><th>Plays</th><th>Skip Rate</th><th>Unique Tracks</th><th>Diversity</th><th>Last Active</th></tr>';
  for (var i = 0; i < data.users.length; i++) { var u = data.users[i]; html += '<tr class="clickable" onclick="switchTab(\'users\');setTimeout(function(){viewUser(\'' + esc(u.user_id) + '\')},100)"><td><strong>' + esc(u.user_id) + '</strong></td><td>' + u.total_events.toLocaleString() + '</td><td>' + (u.plays || 0).toLocaleString() + '</td><td' + (u.skip_rate > 0.5 ? ' class="text-danger"' : '') + '>' + (u.skip_rate * 100).toFixed(1) + '%</td><td>' + u.unique_tracks + '</td><td>' + (u.diversity * 100).toFixed(0) + '%</td><td>' + timeAgo(u.last_active) + '</td></tr>'; }
  html += '</table></div></div>';
  return html;
}

// =========================================================================
// Users View
// =========================================================================
function loadUsers() {
  api('/v1/users?limit=200').then(function(users) { cachedUsers = users; renderUsersList(users); });
}

function renderUsersList(users) {
  var h = '<div class="page-header"><h1 class="page-title">Users (' + users.length + ')</h1></div>';
  if (!users.length) { h += '<div class="empty">No users yet. Users are auto-created when events are ingested.</div>'; $('#app').innerHTML = h; return; }
  h += '<div class="card"><div class="card-body" style="overflow-x:auto"><table><tr><th>UID</th><th>Username</th><th>Display Name</th><th>Events</th><th>Last Seen</th><th>Created</th><th></th></tr>';
  for (var i = 0; i < users.length; i++) { var u = users[i]; h += '<tr class="clickable" onclick="viewUser(\'' + esc(u.user_id) + '\')"><td class="mono muted">' + u.uid + '</td><td><strong>' + esc(u.user_id) + '</strong></td><td>' + esc(u.display_name || '\u2014') + '</td><td>' + u.event_count + '</td><td>' + timeAgo(u.last_seen) + '</td><td>' + fmtTime(u.created_at) + '</td><td><button class="btn btn-secondary btn-sm" onclick="event.stopPropagation();viewUser(\'' + esc(u.user_id) + '\')">View</button></td></tr>'; }
  h += '</table></div></div>';
  $('#app').innerHTML = h;
}

function viewUser(userId) { userDetailState.userId = userId; userDetailState.historyOffset = 0; _loadUserDetail(); }

function _loadUserDetail() {
  var enc = encodeURIComponent(userDetailState.userId), hs = userDetailState;
  Promise.all([
    api('/v1/users/' + enc + '/profile'), api('/v1/users/' + enc + '/interactions?limit=20&sort_by=satisfaction_score&sort_dir=desc'),
    api('/v1/users/' + enc + '/sessions?limit=10'), api('/v1/users/' + enc + '/lastfm/profile').catch(function() { return null; }),
    api('/v1/users/' + enc + '/history?limit=' + hs.historyLimit + '&offset=' + hs.historyOffset)
  ]).then(function(r) { renderUserDetail(r[0], r[1], r[2], r[3], r[4]); }).catch(function(e) { $('#app').innerHTML = '<div class="empty text-danger">Error loading user: ' + esc(e.message) + '</div>'; });
}

function historyPage(delta) { userDetailState.historyOffset = Math.max(0, userDetailState.historyOffset + delta * userDetailState.historyLimit); _loadUserDetail(); }

function renderUserDetail(profile, interactions, sessions, lastfmProfile, history) {
  var userId = profile.user_id, numericUid = profile.uid, tp = profile.taste_profile;
  var h = '<div class="page-header"><div style="display:flex;align-items:center;gap:var(--space-3);flex-wrap:wrap">';
  h += '<button class="btn btn-secondary btn-sm" onclick="loadUsers()">\u2190 Back</button>';
  h += '<h1 class="page-title">' + esc(userId) + '</h1>';
  h += '<span class="badge badge-primary font-mono">UID ' + numericUid + '</span>';
  if (profile.display_name) h += '<span class="text-secondary">' + esc(profile.display_name) + '</span>';
  if (profile.profile_updated_at) h += '<span class="text-muted text-xs">Profile updated ' + timeAgo(profile.profile_updated_at) + '</span>';
  h += '</div><div class="page-actions">';
  h += '<button class="btn btn-secondary btn-sm" onclick="showRenameModal(' + numericUid + ',\'' + esc(userId) + '\',\'' + esc(profile.display_name || '') + '\')">Edit User</button>';
  h += '<button class="btn btn-primary btn-sm" onclick="switchTab(\'recommendations\');setTimeout(function(){document.getElementById(\'reco-user\').value=\'' + esc(userId) + '\';},100)">Get Recs</button>';
  h += '</div></div>';

  // Taste profile
  h += '<div class="grid-2">';
  if (tp && tp.audio_preferences) {
    var ap = tp.audio_preferences;
    h += '<div class="card"><div class="card-header">Audio Preferences</div><div class="card-body"><div class="profile-grid">';
    var prefs = [['BPM',ap.bpm&&ap.bpm.mean,1],['Energy',ap.energy&&ap.energy.mean,2],['Danceability',ap.danceability&&ap.danceability.mean,2],['Valence',ap.valence&&ap.valence.mean,2],['Acousticness',ap.acousticness&&ap.acousticness.mean,2],['Instrumentalness',ap.instrumentalness&&ap.instrumentalness.mean,2],['Loudness',ap.loudness&&ap.loudness.mean,1]];
    for (var i = 0; i < prefs.length; i++) { var p = prefs[i]; h += '<div class="profile-item"><div class="pval">' + (p[1] != null ? p[1].toFixed(p[2]) : '\u2014') + '</div><div class="plbl">' + p[0] + '</div></div>'; }
    h += '</div></div></div>';
  }
  if (tp && tp.behaviour) {
    var b = tp.behaviour;
    h += '<div class="card"><div class="card-header">Behaviour</div><div class="card-body"><div class="profile-grid">';
    var bStats = [['Total Plays',b.total_plays,0],['Active Days',b.active_days,0],['Avg Session',(b.avg_session_tracks||0).toFixed(1),-1],['Skip Rate',b.skip_rate!=null?(b.skip_rate*100).toFixed(1)+'%':'\u2014',-1],['Completion',b.avg_completion!=null?(b.avg_completion*100).toFixed(1)+'%':'\u2014',-1]];
    for (var i = 0; i < bStats.length; i++) { var s = bStats[i]; h += '<div class="profile-item"><div class="pval">' + (s[2] >= 0 ? (s[1] != null ? Number(s[1]).toFixed(s[2]) : '\u2014') : s[1]) + '</div><div class="plbl">' + s[0] + '</div></div>'; }
    h += '</div></div></div>';
  }
  if (tp && tp.mood_preferences) {
    var mp = tp.mood_preferences, moodKeys = Object.keys(mp).sort(function(a,b){return mp[b]-mp[a];});
    h += '<div class="card"><div class="card-header">Mood Preferences</div><div class="card-body">';
    if (!moodKeys.length) h += '<div class="empty">No mood data yet</div>';
    else { var maxMood = mp[moodKeys[0]]||1; for (var i = 0; i < Math.min(moodKeys.length, 8); i++) { var k = moodKeys[i], v = mp[k]; h += '<div class="bar-row"><div class="bar-label">' + esc(k) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (v/maxMood*100).toFixed(1) + '%;background:var(--color-success)"></div></div><div class="bar-count">' + v.toFixed(2) + '</div></div>'; } }
    h += '</div></div>';
  }
  if (tp && tp.key_preferences) {
    var kp = tp.key_preferences, keyKeys = Object.keys(kp).sort(function(a,b){return kp[b]-kp[a];});
    h += '<div class="card"><div class="card-header">Key Preferences</div><div class="card-body">';
    if (!keyKeys.length) h += '<div class="empty">No key data yet</div>';
    else { var maxKey = kp[keyKeys[0]]||1; for (var i = 0; i < Math.min(keyKeys.length, 12); i++) { var k = keyKeys[i], v = kp[k]; h += '<div class="bar-row"><div class="bar-label">' + esc(k) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (v/maxKey*100).toFixed(1) + '%;background:var(--color-info)"></div></div><div class="bar-count">' + v.toFixed(2) + '</div></div>'; } }
    h += '</div></div>';
  }
  h += '</div>';

  // Last.fm
  var lfm = profile.lastfm || null, lfp = lastfmProfile;
  h += '<div class="card" style="margin-top:var(--space-4)"><div class="card-header">Last.fm' + (lfm ? ' <span class="badge badge-success" style="margin-left:var(--space-2)">Connected</span>' : '') + '</div><div class="card-body">';
  if (!lfm) {
    h += '<div style="padding:var(--space-4)"><p class="text-muted text-sm">No Last.fm account linked. Users connect through their music app.</p></div>';
  } else {
    h += '<div style="padding:var(--space-4);display:flex;align-items:center;gap:var(--space-4);flex-wrap:wrap">';
    h += '<div><span class="text-muted text-xs">Username</span><br><strong>' + esc(lfm.username) + '</strong></div>';
    h += '<div><span class="text-muted text-xs">Scrobbling</span><br>' + (lfm.scrobbling_enabled ? '<span class="badge badge-success">Active</span>' : '<span class="badge badge-warning">Read-only</span>') + '</div>';
    h += '<div><span class="text-muted text-xs">Last Synced</span><br>' + timeAgo(lfm.synced_at) + '</div>';
    h += '<button class="btn btn-primary btn-sm" onclick="syncLastfm(\'' + esc(userId) + '\')">Sync Now</button>';
    h += '<button class="btn btn-secondary btn-sm" onclick="backfillScrobbles(\'' + esc(userId) + '\')">Backfill Scrobbles</button>';
    h += '<button class="btn btn-danger btn-sm" onclick="disconnectLastfm(\'' + esc(userId) + '\')">Disconnect</button></div>';
    var lfData = (lfp && lfp.profile) || (lfm && lfm.profile) || null;
    if (lfData) {
      var ui = lfData.user_info;
      if (ui) {
        h += '<div style="border-top:1px solid var(--border);padding:var(--space-3) var(--space-4);display:flex;gap:var(--space-6);flex-wrap:wrap">';
        if (ui.playcount) h += '<div class="profile-item"><div class="pval">' + Number(ui.playcount).toLocaleString() + '</div><div class="plbl">Total Scrobbles</div></div>';
        if (ui.country) h += '<div class="profile-item"><div class="pval">' + esc(ui.country) + '</div><div class="plbl">Country</div></div>';
        if (ui.registered) { var regDate = ui.registered.unixtime ? new Date(ui.registered.unixtime * 1000).getFullYear() : (ui.registered['#text'] || ''); h += '<div class="profile-item"><div class="pval">' + esc(regDate) + '</div><div class="plbl">Member Since</div></div>'; }
        h += '</div>';
      }
      if (lfData.top_artists) {
        var periods = [['7day','7 Days'],['1month','1 Month'],['overall','All Time']];
        h += '<div style="border-top:1px solid var(--border)"><div style="padding:var(--space-3) var(--space-4);font-size:0.8125rem;font-weight:600">Top Artists</div>';
        h += '<div style="display:flex;gap:0;overflow-x:auto">';
        for (var pi = 0; pi < periods.length; pi++) {
          var pk = periods[pi][0], plbl = periods[pi][1], artists = lfData.top_artists[pk] || [];
          h += '<div style="flex:1;min-width:200px;border-right:1px solid var(--border)"><div style="padding:var(--space-2) var(--space-4);font-size:0.6875rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;border-bottom:1px solid var(--border)">' + plbl + '</div>';
          for (var ai = 0; ai < Math.min(artists.length, 8); ai++) { var a = artists[ai]; h += '<div style="padding:var(--space-1) var(--space-4);font-size:0.75rem;display:flex;justify-content:space-between"><span>' + esc(a.name) + '</span><span class="text-muted">' + (a.playcount || '') + '</span></div>'; }
          if (!artists.length) h += '<div style="padding:var(--space-2) var(--space-4);font-size:0.75rem;color:var(--text-muted)">No data</div>';
          h += '</div>';
        }
        h += '</div></div>';
      }
      if (lfData.loved_tracks && lfData.loved_tracks.length > 0) {
        h += '<div style="border-top:1px solid var(--border)"><div style="padding:var(--space-3) var(--space-4);font-size:0.8125rem;font-weight:600">Loved Tracks</div><div style="display:flex;flex-wrap:wrap;gap:0">';
        for (var li = 0; li < Math.min(lfData.loved_tracks.length, 10); li++) { var lt = lfData.loved_tracks[li]; var ltArtist = lt.artist ? (lt.artist.name || lt.artist) : ''; h += '<div style="padding:var(--space-1) var(--space-4);font-size:0.75rem;width:50%"><span class="text-danger" style="margin-right:var(--space-1)">\u2665</span> ' + esc(ltArtist) + ' \u2014 ' + esc(lt.name) + '</div>'; }
        h += '</div></div>';
      }
      if (lfData.genres && Object.keys(lfData.genres).length > 0) {
        var genres = Object.keys(lfData.genres);
        h += '<div style="border-top:1px solid var(--border);padding:var(--space-3) var(--space-4)"><div style="font-size:0.8125rem;font-weight:600;margin-bottom:var(--space-2)">Genres</div><div style="display:flex;flex-wrap:wrap;gap:var(--space-2)">';
        for (var gi = 0; gi < Math.min(genres.length, 20); gi++) h += '<span class="badge badge-purple">' + esc(genres[gi]) + '</span>';
        h += '</div></div>';
      }
    }
  }
  h += '</div></div>';

  // Top interactions
  h += '<div class="card" style="margin-top:var(--space-4)"><div class="card-header">Top Tracks (' + interactions.total + ' interactions)</div>';
  h += '<div class="card-body" style="overflow-x:auto"><table><tr><th>Track</th><th>Score</th><th>Plays</th><th>Skips</th><th>Likes</th><th>Completion</th><th>Last Played</th></tr>';
  var ints = interactions.interactions || [], maxSat = 0;
  for (var i = 0; i < ints.length; i++) { if (ints[i].satisfaction_score > maxSat) maxSat = ints[i].satisfaction_score; }
  for (var i = 0; i < ints.length; i++) { var t = ints[i]; h += '<tr><td class="truncate" title="' + trackTooltip(t) + '">' + trackName(t) + '</td><td class="nowrap">' + scoreBar(t.satisfaction_score, maxSat > 0 ? maxSat : 1) + '</td><td>' + t.play_count + '</td><td>' + t.skip_count + '</td><td>' + t.like_count + '</td><td>' + (t.avg_completion != null ? (t.avg_completion * 100).toFixed(0) + '%' : '\u2014') + '</td><td>' + timeAgo(t.last_played_at) + '</td></tr>'; }
  h += '</table></div></div>';

  // Listening History
  if (history && history.history) {
    var hist = history.history, hs = userDetailState;
    h += '<div class="card" style="margin-top:var(--space-4)"><div class="card-header">Listening History (' + history.total + ' total)</div>';
    h += '<div class="card-body" style="overflow-x:auto"><table><tr><th>Time</th><th>Artist</th><th>Title</th><th>Album</th><th>Duration</th><th>Listened</th><th>Completion</th><th>Result</th><th>Device</th></tr>';
    for (var i = 0; i < hist.length; i++) {
      var entry = hist[i], comp = entry.completion != null ? (entry.completion * 100).toFixed(0) + '%' : '\u2014';
      var compColor = entry.completion != null && entry.completion < 0.5 ? ' class="text-danger"' : '';
      var listened = entry.dwell_ms != null ? fmtDuration(Math.round(entry.dwell_ms / 1000)) : '\u2014';
      var result = entry.reason_end || '\u2014', resultColor = result === 'user_skip' ? ' class="text-danger"' : '';
      h += '<tr><td class="nowrap">' + fmtTime(entry.timestamp) + '</td><td>' + esc(entry.artist || '\u2014') + '</td><td>' + esc(entry.title || '\u2014') + '</td><td>' + esc(entry.album || '\u2014') + '</td><td>' + (entry.duration != null ? fmtDuration(Math.round(entry.duration)) : '\u2014') + '</td><td>' + listened + '</td><td' + compColor + '>' + comp + '</td><td' + resultColor + '>' + esc(result) + '</td><td>' + esc(entry.device_type || '\u2014') + '</td></tr>';
    }
    h += '</table>';
    var showing = hs.historyOffset + 1, showEnd = Math.min(hs.historyOffset + hist.length, history.total);
    h += '<div class="pagination"><span>Showing ' + showing + '\u2013' + showEnd + ' of ' + history.total + '</span><div class="btn-group"><button class="btn btn-secondary btn-sm" onclick="historyPage(-1)"' + (hs.historyOffset === 0 ? ' disabled' : '') + '>\u2190 Prev</button><button class="btn btn-secondary btn-sm" onclick="historyPage(1)"' + (hs.historyOffset + hs.historyLimit >= history.total ? ' disabled' : '') + '>Next \u2192</button></div></div>';
    h += '</div></div>';
  }

  // Sessions
  h += '<div class="card" style="margin-top:var(--space-4)"><div class="card-header">Recent Sessions (' + sessions.total + ' total)</div>';
  h += '<div class="card-body" style="overflow-x:auto"><table><tr><th>Started</th><th>Duration</th><th>Tracks</th><th>Plays</th><th>Skips</th><th>Skip Rate</th><th>Completion</th><th>Context</th><th>Device</th></tr>';
  var sess = sessions.sessions || [];
  for (var i = 0; i < sess.length; i++) { var s = sess[i]; h += '<tr><td class="nowrap">' + fmtTime(s.started_at) + '</td><td>' + fmtDuration(s.duration_s) + '</td><td>' + s.track_count + '</td><td>' + s.play_count + '</td><td>' + s.skip_count + '</td><td' + (s.skip_rate != null && s.skip_rate > 0.5 ? ' class="text-danger"' : '') + '>' + (s.skip_rate != null ? (s.skip_rate * 100).toFixed(0) + '%' : '\u2014') + '</td><td>' + (s.avg_completion != null ? (s.avg_completion * 100).toFixed(0) + '%' : '\u2014') + '</td><td>' + esc(s.dominant_context_type || '\u2014') + '</td><td>' + esc(s.dominant_device_type || '\u2014') + '</td></tr>'; }
  h += '</table></div></div>';
  $('#app').innerHTML = h;
}

// =========================================================================
// Tracks View
// =========================================================================
function loadTracks() {
  var s = trackState, url = '/v1/tracks?limit=' + s.limit + '&offset=' + s.offset + '&sort_by=' + s.sort + '&sort_dir=' + s.dir;
  if (s.search) url += '&search=' + encodeURIComponent(s.search);
  return api(url).then(function(data) { trackState.total = data.total; renderTracks(data); });
}

function searchTracks() { var input = document.getElementById('track-search'); trackState.search = input ? input.value.trim() : ''; trackState.offset = 0; loadTracks(); }
function clearTrackSearch() { trackState.search = ''; var input = document.getElementById('track-search'); if (input) input.value = ''; trackState.offset = 0; loadTracks(); }
function sortTracks(field) { if (trackState.sort === field) trackState.dir = trackState.dir === 'asc' ? 'desc' : 'asc'; else { trackState.sort = field; trackState.dir = 'asc'; } trackState.offset = 0; loadTracks(); }
function trackPage(delta) { trackState.offset = Math.max(0, trackState.offset + delta * trackState.limit); loadTracks(); }

function renderTracks(data) {
  var tracks = data.tracks, s = trackState;
  var arrow = function(f) { return s.sort === f ? (s.dir === 'asc' ? ' \u25B2' : ' \u25BC') : ''; };
  var h = '<div class="card"><div class="card-header"><span>Tracks (' + s.total + ')</span>';
  h += '<div class="page-actions"><div style="position:relative"><input id="track-search" type="text" placeholder="Search title, artist, ID\u2026" value="' + esc(s.search) + '" onkeydown="if(event.key===\'Enter\')searchTracks()" class="form-input" style="width:220px">';
  if (s.search) h += '<span onclick="clearTrackSearch()" style="position:absolute;right:8px;top:50%;transform:translateY(-50%);cursor:pointer;color:var(--text-muted);font-size:16px">\u00D7</span>';
  h += '</div><button class="btn btn-secondary btn-sm" onclick="searchTracks()">Search</button>';
  h += '<button class="btn btn-primary btn-sm" onclick="showGenerateModal()">Generate Playlist</button></div></div>';
  h += '<div class="card-body" style="overflow-x:auto"><table><tr>';
  h += '<th class="sortable" onclick="sortTracks(\'bpm\')">BPM' + arrow('bpm') + '</th><th>Title</th><th>Artist</th><th>Genre</th><th>Key</th>';
  h += '<th class="sortable" onclick="sortTracks(\'energy\')">Energy' + arrow('energy') + '</th>';
  h += '<th class="sortable" onclick="sortTracks(\'danceability\')">Dance' + arrow('danceability') + '</th>';
  h += '<th class="sortable" onclick="sortTracks(\'valence\')">Valence' + arrow('valence') + '</th>';
  h += '<th>Mood</th><th class="sortable" onclick="sortTracks(\'duration\')">Duration' + arrow('duration') + '</th>';
  h += '<th class="sortable" onclick="sortTracks(\'analysis_version\')">Version' + arrow('analysis_version') + '</th><th>Track ID</th></tr>';
  for (var i = 0; i < tracks.length; i++) {
    var t = tracks[i];
    var energyBar = t.energy != null ? '<div style="display:inline-block;width:50px;height:8px;background:var(--bg-base);border-radius:4px;vertical-align:middle;margin-left:4px"><div style="width:' + (t.energy*100) + '%;height:100%;background:var(--color-success);border-radius:4px"></div></div>' : '';
    h += '<tr><td>' + (t.bpm ? t.bpm.toFixed(1) : '\u2014') + '</td><td class="truncate" title="' + trackTooltip(t) + '">' + (esc(t.title || '') || basename(t.file_path)) + '</td><td class="truncate" style="max-width:180px">' + esc(t.artist || '\u2014') + '</td><td class="truncate" style="max-width:150px">' + esc(t.genre || '\u2014') + '</td><td>' + esc(t.key || '\u2014') + ' ' + (t.mode ? t.mode.charAt(0) : '') + '</td><td>' + (t.energy != null ? t.energy.toFixed(2) : '\u2014') + energyBar + '</td><td>' + (t.danceability != null ? t.danceability.toFixed(2) : '\u2014') + '</td><td>' + (t.valence != null ? t.valence.toFixed(2) : '\u2014') + '</td><td>' + topMood(t.mood_tags) + '</td><td>' + fmtTrackDur(t.duration) + '</td><td class="mono text-xs text-muted">' + esc(t.analysis_version || '\u2014') + '</td><td class="mono text-xs">' + esc(t.track_id) + '</td></tr>';
  }
  h += '</table></div>';
  var from = s.offset + 1, to = Math.min(s.offset + tracks.length, s.total);
  h += '<div class="pagination"><span>Showing ' + from + '\u2013' + to + ' of ' + s.total + '</span><div class="btn-group"><button class="btn btn-secondary btn-sm" onclick="trackPage(-1)"' + (s.offset === 0 ? ' disabled' : '') + '>\u2190 Prev</button><button class="btn btn-secondary btn-sm" onclick="trackPage(1)"' + (s.offset + s.limit >= s.total ? ' disabled' : '') + '>Next \u2192</button></div></div></div>';
  setAppContent(h);
}

// =========================================================================
// Playlists View
// =========================================================================
function loadPlaylists() { return api('/v1/playlists?limit=50').then(function(data) { renderPlaylistList(data); }); }

function renderPlaylistList(playlists) {
  var h = '<div class="page-header"><h1 class="page-title">Playlists (' + playlists.length + ')</h1><div class="page-actions"><button class="btn btn-primary btn-sm" onclick="showGenerateModal()">Generate Playlist</button></div></div>';
  if (!playlists.length) { h += '<div class="empty">No playlists yet. Generate one from the button above.</div>'; setAppContent(h); return; }
  h += '<div class="playlist-grid">';
  for (var i = 0; i < playlists.length; i++) { var p = playlists[i]; h += '<div class="playlist-card" onclick="viewPlaylist(' + p.id + ')"><h3>' + esc(p.name) + '</h3><div class="meta">' + strategyBadge(p.strategy) + '<span>' + p.track_count + ' tracks</span><span>' + fmtDuration(p.total_duration) + '</span><span>' + timeAgo(p.created_at) + '</span></div></div>'; }
  h += '</div>';
  setAppContent(h);
}

function viewPlaylist(id) { api('/v1/playlists/' + id).then(function(p) { renderPlaylistDetail(p); }); }

function renderPlaylistDetail(p) {
  var h = '<div class="page-header"><div style="display:flex;gap:var(--space-2)"><button class="btn btn-secondary btn-sm" onclick="loadPlaylists()">\u2190 Back</button><button class="btn btn-danger btn-sm" onclick="deletePlaylist(' + p.id + ')">Delete</button></div></div>';
  h += '<div class="card"><div class="card-header">' + esc(p.name) + ' &nbsp; ' + strategyBadge(p.strategy) + '<span class="subtitle">' + p.track_count + ' tracks \u00B7 ' + fmtDuration(p.total_duration) + '</span></div>';
  h += '<div class="card-body" style="overflow-x:auto"><table><tr><th>#</th><th>Track</th><th>Artist</th><th>BPM</th><th>Key</th><th>Energy</th><th>Mood</th><th>Duration</th></tr>';
  for (var i = 0; i < p.tracks.length; i++) { var t = p.tracks[i]; h += '<tr><td>' + (t.position + 1) + '</td><td class="truncate" title="' + trackTooltip(t) + '">' + (esc(t.title || '') || basename(t.file_path)) + '</td><td class="truncate" style="max-width:150px">' + esc(t.artist || '\u2014') + '</td><td>' + (t.bpm ? t.bpm.toFixed(1) : '\u2014') + '</td><td>' + esc(t.key || '\u2014') + ' ' + (t.mode ? t.mode.charAt(0) : '') + '</td><td>' + (t.energy != null ? t.energy.toFixed(2) : '\u2014') + '</td><td>' + topMood(t.mood_tags) + '</td><td>' + fmtTrackDur(t.duration) + '</td></tr>'; }
  h += '</table></div></div>';
  setAppContent(h);
}

function deletePlaylist(id) { if (!confirm('Delete this playlist?')) return; apiDelete('/v1/playlists/' + id).then(function() { loadPlaylists(); }); }

// =========================================================================
// Generate Playlist Modal
// =========================================================================
function showGenerateModal() {
  var overlay = document.createElement('div'); overlay.className = 'modal-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };
  var m = '<div class="modal"><h2>Generate Playlist</h2>';
  m += '<div class="field"><label>Name</label><input id="gen-name" value="My Playlist"></div>';
  m += '<div class="field"><label>Strategy</label><select id="gen-strategy" onchange="onStrategyChange()"><option value="flow">Flow (smooth transitions)</option><option value="mood">Mood (by feeling)</option><option value="energy_curve">Energy Curve (shape)</option><option value="key_compatible">Key Compatible (harmonic)</option></select></div>';
  m += '<div class="field" id="gen-seed-wrap"><label>Seed Track ID</label><input id="gen-seed" placeholder="paste a track_id"></div>';
  m += '<div class="field" id="gen-mood-wrap" style="display:none"><label>Mood</label><select id="gen-mood"><option value="happy">Happy</option><option value="sad">Sad</option><option value="aggressive">Aggressive</option><option value="relaxed">Relaxed</option><option value="party">Party</option></select></div>';
  m += '<div class="field" id="gen-curve-wrap" style="display:none"><label>Curve</label><select id="gen-curve"><option value="ramp_up_cool_down">Ramp Up + Cool Down</option><option value="ramp_up">Ramp Up</option><option value="cool_down">Cool Down</option><option value="steady_high">Steady High</option><option value="steady_low">Steady Low</option></select></div>';
  m += '<div class="field"><label>Max Tracks</label><input id="gen-max" type="number" value="25" min="5" max="100"></div>';
  m += '<div class="actions"><button class="btn btn-secondary" onclick="this.closest(\'.modal-overlay\').remove()">Cancel</button><button class="btn btn-primary" onclick="submitPlaylist()">Generate</button></div></div>';
  overlay.innerHTML = m; document.body.appendChild(overlay);
}

function onStrategyChange() {
  var s = document.getElementById('gen-strategy').value;
  document.getElementById('gen-seed-wrap').style.display = (s === 'flow' || s === 'key_compatible') ? '' : 'none';
  document.getElementById('gen-mood-wrap').style.display = s === 'mood' ? '' : 'none';
  document.getElementById('gen-curve-wrap').style.display = s === 'energy_curve' ? '' : 'none';
}

function submitPlaylist() {
  var strategy = document.getElementById('gen-strategy').value;
  var body = { name: document.getElementById('gen-name').value || 'Playlist', strategy: strategy, max_tracks: parseInt(document.getElementById('gen-max').value) || 25 };
  if (strategy === 'flow' || strategy === 'key_compatible') body.seed_track_id = document.getElementById('gen-seed').value.trim();
  if (strategy === 'mood') body.params = { mood: document.getElementById('gen-mood').value };
  if (strategy === 'energy_curve') body.params = { curve: document.getElementById('gen-curve').value };
  var overlay = document.querySelector('.modal-overlay');
  apiPost('/v1/playlists', body).then(function(p) { if (overlay) overlay.remove(); switchTab('playlists'); setTimeout(function() { viewPlaylist(p.id); }, 300); }).catch(function(e) { alert('Failed: ' + e.message); });
}

// =========================================================================
// User Actions (Rename, Last.fm, Sync, Scan)
// =========================================================================
function showRenameModal(uid, currentUserId, currentDisplayName) {
  var overlay = document.createElement('div'); overlay.className = 'modal-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };
  var m = '<div class="modal"><h2>Edit User <span class="badge badge-primary font-mono">UID ' + uid + '</span></h2>';
  m += '<div class="field"><label>Username (user_id)</label><input id="rename-userid" value="' + esc(currentUserId) + '"></div>';
  m += '<div class="field"><label>Display Name</label><input id="rename-display" value="' + esc(currentDisplayName) + '"></div>';
  m += '<p class="text-xs text-muted" style="margin-top:var(--space-2)">Changing the username will cascade to all events, sessions, and interactions.</p>';
  m += '<div class="actions"><button class="btn btn-secondary" onclick="this.closest(\'.modal-overlay\').remove()">Cancel</button><button class="btn btn-primary" onclick="submitRename(' + uid + ')">Save</button></div></div>';
  overlay.innerHTML = m; document.body.appendChild(overlay);
}

function submitRename(uid) {
  var newUserId = document.getElementById('rename-userid').value.trim();
  if (!newUserId) { alert('Username cannot be empty.'); return; }
  var body = { user_id: newUserId, display_name: document.getElementById('rename-display').value.trim() || null };
  var overlay = document.querySelector('.modal-overlay');
  apiPatch('/v1/users/' + uid, body).then(function(user) { if (overlay) overlay.remove(); api('/v1/users?limit=200').then(function(u) { cachedUsers = u; }); viewUser(user.user_id); }).catch(function(e) { alert('Failed: ' + e.message); });
}

function syncLastfm(userId) { apiPost('/v1/users/' + encodeURIComponent(userId) + '/lastfm/sync', {}).then(function() { viewUser(userId); }).catch(function(e) { alert('Last.fm sync failed: ' + e.message); }); }
function disconnectLastfm(userId) { if (!confirm('Disconnect Last.fm account?')) return; apiDelete('/v1/users/' + encodeURIComponent(userId) + '/lastfm').then(function() { viewUser(userId); }).catch(function(e) { alert('Failed: ' + e.message); }); }

function backfillScrobbles(userId) {
  if (!confirm('Scan all past plays and enqueue missed scrobbles?')) return;
  apiPost('/v1/users/' + encodeURIComponent(userId) + '/lastfm/backfill', {}).then(function(data) {
    alert('Backfill complete:\n' + data.enqueued + ' scrobbles enqueued\n' + data.already_queued + ' already queued\n' + data.skipped_no_meta + ' skipped (no metadata)\n' + data.skipped_criteria + ' skipped (criteria not met)\n' + data.total_play_ends + ' total play_end events');
  }).catch(function(e) { alert('Backfill failed: ' + e.message); });
}

function triggerSync() {
  apiPost('/v1/library/sync', {}).then(function(data) {
    var msg = 'Sync complete: ' + data.tracks_matched + ' matched, ' + data.tracks_updated + ' updated, ' + data.tracks_metadata + ' metadata refreshed';
    if (data.tracks_unmatched) msg += ', ' + data.tracks_unmatched + ' unmatched';
    if (data.errors && data.errors.length) msg += '\nErrors: ' + data.errors.join(', ');
    alert(msg); loadDashboard();
  }).catch(function(e) { alert('Sync failed: ' + e.message); });
}

function triggerScan() {
  fetch(BASE + '/v1/library/scan', { method: 'POST', headers: headers() }).then(function(res) { return res.json(); }).then(function() { loadDashboard(); }).catch(function(e) { alert('Scan failed: ' + e.message); });
}

// =========================================================================
// Pipeline View
// =========================================================================
var STEP_META = {
  sessionizer:        { icon: '\u23F1', label: 'Sessionizer',  desc: 'Groups raw events into listening sessions' },
  track_scoring:      { icon: '\u2B50', label: 'Scoring',      desc: 'Computes per-track satisfaction scores' },
  taste_profiles:     { icon: '\uD83C\uDFAF', label: 'Taste Profiles', desc: 'Builds user audio preference profiles' },
  collab_filter:      { icon: '\uD83E\uDD1D', label: 'Collab Filter', desc: 'User-user & item-item similarity' },
  ranker:             { icon: '\uD83C\uDFC6', label: 'Ranker',        desc: 'Trains LightGBM ranking model' },
  session_embeddings: { icon: '\uD83E\uDDE0', label: 'Embeddings',    desc: 'Word2Vec skip-gram on sessions' },
  lastfm_cache:       { icon: '\uD83C\uDF10', label: 'Last.fm',       desc: 'External CF via Last.fm similar tracks' },
  sasrec:             { icon: '\u26A1',  label: 'SASRec',       desc: 'Transformer sequential model' },
  session_gru:        { icon: '\uD83D\uDCC8', label: 'Session GRU',   desc: 'Taste drift via GRU over sessions' }
};
var STEP_ORDER = ['sessionizer','track_scoring','taste_profiles','collab_filter','ranker','session_embeddings','lastfm_cache','sasrec','session_gru'];

function loadPipeline() {
  $('#app').innerHTML = '<div class="empty">Loading pipeline status...</div>';
  Promise.all([api('/v1/pipeline/status?limit=10'), api('/v1/pipeline/models').catch(function(){return null;})]).then(function(results) {
    pipelineData.current = results[0].current; pipelineData.history = results[0].history || []; pipelineData.models = results[1];
    pipelineSelectedRun = pipelineData.current || (pipelineData.history.length ? pipelineData.history[0] : null);
    pipelineSelectedStep = null; renderPipeline();
    if (pipelineData.current) pipelineConnectSSE();
  }).catch(function(e) { $('#app').innerHTML = '<div class="empty text-danger">Error: ' + esc(e.message) + '</div>'; });
}

function pipelineRefreshStatus() {
  api('/v1/pipeline/status?limit=10').then(function(data) {
    pipelineData.current = data.current; pipelineData.history = data.history || [];
    if (pipelineData.current) pipelineSelectedRun = pipelineData.current;
    else if (!pipelineSelectedRun && pipelineData.history.length) pipelineSelectedRun = pipelineData.history[0];
    if (pipelineSelectedRun && pipelineSelectedRun.run_id) {
      for (var i = 0; i < pipelineData.history.length; i++) { if (pipelineData.history[i].run_id === pipelineSelectedRun.run_id) { pipelineSelectedRun = pipelineData.history[i]; break; } }
    }
    if (currentView === 'pipeline') renderPipeline();
  });
}

function pipelineConnectSSE() {
  if (pipelineSSE) return;
  var abortCtrl = new AbortController(); pipelineSSE = abortCtrl;
  fetch(BASE + '/v1/pipeline/stream', { headers: { 'Authorization': 'Bearer ' + KEY }, signal: abortCtrl.signal }).then(function(response) {
    var reader = response.body.getReader(), decoder = new TextDecoder(), buffer = '';
    function read() {
      reader.read().then(function(result) {
        if (result.done) { pipelineSSE = null; pipelineUpdateIndicator(false); return; }
        buffer += decoder.decode(result.value, { stream: true });
        var lines = buffer.split('\n'); buffer = lines.pop();
        var eventType = 'message', dataLines = [];
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i];
          if (line.indexOf('event: ') === 0) eventType = line.slice(7).trim();
          else if (line.indexOf('data: ') === 0) dataLines.push(line.slice(6));
          else if (line === '' && dataLines.length > 0) { try { pipelineHandleSSE(eventType, JSON.parse(dataLines.join('\n'))); } catch(e){} eventType = 'message'; dataLines = []; }
        }
        pipelineUpdateIndicator(true); read();
      }).catch(function() { pipelineSSE = null; pipelineUpdateIndicator(false); });
    }
    read();
  }).catch(function() { pipelineSSE = null; pipelineUpdateIndicator(false); });
}

function pipelineDisconnectSSE() { if (pipelineSSE && pipelineSSE.abort) pipelineSSE.abort(); pipelineSSE = null; pipelineUpdateIndicator(false); }

function pipelineUpdateIndicator(connected) {
  var el = document.getElementById('sse-indicator'); if (!el) return;
  el.className = 'sse-indicator' + (connected ? '' : ' disconnected');
  el.innerHTML = '<span class="sse-dot"' + (connected ? '' : ' style="animation:none"') + '></span> ' + (connected ? 'Live' : 'Disconnected');
}

function pipelineHandleSSE(eventType, data) {
  if (eventType === 'pipeline_start') { pipelineRefreshStatus(); }
  else if (eventType === 'step_start' || eventType === 'step_complete' || eventType === 'step_failed') {
    if (pipelineSelectedRun && pipelineSelectedRun.run_id === data.run_id) {
      var steps = pipelineSelectedRun.steps || [];
      for (var i = 0; i < steps.length; i++) {
        if (steps[i].name === data.step) {
          if (eventType === 'step_start') { steps[i].status = 'running'; steps[i].started_at = data.timestamp; }
          else if (eventType === 'step_complete') { steps[i].status = 'completed'; steps[i].duration_ms = data.duration_ms; steps[i].metrics = data.metrics || {}; steps[i].ended_at = data.timestamp; }
          else if (eventType === 'step_failed') { steps[i].status = 'failed'; steps[i].duration_ms = data.duration_ms; steps[i].error = data.error; steps[i].ended_at = data.timestamp; }
          break;
        }
      }
      if (currentView === 'pipeline') renderPipeline();
    }
  } else if (eventType === 'pipeline_end') { pipelineRefreshStatus(); api('/v1/pipeline/models').then(function(m) { pipelineData.models = m; if (currentView === 'pipeline') renderPipeline(); }).catch(function(){}); }
}

function renderPipeline() {
  var run = pipelineSelectedRun;
  var h = '<div class="page-header"><h1 class="page-title">Pipeline</h1><div class="page-actions">';
  h += '<span id="sse-indicator" class="sse-indicator disconnected"><span class="sse-dot" style="animation:none"></span> Disconnected</span>';
  if (!pipelineSSE) h += '<button class="btn btn-secondary btn-sm" onclick="pipelineConnectSSE()">Connect Live</button>';
  else h += '<button class="btn btn-secondary btn-sm" onclick="pipelineDisconnectSSE()">Disconnect</button>';
  h += '<button class="btn btn-primary btn-sm" onclick="runPipelineFromTab()">Run Pipeline</button>';
  h += '<button class="btn btn-danger btn-sm" onclick="resetPipelineFromTab()">Reset Pipeline</button>';
  h += '</div></div>';

  // Flow diagram
  h += '<div class="card"><div class="card-header">';
  if (run) {
    var triggerBadge = run.trigger === 'manual' ? 'primary' : run.trigger === 'startup' ? 'info' : 'warning';
    var statusBadge = run.status === 'completed' ? 'success' : run.status === 'failed' ? 'danger' : 'warning';
    h += 'Pipeline Run <span class="badge badge-' + triggerBadge + '">' + esc(run.trigger) + '</span> <span class="badge badge-' + statusBadge + '">' + esc(run.status).toUpperCase() + '</span>';
    if (run.config_version != null) h += ' <span class="badge badge-info" title="Algorithm config version">cfg v' + run.config_version + '</span>';
    h += '<span class="subtitle" style="margin-left:var(--space-3)">' + esc(run.run_id);
    if (run.duration_ms != null) h += ' \u00B7 ' + fmtMs(run.duration_ms);
    if (run.started_at) h += ' \u00B7 ' + fmtTime(run.started_at);
    h += '</span>';
  } else h += 'Pipeline <span class="subtitle">No runs yet</span>';
  h += '</div><div class="card-body"><div class="pipeline-flow">';
  for (var i = 0; i < STEP_ORDER.length; i++) {
    var name = STEP_ORDER[i], meta = STEP_META[name], step = null;
    if (run && run.steps) { for (var j = 0; j < run.steps.length; j++) { if (run.steps[j].name === name) { step = run.steps[j]; break; } } }
    var status = step ? step.status : 'pending';
    var selected = pipelineSelectedStep === name ? ' style="border-color:var(--color-primary);background:rgba(99,102,241,0.08)"' : '';
    if (i > 0) { var arrowCls = 'pipe-arrow'; if (status === 'completed') arrowCls += ' done'; else if (status === 'running') arrowCls += ' active'; h += '<div class="' + arrowCls + '">\u2192</div>'; }
    h += '<div class="pipe-node ' + status + '" onclick="pipelineSelectStep(\'' + name + '\')"><div class="pipe-box"' + selected + '><div class="pipe-icon">' + meta.icon + '</div><div class="pipe-label">' + esc(meta.label) + '</div>';
    if (step && step.duration_ms != null) h += '<div class="pipe-time">' + fmtMs(step.duration_ms) + '</div>';
    if (step && step.metrics) { var mKey = firstMetricKey(step.metrics); if (mKey) h += '<div class="pipe-metric">' + fmtMetricVal(step.metrics[mKey]) + ' ' + fmtMetricLabel(mKey) + '</div>'; }
    h += '</div><div class="pipe-dot"></div></div>';
  }
  h += '</div></div></div>';

  // Step detail
  if (pipelineSelectedStep && run) {
    var step = null;
    if (run.steps) { for (var j = 0; j < run.steps.length; j++) { if (run.steps[j].name === pipelineSelectedStep) { step = run.steps[j]; break; } } }
    if (step) {
      var meta = STEP_META[pipelineSelectedStep];
      h += '<div class="pipe-detail"><h3>' + meta.icon + ' ' + esc(meta.label) + ' <span class="text-muted text-sm" style="font-weight:400">' + esc(meta.desc) + '</span></h3>';
      h += '<div class="text-xs text-muted" style="margin-bottom:var(--space-3)">Status: <span class="badge badge-' + stepStatusColor(step.status) + '">' + esc(step.status).toUpperCase() + '</span>';
      if (step.duration_ms != null) h += ' \u00B7 Duration: <strong>' + fmtMs(step.duration_ms) + '</strong>';
      if (step.started_at) h += ' \u00B7 Started: ' + fmtTime(step.started_at);
      h += '</div>';
      if (step.metrics && Object.keys(step.metrics).length > 0) {
        h += '<div class="metric-grid">'; var mkeys = Object.keys(step.metrics);
        for (var m = 0; m < mkeys.length; m++) h += '<div class="metric-item"><div class="mk">' + fmtMetricLabel(mkeys[m]) + '</div><div class="mv">' + fmtMetricVal(step.metrics[mkeys[m]]) + '</div></div>';
        h += '</div>';
      }
      if (step.error) h += '<div style="margin-top:var(--space-3)"><div class="text-xs text-danger font-semibold" style="margin-bottom:var(--space-1)">Error</div><pre>' + esc(step.error) + '</pre></div>';
      h += '</div>';
    }
  }

  // Rich step detail placeholder
  h += '<div id="step-detail-rich"></div>';

  // Model readiness
  if (pipelineData.models) {
    h += '<div class="card" style="margin-top:var(--space-4)"><div class="card-header">Model Readiness</div><div class="card-body padded"><div class="model-grid">';
    h += renderModelCard('Ranker (LightGBM)', pipelineData.models.ranker);
    h += renderModelCard('Collaborative Filter', pipelineData.models.collab_filter);
    h += renderModelCard('Session Embeddings', pipelineData.models.session_embeddings);
    h += renderModelCard('SASRec', pipelineData.models.sasrec);
    h += renderModelCard('Session GRU', pipelineData.models.session_gru);
    h += renderModelCard('Last.fm Cache', pipelineData.models.lastfm_cache);
    h += '</div></div></div>';
  }

  // Error log
  var errors = collectErrors();
  if (errors.length > 0) {
    h += '<div class="card" style="margin-top:var(--space-4)"><div class="card-header">Recent Errors <span class="badge badge-danger">' + errors.length + '</span></div><div class="card-body"><div class="error-log">';
    for (var e = 0; e < errors.length; e++) { var err = errors[e]; h += '<div class="error-entry" onclick="this.classList.toggle(\'expanded\')"><span class="error-step">' + esc(STEP_META[err.step] ? STEP_META[err.step].label : err.step) + '</span><span class="error-time">' + fmtTime(err.timestamp) + ' \u00B7 run ' + esc(err.run_id) + '</span><pre>' + esc(err.error) + '</pre></div>'; }
    h += '</div></div></div>';
  }

  // Run history
  if (pipelineData.history.length > 0) {
    h += '<div class="card" style="margin-top:var(--space-4)"><div class="card-header">Run History</div><div class="card-body">';
    for (var r = 0; r < pipelineData.history.length; r++) {
      var rn = pipelineData.history[r], isSelected = pipelineSelectedRun && pipelineSelectedRun.run_id === rn.run_id;
      h += '<div class="run-row' + (isSelected ? '" style="background:rgba(99,102,241,0.06)' : '') + '" onclick="pipelineSelectRun(\'' + esc(rn.run_id) + '\')">';
      h += '<span class="run-id">' + esc(rn.run_id) + '</span>';
      h += '<span class="badge badge-' + (rn.status === 'completed' ? 'success' : rn.status === 'failed' ? 'danger' : 'warning') + '">' + esc(rn.status) + '</span>';
      h += '<span class="text-xs text-muted">' + esc(rn.trigger) + '</span>';
      if (rn.config_version != null) h += '<span class="badge badge-info" style="font-size:10px" title="Algorithm config version">cfg v' + rn.config_version + '</span>';
      h += '<span class="run-steps">'; if (rn.steps) { for (var s = 0; s < rn.steps.length; s++) h += '<span class="run-step-dot ' + esc(rn.steps[s].status) + '" title="' + esc(rn.steps[s].name) + ': ' + esc(rn.steps[s].status) + '"></span>'; } h += '</span>';
      h += '<span class="text-xs text-muted" style="min-width:70px;text-align:right">' + (rn.duration_ms != null ? fmtMs(rn.duration_ms) : '\u2014') + '</span>';
      h += '<span class="text-xs text-muted" style="min-width:80px;text-align:right">' + timeAgo(rn.started_at) + '</span></div>';
    }
    h += '</div></div>';
  }

  $('#app').innerHTML = h;
  pipelineUpdateIndicator(!!pipelineSSE);
  if (pipelineSelectedStep) loadStepDetail(pipelineSelectedStep);
}

function renderModelCard(name, model) {
  if (!model) return '<div class="model-card not-ready"><div class="model-name">' + esc(name) + '</div><div class="model-status text-muted">No data</div></div>';
  var ready = model.trained || model.built || false;
  var h = '<div class="model-card ' + (ready ? 'ready' : 'not-ready') + '"><div class="model-name">' + esc(name) + '</div>';
  h += '<div class="model-status" style="color:' + (ready ? 'var(--color-success)' : 'var(--text-muted)') + '">' + (ready ? '\u2713 Ready' : '\u2717 Not trained') + '</div>';
  var details = [];
  if (model.training_samples) details.push(model.training_samples + ' samples');
  if (model.n_features) details.push(model.n_features + ' features');
  if (model.engine) details.push(model.engine);
  if (model.vocab_size) details.push(model.vocab_size + ' vocab');
  if (model.users) details.push(model.users + ' users');
  if (model.tracks) details.push(model.tracks + ' tracks');
  if (model.seeds_cached) details.push(model.seeds_cached + ' seeds');
  if (model.cache_age_seconds != null && model.cache_age_seconds > 0) details.push(fmtDuration(model.cache_age_seconds) + ' ago');
  if (model.model_version) details.push(model.model_version);
  if (model.trained_at) details.push(timeAgo(model.trained_at));
  if (details.length) h += '<div class="model-detail">' + esc(details.join(' \u00B7 ')) + '</div>';
  return h + '</div>';
}

function collectErrors() {
  var errors = [], runs = pipelineData.history || [];
  for (var r = 0; r < runs.length; r++) { if (!runs[r].steps) continue; for (var s = 0; s < runs[r].steps.length; s++) { if (runs[r].steps[s].error) errors.push({ run_id: runs[r].run_id, step: runs[r].steps[s].name, error: runs[r].steps[s].error, timestamp: runs[r].steps[s].ended_at, duration_ms: runs[r].steps[s].duration_ms }); } }
  errors.sort(function(a, b) { return (b.timestamp || 0) - (a.timestamp || 0); });
  return errors.slice(0, 20);
}

function pipelineSelectStep(name) { pipelineSelectedStep = pipelineSelectedStep === name ? null : name; renderPipeline(); }
function pipelineSelectRun(runId) { for (var i = 0; i < pipelineData.history.length; i++) { if (pipelineData.history[i].run_id === runId) { pipelineSelectedRun = pipelineData.history[i]; pipelineSelectedStep = null; renderPipeline(); return; } } }
function runPipelineFromTab() { apiPost('/v1/pipeline/run', {}).then(function() { pipelineConnectSSE(); setTimeout(function() { pipelineRefreshStatus(); }, 500); }).catch(function(e) { alert('Pipeline failed: ' + e.message); }); }
function resetPipelineFromTab() { if (!confirm('This will delete all sessions, interactions, and taste profiles, then rebuild from raw events. Continue?')) return; apiPost('/v1/pipeline/reset', {}).then(function() { pipelineConnectSSE(); setTimeout(function() { pipelineRefreshStatus(); }, 500); }).catch(function(e) { alert('Reset failed: ' + e.message); }); }

// =========================================================================
// Pipeline Step Detail Views
// =========================================================================
function loadStepDetail(stepName) {
  var el = document.getElementById('step-detail-rich'); if (!el) return;
  if (stepName === 'sessionizer') { el.innerHTML = '<div class="empty">Loading sessionizer stats...</div>'; api('/v1/pipeline/stats/sessionizer').then(renderSessionizerDetail).catch(function(e) { el.innerHTML = '<div class="empty text-danger">' + esc(e.message) + '</div>'; }); }
  else if (stepName === 'track_scoring') { el.innerHTML = '<div class="empty">Loading scoring stats...</div>'; api('/v1/pipeline/stats/scoring').then(renderScoringDetail).catch(function(e) { el.innerHTML = '<div class="empty text-danger">' + esc(e.message) + '</div>'; }); }
  else if (stepName === 'taste_profiles') { el.innerHTML = '<div class="empty">Loading taste profiles...</div>'; loadTasteProfileExplorer(); }
  else if (stepName === 'ranker') { el.innerHTML = '<div class="empty">Loading ranker stats...</div>'; Promise.all([api('/v1/pipeline/models'), api('/v1/stats/model').catch(function(){return null;})]).then(function(r) { renderRankerDetail(r[0], r[1]); }).catch(function(e) { el.innerHTML = '<div class="empty text-danger">' + esc(e.message) + '</div>'; }); }
  else { el.innerHTML = ''; }
}

function renderSessionizerDetail(data) {
  var el = document.getElementById('step-detail-rich'); if (!el) return;
  if (!data.total_sessions) { el.innerHTML = '<div class="empty">No sessions yet.</div>'; return; }
  var h = '<div class="stats-grid" style="margin:var(--space-4) 0"><div class="stat-card"><div class="stat-label">Total Sessions</div><div class="stat-value">' + data.total_sessions.toLocaleString() + '</div></div><div class="stat-card"><div class="stat-label">Avg Duration</div><div class="stat-value">' + fmtDuration(data.avg_duration_s) + '</div></div><div class="stat-card"><div class="stat-label">Avg Tracks/Session</div><div class="stat-value">' + data.avg_tracks_per_session + '</div></div><div class="stat-card"><div class="stat-label">Avg Skip Rate</div><div class="stat-value">' + (data.avg_skip_rate * 100).toFixed(1) + '%</div></div></div>';
  var dist = data.skip_rate_distribution || {}, distKeys = Object.keys(dist), maxDist = 1;
  for (var i = 0; i < distKeys.length; i++) { if (dist[distKeys[i]] > maxDist) maxDist = dist[distKeys[i]]; }
  h += '<div class="grid-2"><div class="card"><div class="card-header">Skip Rate Distribution</div><div class="card-body">';
  for (var i = 0; i < distKeys.length; i++) { var k = distKeys[i], v = dist[k]; h += '<div class="bar-row"><div class="bar-label">' + esc(k) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (v/maxDist*100).toFixed(1) + '%;background:var(--color-danger)"></div></div><div class="bar-count">' + v + '</div></div>'; }
  h += '</div></div>';
  var perUser = data.sessions_per_user || [], maxSpu = 1;
  for (var i = 0; i < perUser.length; i++) { if (perUser[i].sessions > maxSpu) maxSpu = perUser[i].sessions; }
  h += '<div class="card"><div class="card-header">Sessions per User</div><div class="card-body">';
  for (var i = 0; i < perUser.length; i++) { var u = perUser[i]; h += '<div class="bar-row"><div class="bar-label">' + esc(u.user_id) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (u.sessions/maxSpu*100).toFixed(1) + '%;background:var(--color-info)"></div></div><div class="bar-count">' + u.sessions + '</div></div>'; }
  h += '</div></div></div>';
  el.innerHTML = h;
}

function renderScoringDetail(data) {
  var el = document.getElementById('step-detail-rich'); if (!el) return;
  if (!data.total_interactions) { el.innerHTML = '<div class="empty">No interactions yet.</div>'; return; }
  var h = '<div class="stat-card" style="display:inline-block;margin:var(--space-4) 0"><div class="stat-label">Total Interactions</div><div class="stat-value">' + data.total_interactions.toLocaleString() + '</div></div>';
  var bins = data.score_distribution || [], maxBin = 1;
  for (var i = 0; i < bins.length; i++) { if (bins[i].count > maxBin) maxBin = bins[i].count; }
  h += '<div class="grid-2"><div class="card"><div class="card-header">Satisfaction Score Distribution</div><div class="card-body">';
  for (var i = 0; i < bins.length; i++) { var b = bins[i], color = i < 3 ? 'var(--color-danger)' : i < 7 ? 'var(--color-warning)' : 'var(--color-success)'; h += '<div class="bar-row"><div class="bar-label">' + esc(b.range) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (b.count/maxBin*100).toFixed(1) + '%;background:' + color + '"></div></div><div class="bar-count">' + b.count + '</div></div>'; }
  h += '</div></div>';
  var sig = data.signal_counts || {}, sigKeys = ['full_listens','likes','repeats','playlist_adds','early_skips','dislikes'];
  var sigColors = {full_listens:'var(--color-success)',likes:'var(--color-success)',repeats:'var(--color-info)',playlist_adds:'var(--color-info)',early_skips:'var(--color-danger)',dislikes:'var(--color-danger)'};
  var maxSig = 1; for (var i = 0; i < sigKeys.length; i++) { if ((sig[sigKeys[i]]||0) > maxSig) maxSig = sig[sigKeys[i]]; }
  h += '<div class="card"><div class="card-header">Signal Breakdown</div><div class="card-body">';
  for (var i = 0; i < sigKeys.length; i++) { var k = sigKeys[i], v = sig[k]||0; h += '<div class="bar-row"><div class="bar-label">' + fmtMetricLabel(k) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (v/maxSig*100).toFixed(1) + '%;background:' + sigColors[k] + '"></div></div><div class="bar-count">' + v.toLocaleString() + '</div></div>'; }
  h += '</div></div></div>';
  h += '<div class="grid-2">' + renderTrackScoreTable('Top Scored Tracks', data.top_tracks || [], 'var(--color-success)') + renderTrackScoreTable('Lowest Scored Tracks', data.bottom_tracks || [], 'var(--color-danger)') + '</div>';
  el.innerHTML = h;
}

function renderTrackScoreTable(title, tracks, color) {
  var h = '<div class="card"><div class="card-header">' + esc(title) + '</div><div class="card-body"><table><tr><th>Track</th><th>Score</th><th>Plays</th></tr>';
  for (var i = 0; i < tracks.length; i++) { var t = tracks[i]; var name = (t.artist && t.title) ? esc(t.artist) + ' \u2014 ' + esc(t.title) : esc(t.track_id); h += '<tr><td class="truncate" style="max-width:220px">' + name + '</td><td style="color:' + color + ';font-weight:600">' + t.score.toFixed(3) + '</td><td>' + (t.plays || 0) + '</td></tr>'; }
  return h + '</table></div></div>';
}

function loadTasteProfileExplorer() {
  var el = document.getElementById('step-detail-rich'); if (!el) return;
  var h = '<div style="margin:var(--space-3) 0;display:flex;align-items:center;gap:var(--space-2)"><label class="text-xs text-muted">User:</label><select id="taste-user-select" class="form-select" onchange="loadTasteForUser(this.value)"><option value="">Select user...</option>';
  for (var i = 0; i < cachedUsers.length; i++) h += '<option value="' + esc(cachedUsers[i].user_id) + '">' + esc(cachedUsers[i].user_id) + '</option>';
  h += '</select></div><div id="taste-profile-content"><div class="empty">Select a user to view their taste profile.</div></div>';
  el.innerHTML = h;
}

function loadTasteForUser(userId) {
  if (!userId) return;
  var el = document.getElementById('taste-profile-content'); if (!el) return;
  el.innerHTML = '<div class="empty">Loading...</div>';
  api('/v1/users/' + encodeURIComponent(userId) + '/profile').then(function(profile) { renderTasteExplorer(profile); }).catch(function(e) { el.innerHTML = '<div class="empty text-danger">' + esc(e.message) + '</div>'; });
}

function renderTasteExplorer(profile) {
  var el = document.getElementById('taste-profile-content'); if (!el) return;
  var tp = profile.taste_profile;
  if (!tp) { el.innerHTML = '<div class="empty">No taste profile computed yet.</div>'; return; }
  var h = '<div class="grid-2">';
  // Radar chart
  var ap = tp.audio_preferences || {}, ts = tp.timescale_audio || {}, shortP = ts.short || {}, longP = ts.long || {};
  var axes = ['energy','valence','danceability','acousticness','instrumentalness'], labels = ['Energy','Valence','Dance','Acoustic','Instrum.'];
  h += '<div class="card"><div class="card-header">Audio Preferences (Radar)</div><div class="card-body chart-container">';
  h += renderRadarChart(axes, labels, [
    { values: axes.map(function(a) { return (ap[a] && ap[a].mean != null) ? ap[a].mean : 0.5; }), color: 'var(--color-primary)', label: 'All-time' },
    { values: axes.map(function(a) { return shortP[a] != null ? shortP[a] : 0.5; }), color: 'var(--color-success)', label: '7-day' },
    { values: axes.map(function(a) { return longP[a] != null ? longP[a] : 0.5; }), color: 'var(--color-info)', label: 'Long-term' }
  ]);
  h += '<div class="legend-row"><div class="legend-item"><div class="legend-dot" style="background:var(--color-primary)"></div> All-time</div><div class="legend-item"><div class="legend-dot" style="background:var(--color-success)"></div> 7-day</div><div class="legend-item"><div class="legend-dot" style="background:var(--color-info)"></div> Long-term</div></div></div></div>';
  // Heatmap
  var timeP = tp.time_patterns || {};
  h += '<div class="card"><div class="card-header">Time of Day Pattern</div><div class="card-body"><div class="heatmap-grid">';
  var maxTime = 0.01; for (var i = 0; i < 24; i++) { var v = parseFloat(timeP[String(i)] || 0); if (v > maxTime) maxTime = v; }
  for (var i = 0; i < 24; i++) { var v = parseFloat(timeP[String(i)] || 0); var intensity = maxTime > 0 ? v / maxTime : 0; h += '<div class="heatmap-cell" style="background:rgba(99,102,241,' + (intensity * 0.8 + 0.05).toFixed(2) + ')" title="' + i + ':00 \u2014 ' + v.toFixed(3) + '">' + i + '</div>'; }
  h += '</div></div></div></div>';
  // Mood + context bars
  h += '<div class="grid-2">';
  var mp = tp.mood_preferences || {}, moodKeys = Object.keys(mp).sort(function(a,b){return mp[b]-mp[a];});
  h += '<div class="card"><div class="card-header">Mood Preferences</div><div class="card-body">';
  if (!moodKeys.length) h += '<div class="empty">No mood data</div>';
  else { var maxMood = mp[moodKeys[0]]||1; for (var i = 0; i < Math.min(moodKeys.length, 10); i++) { var k = moodKeys[i], v = mp[k]; h += '<div class="bar-row"><div class="bar-label">' + esc(k) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (v/maxMood*100).toFixed(1) + '%;background:var(--color-success)"></div></div><div class="bar-count">' + v.toFixed(2) + '</div></div>'; } }
  h += '</div></div>';
  var patternSets = [['Device',tp.device_patterns||{},'var(--color-info)'],['Context Type',tp.context_type_patterns||{},'var(--color-primary)'],['Output',tp.output_patterns||{},'var(--color-warning)'],['Location',tp.location_patterns||{},'var(--color-success)']];
  for (var p = 0; p < patternSets.length; p++) {
    var pName = patternSets[p][0], pData = patternSets[p][1], pColor = patternSets[p][2];
    var pKeys = Object.keys(pData).sort(function(a,b){return pData[b]-pData[a];}); if (!pKeys.length) continue;
    var maxP = pData[pKeys[0]]||1;
    h += '<div class="card"><div class="card-header">' + esc(pName) + ' Patterns</div><div class="card-body">';
    for (var i = 0; i < pKeys.length; i++) { var k = pKeys[i], v = pData[k]; h += '<div class="bar-row"><div class="bar-label">' + esc(k) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (v/maxP*100).toFixed(1) + '%;background:' + pColor + '"></div></div><div class="bar-count">' + v.toFixed(2) + '</div></div>'; }
    h += '</div></div>';
  }
  h += '</div>';
  // Behaviour
  var b = tp.behaviour || {};
  if (b.total_plays != null) {
    h += '<div class="stats-grid" style="margin-top:var(--space-4)"><div class="stat-card"><div class="stat-label">Total Plays</div><div class="stat-value">' + (b.total_plays||0).toLocaleString() + '</div></div><div class="stat-card"><div class="stat-label">Active Days</div><div class="stat-value">' + (b.active_days||0) + '</div></div><div class="stat-card"><div class="stat-label">Avg Session Tracks</div><div class="stat-value">' + (b.avg_session_tracks||0).toFixed(1) + '</div></div><div class="stat-card"><div class="stat-label">Skip Rate</div><div class="stat-value">' + (b.skip_rate != null ? (b.skip_rate * 100).toFixed(1) + '%' : '\u2014') + '</div></div><div class="stat-card"><div class="stat-label">Avg Completion</div><div class="stat-value">' + (b.avg_completion != null ? (b.avg_completion * 100).toFixed(1) + '%' : '\u2014') + '</div></div></div>';
  }
  el.innerHTML = h;
}

function renderRadarChart(axes, labels, series) {
  var cx = 120, cy = 120, r = 90, n = axes.length;
  var h = '<svg width="240" height="260" viewBox="0 0 240 260">';
  for (var ring = 1; ring <= 4; ring++) { var rr = r * ring / 4, pts = []; for (var i = 0; i < n; i++) { var angle = (Math.PI * 2 * i / n) - Math.PI / 2; pts.push((cx + rr * Math.cos(angle)).toFixed(1) + ',' + (cy + rr * Math.sin(angle)).toFixed(1)); } h += '<polygon points="' + pts.join(' ') + '" fill="none" stroke="' + (ring === 4 ? 'var(--border-strong)' : 'rgba(255,255,255,0.04)') + '" stroke-width="1"/>'; }
  for (var i = 0; i < n; i++) { var angle = (Math.PI * 2 * i / n) - Math.PI / 2; var x2 = cx + r * Math.cos(angle), y2 = cy + r * Math.sin(angle); h += '<line x1="' + cx + '" y1="' + cy + '" x2="' + x2.toFixed(1) + '" y2="' + y2.toFixed(1) + '" stroke="var(--border)" stroke-width="1"/>'; var lx = cx + (r + 14) * Math.cos(angle), ly = cy + (r + 14) * Math.sin(angle); var anchor = Math.abs(Math.cos(angle)) < 0.1 ? 'middle' : Math.cos(angle) > 0 ? 'start' : 'end'; h += '<text x="' + lx.toFixed(1) + '" y="' + (ly + 4).toFixed(1) + '" class="radar-label" text-anchor="' + anchor + '">' + esc(labels[i]) + '</text>'; }
  for (var s = 0; s < series.length; s++) { var vals = series[s].values, color = series[s].color, pts = []; for (var i = 0; i < n; i++) { var angle = (Math.PI * 2 * i / n) - Math.PI / 2; var v = Math.max(0, Math.min(1, vals[i] || 0)); pts.push((cx + r * v * Math.cos(angle)).toFixed(1) + ',' + (cy + r * v * Math.sin(angle)).toFixed(1)); } h += '<polygon points="' + pts.join(' ') + '" fill="' + color + '" fill-opacity="0.1" stroke="' + color + '" stroke-width="2"/>'; for (var i = 0; i < n; i++) { var angle = (Math.PI * 2 * i / n) - Math.PI / 2; var v = Math.max(0, Math.min(1, vals[i] || 0)); h += '<circle cx="' + (cx + r * v * Math.cos(angle)).toFixed(1) + '" cy="' + (cy + r * v * Math.sin(angle)).toFixed(1) + '" r="3" fill="' + color + '"/>'; } }
  return h + '</svg>';
}

function renderRankerDetail(models, modelReport) {
  var el = document.getElementById('step-detail-rich'); if (!el) return;
  var ranker = models ? models.ranker : null;
  if (!ranker || !ranker.trained) { el.innerHTML = '<div class="empty">Ranker model not trained yet.</div>'; return; }
  var h = '<div class="stats-grid" style="margin:var(--space-4) 0"><div class="stat-card"><div class="stat-label">Training Samples</div><div class="stat-value">' + (ranker.training_samples||0).toLocaleString() + '</div></div><div class="stat-card"><div class="stat-label">Features</div><div class="stat-value">' + (ranker.n_features||0) + '</div></div><div class="stat-card"><div class="stat-label">Engine</div><div class="stat-value" style="font-size:1rem">' + esc(ranker.engine||'?') + '</div></div><div class="stat-card"><div class="stat-label">Trained</div><div class="stat-value" style="font-size:1rem">' + timeAgo(ranker.trained_at) + '</div></div>';
  if (modelReport && modelReport.latest_evaluation && modelReport.latest_evaluation.ndcg_at_10 != null) { var ev = modelReport.latest_evaluation; var color = ev.lift_over_popularity_pct > 0 ? 'var(--color-success)' : 'var(--color-warning)'; h += '<div class="stat-card"><div class="stat-label">NDCG@10</div><div class="stat-value" style="color:' + color + '">' + ev.ndcg_at_10.toFixed(4) + '</div>' + (ev.lift_over_popularity_pct != null ? '<div class="stat-sub">Lift: ' + (ev.lift_over_popularity_pct > 0 ? '+' : '') + ev.lift_over_popularity_pct + '%</div>' : '') + '</div>'; }
  h += '</div>';
  var fi = ranker.feature_importances || {}, fiKeys = Object.keys(fi).sort(function(a,b){return fi[b]-fi[a];});
  if (fiKeys.length > 0) { var maxFi = fi[fiKeys[0]]||1; h += '<div class="card"><div class="card-header">Feature Importance (top 20)</div><div class="card-body">'; for (var i = 0; i < Math.min(fiKeys.length, 20); i++) { var k = fiKeys[i], v = fi[k]; h += '<div class="feat-bar"><div class="feat-name">' + esc(k) + '</div><div class="feat-track"><div class="feat-fill" style="width:' + (v/maxFi*100).toFixed(1) + '%"></div></div><div class="feat-val">' + v.toFixed(0) + '</div></div>'; } h += '</div></div>'; }
  if (modelReport && modelReport.latest_evaluation) {
    var ev = modelReport.latest_evaluation;
    h += '<div class="grid-2" style="margin-top:var(--space-4)"><div class="card"><div class="card-header">NDCG@10 Baseline Comparison</div><div class="card-body">';
    var bars = [['Model',ev.ndcg_at_10,'var(--color-primary)'],['Popularity',ev.baseline_popularity_ndcg_at_10,'var(--color-warning)'],['Random',ev.baseline_random_ndcg_at_10,'var(--text-muted)']];
    var maxNdcg = 0.001; for (var i = 0; i < bars.length; i++) { if (bars[i][1] && bars[i][1] > maxNdcg) maxNdcg = bars[i][1]; }
    for (var i = 0; i < bars.length; i++) { if (bars[i][1] == null) continue; h += '<div class="bar-row"><div class="bar-label">' + bars[i][0] + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (bars[i][1]/maxNdcg*100).toFixed(1) + '%;background:' + bars[i][2] + '"></div></div><div class="bar-count">' + bars[i][1].toFixed(4) + '</div></div>'; }
    h += '</div></div>';
    if (modelReport.impressions) { var imp = modelReport.impressions; var funnelMax = Math.max(imp.impressions||1, 1); h += '<div class="card"><div class="card-header">Impression-to-Stream Funnel</div><div class="card-body">'; h += '<div class="bar-row"><div class="bar-label">Impressions</div><div class="bar-track"><div class="bar-fill" style="width:100%;background:var(--color-primary)"></div></div><div class="bar-count">' + (imp.impressions||0).toLocaleString() + '</div></div>'; h += '<div class="bar-row"><div class="bar-label">Streams</div><div class="bar-track"><div class="bar-fill" style="width:' + ((imp.streams_from_reco||0)/funnelMax*100).toFixed(1) + '%;background:var(--color-success)"></div></div><div class="bar-count">' + (imp.streams_from_reco||0).toLocaleString() + '</div></div>'; if (imp.i2s_rate != null) h += '<div style="padding:var(--space-3) var(--space-4);font-size:0.875rem;font-weight:600;color:var(--color-success)">I2S Rate: ' + (imp.i2s_rate * 100).toFixed(1) + '%</div>'; h += '</div></div>'; }
    h += '</div>';
  }
  el.innerHTML = h;
}

// =========================================================================
// Recommendations View
// =========================================================================
function loadRecommendations() {
  var h = '<div class="page-header"><h1 class="page-title">Recommendations</h1><div class="page-actions">';
  h += '<button class="btn btn-secondary btn-sm" onclick="runPipeline()">Run Pipeline</button>';
  h += '<button class="btn btn-danger btn-sm" onclick="resetPipeline()">Reset Pipeline</button>';
  h += '<span style="width:1px;height:20px;background:var(--border);display:inline-block;vertical-align:middle"></span>';
  h += '<select id="reco-user" class="form-select"><option value="">Select user...</option>';
  for (var i = 0; i < cachedUsers.length; i++) h += '<option value="' + esc(cachedUsers[i].user_id) + '">' + esc(cachedUsers[i].user_id) + '</option>';
  h += '</select>';
  h += '<input id="reco-seed" class="form-input" placeholder="Seed track (optional)" style="width:200px">';
  h += '<input id="reco-limit" type="number" class="form-input" value="25" min="1" max="100" style="width:60px">';
  h += '<button class="btn btn-primary btn-sm" onclick="fetchRecommendations()">Get Recs</button>';
  h += '<button class="btn btn-secondary btn-sm" onclick="fetchDebugRecommendations()">Debug Recs</button>';
  h += '</div></div>';
  h += '<div id="reco-results"><div class="empty">Select a user and click "Get Recs" for normal results, or "Debug Recs" to trace the pipeline.</div></div>';
  setAppContent(h);
}

function fetchRecommendations() {
  var userId = document.getElementById('reco-user').value; if (!userId) return;
  var seed = document.getElementById('reco-seed').value.trim();
  var limit = parseInt(document.getElementById('reco-limit').value) || 25;
  var url = '/v1/recommend/' + encodeURIComponent(userId) + '?limit=' + limit;
  if (seed) url += '&seed_track_id=' + encodeURIComponent(seed);
  document.getElementById('reco-results').innerHTML = '<div class="empty">Loading recommendations...</div>';
  api(url).then(function(data) { renderRecoResults(data); }).catch(function(e) { document.getElementById('reco-results').innerHTML = '<div class="empty text-danger">Error: ' + esc(e.message) + '</div>'; });
}

function renderRecoResults(data) {
  var tracks = data.tracks || [];
  var h = '<div class="card"><div class="card-header">Results for ' + esc(data.user_id) + ' <span class="subtitle">' + tracks.length + ' tracks \u00B7 model: ' + esc(data.model_version) + ' \u00B7 request: ' + esc(data.request_id).slice(0, 8) + '...</span></div>';
  if (!tracks.length) { h += '<div class="card-body"><div class="empty">No recommendations available.</div></div></div>'; document.getElementById('reco-results').innerHTML = h; return; }
  var maxScore = 0; for (var i = 0; i < tracks.length; i++) { if (tracks[i].score > maxScore) maxScore = tracks[i].score; }
  h += '<div class="card-body" style="overflow-x:auto"><table><tr><th>#</th><th>Track</th><th>Artist</th><th>Source</th><th>Score</th><th>BPM</th><th>Key</th><th>Energy</th><th>Mood</th><th>Duration</th></tr>';
  for (var i = 0; i < tracks.length; i++) { var t = tracks[i]; h += '<tr><td>' + (t.position + 1) + '</td><td class="truncate" title="' + trackTooltip(t) + '">' + (esc(t.title || '') || basename(t.file_path)) + '</td><td class="truncate" style="max-width:150px">' + esc(t.artist || '\u2014') + '</td><td>' + sourceBadge(t.source) + '</td><td class="nowrap">' + scoreBar(t.score, maxScore) + '</td><td>' + (t.bpm ? t.bpm.toFixed(1) : '\u2014') + '</td><td>' + esc(t.key || '\u2014') + ' ' + (t.mode ? t.mode.charAt(0) : '') + '</td><td>' + (t.energy != null ? t.energy.toFixed(2) : '\u2014') + '</td><td>' + topMood(t.mood_tags) + '</td><td>' + fmtTrackDur(t.duration) + '</td></tr>'; }
  h += '</table></div></div>';
  // Source distribution
  var srcCounts = {}; for (var i = 0; i < tracks.length; i++) { var s = tracks[i].source || 'unknown'; srcCounts[s] = (srcCounts[s] || 0) + 1; }
  h += '<div class="card" style="margin-top:var(--space-4)"><div class="card-header">Source Distribution</div><div class="card-body">';
  var srcKeys = Object.keys(srcCounts); for (var i = 0; i < srcKeys.length; i++) { var k = srcKeys[i], c = srcCounts[k]; h += '<div class="bar-row"><div class="bar-label">' + sourceBadge(k) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (c/tracks.length*100).toFixed(1) + '%"></div></div><div class="bar-count">' + c + '</div></div>'; }
  h += '</div></div>';
  document.getElementById('reco-results').innerHTML = h;
}

function fetchDebugRecommendations() {
  var userId = document.getElementById('reco-user').value; if (!userId) return;
  var seed = document.getElementById('reco-seed').value.trim();
  var limit = parseInt(document.getElementById('reco-limit').value) || 25;
  var url = '/v1/recommend/' + encodeURIComponent(userId) + '?limit=' + limit + '&debug=true';
  if (seed) url += '&seed_track_id=' + encodeURIComponent(seed);
  document.getElementById('reco-results').innerHTML = '<div class="empty">Loading debug recommendations...</div>';
  api(url).then(function(data) { renderDebugResults(data); }).catch(function(e) { document.getElementById('reco-results').innerHTML = '<div class="empty text-danger">Error: ' + esc(e.message) + '</div>'; });
}

function renderDebugResults(data) {
  var tracks = data.tracks || [], debug = data.debug || {};
  var h = '<div class="card" style="margin-bottom:var(--space-4)"><div class="card-header">Debug: ' + esc(data.user_id) + ' <span class="subtitle">' + tracks.length + ' tracks \u00B7 model: ' + esc(data.model_version) + ' \u00B7 total candidates: ' + (debug.total_candidates || '?') + '</span></div></div>';
  var cbs = debug.candidates_by_source || {}, srcKeys = Object.keys(cbs);
  var srcColors = { content:'var(--color-primary)',content_profile:'var(--color-primary)',cf:'var(--color-info)',session_skipgram:'var(--color-warning)',sasrec:'var(--color-success)',lastfm_similar:'var(--color-danger)',artist_recall:'var(--color-success)',popular:'var(--color-warning)' };
  h += '<div class="grid-2"><div class="card"><div class="card-header">Candidates by Source</div><div class="card-body">';
  var maxSrc = 1; for (var i = 0; i < srcKeys.length; i++) { if (cbs[srcKeys[i]].length > maxSrc) maxSrc = cbs[srcKeys[i]].length; }
  for (var i = 0; i < srcKeys.length; i++) { var k = srcKeys[i], cnt = cbs[k].length; h += '<div class="bar-row"><div class="bar-label">' + sourceBadge(k) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (cnt/maxSrc*100).toFixed(1) + '%;background:' + (srcColors[k]||'var(--color-primary)') + '"></div></div><div class="bar-count">' + cnt + '</div></div>'; }
  h += '</div></div>';
  var actions = debug.reranker_actions || [];
  h += '<div class="card"><div class="card-header">Reranker Actions (' + actions.length + ')</div><div class="card-body">';
  if (!actions.length) h += '<div class="empty">No reranker actions taken.</div>';
  else { var byAction = {}; for (var i = 0; i < actions.length; i++) { var a = actions[i].action; byAction[a] = (byAction[a] || 0) + 1; } var actionKeys = Object.keys(byAction); var actionColors = { freshness_boost:'var(--color-success)',skip_suppression:'var(--color-danger)',anti_repetition_exclude:'var(--color-danger)',short_track_exclude:'var(--color-warning)',exploration_slot:'var(--color-info)',artist_diversity_demote:'var(--color-warning)' }; for (var i = 0; i < actionKeys.length; i++) { var k = actionKeys[i]; h += '<div class="bar-row"><div class="bar-label" style="min-width:160px">' + fmtMetricLabel(k) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + (byAction[k]/actions.length*100).toFixed(1) + '%;background:' + (actionColors[k]||'var(--color-primary)') + '"></div></div><div class="bar-count">' + byAction[k] + '</div></div>'; } }
  h += '</div></div></div>';
  // Rank comparison
  var preRank = debug.pre_rerank || [], prePos = {}, postPos = {};
  for (var i = 0; i < preRank.length; i++) prePos[preRank[i].track_id] = i;
  for (var i = 0; i < tracks.length; i++) postPos[tracks[i].track_id] = i;
  h += '<div class="card" style="margin-bottom:var(--space-4)"><div class="card-header">Rank Comparison (Pre-Rerank \u2192 Final)</div><div class="card-body"><div class="debug-columns">';
  h += '<div style="max-height:400px;overflow-y:auto"><div style="padding:var(--space-2) var(--space-3)" class="text-xs text-muted font-semibold" style="text-transform:uppercase;letter-spacing:0.05em">Final Order (click to inspect features)</div>';
  for (var i = 0; i < tracks.length; i++) { var t = tracks[i]; var pre = prePos[t.track_id]; var delta = pre != null ? pre - i : 0; var deltaCls = delta > 0 ? 'up' : delta < 0 ? 'down' : 'same'; var deltaStr = delta > 0 ? '\u25B2' + delta : delta < 0 ? '\u25BC' + Math.abs(delta) : '\u2014'; var name = (t.title && t.artist) ? esc(t.artist) + ' \u2014 ' + esc(t.title) : esc(t.track_id); h += '<div class="rank-compare-row" onclick="toggleFeatureInspector(\'' + esc(t.track_id) + '\')"><span class="rank-num">' + (i + 1) + '</span><span class="rank-track" title="' + esc(t.track_id) + '">' + name + '</span>' + sourceBadge(t.source) + ' <span class="text-xs text-muted">' + t.score.toFixed(3) + '</span> <span class="rank-delta ' + deltaCls + '">' + deltaStr + '</span></div><div id="feat-inspect-' + esc(t.track_id) + '" style="display:none"></div>'; }
  h += '</div>';
  h += '<div style="max-height:400px;overflow-y:auto"><div style="padding:var(--space-2) var(--space-3)" class="text-xs text-muted font-semibold" style="text-transform:uppercase;letter-spacing:0.05em">Reranker Actions Detail</div>';
  for (var i = 0; i < actions.length; i++) { var a = actions[i]; var aColor = (a.action.indexOf('boost') >= 0 || a.action === 'exploration_slot') ? 'var(--color-success)' : 'var(--color-danger)'; h += '<div style="padding:var(--space-1) var(--space-3);font-size:0.75rem;border-bottom:1px solid var(--border)"><span style="color:' + aColor + ';font-weight:600">' + fmtMetricLabel(a.action) + '</span> <span class="font-mono text-xs text-muted">' + esc(a.track_id).slice(0, 16) + '</span>'; if (a.score_before != null) h += ' <span class="text-xs">' + a.score_before.toFixed(3) + ' \u2192 ' + a.score_after.toFixed(3) + '</span>'; if (a.noise_added != null) h += ' <span class="text-xs">noise: +' + a.noise_added.toFixed(3) + '</span>'; if (a.from_position != null) h += ' <span class="text-xs">pos ' + a.from_position + ' \u2192 ' + a.to_position + '</span>'; h += '</div>'; }
  h += '</div></div></div></div>';
  window._debugData = debug;
  document.getElementById('reco-results').innerHTML = h;
}

function toggleFeatureInspector(trackId) {
  var el = document.getElementById('feat-inspect-' + trackId); if (!el) return;
  if (el.style.display !== 'none') { el.style.display = 'none'; return; }
  var fv = window._debugData && window._debugData.feature_vectors && window._debugData.feature_vectors[trackId];
  if (!fv) { el.innerHTML = '<div style="padding:var(--space-2) var(--space-3)" class="text-xs text-muted">No feature data</div>'; el.style.display = 'block'; return; }
  var h = '<div class="feat-grid">'; var keys = Object.keys(fv);
  for (var i = 0; i < keys.length; i++) h += '<div class="feat-cell"><span class="fk">' + esc(keys[i]) + ':</span> <span class="fv">' + fv[keys[i]] + '</span></div>';
  el.innerHTML = h + '</div>'; el.style.display = 'block';
}

function runPipeline() {
  switchTab('pipeline');
  setTimeout(function() { apiPost('/v1/pipeline/run', {}).then(function() { pipelineConnectSSE(); setTimeout(function() { pipelineRefreshStatus(); }, 500); }).catch(function(e) { alert('Pipeline failed: ' + e.message); }); }, 200);
}

function resetPipeline() {
  if (!confirm('This will delete all sessions, interactions, and taste profiles, then rebuild from raw events. Continue?')) return;
  switchTab('pipeline');
  setTimeout(function() { apiPost('/v1/pipeline/reset', {}).then(function() { pipelineConnectSSE(); setTimeout(function() { pipelineRefreshStatus(); }, 500); }).catch(function(e) { alert('Reset failed: ' + e.message); }); }, 200);
}

// =========================================================================
// Charts View
// =========================================================================
function loadCharts() {
  return Promise.all([api('/v1/charts'), api('/v1/charts/stats').catch(function(){return null;}), api('/v1/charts/' + chartsCurrentType + '?scope=' + encodeURIComponent(chartsCurrentScope) + '&limit=100').catch(function(){return null;})]).then(function(r) { renderCharts(r[0], r[1], r[2]); });
}

function chartsLibraryBadge(entry) {
  if (entry.in_library) { if (entry.lidarr_status === 'in_lidarr' || entry.lidarr_status === 'downloading') return '<span class="badge badge-success">in library</span> <span class="badge badge-purple text-xs" style="opacity:0.7">via lidarr</span>'; return '<span class="badge badge-success">in library</span>'; }
  var badges = ''; var ls = entry.lidarr_status;
  if (ls === 'downloading') badges += '<span class="badge badge-info">\u2B07 lidarr</span>';
  else if (ls === 'in_lidarr') badges += '<span class="badge badge-success">\u2B07 lidarr</span>';
  else if (ls === 'pending') badges += '<span class="badge badge-warning">\u23F3 lidarr</span>';
  else if (ls === 'failed') badges += '<span class="badge badge-danger">\u2717 lidarr</span>';
  if (!entry.in_library && entry.track_title) badges += ' <button onclick="chartsDownloadTrack(this,' + entry.position + ')" title="Download" class="btn btn-sm" style="font-size:0.625rem;padding:2px var(--space-2);background:rgba(14,165,233,0.15);color:var(--color-info);border:none">\u2B07 get</button>';
  return badges || '<span class="badge badge-neutral" style="opacity:0.4">not in library</span>';
}

function chartsThumbnail(entry) {
  var src = (entry.library && entry.library.cover_url) || entry.image_url || '';
  if (!src) return '<div style="width:40px;height:40px;border-radius:var(--radius-sm);background:var(--hover-overlay);display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:16px">\u266B</div>';
  return '<img src="' + esc(src) + '" alt="" loading="lazy" style="width:40px;height:40px;border-radius:var(--radius-sm);object-fit:cover;display:block" onerror="this.style.display=\'none\'">';
}

function chartsDownloadTrack(btn, position) {
  btn.disabled = true; btn.textContent = '...';
  apiPost('/v1/charts/download', { chart_type: chartsCurrentType, scope: chartsCurrentScope, position: position }).then(function(data) { if (data.status === 'downloading' || data.status === 'duplicate') btn.outerHTML = '<span class="badge badge-info">\u2B07 queued</span>'; else { btn.textContent = data.status || 'sent'; btn.disabled = false; } }).catch(function(e) { btn.outerHTML = '<span class="badge badge-danger" title="' + esc(e.message || 'failed') + '">\u2717 failed</span>'; });
}

function renderCharts(available, stats, chartData) {
  var h = '<div class="page-header"><h1 class="page-title">Charts</h1><div class="page-actions"><button class="btn btn-primary btn-sm" onclick="buildCharts(this)">Build Charts</button></div></div>';
  if (stats) {
    h += '<div class="stats-grid"><div class="stat-card"><div class="stat-label">Charts</div><div class="stat-value">' + (stats.chart_count||0) + '</div></div><div class="stat-card"><div class="stat-label">Total Entries</div><div class="stat-value">' + (stats.total_entries||0) + '</div></div><div class="stat-card"><div class="stat-label">Library Matches</div><div class="stat-value text-success">' + (stats.library_matches||0) + '</div></div><div class="stat-card"><div class="stat-label">Match Rate</div><div class="stat-value">' + ((stats.match_rate||0) * 100).toFixed(1) + '%</div>' + (stats.last_fetched_at ? '<div class="stat-sub">Updated ' + timeAgo(stats.last_fetched_at) + '</div>' : '') + '</div></div>';
  }
  var charts = (available && available.charts) || [];
  if (!charts.length) { h += '<div class="empty">No charts built yet. Click "Build Charts" to fetch from Last.fm.<br><br>Configure <code>CHARTS_ENABLED=true</code> and <code>CHARTS_TAGS</code> / <code>CHARTS_COUNTRIES</code> in your .env file.</div>'; setAppContent(h); return; }
  var scopes = [], seenScopes = {};
  for (var i = 0; i < charts.length; i++) { if (!seenScopes[charts[i].scope]) { seenScopes[charts[i].scope] = true; scopes.push(charts[i].scope); } }
  h += '<div class="filter-bar"><label>Scope</label><select class="form-select" onchange="chartsCurrentScope=this.value;loadCharts()">';
  for (var i = 0; i < scopes.length; i++) { var label = scopes[i]; if (label === 'global') label = 'Global'; else if (label.indexOf('tag:') === 0) label = 'Genre: ' + label.substring(4); else if (label.indexOf('geo:') === 0) label = 'Country: ' + label.substring(4); h += '<option value="' + esc(scopes[i]) + '"' + (scopes[i] === chartsCurrentScope ? ' selected' : '') + '>' + esc(label) + '</option>'; }
  h += '</select><label>Type</label><select class="form-select" onchange="chartsCurrentType=this.value;loadCharts()">';
  h += '<option value="top_tracks"' + (chartsCurrentType === 'top_tracks' ? ' selected' : '') + '>Top Tracks</option>';
  h += '<option value="top_artists"' + (chartsCurrentType === 'top_artists' ? ' selected' : '') + '>Top Artists</option></select></div>';
  if (!chartData || !chartData.entries || !chartData.entries.length) { h += '<div class="empty">No data for this chart type / scope combination.</div>'; setAppContent(h); return; }
  var isTrack = chartsCurrentType === 'top_tracks';
  h += '<div class="card" style="margin-top:var(--space-4)"><div class="card-header">' + esc(chartsCurrentScope === 'global' ? 'Global' : chartsCurrentScope) + ' \u2014 ' + (isTrack ? 'Top Tracks' : 'Top Artists') + ' <span class="subtitle">' + chartData.total + ' entries' + (chartData.fetched_at ? ', updated ' + timeAgo(chartData.fetched_at) : '') + '</span></div>';
  h += '<div class="card-body" style="overflow-x:auto"><table>';
  if (isTrack) h += '<tr><th style="width:40px">#</th><th style="width:52px"></th><th>Title</th><th>Artist</th><th>Plays</th><th>Listeners</th><th>Status</th></tr>';
  else h += '<tr><th style="width:40px">#</th><th style="width:52px"></th><th>Artist</th><th>Plays</th><th>Listeners</th><th>Library Tracks</th><th>Status</th></tr>';
  for (var i = 0; i < chartData.entries.length; i++) { var e = chartData.entries[i]; h += '<tr><td class="text-muted font-semibold">' + (e.position + 1) + '</td><td>' + chartsThumbnail(e) + '</td>'; if (isTrack) h += '<td><strong>' + esc(e.track_title||'') + '</strong></td><td>' + esc(e.artist_name||'') + '</td>'; else h += '<td><strong>' + esc(e.artist_name||'') + '</strong></td>'; h += '<td>' + fmtNumber(e.playcount) + '</td><td>' + fmtNumber(e.listeners) + '</td>'; if (!isTrack) h += '<td>' + (e.library_track_count||0) + '</td>'; h += '<td>' + chartsLibraryBadge(e) + '</td></tr>'; }
  h += '</table></div></div>';
  setAppContent(h);
}

function buildCharts(btn) {
  btn.disabled = true; btn.textContent = 'Building...';
  apiPost('/v1/charts/build', {}).then(function() { btn.textContent = 'Done'; setTimeout(function() { loadCharts(); }, 1000); }).catch(function(e) { alert('Chart build failed: ' + e.message); btn.disabled = false; btn.textContent = 'Build Charts'; });
}

// =========================================================================
// Discovery View
// =========================================================================
function loadDiscovery() {
  return Promise.all([api('/v1/discovery/stats'), api('/v1/discovery?limit=50')]).then(function(r) { renderDiscovery(r[0], r[1]); });
}

function renderDiscovery(stats, data) {
  var h = '<div class="page-header"><h1 class="page-title">Music Discovery</h1>';
  if (stats.enabled) h += '<div class="page-actions"><button class="btn btn-primary btn-sm" onclick="runDiscovery(this)">Run Discovery</button></div>';
  h += '</div>';
  if (!stats.enabled) { h += '<div class="empty">Music Discovery is not configured.<br>Set <code>LASTFM_API_KEY</code>, <code>LIDARR_URL</code>, and <code>LIDARR_API_KEY</code> in your .env file.</div>'; setAppContent(h); return; }
  var sent = (stats.by_status.sent||0) + (stats.by_status.in_lidarr||0);
  h += '<div class="stats-grid"><div class="stat-card"><div class="stat-label">Total Discovered</div><div class="stat-value">' + stats.total + '</div></div><div class="stat-card"><div class="stat-label">Sent to Lidarr</div><div class="stat-value text-success">' + sent + '</div></div><div class="stat-card"><div class="stat-label">Pending</div><div class="stat-value text-warning">' + (stats.by_status.pending||0) + '</div></div><div class="stat-card"><div class="stat-label">Today</div><div class="stat-value">' + stats.today_count + ' <span style="font-size:0.875rem" class="text-muted">/ ' + stats.daily_limit + '</span></div></div></div>';
  var requests = data.requests || [];
  if (!requests.length) { h += '<div class="empty">No discovery requests yet. Click "Run Discovery" to start finding new music.</div>'; setAppContent(h); return; }
  h += '<div class="card"><div class="card-header">Recent Discoveries <span class="subtitle">' + data.total + ' total</span></div>';
  h += '<div class="card-body" style="overflow-x:auto"><table><tr><th>Artist</th><th>Source</th><th>Seed</th><th>Similarity</th><th>Status</th><th>When</th></tr>';
  for (var i = 0; i < requests.length; i++) {
    var r = requests[i];
    var seed = r.seed_artist ? esc(r.seed_artist) : (r.seed_genre ? '<span class="text-muted">' + esc(r.seed_genre) + '</span>' : '\u2014');
    var srcBadge = r.source === 'lastfm_similar' ? '<span class="badge badge-purple">similar</span>' : r.source === 'lastfm_genre' ? '<span class="badge badge-info">genre</span>' : '<span class="badge">' + esc(r.source) + '</span>';
    var statusCls = r.status === 'sent' ? 'info' : r.status === 'in_lidarr' ? 'success' : r.status === 'failed' ? 'danger' : 'warning';
    var err = r.error_message ? ' title="' + esc(r.error_message) + '"' : '';
    h += '<tr' + err + '><td><strong>' + esc(r.artist_name) + '</strong>' + (r.artist_mbid ? '<br><span class="mono text-xs text-muted">' + esc(r.artist_mbid).substring(0, 16) + '...</span>' : '') + '</td><td>' + srcBadge + '</td><td>' + seed + '</td><td style="min-width:80px">' + (r.similarity_score != null ? '<div class="bar-track" style="height:14px"><div class="bar-fill" style="width:' + (r.similarity_score * 100).toFixed(0) + '%"></div></div>' : '\u2014') + '</td><td><span class="badge badge-' + statusCls + '">' + esc(r.status) + '</span></td><td>' + timeAgo(r.created_at) + '</td></tr>';
  }
  h += '</table></div></div>';
  setAppContent(h);
}

function runDiscovery(btn) {
  btn.disabled = true; btn.textContent = 'Running...';
  apiPost('/v1/discovery/run', {}).then(function() { btn.textContent = 'Started'; setTimeout(function() { loadDiscovery(); }, 3000); setTimeout(function() { loadDiscovery(); }, 10000); }).catch(function(e) { alert('Discovery failed: ' + e.message); btn.disabled = false; btn.textContent = 'Run Discovery'; });
}

// =========================================================================
// News View
// =========================================================================
var newsTagFilter = '';
var newsUser = '';

function loadNews() {
  if (!newsUser && cachedUsers && cachedUsers.length) newsUser = cachedUsers[0].user_id;
  if (!newsUser) { setAppContent('<div class="empty">No users found. Create a user first.</div>'); return; }
  var url = '/v1/news/' + encodeURIComponent(newsUser) + '?limit=50';
  if (newsTagFilter) url += '&tag=' + encodeURIComponent(newsTagFilter);
  api(url).then(renderNews).catch(function(e) {
    if (e.message && e.message.indexOf('503') !== -1) {
      setAppContent('<div class="page-header"><h1 class="page-title">Music News</h1></div><div class="empty">News feed is not enabled.<br>Set <code>NEWS_ENABLED=true</code> in your .env file.</div>');
    } else {
      setAppContent('<div class="page-header"><h1 class="page-title">Music News</h1></div><div class="empty">Failed to load news: ' + esc(e.message) + '</div>');
    }
  });
}

function refreshNewsFeed() {
  apiPost('/v1/news/refresh', {}).then(function(result) {
    loadNews();
  }).catch(function(e) {
    alert('Failed to refresh news: ' + e.message);
  });
}

function renderNews(data) {
  var h = '<div class="page-header"><h1 class="page-title">Music News</h1>';
  h += '<div class="page-actions">';
  // User selector
  if (cachedUsers && cachedUsers.length > 1) {
    h += '<select class="form-select" onchange="newsUser=this.value;loadNews()" style="width:auto;height:32px;font-size:0.75rem;padding:4px 10px;margin-right:8px">';
    for (var i = 0; i < cachedUsers.length; i++) {
      h += '<option value="' + esc(cachedUsers[i].user_id) + '"' + (cachedUsers[i].user_id === newsUser ? ' selected' : '') + '>' + esc(cachedUsers[i].user_id) + '</option>';
    }
    h += '</select>';
  }
  // Tag filter
  h += '<select class="form-select" onchange="newsTagFilter=this.value;loadNews()" style="width:auto;height:32px;font-size:0.75rem;padding:4px 10px">';
  h += '<option value="">All Posts</option>';
  var tags = ['FRESH', 'NEWS', 'DISCUSSION'];
  for (var t = 0; t < tags.length; t++) {
    h += '<option value="' + tags[t] + '"' + (newsTagFilter === tags[t] ? ' selected' : '') + '>' + tags[t] + '</option>';
  }
  h += '</select>';
  // Refresh button
  h += '<button class="btn btn-primary btn-sm" onclick="refreshNewsFeed()" style="margin-left:8px">Refresh Feed</button>';
  h += '</div></div>';

  // Cache status
  if (data.cache_age_minutes != null) {
    var staleClass = data.cache_stale ? ' text-warning' : ' text-muted';
    h += '<div style="text-align:right;margin-bottom:8px;font-size:0.75rem" class="' + staleClass + '">Cache: ' + Math.round(data.cache_age_minutes) + 'min ago' + (data.cache_stale ? ' (stale)' : '') + '</div>';
  }

  if (!data.items || !data.items.length) {
    h += '<div class="empty">No news articles found.' + (newsTagFilter ? ' Try removing the tag filter.' : '') + '</div>';
    setAppContent(h); return;
  }

  h += '<div class="news-grid">';
  for (var i = 0; i < data.items.length; i++) {
    var item = data.items[i];
    h += '<div class="news-card">';
    h += '<div class="news-card-header">';
    h += '<a href="' + esc(item.reddit_url) + '" target="_blank" rel="noopener" class="news-title">' + esc(item.title) + '</a>';
    h += '</div>';
    h += '<div class="news-card-meta">';
    h += '<span class="badge badge-info">r/' + esc(item.subreddit) + '</span> ';
    if (item.is_fresh) h += '<span class="badge badge-success">FRESH</span> ';
    if (item.parsed_tag && item.parsed_tag !== 'FRESH') h += '<span class="badge">' + esc(item.parsed_tag) + '</span> ';
    h += '<span class="text-muted text-sm">' + item.score + ' pts &middot; ' + item.num_comments + ' comments &middot; ' + item.age_hours + 'h ago</span>';
    h += '</div>';
    // Relevance reasons
    if (item.relevance_reasons && item.relevance_reasons.length) {
      h += '<div class="news-reasons">';
      for (var r = 0; r < item.relevance_reasons.length; r++) {
        var reason = item.relevance_reasons[r];
        var label = reason === 'artist_match' ? 'Artist you like' : reason === 'genre_match' ? 'Genre match' : reason === 'fresh' ? 'New release' : reason === 'high_engagement' ? 'Trending' : reason;
        h += '<span class="news-reason">' + esc(label) + '</span>';
      }
      h += '</div>';
    }
    // Artists
    if (item.parsed_artists && item.parsed_artists.length) {
      h += '<div class="text-sm text-muted" style="margin-top:4px">';
      for (var a = 0; a < item.parsed_artists.length; a++) {
        if (a > 0) h += ', ';
        h += esc(item.parsed_artists[a]);
      }
      h += '</div>';
    }
    h += '<div class="news-card-footer">';
    h += '<span class="text-xs text-muted">' + esc(item.domain) + '</span>';
    h += '<span class="news-score text-xs">relevance: ' + item.relevance_score + '</span>';
    if (item.url && item.url !== item.reddit_url) {
      h += ' <a href="' + esc(item.url) + '" target="_blank" rel="noopener" class="btn btn-sm" style="padding:2px 8px;font-size:0.6875rem">Link</a>';
    }
    h += '</div>';
    h += '</div>';
  }
  h += '</div>';
  setAppContent(h);
}

// =========================================================================
// Radio View
// =========================================================================
var radioState = { sessionId: null, seedType: 'track', tracks: [], totalServed: 0, seedDisplayName: '' };

function loadRadio() {
  var h = '<div class="page-header"><h1 class="page-title">Radio</h1></div>';
  h += '<div class="grid-2">';

  // Start panel
  h += '<div class="card"><div class="card-header">Start Radio</div><div class="card-body">';
  h += '<div style="display:flex;flex-direction:column;gap:var(--space-3)">';
  h += '<div style="display:flex;gap:var(--space-2);align-items:center">';
  h += '<select id="radio-user" class="form-select" style="flex:1"><option value="">Select user...</option>';
  for (var i = 0; i < cachedUsers.length; i++) h += '<option value="' + esc(cachedUsers[i].user_id) + '">' + esc(cachedUsers[i].user_id) + '</option>';
  h += '</select></div>';
  h += '<div style="display:flex;gap:var(--space-2);align-items:center">';
  h += '<label class="text-sm" style="min-width:70px">Seed type:</label>';
  h += '<select id="radio-seed-type" class="form-select" style="flex:1" onchange="radioSeedTypeChanged()">';
  h += '<option value="track">Track</option><option value="artist">Artist</option><option value="playlist">Playlist</option>';
  h += '</select></div>';
  h += '<div id="radio-seed-input-wrap">';
  h += radioSeedInput('track');
  h += '</div>';
  h += '<div style="display:flex;gap:var(--space-2)">';
  h += '<button class="btn btn-primary" onclick="startRadio()" id="radio-start-btn">Start Radio</button>';
  h += '</div></div></div></div>';

  // Active sessions panel
  h += '<div class="card"><div class="card-header">Active Sessions</div><div class="card-body" id="radio-sessions-list"><div class="empty">Loading...</div></div></div>';
  h += '</div>';

  // Now playing / queue
  h += '<div id="radio-now-playing"></div>';
  setAppContent(h);

  // Load active sessions
  radioLoadSessions();
}

function radioSeedInput(type) {
  if (type === 'track') {
    return '<input id="radio-seed-value" class="form-input" placeholder="Track ID" style="width:100%">';
  } else if (type === 'artist') {
    return '<input id="radio-seed-value" class="form-input" placeholder="Artist name" style="width:100%">';
  } else {
    return '<select id="radio-seed-value" class="form-select" style="width:100%"><option value="">Loading playlists...</option></select>';
  }
}

function radioSeedTypeChanged() {
  var type = document.getElementById('radio-seed-type').value;
  var wrap = document.getElementById('radio-seed-input-wrap');
  wrap.innerHTML = radioSeedInput(type);
  if (type === 'playlist') {
    api('/v1/playlists?limit=50').then(function(data) {
      var playlists = data.playlists || data || [];
      var sel = document.getElementById('radio-seed-value');
      if (!sel) return;
      var h = '<option value="">Select playlist...</option>';
      for (var i = 0; i < playlists.length; i++) {
        h += '<option value="' + playlists[i].id + '">' + esc(playlists[i].name) + ' (' + playlists[i].track_count + ' tracks)</option>';
      }
      sel.innerHTML = h;
    });
  }
}

function radioLoadSessions() {
  api('/v1/radio').then(function(data) {
    var el = document.getElementById('radio-sessions-list');
    if (!el) return;
    var sessions = data.sessions || [];
    if (!sessions.length) { el.innerHTML = '<div class="empty">No active radio sessions.</div>'; return; }
    var h = '';
    for (var i = 0; i < sessions.length; i++) {
      var s = sessions[i];
      h += '<div style="display:flex;align-items:center;justify-content:space-between;padding:var(--space-2) 0;border-bottom:1px solid var(--border)">';
      h += '<div><strong>' + esc(s.seed_display_name || s.seed_value) + '</strong>';
      h += ' <span class="badge badge-primary">' + esc(s.seed_type) + '</span>';
      h += '<br><span class="text-xs text-muted">' + esc(s.user_id) + ' &middot; ' + s.total_served + ' tracks served &middot; ' + timeAgo(s.last_active) + '</span></div>';
      h += '<div style="display:flex;gap:var(--space-2)">';
      h += '<button class="btn btn-primary btn-sm" onclick="radioResume(\'' + esc(s.session_id) + '\')">Resume</button>';
      h += '<button class="btn btn-danger btn-sm" onclick="radioStop(\'' + esc(s.session_id) + '\')">Stop</button>';
      h += '</div></div>';
    }
    el.innerHTML = h;
  }).catch(function(e) {
    var el = document.getElementById('radio-sessions-list');
    if (el) el.innerHTML = '<div class="empty text-danger">Error: ' + esc(e.message) + '</div>';
  });
}

function startRadio() {
  var userId = document.getElementById('radio-user').value;
  if (!userId) { alert('Select a user first.'); return; }
  var seedType = document.getElementById('radio-seed-type').value;
  var seedValue = document.getElementById('radio-seed-value').value.trim();
  if (!seedValue) { alert('Enter a seed value.'); return; }
  var btn = document.getElementById('radio-start-btn');
  btn.disabled = true; btn.textContent = 'Starting...';
  apiPost('/v1/radio/start', {
    user_id: userId, seed_type: seedType, seed_value: seedValue, count: 10
  }).then(function(data) {
    radioState.sessionId = data.session_id;
    radioState.seedType = data.seed_type;
    radioState.tracks = data.tracks || [];
    radioState.totalServed = data.tracks ? data.tracks.length : 0;
    radioState.seedDisplayName = data.seed_display_name || data.seed_value;
    renderRadioNowPlaying();
    radioLoadSessions();
    btn.disabled = false; btn.textContent = 'Start Radio';
  }).catch(function(e) {
    alert('Failed to start radio: ' + e.message);
    btn.disabled = false; btn.textContent = 'Start Radio';
  });
}

function radioResume(sessionId) {
  radioState.sessionId = sessionId;
  radioFetchNext(10);
}

function radioFetchNext(count) {
  if (!radioState.sessionId) return;
  var el = document.getElementById('radio-now-playing');
  if (el) {
    var loading = document.getElementById('radio-loading');
    if (loading) loading.style.display = 'block';
  }
  api('/v1/radio/' + radioState.sessionId + '/next?count=' + (count || 10)).then(function(data) {
    radioState.tracks = data.tracks || [];
    radioState.totalServed = data.total_served || 0;
    renderRadioNowPlaying();
  }).catch(function(e) {
    if (e.message.indexOf('404') >= 0) {
      radioState.sessionId = null;
      var np = document.getElementById('radio-now-playing');
      if (np) np.innerHTML = '<div class="card" style="margin-top:var(--space-4)"><div class="card-body"><div class="empty">Radio session expired.</div></div></div>';
    } else {
      alert('Error fetching next tracks: ' + e.message);
    }
  });
}

function renderRadioNowPlaying() {
  var el = document.getElementById('radio-now-playing');
  if (!el) return;
  var tracks = radioState.tracks;
  var h = '<div class="card" style="margin-top:var(--space-4)">';
  h += '<div class="card-header" style="display:flex;justify-content:space-between;align-items:center">';
  h += '<span>Now Playing: <strong>' + esc(radioState.seedDisplayName) + '</strong>';
  h += ' <span class="badge badge-primary">' + esc(radioState.seedType) + ' radio</span>';
  h += ' <span class="text-xs text-muted">' + radioState.totalServed + ' tracks served</span></span>';
  h += '<div style="display:flex;gap:var(--space-2)">';
  h += '<button class="btn btn-primary btn-sm" onclick="radioFetchNext(10)">Next 10</button>';
  h += '<button class="btn btn-secondary btn-sm" onclick="radioFetchNext(25)">Next 25</button>';
  h += '<button class="btn btn-danger btn-sm" onclick="radioStop(\'' + esc(radioState.sessionId) + '\')">Stop</button>';
  h += '</div></div>';

  if (!tracks.length) {
    h += '<div class="card-body"><div class="empty">No more tracks available. Try adjusting the seed.</div></div></div>';
    el.innerHTML = h; return;
  }

  var maxScore = 0;
  for (var i = 0; i < tracks.length; i++) { if (tracks[i].score > maxScore) maxScore = tracks[i].score; }

  h += '<div class="card-body" style="overflow-x:auto;padding:0"><table><tr><th>#</th><th>Track</th><th>Artist</th><th>Source</th><th>Score</th><th>BPM</th><th>Key</th><th>Energy</th><th>Mood</th><th>Duration</th><th style="min-width:140px">Feedback</th></tr>';
  for (var i = 0; i < tracks.length; i++) {
    var t = tracks[i];
    h += '<tr id="radio-row-' + esc(t.track_id) + '">';
    h += '<td>' + (t.position + 1) + '</td>';
    h += '<td class="truncate" title="' + esc(t.track_id) + '">' + esc(t.title || t.track_id) + '</td>';
    h += '<td class="truncate" style="max-width:150px">' + esc(t.artist || '\u2014') + '</td>';
    h += '<td>' + radioSourceBadge(t.source) + '</td>';
    h += '<td class="nowrap">' + scoreBar(t.score, maxScore) + '</td>';
    h += '<td>' + (t.bpm ? t.bpm.toFixed(1) : '\u2014') + '</td>';
    h += '<td>' + esc(t.key || '\u2014') + ' ' + (t.mode ? t.mode.charAt(0) : '') + '</td>';
    h += '<td>' + (t.energy != null ? t.energy.toFixed(2) : '\u2014') + '</td>';
    h += '<td>' + topMood(t.mood_tags) + '</td>';
    h += '<td>' + fmtTrackDur(t.duration) + '</td>';
    h += '<td>';
    h += '<button class="btn btn-sm" style="background:var(--color-success);color:#fff;padding:2px 8px;font-size:11px" onclick="radioFeedback(\'' + esc(t.track_id) + '\',\'like\',this)" title="Like">&#9829;</button> ';
    h += '<button class="btn btn-sm" style="background:var(--color-warning);color:#fff;padding:2px 8px;font-size:11px" onclick="radioFeedback(\'' + esc(t.track_id) + '\',\'skip\',this)" title="Skip">&#9654;</button> ';
    h += '<button class="btn btn-sm" style="background:var(--color-danger);color:#fff;padding:2px 8px;font-size:11px" onclick="radioFeedback(\'' + esc(t.track_id) + '\',\'dislike\',this)" title="Dislike">&#10005;</button>';
    h += '</td></tr>';
  }
  h += '</table></div>';

  // Source distribution
  var srcCounts = {};
  for (var i = 0; i < tracks.length; i++) { var s = tracks[i].source || 'unknown'; srcCounts[s] = (srcCounts[s] || 0) + 1; }
  h += '<div style="padding:var(--space-3) var(--space-5);border-top:1px solid var(--border)">';
  h += '<span class="text-xs text-muted font-semibold">Sources: </span>';
  var srcKeys = Object.keys(srcCounts);
  for (var i = 0; i < srcKeys.length; i++) {
    h += radioSourceBadge(srcKeys[i]) + ' <span class="text-xs text-muted">' + srcCounts[srcKeys[i]] + '</span> ';
  }
  h += '</div>';

  h += '<div id="radio-loading" style="display:none;padding:var(--space-3);text-align:center"><span class="text-muted">Loading next batch...</span></div>';
  h += '</div>';
  el.innerHTML = h;
}

function radioSourceBadge(s) {
  if (!s) return '<span class="badge">unknown</span>';
  var cls = 'badge ';
  if (s.indexOf('radio_drift') === 0) cls += 'badge-primary';
  else if (s.indexOf('radio_seed') === 0) cls += 'source-content';
  else if (s.indexOf('radio_content') === 0) cls += 'source-content';
  else if (s.indexOf('radio_skipgram') === 0) cls += 'badge-info';
  else if (s.indexOf('radio_lastfm') === 0) cls += 'badge-purple';
  else if (s.indexOf('radio_cf') === 0) cls += 'source-cf';
  else if (s.indexOf('radio_artist') === 0) cls += 'source-artist';
  else cls += 'badge-primary';
  // Shorten label for display
  var label = s.replace('radio_', '');
  return '<span class="' + cls + '">' + esc(label) + '</span>';
}

function radioFeedback(trackId, action, btn) {
  if (!radioState.sessionId) return;
  var userId = document.getElementById('radio-user') ? document.getElementById('radio-user').value : '';
  if (!userId) return;
  btn.disabled = true;
  // Send feedback through normal event ingestion with context_type=radio
  apiPost('/v1/events', {
    user_id: userId, track_id: trackId, event_type: action,
    context_type: 'radio', context_id: radioState.sessionId
  }).then(function() {
    var row = document.getElementById('radio-row-' + trackId);
    if (row) {
      if (action === 'like') row.style.background = 'rgba(16,185,129,0.08)';
      else if (action === 'dislike') row.style.background = 'rgba(239,68,68,0.08)';
      else if (action === 'skip') row.style.background = 'rgba(245,158,11,0.08)';
    }
  }).catch(function(e) {
    btn.disabled = false;
  });
}

function radioStop(sessionId) {
  apiDelete('/v1/radio/' + sessionId).then(function() {
    if (radioState.sessionId === sessionId) {
      radioState.sessionId = null;
      var np = document.getElementById('radio-now-playing');
      if (np) np.innerHTML = '';
    }
    radioLoadSessions();
  }).catch(function(e) {
    alert('Error stopping radio: ' + e.message);
  });
}

// =========================================================================
// Connections Tab
// =========================================================================

var connectionsRefreshTimer = null;

function loadConnections() {
  $('#app').innerHTML = '<div class="page-header"><h1 class="page-title">Connections</h1><div class="page-actions"><button class="btn btn-primary btn-sm" onclick="loadConnections()">Refresh</button></div></div><div id="conn-grid" class="conn-grid"><div class="empty">Checking integrations\u2026</div></div>';
  api('/v1/integrations/status').then(function(data) {
    renderConnections(data);
  }).catch(function(e) {
    document.getElementById('conn-grid').innerHTML = '<div class="empty text-danger">Failed to check integrations: ' + esc(e.message) + '</div>';
  });
}

function renderConnections(data) {
  var integrations = data.integrations;
  var order = [
    { key: 'media_server', label: 'Media Server', icon: '\uD83C\uDFB5', desc: 'Navidrome or Plex — source of track IDs and library metadata' },
    { key: 'lidarr', label: 'Lidarr', icon: '\uD83D\uDCE5', desc: 'Automatic music discovery and download management' },
    { key: 'spotdl_api', label: 'spotdl-api', icon: '\u2B07', desc: 'YouTube Music downloads matched via Spotify metadata' },
    { key: 'slskd', label: 'Soulseek (slskd)', icon: '\uD83D\uDD17', desc: 'Peer-to-peer music downloads via Soulseek network' },
    { key: 'lastfm', label: 'Last.fm', icon: '\uD83C\uDFB6', desc: 'Scrobbling, taste enrichment, similar tracks, charts' },
    { key: 'acousticbrainz_lookup', label: 'AcousticBrainz Lookup', icon: '\uD83E\uDDE0', desc: 'Audio-feature similarity search across 29.5M tracks' }
  ];

  var h = '';
  for (var i = 0; i < order.length; i++) {
    var o = order[i];
    var s = integrations[o.key] || {};
    var configured = s.configured;
    var connected = s.connected;

    var statusClass, statusLabel;
    if (!configured) {
      statusClass = 'conn-not-configured';
      statusLabel = 'Not configured';
    } else if (connected) {
      statusClass = 'conn-connected';
      statusLabel = 'Connected';
    } else {
      statusClass = 'conn-error';
      statusLabel = 'Error';
    }

    h += '<div class="conn-card ' + statusClass + '">';
    h += '<div class="conn-card-header">';
    h += '<span class="conn-icon">' + o.icon + '</span>';
    h += '<div class="conn-title-group"><span class="conn-title">' + esc(o.label) + '</span>';
    if (s.type) h += '<span class="conn-type">' + esc(s.type) + '</span>';
    if (s.version) h += '<span class="conn-version">v' + esc(String(s.version)) + '</span>';
    h += '</div>';
    h += '<span class="conn-status-badge ' + statusClass + '">' + statusLabel + '</span>';
    h += '</div>';

    h += '<div class="conn-desc">' + o.desc + '</div>';

    if (configured && s.url) {
      h += '<div class="conn-detail"><span class="conn-detail-label">URL</span><span class="conn-detail-value font-mono">' + esc(s.url) + '</span></div>';
    }

    if (s.scrobbling !== undefined) {
      h += '<div class="conn-detail"><span class="conn-detail-label">Scrobbling</span><span class="conn-detail-value">' + (s.scrobbling ? '<span class="text-success">Enabled</span>' : '<span class="text-muted">Disabled</span>') + '</span></div>';
    }

    if (s.status) {
      h += '<div class="conn-detail"><span class="conn-detail-label">Status</span><span class="conn-detail-value">' + esc(s.status) + '</span></div>';
    }

    if (s.details) {
      var keys = Object.keys(s.details);
      for (var j = 0; j < keys.length; j++) {
        var dk = keys[j], dv = s.details[dk];
        if (dv === null || dv === undefined) continue;
        var label = dk.replace(/_/g, ' ').replace(/([A-Z])/g, ' $1').trim();
        label = label.charAt(0).toUpperCase() + label.slice(1);
        h += '<div class="conn-detail"><span class="conn-detail-label">' + esc(label) + '</span><span class="conn-detail-value font-mono">' + esc(String(dv)) + '</span></div>';
      }
    }

    if (s.error) {
      h += '<div class="conn-error-msg">' + esc(s.error) + '</div>';
    }

    if (!configured) {
      h += '<div class="conn-hint">Set the required environment variables in your <code>.env</code> file to enable this integration.</div>';
    }

    h += '</div>';
  }

  var grid = document.getElementById('conn-grid');
  if (grid) grid.innerHTML = h;
}

// =========================================================================
// Algorithm Config Tab (Phase B+C)
// =========================================================================
var algoDefaults = null;
var algoCurrent = null;
var algoEdited = null;
var algoHistory = null;

function loadAlgorithm() {
  $('#app').innerHTML = '<div class="empty">Loading algorithm config\u2026</div>';
  Promise.all([
    api('/v1/algorithm/config/defaults'),
    api('/v1/algorithm/config'),
    api('/v1/algorithm/config/history?limit=50')
  ]).then(function(results) {
    algoDefaults = results[0];
    algoCurrent = results[1];
    algoEdited = JSON.parse(JSON.stringify(algoCurrent.config));
    algoHistory = results[2];
    renderAlgorithm();
  }).catch(function(e) {
    $('#app').innerHTML = '<div class="empty">Failed to load config: ' + esc(e.message) + '</div>';
  });
}

var ALGO_FIELD_META = {
  track_scoring: {
    w_full_listen: { desc: "Weight for a full listen (completion >= 0.8 or dwell >= 30s)", min: -10, max: 10, step: 0.1 },
    w_mid_listen: { desc: "Weight for a mid-length listen (2s-30s dwell)", min: -10, max: 10, step: 0.1 },
    w_early_skip: { desc: "Default weight for an early skip (<2s dwell)", min: -10, max: 10, step: 0.1 },
    w_early_skip_playlist: { desc: "Early skip weight in playlist/album context", min: -10, max: 10, step: 0.1 },
    w_early_skip_radio: { desc: "Early skip weight in radio/search context", min: -10, max: 10, step: 0.1 },
    w_like: { desc: "Weight for an explicit like", min: -10, max: 10, step: 0.1 },
    w_dislike: { desc: "Weight for an explicit dislike", min: -10, max: 10, step: 0.1 },
    w_repeat: { desc: "Weight for a repeat action", min: -10, max: 10, step: 0.1 },
    w_playlist_add: { desc: "Weight for adding track to a playlist", min: -10, max: 10, step: 0.1 },
    w_queue_add: { desc: "Weight for adding track to the queue", min: -10, max: 10, step: 0.1 },
    w_heavy_seek: { desc: "Penalty per excess seek above threshold", min: -10, max: 10, step: 0.1 },
    early_skip_ms: { desc: "Milliseconds threshold for early skip classification", min: 100, max: 30000, step: 100, integer: true },
    mid_skip_ms: { desc: "Milliseconds threshold for mid-skip classification", min: 1000, max: 120000, step: 500, integer: true },
    heavy_seek_threshold: { desc: "Seeks per play above which heavy-seek penalty applies", min: 1, max: 20, step: 1, integer: true }
  },
  reranker: {
    artist_diversity_top_n: { desc: "Number of top positions to enforce artist diversity in", min: 1, max: 100, step: 1, integer: true },
    artist_max_per_top: { desc: "Max tracks from same artist in top N", min: 1, max: 20, step: 1, integer: true },
    repeat_window_hours: { desc: "Hours to suppress recently played tracks", min: 0, max: 168, step: 0.5 },
    freshness_boost: { desc: "Score multiplier boost for never-played tracks", min: 0, max: 1, step: 0.01 },
    skip_threshold: { desc: "Early skip count above which skip suppression activates", min: 1, max: 50, step: 1, integer: true },
    skip_demote_factor: { desc: "Score multiplier for skip-suppressed tracks", min: 0, max: 1, step: 0.05 },
    exploration_fraction: { desc: "Fraction of slots for under-explored tracks", min: 0, max: 0.5, step: 0.01 },
    exploration_low_plays: { desc: "Play count below which track is under-explored", min: 1, max: 50, step: 1, integer: true },
    exploration_noise_scale: { desc: "Noise magnitude for exploration scoring", min: 0, max: 2, step: 0.05 },
    min_duration_car: { desc: "Min track duration (seconds) in car/speaker mode", min: 0, max: 600, step: 5 }
  },
  candidate_sources: {
    content: { desc: "FAISS content-based similarity (from seed track)", min: 0, max: 5, step: 0.1 },
    content_profile: { desc: "FAISS similarity from user taste centroid", min: 0, max: 5, step: 0.1 },
    cf: { desc: "Collaborative filtering", min: 0, max: 5, step: 0.1 },
    session_skipgram: { desc: "Session skip-gram behavioural co-occurrence", min: 0, max: 5, step: 0.1 },
    lastfm_similar: { desc: "Last.fm similar tracks (external CF)", min: 0, max: 5, step: 0.1 },
    sasrec: { desc: "SASRec transformer next-track prediction", min: 0, max: 5, step: 0.1 },
    popular: { desc: "Global popularity fallback", min: 0, max: 5, step: 0.1 },
    artist_recall: { desc: "Recently heard artist tracks", min: 0, max: 5, step: 0.1 }
  },
  taste_profile: {
    timescale_short_days: { desc: "Short-term taste window (days)", min: 1, max: 90, step: 1 },
    timescale_long_days: { desc: "Long-term taste window (days)", min: 30, max: 3650, step: 10 },
    top_tracks_limit: { desc: "Top tracks in taste profile", min: 10, max: 500, step: 10, integer: true },
    lastfm_decay_interactions: { desc: "Interactions at which Last.fm weight reaches ~37%", min: 10, max: 1000, step: 10 },
    onboarding_decay_interactions: { desc: "Interactions at which onboarding weight reaches ~37%", min: 10, max: 500, step: 10 },
    enrichment_min_weight: { desc: "Min weight below which enrichment is skipped", min: 0.001, max: 0.5, step: 0.005 }
  },
  ranker: {
    n_estimators: { desc: "Number of boosting rounds (trees) [RETRAIN]", min: 10, max: 2000, step: 10, integer: true },
    max_depth: { desc: "Maximum tree depth [RETRAIN]", min: 2, max: 20, step: 1, integer: true },
    learning_rate: { desc: "Boosting learning rate [RETRAIN]", min: 0.001, max: 1.0, step: 0.005 },
    num_leaves: { desc: "Max leaves per tree [RETRAIN]", min: 4, max: 256, step: 1, integer: true },
    min_child_samples: { desc: "Min samples per leaf [RETRAIN]", min: 1, max: 100, step: 1, integer: true },
    subsample: { desc: "Row subsampling ratio [RETRAIN]", min: 0.1, max: 1.0, step: 0.05 },
    colsample_bytree: { desc: "Column subsampling ratio [RETRAIN]", min: 0.1, max: 1.0, step: 0.05 },
    reg_alpha: { desc: "L1 regularisation [RETRAIN]", min: 0, max: 10, step: 0.1 },
    reg_lambda: { desc: "L2 regularisation [RETRAIN]", min: 0, max: 10, step: 0.1 },
    min_training_samples: { desc: "Min samples required to train [RETRAIN]", min: 5, max: 1000, step: 5, integer: true },
    weight_disliked: { desc: "Sample weight for disliked tracks", min: 1, max: 10, step: 0.1 },
    weight_heavy_skip: { desc: "Sample weight for heavily skipped tracks", min: 1, max: 10, step: 0.1 },
    weight_strong_positive: { desc: "Sample weight for liked/repeated tracks", min: 1, max: 10, step: 0.1 },
    weight_impression_negative: { desc: "Sample weight for shown-but-not-played tracks", min: 1, max: 10, step: 0.1 }
  },
  radio: {
    seed_weight: { desc: "How much the seed anchor influences drift embedding", min: 0, max: 1, step: 0.05 },
    feedback_weight: { desc: "How much feedback shifts drift embedding", min: 0, max: 1, step: 0.05 },
    profile_weight: { desc: "How much user global taste contributes", min: 0, max: 1, step: 0.05 },
    source_drift: { desc: "Score multiplier for drift-FAISS candidates", min: 0, max: 5, step: 0.1 },
    source_seed: { desc: "Score multiplier for seed-FAISS candidates", min: 0, max: 5, step: 0.1 },
    source_content: { desc: "Score multiplier for content similarity candidates", min: 0, max: 5, step: 0.1 },
    source_skipgram: { desc: "Score multiplier for session skip-gram candidates", min: 0, max: 5, step: 0.1 },
    source_lastfm: { desc: "Score multiplier for Last.fm similar candidates", min: 0, max: 5, step: 0.1 },
    source_cf: { desc: "Score multiplier for CF candidates", min: 0, max: 5, step: 0.1 },
    source_artist: { desc: "Score multiplier for same-artist candidates", min: 0, max: 5, step: 0.1 },
    feedback_like_weight: { desc: "Attraction weight when user likes a track", min: 0, max: 5, step: 0.1 },
    feedback_dislike_weight: { desc: "Repulsion weight when user dislikes", min: 0, max: 5, step: 0.1 },
    feedback_skip_weight: { desc: "Mild repulsion weight on skip", min: 0, max: 5, step: 0.1 },
    feedback_decay: { desc: "Exponential decay for older feedback", min: 0.1, max: 1, step: 0.05 },
    session_ttl_hours: { desc: "Hours of inactivity before session expires", min: 0.5, max: 24, step: 0.5 },
    max_sessions: { desc: "Maximum concurrent radio sessions", min: 1, max: 500, step: 1, integer: true }
  },
  session_embeddings: {
    embedding_dim: { desc: "Embedding vector dimensionality [RETRAIN]", min: 16, max: 512, step: 16, integer: true },
    window_size: { desc: "Context window size (tracks before/after) [RETRAIN]", min: 1, max: 20, step: 1, integer: true },
    min_count: { desc: "Ignore tracks appearing fewer times [RETRAIN]", min: 1, max: 50, step: 1, integer: true },
    epochs: { desc: "Training iterations [RETRAIN]", min: 1, max: 100, step: 1, integer: true },
    min_sessions: { desc: "Minimum sessions required to train", min: 1, max: 500, step: 1, integer: true },
    min_vocab: { desc: "Minimum unique tracks required to train", min: 2, max: 100, step: 1, integer: true }
  }
};

function algoGetFieldMeta(groupKey, fieldKey) {
  var group = ALGO_FIELD_META[groupKey];
  if (group && group[fieldKey]) return group[fieldKey];
  var defVal = algoDefaults.config[groupKey][fieldKey];
  if (typeof defVal === 'number' && Number.isInteger(defVal))
    return { desc: fieldKey.replace(/_/g, ' '), min: 0, max: defVal * 10 || 100, step: 1, integer: true };
  return { desc: fieldKey.replace(/_/g, ' '), min: 0, max: 10, step: 0.1 };
}

function algoHasChanges() {
  return JSON.stringify(algoEdited) !== JSON.stringify(algoCurrent.config);
}

function algoHasRetrainChanges() {
  if (!algoDefaults) return false;
  var groups = algoDefaults.groups;
  for (var i = 0; i < groups.length; i++) {
    if (!groups[i].retrain_required) continue;
    if (JSON.stringify(algoEdited[groups[i].key]) !== JSON.stringify(algoCurrent.config[groups[i].key])) return true;
  }
  return false;
}

function renderAlgorithm() {
  var groups = algoDefaults.groups;
  var hasChanges = algoHasChanges();
  var retrainWarning = hasChanges && algoHasRetrainChanges();

  var h = '<div class="page-header"><div>';
  h += '<h2 class="page-title">Algorithm Configuration</h2>';
  h += '<span class="subtitle">Version ' + algoCurrent.version + (algoCurrent.name ? ' \u2014 ' + esc(algoCurrent.name) : '') + '</span>';
  h += '</div><div class="page-actions">';
  h += '<button class="btn btn-secondary btn-sm" onclick="algoExport()">Export</button>';
  h += '<button class="btn btn-secondary btn-sm" onclick="algoShowImport()">Import</button>';
  h += '<button class="btn btn-secondary btn-sm" onclick="algoShowHistory()">History</button>';
  h += '<button class="btn btn-secondary btn-sm" onclick="algoShowDiff()">Diff</button>';
  h += '<span class="divider-v"></span>';
  h += '<button class="btn btn-secondary btn-sm" style="color:var(--color-warning)" onclick="algoResetToDefaults()">Reset to Defaults</button>';
  if (hasChanges) h += '<button class="btn btn-secondary btn-sm" onclick="algoDiscardChanges()">Discard</button>';
  h += '<button class="btn btn-primary btn-sm"' + (hasChanges ? '' : ' disabled') + ' onclick="algoSaveAndApply()">Save & Apply</button>';
  h += '</div></div>';

  if (retrainWarning) {
    h += '<div class="alert alert-warning" style="margin-bottom:var(--space-4)">';
    h += '\u26A0 Changes include parameters that trigger a full model retrain (Ranking Model and/or Session Embeddings).';
    h += '</div>';
  }

  for (var g = 0; g < groups.length; g++) {
    var group = groups[g];
    var gk = group.key;
    var groupChanged = JSON.stringify(algoEdited[gk]) !== JSON.stringify(algoCurrent.config[gk]);

    h += '<div class="card" style="margin-bottom:var(--space-3)">';
    h += '<div class="card-header algo-group-header" onclick="algoToggleGroup(\'' + gk + '\')">';
    h += '<div style="display:flex;align-items:center;gap:var(--space-2)">';
    h += '<span class="algo-chevron" id="algo-chev-' + gk + '">\u25B6</span>';
    h += '<span>' + esc(group.label) + '</span>';
    if (group.retrain_required) h += ' <span class="badge badge-warning" style="font-size:10px">RETRAIN</span>';
    if (groupChanged) h += ' <span class="badge badge-primary" style="font-size:10px">MODIFIED</span>';
    h += '</div>';
    h += '<span class="text-xs text-muted" style="font-weight:400">' + esc(group.description) + '</span>';
    h += '</div>';

    h += '<div class="card-body algo-group-body" id="algo-body-' + gk + '" style="display:none;padding:var(--space-4)">';
    h += '<div class="algo-fields-grid">';
    var defaults = algoDefaults.config[gk];
    for (var fk in defaults) {
      if (!defaults.hasOwnProperty(fk)) continue;
      var meta = algoGetFieldMeta(gk, fk);
      var val = algoEdited[gk][fk];
      var defVal = defaults[fk];
      var changed = val !== algoCurrent.config[gk][fk];
      var isRetrain = meta.desc.indexOf('[RETRAIN]') >= 0;

      h += '<div class="algo-field' + (changed ? ' algo-field-changed' : '') + '">';
      h += '<div class="algo-field-header">';
      h += '<label class="algo-field-label">' + esc(fk.replace(/_/g, ' ')) + '</label>';
      if (isRetrain) h += '<span class="badge badge-warning" style="font-size:9px;padding:1px 4px">RETRAIN</span>';
      h += '</div>';
      h += '<div class="algo-field-controls">';
      h += '<input type="range" class="algo-slider" min="' + meta.min + '" max="' + meta.max + '" step="' + meta.step + '" value="' + val + '" data-gk="' + gk + '" data-fk="' + fk + '" data-int="' + (meta.integer ? '1' : '0') + '">';
      h += '<div class="algo-num-wrap">';
      h += '<button type="button" class="algo-spin algo-spin-down" data-gk="' + gk + '" data-fk="' + fk + '" data-dir="-1">&minus;</button>';
      h += '<input type="number" class="algo-num" min="' + meta.min + '" max="' + meta.max + '" step="' + meta.step + '" value="' + val + '" data-gk="' + gk + '" data-fk="' + fk + '" data-int="' + (meta.integer ? '1' : '0') + '">';
      h += '<button type="button" class="algo-spin algo-spin-up" data-gk="' + gk + '" data-fk="' + fk + '" data-dir="1">+</button>';
      h += '</div>';
      h += '</div>';
      h += '<div class="algo-field-info">';
      h += '<span class="text-muted">' + esc(meta.desc.replace(' [RETRAIN]', '')) + '</span>';
      if (val !== defVal) h += ' <span style="color:var(--color-primary)">(default: ' + defVal + ')</span>';
      h += '</div></div>';
    }
    h += '</div></div></div>';
  }

  $('#app').innerHTML = h;

  // Attach input handlers via delegation
  document.querySelectorAll('.algo-slider').forEach(function(sl) {
    sl.addEventListener('input', function() {
      var gk = this.dataset.gk, fk = this.dataset.fk, isInt = this.dataset.int === '1';
      var v = isInt ? parseInt(this.value, 10) : parseFloat(this.value);
      algoEdited[gk][fk] = v;
      var num = this.parentElement.querySelector('.algo-num');
      if (num) num.value = v;
    });
  });
  document.querySelectorAll('.algo-num').forEach(function(num) {
    num.addEventListener('change', function() {
      var gk = this.dataset.gk, fk = this.dataset.fk, isInt = this.dataset.int === '1';
      var meta = algoGetFieldMeta(gk, fk);
      var v = isInt ? parseInt(this.value, 10) : parseFloat(this.value);
      if (isNaN(v)) return;
      if (v < meta.min) v = meta.min;
      if (v > meta.max) v = meta.max;
      algoEdited[gk][fk] = v;
      renderAlgorithm();
    });
  });
  document.querySelectorAll('.algo-spin').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var gk = this.dataset.gk, fk = this.dataset.fk, dir = parseInt(this.dataset.dir, 10);
      var meta = algoGetFieldMeta(gk, fk);
      var isInt = meta.integer;
      var cur = algoEdited[gk][fk];
      var v = cur + dir * meta.step;
      v = isInt ? Math.round(v) : parseFloat(v.toPrecision(10));
      if (v < meta.min) v = meta.min;
      if (v > meta.max) v = meta.max;
      algoEdited[gk][fk] = v;
      renderAlgorithm();
    });
  });
}

function algoToggleGroup(gk) {
  var body = document.getElementById('algo-body-' + gk);
  var chev = document.getElementById('algo-chev-' + gk);
  if (!body) return;
  var open = body.style.display !== 'none';
  body.style.display = open ? 'none' : 'block';
  if (chev) chev.textContent = open ? '\u25B6' : '\u25BC';
}

function algoSaveAndApply() {
  var retrainWarn = algoHasRetrainChanges();
  var msg = 'Save configuration as new version and run the pipeline?';
  if (retrainWarn) msg = 'This will trigger a full model retrain. Save and run pipeline?';
  if (!confirm(msg)) return;
  var name = prompt('Version name (optional):', '');
  apiPut('/v1/algorithm/config', { name: name || null, config: algoEdited }).then(function(saved) {
    algoCurrent = saved;
    algoEdited = JSON.parse(JSON.stringify(saved.config));
    switchTab('pipeline');
    setTimeout(function() {
      apiPost('/v1/pipeline/reset').then(function() {
        pipelineConnectSSE();
        setTimeout(function() { pipelineRefreshStatus(); }, 500);
      }).catch(function(e) { alert('Pipeline run failed: ' + e.message); });
    }, 200);
  }).catch(function(e) { alert('Save failed: ' + e.message); });
}

function algoDiscardChanges() {
  algoEdited = JSON.parse(JSON.stringify(algoCurrent.config));
  renderAlgorithm();
}

function algoResetToDefaults() {
  if (!confirm('Reset all algorithm parameters to their default values? This creates a new config version.')) return;
  apiPost('/v1/algorithm/config/reset').then(function(saved) {
    algoCurrent = saved;
    algoEdited = JSON.parse(JSON.stringify(saved.config));
    renderAlgorithm();
  }).catch(function(e) { alert('Reset failed: ' + e.message); });
}

function algoExport() {
  fetch(BASE + '/v1/algorithm/config/export', { headers: headers() }).then(function(res) {
    if (!res.ok) throw new Error(res.statusText);
    return res.blob().then(function(blob) {
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'grooveiq-config-v' + algoCurrent.version + '.json';
      a.click();
      URL.revokeObjectURL(a.href);
    });
  }).catch(function(e) { alert('Export failed: ' + e.message); });
}

function algoShowImport() {
  var overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = '<div class="modal"><h2>Import Configuration</h2>'
    + '<div class="field"><label>JSON file</label>'
    + '<input type="file" id="algo-import-file" accept=".json"></div>'
    + '<div class="field"><label>Name (optional)</label>'
    + '<input type="text" id="algo-import-name" placeholder="e.g., Imported from backup"></div>'
    + '<div class="actions">'
    + '<button class="btn btn-secondary btn-sm" onclick="this.closest(\'.modal-overlay\').remove()">Cancel</button>'
    + '<button class="btn btn-primary btn-sm" onclick="algoDoImport()">Import</button>'
    + '</div></div>';
  document.body.appendChild(overlay);
}

function algoDoImport() {
  var fileInput = document.getElementById('algo-import-file');
  var nameInput = document.getElementById('algo-import-name');
  if (!fileInput || !fileInput.files.length) { alert('Select a JSON file'); return; }
  var reader = new FileReader();
  reader.onload = function(e) {
    try {
      var data = JSON.parse(e.target.result);
      var config = data.config || data;
      var name = (nameInput && nameInput.value) || data.name || 'Imported';
      apiPost('/v1/algorithm/config/import', { name: name, config: config }).then(function(saved) {
        var overlay = document.querySelector('.modal-overlay');
        if (overlay) overlay.remove();
        algoCurrent = saved;
        algoEdited = JSON.parse(JSON.stringify(saved.config));
        api('/v1/algorithm/config/history?limit=50').then(function(h) { algoHistory = h; });
        renderAlgorithm();
      }).catch(function(err) { alert('Import failed: ' + err.message); });
    } catch (ex) { alert('Invalid JSON: ' + ex.message); }
  };
  reader.readAsText(fileInput.files[0]);
}

function algoShowHistory() {
  var overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  var h = '<div class="modal" style="min-width:600px;max-width:700px;max-height:80vh;overflow:auto">';
  h += '<h2>Version History</h2>';
  h += '<table style="width:100%;font-size:13px"><tr><th>Version</th><th>Name</th><th>Active</th><th>Created</th><th>Actions</th></tr>';
  for (var i = 0; i < algoHistory.length; i++) {
    var v = algoHistory[i];
    h += '<tr><td>v' + v.version + '</td>';
    h += '<td>' + esc(v.name || '\u2014') + '</td>';
    h += '<td>' + (v.is_active ? '<span class="badge badge-success">active</span>' : '') + '</td>';
    h += '<td class="text-xs text-muted">' + fmtTime(v.created_at) + '</td>';
    h += '<td style="display:flex;gap:4px">';
    if (!v.is_active) h += '<button class="btn btn-secondary btn-sm" style="font-size:11px;padding:2px 8px" onclick="algoActivateVersion(' + v.version + ')">Activate</button>';
    h += '<button class="btn btn-secondary btn-sm" style="font-size:11px;padding:2px 8px" onclick="algoDiffVersion(' + v.version + ')">Diff</button>';
    h += '</td></tr>';
  }
  h += '</table>';
  h += '<div style="display:flex;justify-content:flex-end;margin-top:var(--space-4)"><button class="btn btn-secondary btn-sm" onclick="this.closest(\'.modal-overlay\').remove()">Close</button></div>';
  h += '</div>';
  overlay.innerHTML = h;
  document.body.appendChild(overlay);
}

function algoActivateVersion(version) {
  if (!confirm('Activate config v' + version + '? This rolls back to that version.')) return;
  apiPost('/v1/algorithm/config/activate/' + version).then(function(saved) {
    var overlay = document.querySelector('.modal-overlay');
    if (overlay) overlay.remove();
    algoCurrent = saved;
    algoEdited = JSON.parse(JSON.stringify(saved.config));
    api('/v1/algorithm/config/history?limit=50').then(function(h) { algoHistory = h; });
    renderAlgorithm();
  }).catch(function(e) { alert('Activate failed: ' + e.message); });
}

function algoShowDiff() {
  algoDiffAgainst(algoCurrent.config, 'current (v' + algoCurrent.version + ')');
}

function algoDiffVersion(version) {
  api('/v1/algorithm/config/' + version).then(function(ver) {
    var overlay = document.querySelector('.modal-overlay');
    if (overlay) overlay.remove();
    algoDiffAgainst(ver.config, 'v' + ver.version + (ver.name ? ' (' + ver.name + ')' : ''));
  }).catch(function(e) { alert('Failed: ' + e.message); });
}

function algoDiffAgainst(compareConfig, compareLabel) {
  var groups = algoDefaults.groups;
  var overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  var h = '<div class="modal" style="min-width:640px;max-width:720px;max-height:80vh;overflow:auto">';
  h += '<h2>Diff: Working Copy vs ' + esc(compareLabel) + '</h2>';
  var anyDiff = false;
  for (var g = 0; g < groups.length; g++) {
    var gk = groups[g].key, diffs = [];
    var edited = algoEdited[gk], compare = compareConfig[gk];
    for (var key in edited) { if (edited.hasOwnProperty(key) && edited[key] !== compare[key]) diffs.push({ key: key, from: compare[key], to: edited[key] }); }
    if (!diffs.length) continue;
    anyDiff = true;
    h += '<div style="margin-bottom:var(--space-3)"><div style="font-weight:600;font-size:13px;margin-bottom:4px">' + esc(groups[g].label) + '</div>';
    h += '<table style="width:100%;font-size:12px"><tr><th>Parameter</th><th>From</th><th>To</th></tr>';
    for (var d = 0; d < diffs.length; d++) {
      h += '<tr><td style="font-family:var(--font-mono)">' + esc(diffs[d].key) + '</td>';
      h += '<td style="color:var(--color-danger)">' + diffs[d].from + '</td>';
      h += '<td style="color:var(--color-success)">' + diffs[d].to + '</td></tr>';
    }
    h += '</table></div>';
  }
  if (!anyDiff) h += '<div class="text-muted" style="padding:var(--space-4) 0">No differences found.</div>';
  h += '<div style="display:flex;justify-content:flex-end;margin-top:var(--space-4)"><button class="btn btn-secondary btn-sm" onclick="this.closest(\'.modal-overlay\').remove()">Close</button></div>';
  h += '</div>';
  overlay.innerHTML = h;
  document.body.appendChild(overlay);
}

// =========================================================================
// Static element event listeners (avoid inline handlers in HTML)
// =========================================================================
document.getElementById('connect-btn').addEventListener('click', connect);

// =========================================================================
// Session Restore
// =========================================================================
var saved = sessionStorage.getItem('grooveiq_key');
if (saved) { $('#api-key').value = saved; connect(); }
