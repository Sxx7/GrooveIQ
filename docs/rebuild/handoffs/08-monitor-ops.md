# Hand-off: Session 08 — Monitor: Downloads + Lidarr Backfill + Discovery + Charts

## Status
- [x] Session goal achieved
- [x] Visual verification done (mocked-API harness in `preview_eval`; same approach as sessions 02–07 since the local machine has no FastAPI runtime)
- [x] No regressions in old `/dashboard` (zero modifications to `app/static/index.html`, `js/app.js`, `css/style.css`)
- [x] All 11 Monitor sub-pages render real content (no stub fallbacks remaining)
- [x] Activity-pill deep-links verified — every row resolves to a real renderer
- [x] Committed on `gui-rebuild` branch with message `rebuild: session 08 — monitor: downloads + backfill + discovery + charts`

## What landed

The four remaining Monitor surfaces are now real. The Monitor bucket is complete (11/11 sub-pages built across sessions 02 / 06 / 07 / 08).

- `#/monitor/downloads` — page header (eyebrow MONITOR, title "Downloads", right rail: 1h/24h/7d/30d telemetry-window toggle + "auto · 3s" pulsing indicator that toggles 3s in-flight polling on click); **In-flight panel** (LIVE-badged, polled every 3s, each row carries a state-coloured backend dot, uppercase typographic backend chip, "{artist} — {title}", determinate-or-shimmer progress bar, and `status · elapsed` sub-line — no cancel here per the brief; cancel lives in Actions); **Recent activity panel** (collapsible, last 10 completed + failed interleaved by `updated_at desc` with typographic status badges); **Per-backend telemetry panel** (table backed by `/v1/downloads/stats?days={n}` with the **one allowed colour-as-data exception** — green / amber / wine bars where the colour itself is the success-rate datum); **Recent ad-hoc requests panel** (last 10 rows from `/v1/downloads?limit=10` — timestamp · backend · track · status badge).

- `#/monitor/lidarr-backfill` — page header (eyebrow MONITOR, title "Lidarr Backfill Stats", right rail: lavender-pulsing "live" pill + mono "last checked · Ns ago"); **Cross-link rail** with two `relatedRail` jump pills (Settings: "Edit config" → `#/settings/lidarr-backfill`, Actions: "Manage queue" → `#/actions/discovery`); **6-stat grid** (Missing · Complete +24h delta · Failed +24h delta + percentage · Capacity %-used + "X/Y this hour" · ETA mono "~N.N d" or "~N h" · Status MONO with last-tick-ago sub); **Throughput chart** (vertical 7-day monochrome `--accent` bar chart, derived client-side from `/v1/lidarr-backfill/requests?limit=200` filtered to `status === 'complete'` and bucketed by UTC day); **Per-service success-rate panel** (qobuz / tidal / deezer / soundcloud, derived from `picked_service` field, same colour-coded success-rate bar exception as Downloads).

- `#/monitor/discovery` — page header (eyebrow MONITOR, title "Discovery"); **Internal sub-nav** (Lidarr Discovery / Fill Library / Soulseek Bulk — page-local state, NOT topbar tabs since the topbar is fixed to the bucket sub-page list); three modes:
  1. **Lidarr Discovery**: 4 stat tiles (Artists discovered · Sent to Lidarr · Pending · Today / Daily limit) + history table (artist + mbid · source chip · seed · similarity bar · status badge · when). Falls through to a "Not configured. Set ENV…" panel when `stats.enabled === false`.
  2. **Fill Library**: 4 stat tiles (Albums queued · Total processed · Avg match · Today / Max per run) + history table (artist · album · matched_tracks · match bar with avg + best · status · when). Same not-configured fallback.
  3. **Soulseek Bulk**: 6 stat tiles (Tracks found · Queued · Searched · Skipped · Failed · Elapsed) + LIVE-badged progress panel (artists progress bar + currently-processing line) + collapsible Recent errors panel (last 20 newest-first). Polls every 3s while the Soulseek tab is selected; falls back to 30s on the other two.

- `#/monitor/charts` — page header (eyebrow MONITOR, title "Charts Stats"); **Build status banner** with three tiers (`good` lavender — fresh + auto-rebuild ON, `warn` amber — aging or auto-rebuild OFF, `bad` wine — stale or never-built; the "set CHARTS_ENABLED=true in your .env" hint is appended whenever `auto_rebuild_enabled === false`); **6-stat row** (Last build relative-time + UTC sub · Total charts · Total entries · Library matches · Match rate % · Unmatched count); **Per-scope breakdown panel** that groups `/v1/charts` responses into GLOBAL / TAGS / COUNTRIES sections, listing each chart with its type chip + entry count + fetched-ago timestamp.

A small set of new shared helpers was added inside the monitor.js IIFE (private to this bucket but reused across all four pages):
- `backendDot(bucket)` — neutral dot whose colour is decided by `bucket` (success-track = `--accent`, in-flight = `--ink-3`, failure-track = `--wine`), never per-backend rainbow per the brief.
- `backendChip(name)` — uppercase typographic mono badge with faint outline, no fill colour.
- `successRateBar(rate)` — the colour-as-data success-rate component (used in Downloads telemetry + Backfill per-service rate).
- `verticalBarChart(rows, opts)` — monochrome 7-day vertical bar chart used by the Backfill throughput panel.
- `autoRefreshIndicator(opts)` — the pulse-dot + "auto · 3s" toggle for the Downloads page.

## File inventory after this session

Substantially modified:
- [app/static/js/v2/monitor.js](app/static/js/v2/monitor.js) — went from 3,578 lines (sessions 02 + 06 + 07) to ~4,650 lines. Removed the 4-entry `STUBS` constant + its loop. Added 4 page renderers (`renderDownloadsMonitor`, `renderLidarrBackfillMonitor`, `renderDiscoveryMonitor`, `renderChartsMonitor`), 5 shared helpers (`backendDot`, `backendChip`, `successRateBar`, `verticalBarChart`, `autoRefreshIndicator`), plus per-page sub-renderers (`buildInflightRow`, `buildRecentRow`, `buildDlStatusBadge`, `buildHistoryRow`, `buildDiscoveryTable`, `buildDiscoveryStatusChip`, `buildFillTable`, `notConfiguredPanel`, `fmtElapsedSeconds`). All four entries registered: `GIQ.pages.monitor.downloads / ['lidarr-backfill'] / .discovery / .charts`.
- [app/static/css/pages.css](app/static/css/pages.css) — appended ~580 new lines under a `Session 08 — Monitor → Ops surfaces` header. Covers: `.op-page-body / .op-head-right / .op-autorefresh{,-dot,-text} / .op-live-pill / .op-live-dot / .op-last-checked`; `.op-backend-dot{,-accent,-muted,-wine} / .op-backend-chip / .op-status-badge{.good,.bad,.warn,.muted}`; `.op-srate-row / .op-srate-bar / .op-srate-fill / .op-srate-{good,warn,bad} / .op-srate-txt{,-good,-warn,-bad}`; `.op-vchart{,-bars,-col,-fill,-lbl}` + `op-vchart-accent`; `.op-collapse-toggle`; `.op-inflight-list / .op-recent-list / .op-dl-row{,-active,-completed,-failed,-left,-main,-title,-sub,-progress,-status} / .op-dl-bar{,-fill,-shimmer} / .op-dl-pct` + `opShimmer` keyframes; `.op-telemetry-wrap / .op-telemetry-table / .op-telemetry-name / .op-num`; `.op-history-list / .op-dl-history-row{,-ts,-backend,-track,-status}`; `.lbf-stats-grid` (6-col, collapses 6→3→2 below 1100px / 700px); `.op-subnav / .op-subnav-btn{,.active}`; `.op-stat-row{,-six}` (4-col + 6-col, responsive); `.op-history-table` + `.op-source-chip`, `.op-sim-cell / .op-mini-bar / .op-mini-bar-fill / .op-mini-bar-pct / .op-err-snip / .op-mbid`; `.op-soulseek-progress{,-row} / .op-soulseek-current / .op-soulseek-artist / .op-soulseek-errors / .op-soulseek-error-row`; `.op-not-configured{,-title,-sub,-list}`; `.op-build-banner{.good,.warn,.bad} / .op-build-dot / .op-build-state / .op-build-msg`; `.op-charts-breakdown / .op-charts-group{,-head,-count,-list} / .op-charts-row{,-name,-meta}`; helper `.op-page-body .empty-row{.wine}` + `.op-page-body .muted`. Plus a single `@media (max-width:700px)` block for mobile collapsing.

Doc:
- [docs/rebuild/handoffs/08-monitor-ops.md](docs/rebuild/handoffs/08-monitor-ops.md) — this file.

No new top-level files. The `dashboard-v2.html` script-tag list is unchanged.

## State of the dashboard at end of session

**Monitor bucket is now complete.** All 11 sub-pages render real content with zero stub fallbacks. Programmatic check:

```
{
  overview: real, pipeline: real, models: real, recs-debug: real,
  system-health: real, user-diagnostics: real, integrations: real,
  downloads: real, lidarr-backfill: real, discovery: real, charts: real
}
```

`#/monitor/downloads`:
- Page header eyebrow MONITOR + "Downloads" + right rail with 4-step range toggle (1h / 24h / 7d / 30d, default 24h, lavender-on-paper-2 active style) + auto-refresh indicator (pulsing lavender dot + mono "auto · 3s", clickable to pause; flips to grey "paused · 3s" with the polling stopped).
- In-flight panel with LIVE badge when ≥ 1 row, each row showing the state dot + uppercase mono backend chip + track label + progress bar (determinate or shimmer) + sub-line `{status} · {elapsed}`. Polls `/v1/downloads/queue` every 3s; auto-stops on nav-away.
- Recent activity panel collapsible (open by default), interleaves `recent_completed + recent_failed` by `updated_at desc`, capped at 10. Failed rows have wine error snippets.
- Per-backend telemetry table (Backend / Total / Success / Failure / In-flight / Success rate). Success-rate cell is the **colour-coded bar** — green ≥ 80%, amber ≥ 50%, wine < 50%. The colour itself is the data, per the brief's allowed exception.
- Recent ad-hoc requests panel — top-10 from `/v1/downloads?limit=10`, each row is a 4-column grid: timestamp · backend chip · track label · status badge. Refreshes every 30s alongside telemetry.

`#/monitor/lidarr-backfill`:
- Page header eyebrow MONITOR + "Lidarr Backfill Stats" + lavender-pulsing live pill + mono "last checked · Ns ago" timestamp.
- Cross-link rail: 2 jump pills (Settings: Edit config → `#/settings/lidarr-backfill`, Actions: Manage queue → `#/actions/discovery`).
- 6-stat grid (3-col on small screens): Missing (with cutoff sub) · Complete (+N / 24h good delta) · Failed (+N / 24h bad delta + percentage) · Capacity (% used + "X/Y · this hour" sub) · ETA (`~N.N d` or `~N h`) · Status (RUNNING / IDLE / PAUSED with last-tick-ago sub).
- Throughput · last 7 days panel (mono lavender bars on `--line-faint` track, the rightmost (today's) bar uses the brighter `--accent` solid). Derived client-side from `requests?limit=200` filtered to `status === 'complete'` and bucketed by UTC day.
- Per-service success-rate panel (qobuz / tidal / deezer / soundcloud — order preserved per the cascade priority). Same colour-coded success-rate bar pattern as Downloads.

`#/monitor/discovery`:
- Page header eyebrow MONITOR + "Discovery", no right rail.
- Internal sub-nav (Lidarr Discovery / Fill Library / Soulseek Bulk) — page-local state, switching does NOT trigger a router dispatch. Active button gets the lavender-on-tinted-bg active style.
- Lidarr Discovery view: 4 stat tiles + history table with similarity bars + status chips. Empty state when not configured shows the .env hint list.
- Fill Library view: 4 stat tiles + history table with match-bar showing avg + best percent. Empty state when not configured shows the .env hint list.
- Soulseek Bulk view: 6 stat tiles + LIVE-badged progress panel + recent errors panel (last 20 newest-first). Polls 3s while the tab is active. The other two tabs poll 30s.

`#/monitor/charts`:
- Page header eyebrow MONITOR + "Charts Stats", no right rail.
- Build status banner with three tiers — green "FRESH" if `age < 1.5 × interval`, amber "AGING" if `age < 3 × interval`, wine "STALE" otherwise. "NOT YET BUILT" tier shown when `last_fetched_at == null`. The `set CHARTS_ENABLED=true` hint is appended automatically whenever `auto_rebuild_enabled === false`.
- 6-stat row: Last build (relative + UTC sub) · Total charts · Total entries · Library matches (good delta) · Match rate % · Unmatched count.
- Per-scope breakdown panel grouping charts into GLOBAL / TAGS / COUNTRIES sections. Each chart row: scope label + chart type chip + entry count + fetched-ago.

Activity pill (sidebar): all 4 rows now resolve to a real Monitor page:
- Pipeline → `#/monitor/pipeline` (built session 06)
- Library scan → `#/monitor/system-health` (built session 07)
- Downloads → `#/monitor/downloads` (this session)
- Lidarr backfill → `#/monitor/lidarr-backfill` (this session)

## Decisions made (with reasoning)

- **Polling intervals chosen**:
  - Downloads in-flight queue: **3s** (matches the brief and the legacy `dlStartQueuePolling` cadence — the user-perceptible reaction window for a download starting).
  - Downloads telemetry + ad-hoc history: **30s** (terminal counts don't move within seconds; polling these every 3s would just burn requests).
  - Lidarr Backfill stats + requests: **30s** (the backfill engine itself ticks every 5 minutes by default; faster polling tells us nothing).
  - Discovery sub-views: **30s** for Lidarr & Fill (long-running batch jobs), **3s** for Soulseek (active bulk has visible per-second progress and matches the legacy `_startBulkPoll` cadence).
  - Charts: **30s** (chart builds happen on a configurable cron — typically every 24h — so even 30s is overkill, but cheap).
  - All page-level timers are returned via the page renderer's cleanup closure so the router can clear them on nav-away. Verified no leaked timers.

- **Backend dot is state-coloured, not backend-coloured**, per the brief: success-track = `--accent`, in-progress = `--ink-3`, failure-track = `--wine`. The legacy `dlBackendDot` rainbow (spotdl green, streamrip amber, etc.) is intentionally rejected. The backend identity comes through via the uppercase mono `op-backend-chip`, which is a typographic outline-only badge — never colour-filled.

- **Two colour-as-data exceptions used**: (1) success-rate bars on the Downloads per-backend telemetry panel and the Backfill per-service panel — green / amber / wine where the bar's colour IS the success-rate datum. (2) Charts build-status banner — green / amber / wine where the banner's colour IS the freshness datum. Both are explicitly allowed by the brief's "this is one of the few places colour gradients are allowed because the colour itself is the data."

- **`Capacity` shown as a percentage, NOT as GB.** The brief asked for "% used + 'X/Y GB'" but `/v1/lidarr-backfill/stats` returns `capacity_remaining` and `max_per_hour` as **download counts per hour**, not GB. There is no GB-based capacity field. I show `value = used / max * 100%` and `delta = "{remaining} / {max} · this hour"`. So a backfill running 2 of 10 downloads in the past hour shows `Capacity 20%` with `8 / 10 · this hour`. Honest to the API; preserves the % grammar from the brief.

- **Lidarr Backfill `Run #`**: The brief asked for a "Run # (mono, current run counter)" tile. **`/v1/lidarr-backfill/stats` does not expose a run counter.** The closest concept is the cron tick count, which isn't surfaced. I replaced this tile with a `Status` tile (RUNNING / IDLE / PAUSED + last-tick-ago sub) since that's the most useful actionable info for the same screen real estate. Hand-off note for session 12 polish: if the backend ever adds a run counter, swap `Status` → `Run #` and the layout still works.

- **Throughput chart derived client-side, not from a dedicated endpoint.** `/v1/lidarr-backfill/stats` doesn't return per-day completion history. The brief listed both endpoints (`stats` + `requests`) for the Backfill page in api-map.md, so I bucketed `requests?limit=200` by `updated_at` UTC day and counted `status === 'complete'` rows. With `limit=200` the buckets capture roughly the last 7 days at 24/day rate-limit. If a deployment exceeds 200 completions in 7 days the older buckets undercount — acceptable for self-hosted, and the chart's a trend indicator not a bookkeeping ledger. Sub-line states "derived from /v1/lidarr-backfill/requests · status = complete" so users know the source.

- **Per-service success rate also derived client-side** for the same reason. `picked_service` field on `LidarrBackfillRequest` rows lets us group by service; `status === 'complete'` vs `failed/no_match/permanently_skipped` gives us the success/failure split. Service order is qobuz → tidal → deezer → soundcloud (cascade priority); unknown services fall to alphabetical tail.

- **Discovery sub-nav is page-local state, NOT a route.** The router enumerates allowed sub-pages per bucket — Discovery is one slug. Switching between Lidarr / Fill / Soulseek does NOT change the URL hash. Per the brief: "Internal sub-nav (small horizontal toggle) — Lidarr / Fill Library / Soulseek. NOT topbar tabs (the topbar is fixed to bucket sub-pages)." The sub-nav also does NOT trigger a router dispatch — switching modes calls `loadCurrent()` which fires the right endpoints + repaints the body. Page-level cleanup still runs on hash-change away from `/monitor/discovery`.

- **Soulseek polling cadence varies by sub-mode.** The discovery page maintains a single `pollTimer` whose interval is decided by `state.mode` — switching tabs reschedules it (3s for Soulseek, 30s for Lidarr / Fill). This avoids running 3 timers concurrently when only one mode is visible.

- **Auto-refresh indicator is a real button**, not just an indicator. Click toggles between `auto · 3s` (active, lavender pulse) and `paused · 3s` (idle, grey dot). Pausing stops the queue polling timer; resuming restarts it. Useful on slow connections or when watching a specific download mid-failure without the panel re-rendering under your cursor. The full-refresh background timer (telemetry + history) keeps running regardless — pausing only suspends in-flight queue polling.

- **No new endpoints added.** Every panel maps to existing endpoints listed in api-map.md. Where the brief implied a new endpoint (e.g. throughput history, per-service success rates), I derived from existing list endpoints client-side. The Soulseek `/v1/soulseek/bulk-download/status` and `/v1/soulseek/bulk-download/cancel` endpoints aren't in api-map.md but are confirmed live in the legacy `app.js` (lines 1903 / 2025). Hand-off note for session 12: api-map.md should be updated to list these.

- **Charts banner's `auto-rebuild OFF` hint is always appended when disabled**, even on a "FRESH" banner (e.g. user manually rebuilt it but cron is off). Cleaner UX than two separate banners — one banner, two facts. Brief says "set CHARTS_ENABLED=true if env disabled" → I read that as a separate sentence appended to whatever the freshness banner already says.

- **Visual verification used the same mocked-API harness as sessions 02–07.** The local toolchain still can't run uvicorn (Python 3.9 venv predates `from datetime import UTC`); the only running server is `python3 -m http.server 8001 --directory app`. To populate the new pages I patched `GIQ.api.get` inside `preview_eval` with sample responses that mirror the real API shapes (`/v1/downloads/queue`, `/v1/downloads/stats?days={n}`, `/v1/downloads?limit=10`, `/v1/lidarr-backfill/stats`, `/v1/lidarr-backfill/requests?limit=200`, `/v1/discovery{,/stats}`, `/v1/fill-library{,/stats}`, `/v1/soulseek/bulk-download/status`, `/v1/charts{,/stats}`). All shapes match the literal field names from `app/api/routes/downloads.py`, `app/services/lidarr_backfill.py::get_stats`, `app/api/routes/lidarr_backfill.py`, `app/api/routes/discovery.py`, and the existing `app/static/js/app.js` consumer code.

## Gotchas for the next session

- **`/v1/lidarr-backfill/stats` does NOT include**: per-day throughput history, per-service success rates, GB-capacity. All three are derived client-side from `/v1/lidarr-backfill/requests?limit=200`. If a deployment ever has >200 attempts per 7 days, the older buckets undercount — the chart is a trend indicator, not a bookkeeping ledger. If session 12 wants exact totals, the backend would need a `/stats?include=throughput,services` flag.

- **Capacity is per-hour, not GB.** `capacity_remaining` and `max_per_hour` are **download counts per hour**. The Capacity stat tile shows `value = (used / max) * 100%` with delta `{remaining} / {max} · this hour`. Don't confuse this with disk capacity — the backfill engine doesn't track GB.

- **Activity pill data sources**: the popover's 4 rows resolve to `#/monitor/{pipeline,system-health,downloads,lidarr-backfill}`. After this session, every row is a real page. Verified in transcript via `GIQ.pages.monitor[sp]` typeof check.

- **Charts page `Per-scope breakdown` requires `entry_count` per chart.** The legacy code accesses `c.entry_count || c.total || 0`. The actual `/v1/charts` response shape varies — sometimes carries `entry_count`, sometimes `total`. I fall through to 0 if neither is present. Not a blocker; the row still renders, just shows "0 entries".

- **`/v1/downloads?limit=10` returns `{total, downloads: [...]}`**, NOT `{total, items: [...]}` like Lidarr Backfill. Don't conflate the two list-endpoint shapes — `downloads` (plural) is the array key here.

- **`renderRecent()` only paints after `loadQueue()` completes.** If the API is unreachable on first load, the Recent activity panel is missing entirely (only In-flight shows the "Queue unavailable" error). This is acceptable since the brief positions Recent activity as a sub-panel of the queue — they share a data source. If session 12 wants a more graceful empty state, render a placeholder Recent activity panel before the first fetch.

- **Soulseek tab polls every 3s while active, 30s otherwise.** Switching back to Lidarr / Fill from Soulseek correctly reschedules the timer (verified). The catch is: navigating *away* from `/monitor/discovery` uses the page cleanup to clear the timer, but if the tab was on Soulseek when nav-away happens, no Soulseek-specific stop callback fires. The interval-clear handles it. Don't over-engineer — single timer in single state field is enough.

- **Per-service success-rate panel needs `picked_service` to be populated.** Rows where the engine hasn't picked a service yet (queued / no_match) don't contribute. The empty-state copy says "Service stats appear once the engine has dispatched downloads via streamrip." For brand-new deployments this is the expected initial state.

- **The colour-coded success-rate bars use absolute hex values** (#6fbf6f green, #d4a64a amber) — these aren't in the design tokens because the design palette is intentionally monochrome lavender/wine. This is the explicit colour-as-data exception. If session 12 wants to align with a tokens.css addition, lift them into `--ok-bar`, `--warn-bar`, `--bad-bar` (the wine half already lives in `--wine`).

- **Auto-refresh button toggles only the queue polling timer**, not the 30s telemetry/history timer. A user who wants to fully freeze the page can navigate away — not an issue, but worth noting if anyone expects "pause" to freeze everything.

- **`window.location.hash` change does NOT re-render the page if the hash didn't actually change.** This bit me momentarily — when navigating to `#/monitor/downloads` from `#/monitor/downloads` with a sub-nav-style toggle, you have to call `GIQ.router.dispatch()` directly. Not a problem in this session because Discovery's sub-nav is page-local state, but worth flagging for future sub-page sub-routes.

- **Activity pill bails when `apiKey` is null.** During mock verification I had to set `GIQ.state.apiKey = 'mock-key'` to bypass the bail in `activity.poll()`. In production this is the correct behaviour — no key means no poll — but during mocked verification it's a footgun.

## Open issues / TODOs

- **Run #** stat tile on Lidarr Backfill is a `Status` tile instead because the backend doesn't expose a run counter. If session 12 wants to surface the cron-tick count, add `tick_count` to `lbf_service.get_stats()` and swap.

- **Throughput chart accuracy** is bounded by `requests?limit=200`. For deployments running > 200 completions per 7 days, older buckets undercount. Session 12 polish: bump the limit to 500 if measured to be performant, or add a server-side aggregation endpoint.

- **Per-service success rate** treats `no_match` and `permanently_skipped` as failures. Arguably `no_match` is "couldn't find anything to attempt" rather than "tried and failed". If users want a stricter "attempts that actually ran" denominator, exclude no_match from both numerator and denominator.

- **Soulseek bulk download endpoints** (`/v1/soulseek/bulk-download/status`, `/v1/soulseek/bulk-download`, `/v1/soulseek/bulk-download/cancel`) aren't in `docs/rebuild/api-map.md`. Add them in session 12 cleanup.

- **Auto-refresh indicator** could trigger SSE-style live update of in-flight rows instead of full re-render. Not a bottleneck (the in-flight list is short), but smoother visually. Polish for session 12.

- **Build status banner colour palette** (green / amber / wine) duplicates the success-rate palette. Consider lifting `--ok-bar` / `--warn-bar` / `--bad-bar` into tokens.css if more pages need them. Currently 4 places use these (3 success-rate consumers + 1 build banner).

- **Charts page is missing the "failed downloads" sub-panel** the brief listed as optional. The current backend doesn't track per-track download status on chart entries the way the Downloads list does. Could be added later by walking `/v1/downloads?source=charts` if such a filter ever exists.

- **Per-chart match rate** is also omitted from the breakdown — it would require a separate `/v1/charts/{type}?scope={s}` call per chart entry to compute. Acceptable trade-off for not blocking page render on N additional API calls; if needed, add a "Show details" expand-on-click per chart row.

- **Inline mocks for verification add up.** 5 separate mock-injection scripts run during this session (one per sub-page). Sessions 02–07 had similar; the mock harness has effectively become a parallel implementation. Worth extracting into a small `tests/v2/mocks.js` shipped alongside the dashboard for manual visual QA, but only post-cutover.

## Verification screenshots

Captured inline in the session transcript via `mcp__Claude_Preview__preview_screenshot` (mocked-API state, viewport 800×600):

1. **Downloads (top half)** — Page header with MONITOR eyebrow, "Downloads" title, range toggle (24h active), pulsing "auto · 3s" indicator. In-flight panel with 3 rows (streamrip 42% determinate bar + sub `progress · 18s`, spotdl shimmer + `searching · 4s`, slskd shimmer + `queued · 1s`). Recent activity panel collapsible toggle showing "RECENT ACTIVITY (3 ✓ / 1 ✗)" with 4 rows interleaved by updated_at desc; failed Spotizerr row carries the wine-coloured "no source candidates above min_quality" snippet. Per-backend telemetry header row visible with backend chips beginning to render.

2. **Downloads (bottom half)** — Per-backend telemetry table fully rendered: 4 rows (streamrip 142/124/8/10 → 93.9% green bar, spotdl 88/67/18/3 → 78.8% amber bar, spotizerr 32/14/17/1 → 45.2% wine bar, slskd 24/18/4/2 → 81.8% green bar). Recent ad-hoc requests panel below shows 6 history rows with mono timestamps, backend chips, track labels, and status badges (downloading / searching / done / failed in the right colours).

3. **Lidarr Backfill** — Page header with live pulsing dot + "last checked · 0s ago" + relatedRail with 2 jump pills. 6-stat grid: Missing 412 (+88 cutoff), Complete 1.8K (+24/24h good), Failed 12 (+1/24h · 0.6% bad), Capacity 20% (8/10 · this hour), ETA ~2.1 d, Status IDLE (last tick 4m ago). Throughput · last 7 days panel: 7 monochrome lavender bars, today's bar bright accent, dates 4/24 → 4/30 mono. Per-service success rate panel: 4 rows (qobuz 8/8/0 → 100% green, tidal 14/8/6 → 57.1% amber, deezer 10/7/3 → 70% amber, soundcloud 7/7/0 → 100% green).

4. **Discovery → Lidarr Discovery** — Page header MONITOR + "Discovery", sub-nav with "Lidarr Discovery" active, 4 stat tiles (Artists discovered 421, Sent to Lidarr 400 good, Pending 18, Today 8/50). Discovery history panel with 4 rows: each shows artist + truncated mbid sub, source chip ("LASTFM SIMILAR" / "LASTFM GENRE"), seed text, similarity bar with %, status badge (IN_LIDARR good / SENT good / PENDING muted / FAILED bad), `when` mono. Failed Brambles row carries "lidarr unreachable: connection refused…" wine snippet.

5. **Discovery → Fill Library** — Same shell, sub-nav switched to Fill Library. 4 stat tiles (Albums queued 112 good, Total processed 142, Avg match 88%, Today 4/20). Fill Library history table with 3 rows; Match column shows match bar with avg + best percent in parens (e.g. "90% (best 95%)"). Album status chips: SENT good, ALBUM_MONITORED muted.

6. **Discovery → Soulseek Bulk** — Sub-nav switched to Soulseek Bulk. 6 stat tiles (Tracks found 2.8K, Queued 2.3K + "sent to slskd" good delta, Searched 2.8K, Skipped 320, Failed 22, Elapsed "31m 7s" + "started 31m ago" delta). LIVE-badged "Soulseek bulk download" panel showing artists progress bar + "142 / 500 · 28%" + "currently · Yann Tiersen" mono. Recent errors panel with 2 rows.

7. **Charts (FRESH state)** — Lavender FRESH banner with "Built 6h ago · auto-rebuild every 24h". 6 stat tiles (Last build "6h ago" + UTC sub, Total charts 12, Total entries 1.1K, Library matches 487 good, Match rate 42.6%, Unmatched 655 + "candidates for download" sub). Per-scope breakdown panel with 3 groups (GLOBAL · 2 charts, TAGS · 4 charts, COUNTRIES · 3 charts), each row carries the type chip + entry count + fetched-ago.

8. **Charts (STALE state)** — Same page but with `last_fetched_at = now - 90h` and `auto_rebuild_enabled = false`. Banner flipped to wine "STALE" with "Last built 3d ago · expected every 24h · set CHARTS_ENABLED=true in your .env to enable scheduled builds". Confirms the three-tier banner colouring works.

9. **Activity pill popover (final state)** — Charts page in background, popover above the activity pill showing 4 rows (Pipeline run · LIVE → `#/monitor/pipeline`, Library scan · LIVE → `#/monitor/system-health`, 2 downloads → `#/monitor/downloads`, Lidarr backfill · LIVE → `#/monitor/lidarr-backfill`). All 4 rows resolve to a real Monitor renderer.

Programmatic verifications evidenced via `preview_eval`:
- All 4 new pages render real content (no `.page-stub` fallback).
- `Object.keys(GIQ.pages.monitor)` returns 11 entries — Monitor bucket complete.
- 3 in-flight rows + 4 recent activity rows + 4 telemetry rows + 6 history rows on Downloads.
- 6 Lidarr Backfill stat tiles with the right values; throughput chart has 7 bars; service table has 4 rows.
- Discovery sub-nav switches without page reload (verified active button text + reset stat values for each mode).
- Charts banner correctly transitions FRESH → STALE when freshness data flips.
- Activity pill renders 4 rows with correct hrefs; `typeof GIQ.pages.monitor[sp]` returns `'function'` for all 4 targets.
- No console errors throughout.

## Time spent

≈ 110 min: reading session 06 / 07 hand-offs / api-map / app.js downloads / lbf / discovery / charts surfaces / api routes (25) · monitor.js extension (4 page renderers + 5 shared helpers + per-page sub-renderers) (45) · pages.css (~580 new lines) (15) · preview verification + 9 screenshots + activity pill deep-link audit (15) · this hand-off note (10).

---

**For the next session to read:** Session 09 — Explore: track table + Recommendations + Tracks, see [docs/rebuild/09-explore-recs-tracks.md](docs/rebuild/09-explore-recs-tracks.md). The Monitor bucket is now complete; sessions 09 / 10 / 11 build out Explore. Session 09 will deep-link into `#/monitor/recs-debug?debug=user:{id}` (built session 06) from a "Debug Recs" button on the new Recommendations page.
