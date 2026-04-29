# Hand-off: Session 02 — Monitor → Overview + Activity pill

## Status
- [x] Session goal achieved
- [x] Visual verification done (screenshots inline in transcript; populated state captured with mocked API responses since the local toolchain can't run the FastAPI backend — see Gotchas)
- [x] No regressions in old `/dashboard` (zero modifications to `app/static/index.html`, `js/app.js`, `css/style.css`)
- [x] Committed on `gui-rebuild` branch with message `rebuild: session 02 — monitor overview + activity pill`

## What landed

The Monitor → Overview page is now built end-to-end at full design fidelity: page header (eyebrow + title + last-update timestamp + 1h/24h/7d/30d range toggle), 6-column stat row, and a 2fr/1fr body grid. The left column hosts the smooth area-chart Event ingest panel (cubic-Bézier SVG with gradient fill, 3 dashed gridlines, mono axis labels, legend), a two-up of Top tracks (rank chips + mini bars + counts) and Event types (typographic horizontal bars, lavender for engagement / muted for skip+pause / wine for dislike), and the Recent events panel with a LIVE badge pulling `/v1/events?limit=4` every 5 s. The right column hosts Models (6-row readiness with lavender/wine dots and uppercase mono state chips), Library scan (LIVE-badged when running, gradient progress bar + 4 mini-stat cells, falls back to "No scans yet · Start scan →" when idle), and Quick run (4 deep-link rows to Actions, sub-lines wired to the most-recent pipeline run / scan state).

The shared component layer (`GIQ.components.statTile / panel / liveBadge / pageHeader / rangeToggle / areaChart`) is now built and reusable for sessions 03+. `GIQ.sse` is a singleton fetch-based SSE bus on `/v1/pipeline/stream` that auto-reconnects with exponential backoff, drives the topbar `SSE live`/`SSE off` pill, and re-broadcasts events to subscribers. `GIQ.activity` polls the four real endpoints every 5 s, drives the sidebar pill (live count + "N active · pipeline · scan · 2 dl" sub-line, idle → grey dot + "no active jobs"), and renders a 320 px popover above the pill with one row per running job, each carrying a `View →` deep-link to the right Monitor surface. SSE events (`pipeline_start`, `pipeline_end`, `step_complete`, `step_failed`) trigger immediate activity refreshes so the pill doesn't wait for the next 5 s tick.

## File inventory after this session

New:
- [app/static/js/v2/activity.js](app/static/js/v2/activity.js) — `GIQ.activity` aggregator (polls pipeline / stats / downloads queue / lidarr-backfill stats, renders pill, manages popover, hooks SSE).

Substantially modified:
- [app/static/js/v2/components.js](app/static/js/v2/components.js) — built `statTile`, `panel`, `liveBadge`, `pageHeader`, `rangeToggle`, `areaChart`, plus the `GIQ.sse` bus.
- [app/static/js/v2/monitor.js](app/static/js/v2/monitor.js) — Monitor → Overview renderer (full panel set + per-second timestamp tick + 5 s recent-events refresh + 15 s full refresh + SSE-driven re-render + cleanup that disposes all timers and SSE subscriptions). Other 10 Monitor sub-pages remain stubs.
- [app/static/js/v2/shell.js](app/static/js/v2/shell.js) — activity-pill markup driven by `data-count` and `idle` class instead of hard-coded copy; sidebar render calls `GIQ.activity.rebind()` to re-attach the click handler after re-render; API-key submit kicks off `GIQ.sse.connect()` + `GIQ.activity.refresh()` on success and `GIQ.sse.disconnect()` on clear/failure.
- [app/static/js/v2/index.js](app/static/js/v2/index.js) — boot sequence now starts `GIQ.activity.start()` immediately and connects SSE if a key is already present.
- [app/static/dashboard-v2.html](app/static/dashboard-v2.html) — adds `<script src="/static/js/v2/activity.js"></script>` after `components.js`.
- [app/static/css/components.css](app/static/css/components.css) — populated with page header, range toggle, last-update, stat tile, panel, LIVE badge, area chart, idle-pill, and activity-popover styles.
- [app/static/css/pages.css](app/static/css/pages.css) — added Monitor → Overview rules (`.overview-*`, top-tracks, event-types, recent-events, models, scan, quick-run).
- [app/static/css/shell.css](app/static/css/shell.css) — collapsed-sidebar pill `::after` now uses `content: attr(data-count)` instead of literal `"3"`.

Doc:
- [docs/rebuild/handoffs/02-overview.md](docs/rebuild/handoffs/02-overview.md) — this file.

## State of the dashboard at end of session

Working at `/static/dashboard-v2.html#/monitor/overview`:
- Page header with `MONITOR` eyebrow, "Overview" title, live "last update · Ns ago" pill (mono, ticks every second), and segmented 1h/24h/7d/30d toggle (24h default, lavender-on-paper-2 active style).
- 6-column stat row: Events / Users / Tracks / Playlists / Events per hour / Ranker. Each tile has the locked layout — eyebrow + Inter Tight 22/600 value + 11 px delta line. Up-deltas are lavender with `↑`, down-deltas wine with `↓`, flat is `--ink-3`. Empty state shows `—`.
- Event ingest panel: smooth cubic-Bézier area chart over the 96 buckets returned by `/v1/pipeline/stats/events`, mono axis labels (HH:MM derived from the bucket timestamps), legend, "View full breakdown →" deep-link to System Health.
- Top tracks panel: 6 ranked rows from `stats.top_tracks_24h` with rank chip, title + artist meta, 60 × 3 lavender mini bar, mono right-aligned count.
- Event types panel: per-type horizontal bar — `--accent` for `play_end`/`like`/`play_start`, `--ink-3` for skip/pause/volume/etc, `--wine` for `dislike`. Sorted by count desc.
- Recent events panel with LIVE badge, refreshes every 5 s. Each row: 60 px mono timestamp, 70 px uppercase typographic event-type chip with faint outline (no colour fill, per the rules), 50 px lavender user, ellipsised track, mono right-aligned duration / completion %.
- Models panel: 6 rows (Ranker, Collaborative, Embeddings, SASRec, Session GRU, Last.fm cache) with lavender/wine 7 px dot, name + mono sub-line of the most relevant 2 stats from `/v1/pipeline/models`, mono uppercase right-aligned state chip in `--accent` for ready, `--wine` for stale.
- Library scan panel: LIVE badge + sub-line `phase · {phase}` when running. Body shows progress label (analysed / found), mono lavender percentage, gradient `--paper-2 → --accent` 6 px progress bar, and a 2 × 2 mini-stat grid (Found / Analyzed / Skipped / Failed). Idle state: "No scans yet · Start scan →" deep-link to Actions → Library.
- Quick run panel: 4 deep-link rows (Run pipeline → `#/actions/pipeline-ml`, Scan library → `#/actions/library`, Build charts → `#/actions/charts`, Backfill CLAP → `#/actions/library`) with arrow `→` in lavender. The first two rows pick up live sub-lines from `/v1/pipeline/status` and `stats.latest_scan` (e.g. "running" or "14m ago · completed").
- Activity pill (sidebar): driven by `GIQ.activity` — pulsing lavender dot + "N active" + sub-line when ≥ 1 job running, grey dot + "idle" otherwise. Click → 320 px popover above with one row per running job (icon, label, mono sub, optional LIVE badge, "View →" deep-link). Outside-click and Escape close the popover. Collapsed sidebar variant: lavender circle + count via `data-count` attr.
- Topbar SSE pill: lavender pulse "SSE live" when `GIQ.sse.isConnected()`, grey "SSE off" otherwise. State driven entirely by the bus connection lifecycle.

Stubbed:
- Other 10 Monitor sub-pages (Pipeline, Models, System Health, Recs Debug, User Diagnostics, Integrations, Downloads, Lidarr Backfill, Discovery, Charts) still render the session-01 "TBD" placeholder. Sessions 06–08 fill them in.
- Quick-run rows deep-link to Actions sub-pages — those Action pages are stubs until session 05.
- The `period` argument of the range toggle is captured into `state.range` but doesn't yet narrow the API queries (the existing endpoints don't accept a window param). Treated as a UI affordance for now; sessions 06–08 may wire it up if the underlying endpoints grow window params.

## Decisions made (with reasoning)

- **`fetch` + `ReadableStream` for SSE, not `EventSource`.** `EventSource` doesn't allow custom headers, and we authenticate via `Authorization: Bearer ...`. Same pattern the old `app.js` already uses, ported into a singleton with auto-reconnect (exp backoff, capped at 30 s) and emit-bus semantics so multiple pages can subscribe without each opening their own connection.
- **Activity pill is a separate file (`activity.js`), not part of `shell.js`.** It's stateful (poll loop, popover lifecycle, SSE subscriptions, outside-click handler) and has 5 distinct concerns. Keeping it out of `shell.js` keeps the shell file focused on layout-and-routing wiring.
- **Activity polling assembled client-side from 4 endpoints.** Per `api-map.md` notes: no new `/v1/activity` endpoint during the rebuild. Used `/v1/pipeline/status?limit=1`, `/v1/stats` (for `latest_scan`), `/v1/downloads/queue` (for in-flight count — that endpoint already exists and groups by status), and `/v1/lidarr-backfill/stats`. All wrapped in `.catch(() => null)` so a single endpoint's failure doesn't break the pill.
- **Single-series area chart, not dual.** The realistic mockup overlays an "engagement" series alongside total events, but `/v1/pipeline/stats/events` only returns total counts per bucket. Splitting by event type requires `/v1/pipeline/stats/activity` which has a different schema (per-bucket map keyed by event type). Rather than fudge with a fake ratio, I render only the total series with the lavender stroke + gradient fill. Session 07 (System Health) can layer in the engagement series properly when it picks up the `activity` endpoint.
- **15 s full refresh + 5 s recent-events refresh + 1 s timestamp tick.** Three timers because each panel has a different staleness budget. All three are cleared in the page's `cleanup()` return, and SSE events kick a full refresh out-of-band.
- **`stat-delta` always reserves a 14 px line.** Tiles without a delta render a `&nbsp;` placeholder so the row stays aligned. Looks crisper than rows with mismatched heights.
- **Mocked API responses used for visual verification only.** The local Python toolchain still can't run uvicorn (per session 01 hand-off), and the running preview is a static `python3 -m http.server` rooted at `app/`. Verified the populated visual state by patching `GIQ.api.get` with sample `/v1/stats`, `/v1/pipeline/models`, `/v1/pipeline/stats/events`, `/v1/events`, `/v1/pipeline/status`, `/v1/downloads/queue`, and `/v1/lidarr-backfill/stats` responses inside `preview_eval`. Every shape mirrors the real API as documented in `app/api/routes/stats.py`, `app/services/lidarr_backfill.py`, and the existing `app/static/js/app.js` consumer code.

## Gotchas for the next session

- **Local server topology is fragile.** The `claude/launch.json` configurations don't actually start a server (they sleep). For verification I started `python3 -m http.server 8001 --directory app` manually; that serves the static files correctly but `/v1/...` and `/health` 404. The real backend is at `10.10.50.5:8000` (`/health` returned 200 + JSON during this session). For sessions that need to test against live data, point the preview at the remote box once the branch is rsynced/pushed there. **Stop and re-run a static server if the preview URL points at `chrome-error://chromewebdata/` after a reload — the previous one died.**
- **`/v1/pipeline/stats/events` returns at most 96 buckets (24 h / 15 min)** and is keyed by `timestamp` (Unix epoch) + `count`. Sometimes the backend will return fewer buckets if the event table is shallow. The chart renderer guards against `< 2` data points (reduces to empty state).
- **`/v1/pipeline/models` ships sub-objects keyed by service name (`ranker`, `collab_filter`, `session_embeddings`, `sasrec`, `session_gru`, `lastfm_cache`).** Each carries its own readiness flag — `trained` for the ML models, `built` for the FAISS / Word2Vec / cache types. The Models panel falls back to `built` when `trained` is missing. `latest_evaluation` and `impressions` live at the top level alongside the per-model objects, not inside `ranker`. Sessions 06–08 will need to surface those properly.
- **`/v1/stats` does not have a `top_tracks_1h` / `top_tracks_7d` etc.** It only emits `top_tracks_24h`. The range toggle currently affects the `state.range` field only; it does NOT narrow the data because there are no narrower queries available. Don't pretend otherwise.
- **`/v1/events?limit=4` returns the most-recent 4 events without auth-scoping; it's an admin endpoint.** Each event has at most: `timestamp`, `event_type`, `user_id`, `track_id`, optional `value`, optional `dwell_ms`, plus the rich-signal fields. The Recent events panel currently shows `track_id` raw because the response doesn't include resolved `title`/`artist`. Session 07 may want to enrich with a `track_id → title` lookup if the panel becomes a primary surface there.
- **`/v1/downloads/queue` returns `{ in_flight: [...], recent_completed: [...], recent_failed: [...] }`.** I count `in_flight.length`. Don't be surprised if the array carries enriched probe data (per-row backend status with a 2 s probe cache) — the activity pill ignores everything except the count.
- **`/v1/lidarr-backfill/stats` returns `enabled` and `tick_in_progress` flags.** Unless `enabled === true && tick_in_progress === true`, the activity pill skips the row. Don't repurpose `enabled` alone — the tick can be off while the feature is enabled.
- **Activity pill data-count attribute carries the count even when `> 99`.** No truncation. If a single user ever hits 100+ active jobs, the collapsed circle gets cramped. Acceptable for self-hosted.
- **`GIQ.sse.connect()` is a no-op when `apiKey` is missing,** but the bus still flips `state.sseConnected = false` and renders the topbar pill grey. So at boot before the user enters a key, the topbar correctly reads "SSE off" without throwing.
- **`outsideHandler` for the popover binds via `setTimeout(..., 0)`** so the click that opened the popover doesn't immediately close it. Don't refactor this into a synchronous `addEventListener`; it'll close the popover on its own opening click.
- **Population uses page-level state, not a cache.** Each `refreshAll` rebuilds the panels from scratch (clears `host.innerHTML` then appends a fresh panel). This keeps the code simple and prevents stale DOM children between renders. CPU cost is trivial for the data volumes involved.

## Open issues / TODOs

- The Event ingest area chart is currently single-series. When session 07 (System Health) lands the dual-series version using `/v1/pipeline/stats/activity` per event type, port the implementation back here — the realistic mockup has both series for a reason.
- The "Recent events" panel doesn't resolve track_id → title/artist. Worth enriching once session 09 has the track-lookup path warm.
- Library scan "phase X of Y" sub-line is approximated. The current `latest_scan` payload has `current_file` but not a structured "phase" object. Session 07's System Health surface may want to add it; for now Overview shows `phase · indexing` when running and `phase · preparing` when current_file is missing.
- The range toggle (1h/24h/7d/30d) is wired to UI state only. Capturing `state.range` is in place so sessions 07–08 can pick it up cheaply when they wire window params.
- Quick run rows currently deep-link to Actions sub-pages that are still session-01 stubs. Session 05 needs to make sure the destinations exist; the deep-links themselves don't need to change.

## Verification screenshots

Captured inline in the session transcript via `mcp__Claude_Preview__preview_screenshot` (the MCP returns base64 JPEGs, not file paths). Three relevant captures:

1. Empty state — populated panels with `connect API key to load data`, no live data. Confirms layout, topbar SSE off, activity pill idle ("idle · no active jobs").
2. Populated state — full Overview with mocked API responses: stat row showing `1.240.000 / 142 / 48.201 / 1024 / 2.847 / ready`, smooth area chart over 96 buckets, top tracks (Yann Tiersen, Ludovico Einaudi, Ólafur Arnalds, Bon Iver), event-types bars, models with READY / STALE chips, Library scan with LIVE badge + 65% bar + 4 mini-stats. Activity pill says "2 active · scan · 2 dl".
3. Activity popover open — 320 px popover above the pill, two rows: Library scan with LIVE badge + "65% · 14302 / 22000 · View →" link to System Health; 2 downloads + "in flight · View →" link to Downloads.
4. Bottom-of-page scroll — Recent events panel rendering 4 rows with mono timestamps, typographic event-type chips, lavender users; Library scan + Quick run panels.
5. Collapsed sidebar — single "g" logo, icon-only nav (Monitor active with the lavender bar), activity pill collapsed to circle with lavender "2".

(Re-create with the same `preview_eval` mock-injection used in the transcript if you need to capture again.)

## Time spent

≈ 105 min: reading prior docs / api shapes / app.js patterns (25) · components + SSE bus (20) · activity pill + popover (20) · Monitor → Overview render + CSS (30) · preview verification + screenshots (10).

---

**For the next session to read:** Session 03 — Settings: versioned-config shell + Algorithm, see [docs/rebuild/03-settings-config-shell-algorithm.md](docs/rebuild/03-settings-config-shell-algorithm.md). Sessions 05–08 also unblocked by this one (they depend on session 02). They can run in parallel tracks per the master plan.
