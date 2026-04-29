# Session 02 — Monitor → Overview at full fidelity + activity pill wiring

You are continuing the GrooveIQ dashboard rebuild. This is **session 02 of 12**.

This session builds the canonical landing page (Monitor → Overview) at high fidelity — the visual benchmark for everything that follows — and wires the activity pill to real data so it works on every page.

## Read first

1. `docs/rebuild/README.md`
2. `docs/rebuild/handoffs/01-foundation.md` ← prior session's hand-off note. Verify state matches; if not, stop and ask.
3. `docs/rebuild/conventions.md`
4. `docs/rebuild/components.md` — especially **Stat tile**, **Panel**, **LIVE badge**, **SSE bus**.
5. `docs/rebuild/api-map.md` → "Monitor → Overview" row.
6. `design_handoff_grooveiq_dashboard/page-realistic.jsx` — **canonical reference**. Match layout, spacing, colours, typography exactly.
7. `design_handoff_grooveiq_dashboard/README.md` § "Monitor → Overview (HIGH FIDELITY — match exactly)".

## Goal

The user navigates to `#/monitor/overview` and sees the full Overview page populated with real data:
- Header (eyebrow MONITOR + title + range toggle 1h/24h/7d/30d + last-update timestamp).
- 6 stat tiles in a row (Events, Users, Tracks, Playlists, Events/hr, Ranker).
- Two-column body (`2fr 1fr` grid):
  - **Left:** Event ingest area chart panel · Top tracks + Event types two-up · Recent events panel with LIVE badge.
  - **Right:** Models panel (6 rows) · Library scan panel with LIVE badge + progress bar + 4 mini-stats · Quick run panel (4 rows linking to Actions).
- Activity pill in the sidebar polls and shows real running jobs (pipeline / scan / downloads). Click to expand the 320px popover with deep-links to Monitor surfaces.
- SSE pill in the topbar reflects real connection state.

## Out of scope

- Other Monitor surfaces (Pipeline, Recs Debug, etc.) — they remain stubs from session 01. Wired in sessions 06–08.
- Mobile responsive (handled in session 12).
- Light mode.

## Tasks (ordered)

### A. Shared components (build once, used everywhere)

1. **`GIQ.components.statTile({ label, value, delta, deltaKind })`** — see `components.md → Stat tile`. Add styles to `components.css`.
2. **`GIQ.components.panel({ title, sub, action, badge, children })`** — see `components.md → Panel`. Children is a DOM element; append to body slot.
3. **`GIQ.components.liveBadge()`** — see `components.md → LIVE badge`.
4. **`GIQ.components.pageHeader({ eyebrow, title, right })`** — left side eyebrow + Inter Tight 26/600 title; right side accepts a DOM node for controls (range toggle / buttons / etc.).
5. **`GIQ.components.rangeToggle({ values, current, onChange })`** — `1h / 24h / 7d / 30d`-style segmented control. Active item: `--paper-2` background, weight 600, `--ink` text. Idle: transparent, weight 400, `--ink-3`.
6. **`GIQ.sse`** — single subscription bus to `/v1/pipeline/stream` (auth via header — use `fetch` with ReadableStream, not `EventSource`, since EventSource doesn't allow custom headers). Methods: `subscribe(eventName, handler) → unsubscribe`, `connect()`, `disconnect()`, `isConnected()`. Auto-reconnect with exponential backoff, max 30s. Emit `connected` / `disconnected` events for the SSE pill.
7. **`GIQ.toast(msg, kind, duration?)`** — port from old `app.js:notify(...)`. Bottom-right stack.

### B. Activity pill (lives on every page from now)

1. **`GIQ.components.activityPill()`** — render the pill in the sidebar (or whatever location `shell.js` uses). Two states: collapsed sidebar (circle with count) and expanded sidebar (full pill with "3 active" + sub-line + chevron).
2. **Data source.** Build a client-side aggregator `GIQ.activity.poll()` that calls these every 5s and merges:
   - `GET /v1/pipeline/status?limit=1` — running pipeline run if any.
   - `GET /v1/library/scan/active` (if exists; else iterate latest scan IDs) — verify endpoint by inspecting current `app/static/js/app.js`.
   - `GET /v1/downloads?status=in_flight&limit=20` — count in-flight downloads.
   - `GET /v1/lidarr-backfill/stats` — running backfill flag if cfg.enabled.
3. **Popover.** On click, render a 320px-wide popover above the pill with one row per running job. Each row: icon, label, sub-text (e.g. "step 4 of 10 · scoring"), optional LIVE badge, "View →" deep-link to the relevant Monitor surface. Close on outside click or ✕.
4. **Deep-links.** Pipeline run → `#/monitor/pipeline`. Scan → `#/monitor/system-health`. Downloads → `#/monitor/downloads`. Backfill → `#/monitor/lidarr-backfill`.
5. **Hook to SSE.** When `GIQ.sse` emits `pipeline_start` / `pipeline_end` / `step_complete`, refresh the activity pill immediately rather than waiting for the next 5s poll.

### C. Monitor → Overview page

Render function `GIQ.pages.monitor.overview` in `monitor.js`. Build the page:

1. **Page header** — eyebrow MONITOR, title "Overview", right side: "last update · 2s ago" mono `--ink-3` + range toggle (1h / 24h / 7d / 30d, default 24h).
2. **Stat row** — `display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px`. Six tiles wired to `GET /v1/stats`:
   - Events: `total_events` value, `+events_last_24h` delta, kind `good`.
   - Users: `total_users` value, `+new_users_this_week` delta if available, else flat.
   - Tracks: `total_tracks_analyzed` value, delta `+412 / week` if available.
   - Playlists: `total_playlists` value.
   - Events / hr: `events_last_1h` value, delta direction relative to the 24h average.
   - Ranker: tile shows `"ready"` value (or `"not trained"`), delta `ndcg X.XXX` from `GET /v1/pipeline/models`.
3. **2-col body** — `display: grid; grid-template-columns: 2fr 1fr; gap: 14px`.
4. **Left col panels:**
   - **Event ingest panel.** Title "Event ingest", sub mono "play_end · like · skip · pause · etc · last 24h · 5m bins", action "View full breakdown →" jumping to `#/monitor/system-health`. Body: smooth area chart (SVG, dual series — total events lavender stroke 2px + gradient fill `#a887ce` 0.45→0; engagement = likes+plays-through wine stroke 1.5px @ 0.7 opacity + gradient fill `#9c526d` 0.35→0). Three horizontal gridlines at 25/50/75% in `rgba(236,232,242,0.04)` dasharray `2 4`. Below: 7 axis timestamps in mono 9px. Legend: two coloured 8×8 squares + label. **Bezier smoothing** between points (cubic). Data from `GET /v1/pipeline/stats/events`.
   - **Top tracks + Event types two-up.** `grid-template-columns: 1.1fr 1fr; gap: 14px`.
     - Top tracks panel: rank chip (22×22 `--paper-2`, mono number), track + artist (track 12px weight 500, artist 11px `--ink-3`), mini bar 60×3 lavender on `--line-faint`, play count mono right-aligned 32px wide. Row padding `7px 0`, divider `--line-faint`. Use `/v1/stats` `top_tracks_24h` field, fallback to a separate endpoint if missing.
     - Event types panel: horizontal bar list. Each row: label (mono 10px), value (mono 10px `--ink-3`), 5px-tall bar with filled bar. Bar colour: `--accent` for play_end/like, `--ink-3` for skip/pause/volume, `--wine` for dislike. Data from `/v1/stats` `event_types_24h`.
   - **Recent events panel** with LIVE badge. Title "Recent events", sub "live tail · 4 most recent". Each row: timestamp mono 60px, event-type chip (mono 9px uppercase, faint outline, 70px centered, never colour-filled), user (lavender 50px), track (truncated), duration mono right. Refresh every 5s via `GET /v1/events?limit=4`.
5. **Right col panels:**
   - **Models panel.** Title "Models", sub "readiness · 6 surfaces", action "See all →" jumping to `#/monitor/models`. 6 rows from `GET /v1/pipeline/models`. Each row: 7px dot (lavender for ready, wine for stale), name + sub-line mono 10px, state chip mono 9px uppercase right-aligned (`--accent` for ready, `--wine` for stale).
   - **Library scan panel** with LIVE badge (only when scan running). Title "Library scan", sub "phase X of Y · {phase_label}". Body: progress label + percent (mono lavender), 6px-tall progress bar with linear gradient `--paper-2`→`--accent`. 2×2 mini-stat grid below: Found · New · Updated · Removed. Each cell: `rgba(236,232,242,0.04)` background, radius 6px, padding `8px 10px`, mono eyebrow + Inter Tight 16/600 value. If no scan active, show "No scan running" + a "Start scan →" link to Actions → Library.
   - **Quick run panel.** Title "Quick run", sub "jumps to Actions". 4 rows: Run pipeline, Scan library, Build charts, Backfill CLAP. Each row: name + sub-line ("14m ago · ok" or "running"), arrow → `--accent` on the right. Click jumps to `#/actions/...` (stub for now — those Action pages land in session 05).

### D. SSE pill in topbar

Wire to `GIQ.sse`. Lavender pulse "SSE live" when `GIQ.sse.isConnected()`; grey "SSE off" when not. Connect on app boot (fire-and-forget); no UI to disconnect manually.

## Verification

1. Run `preview_start` against the dev server and load `/static/dashboard-v2.html#/monitor/overview`.
2. **Compare side-by-side** with `design_handoff/page-realistic.jsx` rendered (open `GrooveIQ Wireframes.html` if it renders the realistic page). Match: stat row layout, chart smoothing, panel spacing, LIVE badges, mono-uppercase typographic chips (no colour fill).
3. Take screenshots of the full page and save under `docs/rebuild/handoffs/screenshots/02-overview-{full,top,bottom}.png` (or wherever — note in hand-off).
4. Click a sub-page tab and back; verify the cleanup function disconnected polling timers (no leaked intervals; check console).
5. Trigger a pipeline run from the old `/dashboard` (in another tab) and verify the activity pill on the new dashboard reflects it within 5s.
6. Click the activity pill → popover opens. Click "View →" on the pipeline row → URL becomes `#/monitor/pipeline` (still stub).

## Hand-off

Write `docs/rebuild/handoffs/02-overview.md` per template. Note any data-shape surprises from `/v1/stats`, `/v1/pipeline/models`, `/v1/events` — sessions 06–08 will need to know.

Commit: `rebuild: session 02 — monitor overview + activity pill`.

## Tip

The smoothest area chart is built with cubic Bézier (`C cx1 cy1, cx2 cy2, x y`) between points; see `page-realistic.jsx:RealAreaChart`. Lift that approach.
