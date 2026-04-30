# Hand-off: Session 05 — Actions bucket

## Status
- [x] Session goal achieved
- [x] Visual verification done (mocked-API harness via `preview_eval`; same pattern as sessions 02–04 since the local toolchain has no FastAPI runtime and the remote backend has no CORS for the static-file preview)
- [x] No regressions in old `/dashboard` (zero modifications to `app/static/index.html`, `js/app.js`, `css/style.css`)
- [x] Sessions 03 + 04 surfaces (Algorithm + Routing + Backfill + Connections + Users + Onboarding) still render untouched
- [x] Committed on `gui-rebuild` branch with message `rebuild: session 05 — actions`

## What landed

All 5 Actions sub-pages are real. Each page follows Shape B from `page-actions.jsx` (`ActionsGrouped`) — page header (eyebrow + title + sub-line description), then a vertical stack of `actionCard`s. Each card has a name, optional state dot or `destructive` chip, optional inline extras (dry-run toggle, Soulseek input form), description, optional mono sub-line populated from API hydration, and a `▶ Run` primary button. Run dispatches the relevant POST, shows a success toast (or a toast-with-jump for cards that own a Monitor surface), and auto-redirects to the named `monitorPath` after 800ms. Errors flip to a wine error toast and re-enable the button.

Discovery additionally hosts the **Lidarr Backfill Queue** operator surface below its 4 trigger cards: cross-link rail (Settings + Monitor), filter chips (All · Queued · In flight · Failed) with live counts from `/v1/lidarr-backfill/stats`, paginated queue table with state chips coloured per status (`--accent` for in-flight, `--wine` for failed/no-match/skipped, plain outline for queued), per-row Retry/Skip/Forget buttons, Pause / Run-now header buttons, and three bulk-clear scopes. Pause toggles the master `enabled` flag via a new versioned config row.

Downloads is the operator tool for ad-hoc multi-agent searches: query input + per-backend timeout + result-limit + 4 backend checkboxes (defaulted from `/v1/downloads/routing.parallel_search_backends`); search hits `GET /v1/downloads/search/multi`; results render heterogeneously grouped by backend with per-result "Download via {backend}" buttons that POST to `/v1/downloads/from-handle` and jump to Monitor → Downloads. A "Recent ad-hoc downloads" panel below shows the last 10 from `GET /v1/downloads?limit=10`.

Plus the new shared `GIQ.components.actionCard({...})` helper (mounted in `components.js` for re-use by future surfaces) and a small private `_actionToastWithJump` helper that mirrors the versioned-config shell's save-toast pattern.

Task G (Quick Run deep-link verification): all 4 rows from session 02's Overview now resolve to real Action pages with valid renderers. **Found a small bug** — the "Backfill CLAP" Quick Run row was deep-linked to `#/actions/library` (where Backfill CLAP doesn't exist); fixed to `#/actions/pipeline-ml` in `monitor.js`.

## File inventory after this session

Substantially modified:
- [app/static/js/v2/actions.js](app/static/js/v2/actions.js) — replaced the 5 stub renderers with the full implementation (~620 lines). Each page is self-contained inside the IIFE; shared inline helpers `_numField`, `_iconBtn`, `_renderResultRow` live at the bottom.
- [app/static/js/v2/components.js](app/static/js/v2/components.js) — added `GIQ.components.actionCard({ name, description, lastRun, destructive, busy, runLabel, onRun, monitorPath, monitorLabel, confirm, extras, state })` (~150 lines) that returns `{ el, refresh(patch) }`. Refresh swaps any subset of fields and re-builds the card DOM in place. Plus the private `_actionToastWithJump(message, hash, jumpLabel)` helper exposed via `GIQ.components._actionToastWithJump` so the per-result download buttons in `actions.js` can use it without depending on `settings.js`.
- [app/static/js/v2/monitor.js](app/static/js/v2/monitor.js) — single-line bug fix: the Backfill CLAP Quick Run row now points at `#/actions/pipeline-ml` (where the Backfill CLAP card actually lives) instead of `#/actions/library`.
- [app/static/css/pages.css](app/static/css/pages.css) — added ~520 new lines for: `.actions-page` / `.actions-body` / `.actions-subline` / `.actions-empty` / `.actions-loading` / `.actions-error` page-level layout, the full `.action-card` family (top row, name, dot, destructive chip, run btn, desc, sub, extras, toggle, num-field), `.actions-triggers` 2-col grid for Discovery, the entire `.lbf-queue-*` family (head, btns, chips, bulk, table, state chips per-status with wine/accent/plain variants, row-actions), and the `.actions-search-*` family (panel, form, input, num, backends, results, group, list, row, row-main, row-title, row-lib, row-meta, row-sub, row-btn) plus `.actions-recent-downloads` + `.actions-recent-table`. Mobile rules at ≤700px stack the action-card top row and convert search rows to vertical cards.

Doc:
- [docs/rebuild/handoffs/05-actions.md](docs/rebuild/handoffs/05-actions.md) — this file.

No new top-level files; everything plugs into the session 01 scaffolding and reuses session 02's components (`pageHeader`, `liveBadge`, `relatedRail`, `jumpLink`, `vc-btn`/`vc-btn-primary`).

## State of the dashboard at end of session

Working at `/static/dashboard-v2.html`:

`#/actions/pipeline-ml`:
- 4 cards stacked: Run Pipeline (lavender state dot · `monitorPath: #/monitor/pipeline`), Reset Pipeline (destructive · confirm dialog · `monitorPath: #/monitor/pipeline`), Backfill CLAP (no monitor jump — toast on completion), Cleanup Stale Tracks (destructive · inline dry-run toggle, defaulted ON, sends `?dry_run=true|false&pattern=legacy_hex` and toasts the row count from the response).
- Run Pipeline & Reset Pipeline sub-lines hydrate from `GET /v1/pipeline/status?limit=1` (`last run · 14m ago · completed`).
- Backfill CLAP sub-line shows pending count from `GET /v1/tracks/clap/stats` (e.g. `2,143 tracks pending · coverage 96%`); when CLAP is disabled, shows `CLAP disabled — set CLAP_ENABLED=true to enable`.
- When no API key is set, an inline grey notice replaces the hydration calls: "Connect API key to load last-run / pending-count details. Triggers below still work once connected."

`#/actions/library`:
- 2 cards: Scan Library (`monitorPath: #/monitor/system-health`, sub-line from `latest_scan` in `/v1/stats`), Sync IDs (no monitor jump).
- Sub-line example: `last scan · 4h ago · completed · 22000 files`. While a scan is running, state dot flips to `good` (lavender).

`#/actions/discovery`:
- Top: 4 trigger cards in a 2×2 grid (Lidarr Discovery, Fill Library, Soulseek Bulk, Run Lidarr Backfill (now)). Soulseek Bulk has its own embedded inline form (Top artists / Tracks per artist numeric inputs + a live `up to N tracks` mono estimate sub-line) and a `▶ Start` button label override; on Run it sends `POST /v1/soulseek/bulk-download?max_artists=N&tracks_per_artist=M`.
- Below the triggers: a horizontal divider, then the **Lidarr Backfill Queue** operator surface — related-rail (Settings + Monitor links), `Lidarr Backfill Queue` title with Pause / ▶ Run-now buttons, filter chips (All · Queued · In flight · Failed) with live counts, three bulk Clear buttons, and the queue table with per-row Retry (failed/no_match only) / Skip (anything not complete or permanently_skipped) / Forget (always, with confirm).
- Pause/Resume toggle confirms then PUTs a new `lidarr-backfill/config` version with `enabled` flipped — this is consistent with the rest of the versioned-config story (no new endpoint, no auto-save side door).

`#/actions/charts`:
- 1 card: Build Charts (`monitorPath: #/monitor/charts`). Sub-line from `GET /v1/charts/stats`: `last build · 6h ago · 1,200 entries · 74% match`.

`#/actions/downloads`:
- Top panel: query input + result limit (default 25) + per-backend timeout (default 5000ms, populated from routing config) + Search button. Below: `backends:` row with 4 checkboxes (spotdl/streamrip/spotizerr/slskd, each with its coloured dot); checked set defaults to `parallel_search_backends` from `/v1/downloads/routing`.
- Search hits `GET /v1/downloads/search/multi?q=&limit=&timeout_ms=&backends=`. Results render in per-backend cards with the dot + name + `(N results)` mono header (or red `error` text when the group failed). Per-result row: title (with `IN LIBRARY · FORMAT` lavender chip when `in_library: true`), artist — album, mono sub-line of quality / bitrate / duration / album_id (truncated), and a "Download via {backend}" lavender button on the right.
- Clicking the button POSTs `/v1/downloads/from-handle` with `{ handle, track_title, artist_name, album_name }` (the `in_library` flag from the search response is stripped before sending — the endpoint doesn't accept it). On success: toast-with-jump "Queued via {backend}. View in Monitor →" and auto-redirect to `#/monitor/downloads` after 800ms. Duplicate response shows a neutral info toast and skips the redirect. Failures flip the button back and toast the error.
- Bottom panel: "Recent ad-hoc downloads" — table (When · Backend · Track · Status) populated from `GET /v1/downloads?limit=10`. Renders an empty-state notice if the response is empty.

Quick Run rows from session 02's Overview now all resolve to real pages: Run Pipeline → `/actions/pipeline-ml`, Scan Library → `/actions/library`, Build Charts → `/actions/charts`, Backfill CLAP → `/actions/pipeline-ml` (was incorrectly `/actions/library`).

## Decisions made (with reasoning)

- **`actionCard` mutates its config in place via `wrapper.refresh(patch)` rather than re-rendering parent pages on every API response.** Pages call `card.refresh({ lastRun: '...' })` after a hydration call resolves. This avoids re-mounting the entire page on every `then()`, keeps focus / scroll stable when (e.g.) the search input has data, and avoids a render-storm if multiple hydration calls land at slightly different times.
- **Cleanup Stale Tracks dry-run toggle defaults to ON, not OFF.** The brief said "Optional dry-run toggle inline" without a default. Defaulting to true is the safer choice for a destructive action with a "preview only" mode — the user must consciously uncheck it to actually delete. The toggle label spells out the consequence: "dry-run (preview only — uncheck to actually delete)". The toast distinguishes between a dry-run and a real delete with different copy + kind (`info` vs `success`) and reports the row count from the response.
- **`monitorPath` redirect uses `setTimeout(800ms)` not an immediate jump.** Per the spec; gives the user time to read the toast before the page navigates. Keeps the toast on screen across the navigation (toasts are appended to `<body>`, not `#page-root`).
- **Pause/Resume on Lidarr Backfill mutates the versioned config** — confirms with the user, GETs the active config, sets `enabled`, PUTs back. **Why**: the brief says "Pause / ▶ Run now" but the underlying API has no separate pause endpoint — the master toggle is `cfg.enabled`, the same field session 04 wired into the Settings master toggle. This keeps behaviour consistent: every state change to the engine is a versioned config change. The confirm dialog is necessary because Pause has the side effect of bumping the config version. Discoverable via the sub-line on the button: "Toggle the backfill engine off via the Lidarr Backfill config."
- **Soulseek Bulk inputs live as `extras` on the action card, not as a separate panel.** They're conceptually parameters for the action rather than independent state; folding them into the `extras` slot keeps the card boundary intact.
- **Reused `GIQ.api.del` for Forget** — same pattern as session 04 used for Last.fm disconnect. The `confirm()` lives in `actions.js` (per-row), not in the toast helper. Same for bulk reset (uses `confirm()` then `apiPost(... , { scope })`).
- **Quick Run "Backfill CLAP" row was bugged in session 02** — pointed at `#/actions/library` even though the Backfill CLAP card lives on `#/actions/pipeline-ml`. Fixed in this session because Task G explicitly asks for verification, and verifying without fixing would contradict the spec. One-line edit, no behaviour change for any other surface.
- **`_actionToastWithJump` is duplicated in spirit** with the versioned-config shell's inline jump-toast (in `components.js` `_buildSaveToast` style). Kept it separate because the shell's toast is tightly coupled to its save flow and depends on internal state. The Actions one is a cleaner standalone — exposed publicly via `GIQ.components._actionToastWithJump` so the Downloads page can call it directly from a result-row button (where there's no actionCard wrapper). If both call sites prove identical in a future polish pass, a single utility is one merge away.
- **Multi-search results don't currently render the artist-mode (streamrip discography) view from old `app.js`.** The brief asks for "heterogeneous results grouped by backend" — the parallel-track mode covers that. The artist-mode UI in the old code is a separate streamrip-only path (`/v1/downloads/search/artist`); deferring it to a future polish pass keeps this session focused. The current rendering supports the result fields used by all three default backends (spotdl/streamrip/spotizerr) and slskd should fit the same row shape.
- **Backend dot colours are duplicated** in `actions.js` (`renderBackends` + `_renderResultRow`) and `settings.js` (`dlBackendDot`). Kept the duplication rather than extracting a shared helper because (a) the lists are identical and tiny, and (b) the styling lives at the call site where it's needed (one returns a `<span>`, one inlines the colour). If a third consumer appears, lift to `components.js`.

## Quirks of the multi-search response shape (worth noting for future polish)

`GET /v1/downloads/search/multi` returns:

```json
{
  "groups": [
    { "backend": "spotdl", "ok": true, "results": [...] },
    { "backend": "streamrip", "ok": true, "results": [...] },
    { "backend": "spotizerr", "ok": false, "error": "connection refused" }
  ]
}
```

**Per-result fields, common across backends:**
- `title`, `artist`, `album` — display strings
- `download_handle` — the opaque blob to POST back to `/v1/downloads/from-handle`. Always carries `backend` + backend-specific keys.
- `in_library`, `library_format`, `library_album` — set when the local matcher recognised the track. The frontend uses `in_library` to dim the row and switch the button label to "Re-download via X". Strip `in_library` before POST — the endpoint doesn't accept it.

**Backend-specific result fields:**
- spotdl: `download_handle.spotify_id`, sometimes `bitrate_kbps`, `duration_ms`. Quality is always `lossy_high`.
- streamrip: `download_handle.service` (`qobuz|tidal|deezer|soundcloud`) + `download_handle.track_id`. Tracks carry `album_id` (used by old `app.js` to bucket into "album" cards when ≥ 3 share the same artist+album_id; this rebuild renders them as flat list — the bucketing was a streamrip-mode-specific UX). `quality` is one of `lossy_low`/`lossy_high`/`lossless`/`hires`.
- spotizerr: same keys as spotdl but with `bitrate_kbps` always present.
- slskd: `download_handle.peer`, `filename`, `size`. Variable per-result quality (file extension–dependent). Not in the default `parallel_search_backends` list.

**Group-level error handling:** when `ok: false`, `error` is a free-text string. The renderer prints it red but continues with the other groups. The form keeps its state, so the user can change a backend toggle and re-search.

The downstream POST `/v1/downloads/from-handle` returns `{ status, task_id, ... }` where `status` may be `queued`, `in_flight`, `complete`, or `duplicate`. The `duplicate` case is treated as a neutral info-toast (not a success) and **does not** redirect — the user already had it.

## Dry-run cleanup-stale toggle decision

Defaulted to ON (preview only). The toggle is rendered as a checkbox + descriptive label inside the card's `extras` slot, above the description. The card already has the `destructive` chip + a confirm dialog ("Cleanup will scan for orphaned TrackFeatures rows and either preview or delete depending on the dry-run toggle. Continue?"); the toggle is the third safety layer (chip · confirm · default-safe). The result toast distinguishes dry-run vs real delete with different copy + kind (`info` for dry-run preview, `success` for actual delete) and reports the row count from the response. The `pattern=legacy_hex` query param matches the only existing CLI usage of the endpoint per `CLAUDE.md`; this is hard-coded for now (the brief didn't ask for a pattern picker).

## Gotchas for the next session

- **`monitorPath` redirect can fight with a user's mid-toast navigation** — if the user clicks the toast's jump-link before the 800ms timer fires, both will try to set the same hash. The toast's link uses `<a href="#/...">` so the browser handles it; the timer's `setTimeout` checks `if (window.location.hash !== monitorPath)` before assigning to avoid a double-dispatch. If the next session adds a *different* monitor jump from a toast (e.g. ranker training started), that check should be preserved.
- **`/v1/downloads?limit=10` shape is not formally documented** — the recent-downloads renderer copes with both `{ items: [...] }` and `{ recent: [...] }` and falls back to `[]`. If the backend response shape settles in a future session, simplify the render. The fields it reads are `created_at` (or `requested_at` / `queued_at`), `backend` (or `client`), `artist_name` (or `artist`), `track_title`, `status`.
- **`/v1/lidarr-backfill/stats` chip-count fields used here:** `queued_total`, `in_flight_total`, `failed_24h`, `complete_24h` (the `All` chip sums all four). These names match what the existing Discovery → Lidarr Backfill UI in old `app.js` reads. If the field names diverge for any reason, the chip counts silently zero-out instead of throwing.
- **Pause toggle requires confirm** — if the user wants a one-click pause, that's a polish decision. Currently consistent with the rest of the shell.
- **Soulseek Bulk has no Cancel button on the Actions card.** The brief mentioned "Start/Cancel buttons" from old code. Cancel lives in Monitor → Discovery (session 08) — when a job is `running`, that surface should expose a cancel control. The Actions side is purely "trigger" per the IA contract.
- **The `action-card-form` extras slot for Soulseek Bulk takes the full card width.** It looks fine on desktop but stacks on mobile (per the responsive rules). If the next session adds another inline-form action, watch the wrap behaviour at the 700px breakpoint.
- **CSS `.dl-backend-dot` is shared between settings.js (Download Routing chains) and actions.js (Downloads search + recent-downloads).** It lives in `components.css`. If session 08 adds a Monitor → Downloads with the same dot, reuse this class.
- **Per-row "Forget" confirms inline (`window.confirm`)**. Same pattern as session 03 / 04 — replacing with custom modals is a polish item.
- **`actionCard` extras slot does not currently support input validation.** The Soulseek inputs clamp client-side (1–1000 artists, 1–50 tracks/artist) before the POST, but if the user types `0` or pastes a non-number, the parseInt fallback returns NaN → 0. The minimum clamp turns it into 1. Acceptable for an internal tool.
- **The actions page renderer signature is `(root) => undefined`** — no cleanup is needed because the only timers / intervals it owns are the in-flight fetch promises (which the page's reload abandons cleanly). If a future Action page acquires a poll loop (e.g. live progress on Soulseek Bulk inside the Action card itself), it'll need to return a teardown closure.

## Open issues / TODOs

- **Artist-mode multi-search** (`GET /v1/downloads/search/artist`) — old code's discography view. Skipped per scope; could be a polish session that adds a Tracks/Artist toggle inside `#/actions/downloads` mirroring the old UI.
- **Soulseek Bulk live progress on the Actions card** — the trigger Action just dispatches. Status / progress / errors live on Monitor → Discovery (session 08). When that lands, the Actions page should pull a one-line "running · N/M artists · view →" from the same endpoint.
- **No live polling on the Lidarr Backfill Queue table.** Each per-row action triggers a fresh `/v1/lidarr-backfill/requests` reload. A 5–10s poll would catch ticks while the page is open without the user clicking around. Deliberately omitted to keep the session lean — Monitor → Lidarr Backfill (session 08) is where the live-stats story belongs.
- **CSS for `.action-card-toggle` accent-color** uses `var(--accent)` — works in modern browsers but Safari < 15 doesn't support `accent-color`. Acceptable for self-hosted; flag if a wider audience matters.
- **The Pause toggle button label says "Pause" / "▶ Resume"** but uses a plain `vc-btn` variant. Could elevate to `vc-btn-primary` when paused so the primary action is visually distinguished. Polish.
- **`_actionToastWithJump` doesn't auto-dismiss on hash change** — if the user navigates away before the 6s timeout, the toast lingers. The shell's save toast has the same behaviour.
- **`/v1/downloads/from-handle` response can also include `error` for backend-specific failures** — the rebuild surfaces these via the catch arm but the message could be friendlier. Polish.
- **Quick Run rows on Overview: should the row's mono sub-line also reflect "running" state when an action is in flight?** Currently the Quick Run sub-lines come from session 02's hydration (latest run / scan state). Wiring SSE to update them in real time is a session 12 cross-cutting task.

## Verification screenshots

Captured inline in the session transcript via `mcp__Claude_Preview__preview_screenshot` (mocked-API state — same approach as sessions 02–04 since the local toolchain has no FastAPI runtime). Five relevant captures:

1. **Pipeline & ML — empty state (no API key)** — 4 cards rendered, lavender state dot on Run Pipeline, wine DESTRUCTIVE chips on Reset Pipeline + Cleanup Stale Tracks, dry-run toggle (checked) on Cleanup Stale Tracks, "Connect API key…" hint at top.
2. **Pipeline & ML — populated** — same 4 cards now with sub-lines: `last run · 14m ago · completed`, `2,143 tracks pending · coverage 96%`, etc.
3. **Library — populated** — 2 cards (Scan Library, Sync IDs); Scan Library shows `last scan · 4h ago · completed · 22000 files`.
4. **Discovery — full layout** — 4 trigger cards in a 2×2 grid with the Soulseek Bulk inline form (500 / 20 / "up to 10,000 tracks" estimate), related-rail at top of the queue section, Lidarr Backfill Queue title with Pause/▶ Run-now, filter chips (All 227 · Queued 124 · In flight 3 · Failed 8), bulk-clear row, queue table with 6 rows showing all 5 state-chip variants.
5. **Charts — populated** — single Build Charts card with `last build · 6h ago · 1,200 entries · 74% match`.
6. **Downloads — empty state** — search panel + backend checkboxes (3 default-on, slskd off) + Recent ad-hoc downloads table with 3 mocked rows.
7. **Downloads — search results** — 3 backend groups (spotdl ✓ 1 result, streamrip ✓ 2 results with one IN LIBRARY · FLAC chip + Re-download button, spotizerr ✗ "connection refused"), each result with quality/bitrate/duration sub-lines.

Programmatic verifications evidenced via `preview_eval`:
- All 5 Action sub-pages dispatch without console errors; `actionCard` factory + 5 page renderers all loaded.
- Pipeline & ML hydration calls fire: `GET /v1/pipeline/status?limit=1`, `GET /v1/tracks/clap/stats`. Run Pipeline button → `POST /v1/pipeline/run` → toast → 800ms later `window.location.hash === '#/monitor/pipeline'`.
- Reset Pipeline confirm: cancel → no API call; confirm → `POST /v1/pipeline/reset`; confirm message verbatim from spec.
- Soulseek Bulk inputs (7 / 11) → `POST /v1/soulseek/bulk-download?max_artists=7&tracks_per_artist=11` (numbers correctly serialized into the query string).
- Lidarr Backfill Queue: clicking "Failed" chip → `GET /v1/lidarr-backfill/requests?limit=50&status=failed`; per-row Retry/Skip/Forget hit `/requests/{id}/retry` (POST), `/requests/{id}/skip` (POST), `/requests/{id}` (DELETE) respectively; bulk Clear failed → `POST /requests/reset` body `{scope: "failed"}`.
- Multi-search: form submit → `GET /v1/downloads/search/multi?q=Radiohead+Creep&limit=25&timeout_ms=5000&backends=spotdl,streamrip,spotizerr`; "Download via spotdl" button on result row → `POST /v1/downloads/from-handle` with body `{ handle: { backend: 'spotdl', spotify_id: 'sp123' }, track_title, artist_name, album_name }`.
- Quick Run rows on `#/monitor/overview` resolve correctly to all 4 destinations; renderers exist for all 4.
- No console errors throughout.

## Time spent

≈ 110 min: reading session 03/04 hand-offs + components.js / settings.js patterns + old `app.js` Discovery & Downloads code (25) · `actionCard` helper + `_actionToastWithJump` (15) · Pipeline & ML page (10) · Library page (5) · Discovery page (with Soulseek inline form + Lidarr Backfill Queue) (25) · Charts page (5) · Downloads page (with multi-search + recent panel) (15) · CSS for all components (~520 new lines) (10) · preview verification + screenshots + Quick Run bug fix (10) · this hand-off note (10).

---

**For the next session to read:** Sessions 06, 07, and 08 (Monitor — Pipeline + Models + Recs Debug · System Health + User Diagnostics + Integrations · Downloads + Backfill + Discovery + Charts stats). All three are unblocked by sessions 02 and 05 (Quick Run links + queue surface) and can run in parallel. Session 09 (Explore — Recommendations + Tracks) is also unblocked by session 02; its track-table component is shared with sessions 10 + 11.
