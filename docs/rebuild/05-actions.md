# Session 05 — Actions bucket

You are continuing the GrooveIQ dashboard rebuild. This is **session 05 of 12**.

This session builds the Actions bucket — 5 grouped pages following Shape B from the design hand-off. Each page lists its triggers as cards: name, description, optional state dot or "destructive" chip, "▶ Run" button, optional sub-line ("last run · 14m ago · ok"), optional jump to corresponding Monitor surface.

This session is **independent of sessions 03 / 04** — can run in parallel.

## Read first

1. `docs/rebuild/README.md`
2. All prior hand-offs (01–04 if done; at minimum 01 + 02).
3. `docs/rebuild/components.md` → no new shared components needed beyond what session 02 built; but you may add an `actionCard` helper.
4. `docs/rebuild/api-map.md` → entire "Actions" section.
5. `gui-rework-plan.md` § "New IA — Actions bucket" + "Splits requiring careful design treatment".
6. `design_handoff_grooveiq_dashboard/page-actions.jsx` — `ActionsGrouped` (Shape B) is the reference.
7. `design_handoff_grooveiq_dashboard/README.md` § 5 "Actions bucket shape".
8. The current implementations of each trigger in `app/static/js/app.js` — port behaviour, don't reinvent.

## Goal

User navigates to any of `#/actions/<group>` and sees:
- Page header (eyebrow ACTIONS, title group name).
- A vertical stack of action cards.
- Each card: name + description + state dot + "▶ Run" button. Optional last-run sub-line. Optional "destructive" chip.
- Clicking Run dispatches the appropriate API call, shows a toast, and (for actions with a Monitor surface) auto-redirects after 800ms.

The Settings → Lidarr Backfill page from session 04 has a cross-link rail pointing at Actions → Discovery → Lidarr Backfill Queue. That sub-surface gets built here as a page-with-table (not just card list) — see Group D below.

## Out of scope

- Live status of in-flight actions — that's Monitor (sessions 06–08).
- Any new triggers — only port what exists today.

## Tasks (ordered)

### A. `actionCard` helper

Build `GIQ.components.actionCard({ name, description, lastRun, destructive, onRun, monitorPath })`:
- Background `--paper`, border `1px solid --line-soft`, radius 10px, padding 14px.
- Top row: name (Inter 14/600) + state dot or `destructive` chip on the left; "▶ Run" primary button on the right.
- Description (12px `--ink-2`).
- Sub-line (mono 10px `--ink-3`) — e.g. "last run · 14m ago".
- `monitorPath` — if present, after Run dispatch, show a toast "{action} triggered. View in Monitor →" with jump.

### B. Actions → Pipeline & ML (`#/actions/pipeline-ml`)

Cards:
- **Run Pipeline** → `POST /v1/pipeline/run`. Monitor path `#/monitor/pipeline`. Sub-line shows last run time + status from `GET /v1/pipeline/status?limit=1`.
- **Reset Pipeline** → confirmation modal ("Reset clears all pipeline state and rebuilds from raw events. Continue?") → `POST /v1/pipeline/reset`. Monitor path `#/monitor/pipeline`. Mark destructive.
- **Backfill CLAP** → `POST /v1/tracks/clap/backfill`. Sub-line shows pending count from `GET /v1/tracks/clap/stats`.
- **Cleanup Stale Tracks** → confirmation → `POST /v1/library/cleanup-stale?dry_run=false`. Mark destructive. Optional dry-run toggle inline.

### C. Actions → Library (`#/actions/library`)

Cards:
- **Scan Library** → `POST /v1/library/scan`. Monitor path `#/monitor/system-health` (scroll/anchor to scan section).
- **Sync IDs** → `POST /v1/library/sync`. No Monitor path (toast on completion suffices).

### D. Actions → Discovery (`#/actions/discovery`)

This page has more than just trigger cards — it's the operator surface for queue management.

Top section: 4 trigger cards.
- **Lidarr Discovery** → `POST /v1/discovery/run`. Monitor `#/monitor/discovery`.
- **Fill Library** → `POST /v1/fill-library/run`. Monitor `#/monitor/discovery`.
- **Soulseek Bulk** → trigger UI (max-artists, tracks-per-artist inputs) + Start/Cancel buttons. Behaviour from current code under `// Discovery > Soulseek Bulk Download`.
- **Run Lidarr Backfill (now)** → `POST /v1/lidarr-backfill/run`. Monitor `#/monitor/lidarr-backfill`.

Below the trigger cards, a **Lidarr Backfill Queue** sub-section. Spec:
- Cross-link rail at top: links to Settings (edit config) and Monitor (live stats).
- Header: title "Lidarr Backfill Queue", right-side button row Pause / ▶ Run now.
- Filter chips: All · Queued · In flight · Failed (counts from `GET /v1/lidarr-backfill/stats`).
- Queue table: artist · album · state chip (mono 9px uppercase, `--accent` bg for in flight, `--wine` for failed, outline-only for queued) · score (mono) · per-row actions (Retry · Skip · Forget). Endpoints: `GET /v1/lidarr-backfill/requests`, per-row `POST .../retry`, `POST .../skip`, `DELETE .../{id}`.
- Bulk actions: "Clear failed" / "Clear no_match" / "Clear permanently_skipped" buttons → `POST /v1/lidarr-backfill/requests/reset` body `{"scope":"failed"|...}`.

This page is the "Lidarr Backfill triple split" Actions third — see `gui-rework-plan.md`.

### E. Actions → Charts (`#/actions/charts`)

Card: **Build Charts** → `POST /v1/charts/build`. Monitor path `#/monitor/charts`. Sub-line from `GET /v1/charts/stats` last_built_at.

### F. Actions → Downloads (`#/actions/downloads`)

This is the operator tool for ad-hoc multi-agent searches.

- Top: search input + backend checkboxes (default selection from `GET /v1/downloads/routing` parallel_search_backends) + timeout input + Search button.
- Search → `GET /v1/downloads/search/multi?q=&limit=&backends=&timeout_ms=`.
- Results section: heterogeneous results grouped by backend. Each result row: title + artist + bitrate / quality + download handle + per-row "Download via {backend}" button → `POST /v1/downloads/from-handle` with the handle.
- After download dispatch: toast + jump to `#/monitor/downloads`.
- Below the results: a small "Recent ad-hoc downloads" panel showing the last 10 from `GET /v1/downloads?limit=10`.

### G. Wire deep-links from Monitor → Overview's Quick Run panel

The Quick Run rows in session 02's Overview panel link to Actions stubs. Now they link to real pages. Verify the four Quick Run rows resolve correctly.

## Verification

1. Load each `#/actions/<group>` page. Cards render. Run buttons work end-to-end.
2. Click Run Pipeline → toast → auto-redirects to `#/monitor/pipeline` after 800ms.
3. Click Reset Pipeline → confirmation → on confirm, `POST /v1/pipeline/reset` fires (verify in network tab) → toast.
4. Trigger a Soulseek bulk download — verify the inputs are sent correctly.
5. Search & Download: enter a query, verify multi-backend results render, click "Download via spotdl" on a result, verify `/v1/downloads/from-handle` is called with the right backend handle.
6. Lidarr Backfill Queue: filter chips work, per-row Retry / Skip / Forget update the row state, bulk Clear failed empties failed rows.

## Hand-off

Write `handoffs/05-actions.md`. Note any quirks of the multi-search response shape (which fields are common vs backend-specific) — useful for future polish. Note the dry-run cleanup-stale toggle decision.

Commit: `rebuild: session 05 — actions`.
