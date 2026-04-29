# Session 08 — Monitor: Downloads + Lidarr Backfill stats + Discovery + Charts stats

You are continuing the GrooveIQ dashboard rebuild. This is **session 08 of 12**.

The last batch of Monitor surfaces — operational telemetry pages. Independent of sessions 06 / 07; can run in parallel. After this session, the Monitor bucket is complete.

## Read first

1. `docs/rebuild/README.md`
2. All prior hand-offs (01–02 minimum).
3. `docs/rebuild/components.md`
4. `docs/rebuild/api-map.md` → "Downloads (queue + telemetry)", "Lidarr Backfill (stats)", "Discovery (stats)", "Charts (stats)" rows.
5. `gui-rework-plan.md` § "Downloads tab → split", § "Discovery → Lidarr Backfill → split", § "Discovery sub-tab → multi-way split", § "Charts sub-tab → split".
6. `design_handoff_grooveiq_dashboard/page-lidarr.jsx` — `LBMonitor` reference layout (6-stat grid + throughput chart + cross-link rail).
7. The current implementations of Live Queue, Telemetry, Discovery stats, Charts stats in `app/static/js/app.js`.

## Goal

Four Monitor pages working:
- `#/monitor/downloads` — live in-flight queue + per-backend telemetry (success/failure rates).
- `#/monitor/lidarr-backfill` — 6-stat grid (missing / complete / failed / capacity / ETA / run #) + throughput bar chart + cross-link rail.
- `#/monitor/discovery` — Lidarr discovery stats + Fill Library run history + Soulseek bulk download log.
- `#/monitor/charts` — last build, match rate, version distribution.

## Out of scope

- Routing config (Settings, session 04).
- Backfill config (Settings, session 04).
- Backfill queue management (Actions, session 05).
- Trigger buttons (Actions, session 05).

## Tasks (ordered)

### A. Monitor → Downloads

`GIQ.pages.monitor.downloads`:

1. **Page header** — eyebrow MONITOR, title "Downloads", right side: range toggle (1h / 24h / 7d / 30d) for telemetry window; "Auto-refresh every 3s" indicator (pulsing dot if active, click to pause).
2. **In-flight panel** — `GET /v1/downloads?status=in_flight&limit=50`, polled every 3s (auto-stop on nav-away). Each row:
   - Backend dot (colour from a single neutral palette family — never rainbow): `--accent` for success-track backends, `--ink-3` for in-progress, `--wine` for failure-track.
   - Backend name (mono uppercase typographic chip).
   - Track label "{artist} — {title}" or "(unnamed)".
   - Progress bar (% if `progress_pct` available, shimmer otherwise).
   - Status sub-text + elapsed.
   - **No cancel button here** — that's Actions. Just observability.
3. **Recent activity panel** — collapsible. Last 10 completed + failed downloads, interleaved by `updated_at desc`.
4. **Per-backend telemetry panel** — `GET /v1/downloads/stats?days={range}`. Table columns: backend / success / failure / in-flight / success-rate %. Success-rate bar (colour-coded green / yellow / red — this is one of the few places colour gradients are allowed because the colour itself is the data).
5. **Recent ad-hoc requests** — small panel with last 10 rows from `GET /v1/downloads?limit=10`. Each row: timestamp / backend / track / status badge.

### B. Monitor → Lidarr Backfill

`GIQ.pages.monitor.lidarrBackfill`:

1. **Page header** — eyebrow MONITOR, title "Lidarr Backfill Stats", right side: live indicator (pulse + "live") + last-checked timestamp.
2. **Cross-link rail** — links to Settings (Edit config) + Actions (Manage queue).
3. **6-stat grid** — `GET /v1/lidarr-backfill/stats`. Tiles:
   - Missing (count)
   - Complete (count, delta last 24h)
   - Failed (count, delta last 24h, percentage)
   - Capacity (% used + "X/Y GB")
   - ETA (mono, "~3.4 d" format)
   - Run # (mono, current run counter)
4. **Throughput chart** — bar chart of last 7 days, albums/day. Monochrome `--accent` bars on `--line-faint` background.
5. **Capacity timeline** (optional, if `/v1/lidarr-backfill/stats` returns historic capacity data) — line showing % capacity used over time.
6. **Service success-rate panel** — per-service breakdown (qobuz / tidal / deezer / soundcloud) showing success rate from recent attempts. Same colour-coded bar pattern as Downloads telemetry (allowed exception).

### C. Monitor → Discovery

`GIQ.pages.monitor.discovery`:

1. **Page header** — eyebrow MONITOR, title "Discovery".
2. **Internal sub-nav** (small horizontal toggle) — Lidarr / Fill Library / Soulseek. NOT topbar tabs (the topbar is fixed to bucket sub-pages).
3. **Lidarr Discovery panel** — `GET /v1/discovery` (list) + `GET /v1/discovery/stats`. Stat cards: artists discovered / albums queued / last run. Per-run history table.
4. **Fill Library panel** — `GET /v1/fill-library` (list) + `GET /v1/fill-library/stats`. Stat cards. Per-run history table with status badges.
5. **Soulseek panel** — bulk-download progress + completion log (from same source the current Soulseek bulk page uses). Live polling while a bulk is active.

### D. Monitor → Charts

`GIQ.pages.monitor.charts`:

1. **Page header** — eyebrow MONITOR, title "Charts Stats".
2. **Build status banner** — green if recent + auto-rebuild ON, yellow if stale, plus "set CHARTS_ENABLED=true" hint if env disabled.
3. **6-stat row** — `GET /v1/charts/stats`. Last build / total charts / total entries / library matches / match rate / unmatched count.
4. **Per-chart breakdown** (optional) — small table grouping charts by scope (global / tag / country) with per-chart match rate.
5. **Failed downloads** (if any) — list of chart entries that failed to auto-download. Useful for diagnostics.

### E. Wire deep-links from Activity Pill

The activity pill (built session 02) deep-links to:
- Pipeline → `#/monitor/pipeline` ✓ (session 06)
- Scan → `#/monitor/system-health` ✓ (session 07)
- Downloads → `#/monitor/downloads` (this session)
- Backfill → `#/monitor/lidarr-backfill` (this session)

After this session, every activity-pill row resolves to a real page. Verify.

## Verification

1. Trigger a download from Actions → Downloads → Search. Watch `#/monitor/downloads` reflect the in-flight row within 3s.
2. `#/monitor/lidarr-backfill` shows 6-stat grid + throughput chart. Cross-link rail jumps to Settings (config) and Actions (queue).
3. `#/monitor/discovery` — sub-nav toggles between Lidarr / Fill Library / Soulseek without page reload.
4. `#/monitor/charts` — last build timestamp colour-coded green / yellow / red appropriately.
5. Activity pill: click each row, confirm correct deep-link target.

## Hand-off

Write `handoffs/08-monitor-ops.md`. Note:
- Polling intervals chosen for each panel (3s for downloads, 30s for backfill, etc.).
- Any colour-as-data exceptions you used (success-rate bars, capacity %).
- Whether the `Capacity` stat made sense as percentage (depends on `/v1/lidarr-backfill/stats` shape).

Commit: `rebuild: session 08 — monitor: downloads + backfill + discovery + charts`.

**The Monitor bucket is now complete.** Sessions 09–11 build out Explore.
