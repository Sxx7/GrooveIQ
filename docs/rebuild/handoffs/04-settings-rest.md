# Hand-off: Session 04 — Settings: Routing + Backfill + Connections + Users + Onboarding

## Status
- [x] Session goal achieved
- [x] Visual verification done (mocked-API harness in `preview_eval`; same approach as sessions 02–03 since the local machine has no FastAPI runtime and the remote backend has no CORS)
- [x] No regressions in old `/dashboard` (zero modifications to `app/static/index.html`, `js/app.js`, `css/style.css`)
- [x] Algorithm config (session 03) still renders untouched
- [x] Committed on `gui-rebuild` branch with message `rebuild: session 04 — settings: routing + backfill + connections + users + onboarding`

## What landed

All 6 Settings sub-pages are now real:

- `#/settings/algorithm` — unchanged (session 03).
- `#/settings/download-routing` — versioned-config shell with custom chain renderers. 3 chain groups (`individual`, `bulk_per_track`, `bulk_album`) where each entry is a row with up/down arrows, enabled toggle, min-quality dropdown, timeout input, remove button, and an "+ Add backend" affordance per group. 4th group is the parallel-search panel (4 backend checkboxes + ms timeout). "related →" rail at top deep-links to Actions → Downloads and Monitor → Downloads.
- `#/settings/lidarr-backfill` — versioned-config shell with 4 grouped tabs (Sources & Filters / Rate & Schedule / Match Quality / Retry & Import). Master Enabled toggle below the header (large, prominent, `--accent` lavender, mutes the rest of the page to opacity 0.6 when off). "Preview Match →" button between Import and Save & Apply opens a modal that POSTs the working (unsaved) config + a limit to `/v1/lidarr-backfill/preview` and renders the resulting candidates in a `would_queue / no_match / rejected` table. Service priority uses inline up/down arrows for reorder. Allow / deny lists are textareas, one entry per line. "related →" rail deep-links to Actions → Discovery and Monitor → Lidarr Backfill.
- `#/settings/connections` — read-only snapshot grid of 7 integration cards. Each card uses the new `GIQ.components.integrationCard` (shared with Monitor → Integrations in session 07). `configured` cards show URL + extra details + "Live health probe lives on Monitor → Integrations" footer; unconfigured cards show a dashed border and the `.env` hint. Status badge is `configured` / `not configured` only — no live probing.
- `#/settings/users` — list + detail. List shows UID, username, display name, events, last seen, created, "View →" jump link per row; whole row is clickable. "+ Add user" button top-right opens a modal that POSTs to `/v1/users`. Click row → `#/settings/users?user=<id>` (query-param routing — see "Decisions" below). Detail page has Identity card (user_id + display_name + UID), Last.fm card (connected ↔ disconnected with Connect/Disconnect/Sync/Backfill buttons), jump link to `#/monitor/user-diagnostics?user={id}`, and an "Edit onboarding →" deep-link to the onboarding sub-page.
- `#/settings/onboarding` — picker (lists all users) when no `?user=` query param; per-user editor when present. Editor has 8 sections: 3 textareas (artists / genres / track_ids), 3 chip groups (moods / contexts / devices), 2 sliders (energy / danceability). Save → POST `/v1/users/{id}/onboarding` → toast.

The shared **versioned-config shell** (`GIQ.components.versionedConfigShell`) gained 5 extension hooks so structurally non-flat configs (Routing, Backfill) can plug into it:
- `topRail: () => Element` — rendered above the header (used for the "related →" rails).
- `headerExtras: (ctx) => Element` — rendered between header and groups (used for the LBF master toggle).
- `extraButtons: Array | (ctx) => Array` — extra buttons inserted between Discard and Save & Apply (used for the LBF "Preview Match →" button).
- `renderGroupBody: (ctx) => Element` — replaces the default field grid for that group entirely (used by both Routing's chain rows and LBF's nested-path field renderer).
- `bodyClass: string | (ctx) => string` — extra CSS class on the host (used for `lbf-disabled` mute).

Plus a shared `GIQ.components.relatedRail({ label, links })` factory (uses `jumpLink` internally) and a shared `GIQ.components.integrationCard({ name, icon, type, version, configured, details, description, snapshot, error, configurePath })` factory.

## File inventory after this session

Substantially modified:
- [app/static/js/v2/components.js](app/static/js/v2/components.js) — extended `versionedConfigShell` with `topRail` / `headerExtras` / `extraButtons` / `renderGroupBody` / `bodyClass` hooks; fixed `fieldsEqual` to deep-equal arrays/objects (fixes Routing dirty-detection, see Decisions); added `_refreshGroupBody`, `_refreshHeaderWithBanner`, `pathGet`, `pathSet`, `buildCtx`. Added two new components: `GIQ.components.relatedRail` and (via settings.js) `GIQ.components.integrationCard`.
- [app/static/js/v2/settings.js](app/static/js/v2/settings.js) — replaced the 5 stubs with full implementations for download-routing, lidarr-backfill, connections, users (list + detail), and onboarding. ~1100 lines total. Lifted `LBF_FIELD_META` from old `app.js` verbatim (24 paths covering scalar / select / order / textarea / int / float / bool types).
- [app/static/js/v2/router.js](app/static/js/v2/router.js) — `parseHash` now returns `{ bucket, subpage, params }`, parsing `?key=value` query strings; `dispatch` passes `params` as the second argument to page renderers; `navigate(bucket, subpage, params)` accepts an optional params object and serialises the query string.
- [app/static/css/components.css](app/static/css/components.css) — added ~470 new lines of CSS: related-rail · vc-header-extras · dl-chain-row / dl-chain-add / dl-backend-dot · dl-parallel · lbf-master-toggle (toggle + slot animation) · lbf-disabled mute · lbf-subgroup · lbf-toggle-inline · lbf-select / lbf-order / lbf-textarea / lbf-preview · conn-snapshot-note / conn-grid / conn-card (configured + unconfigured variants) · users-panel / users-table / user-detail-header / user-id-card / user-lastfm-card / lastfm-stats / lastfm-actions · form-field · onboarding-picker / onboarding-editor / onboarding-section / onboarding-chips / onboarding-slider-wrap.

Doc:
- [docs/rebuild/handoffs/04-settings-rest.md](docs/rebuild/handoffs/04-settings-rest.md) — this file.

No new top-level files; everything plugs into the session 01–03 scaffolding. The router change is fully backwards-compatible: pages that ignore the second `params` argument keep working unchanged.

## State of the dashboard at end of session

Working at `/static/dashboard-v2.html`:

`#/settings/download-routing`:
- Eyebrow "VERSIONED CONFIG · v{n}" + title "Download Routing · {name}". Right rail: History · Diff · Reset · (Discard) · Export · Import · Save & Apply.
- Top "related →" rail with two pills: `Actions · Search & Download →` and `Monitor · Live queue + telemetry →`.
- 4 collapsible groups. The first 3 are chains (`Individual Downloads` / `Bulk (Per-Track)` / `Bulk (Album/Artist)`); each chain renders entries as full-width rows: rank chip · ▲▼ arrows · backend dot + name · `enabled` checkbox · `min quality` dropdown (5 tiers) · `timeout` numeric input + `s` unit · `×` remove. Add row at the bottom with a backend dropdown (filtered to `backends_eligible \ existing`) plus an `+ Add backend` button.
- 4th group is `Parallel Search`: row of backend checkboxes (all 4 from `backends_eligible`) plus a `timeout: NNN ms` numeric input.
- MODIFIED badge appears on a group as soon as any of its entries diverge from the active row.
- Save side-effect: `Saved. Routing updated — next download will use the new chain` toast (no jump link, since there's no Monitor surface to jump to from the toast — the rail at top is the right place).

`#/settings/lidarr-backfill`:
- Eyebrow "VERSIONED CONFIG · v{n}" + title "Lidarr Backfill Config · {name}". Right rail: History · Diff · Reset · (Discard) · Export · Import · Preview Match → · Save & Apply.
- Top "related →" rail to `Actions · Backfill queue →` and `Monitor · Backfill stats →`.
- Master Enabled toggle prominent below the header — large lavender slot-toggle, eyebrow "MASTER SWITCH", title "Backfill engine ENABLED" / "Backfill engine DISABLED", explanatory sub-line. When OFF: toggle goes grey, host gets `lbf-disabled` class, all 4 groups + retrain banner mute to `opacity: 0.6` and `pointer-events: none`. The toggle itself stays interactive so the user can re-enable.
- 4 groups (Sources & Filters / Rate & Schedule / Match Quality / Retry & Import). Each group's body uses the LBF-specific path-walking renderer that handles all 6 field types from `LBF_FIELD_META` (`bool`, `select`, `order`, `textarea`, `int`, `float`, `text`).
- Nested objects (`sources`, `filters`, `match`, `retry`, `import_options`) render as `lbf-subgroup` panels with a small mono uppercase heading and their own card padding so the user sees the hierarchy.
- Service priority field (`order` type in Rate & Schedule's "Match Quality" parent — actually lives on the top-level config, displayed in Match Quality group per `CONFIG_GROUPS`) renders as a vertical list of items with up/down arrows. Single-item moves trigger only that group to re-render (preserves scroll/focus elsewhere).
- Allow / deny lists render as `lbf-textarea` (one entry per line, joined back into an array on `blur`).
- Sliders + numeric inputs for int/float fields with FP-clean stepping (lifted from session 03's `roundToStep`).
- "Preview Match →" button opens a modal: limit input + Run preview button, runs `POST /v1/lidarr-backfill/preview` with `{ limit, config_override: working }`, renders candidates in a table (decision badge · artist · album with optional `match: …` sub-line · score % · service · reasons). Modal stays open across runs so the user can iterate on thresholds.
- Save side-effect: `Saved. Backfill policy updated — takes effect on next tick` toast with `Backfill stats →` jump link to `#/monitor/lidarr-backfill`.

`#/settings/connections`:
- Standard `pageHeader` (`SETTINGS / Connections`) + sub-line note ("Read-only snapshot of integration configuration. Live health probes live on Monitor → Integrations.").
- 7 cards in an auto-fill grid (`minmax(320px, 1fr)`): Media Server · Lidarr · spotdl-api · streamrip-api · Soulseek (slskd) · Last.fm · AcousticBrainz Lookup.
- Configured cards: solid border, icon + name + (type · v{version}) + `configured` badge, description, and a vertical list of `(label : value)` rows for URL + scrobbling + any other `details.*` keys, plus a small "Live health probe lives on Monitor → Integrations" footer.
- Unconfigured cards: dashed border, transparent background, name + `not configured` badge, description, and the `.env` hint.

`#/settings/users` (list):
- `pageHeader` + "+ Add user" button top-right (modal: username + display name → POST `/v1/users` → toast on success → list reloads).
- Single panel containing a `users-table` with UID · Username · Display Name · Events · Last seen · Created · `View →` action. Whole row clickable → navigates to detail.
- Empty state: "No users yet. Users are auto-created when events are ingested, or click '+ Add user' above."

`#/settings/users?user={id}` (detail):
- `← Users` back button + page title (user_id) + UID badge + "Profile updated …ago" sub-line.
- Top-right: "Monitor · View diagnostics →" jump link to `#/monitor/user-diagnostics?user={id}`.
- Two-column body: Identity card (user_id / display_name / UID rows + Edit user button + Edit onboarding → button) and Last.fm card (connected/not, sync now / backfill / disconnect when connected, username + session_key inputs when not).
- "Edit user" modal: PATCH `/v1/users/{uid}` with `{ user_id, display_name }` (rename cascades on the backend).

`#/settings/onboarding` (picker, no user param):
- `pageHeader` + sub-line note + a list of all users with one row each linking to `#/settings/onboarding?user={id}`.

`#/settings/onboarding?user={id}` (editor):
- `← User` back button + user_id title.
- 8 sections in a 2-col layout (label/sub on left, control on right; collapses to single col below 700px):
  - Favourite artists / genres / track_ids — textareas (one per line, sent as arrays on save; empty arrays converted to null).
  - Preferred moods / contexts / devices — chip groups (toggle on click, lavender highlight when selected).
  - Energy / Danceability — sliders 0–1 step 0.05 with a mono lavender numeric readout.
- Save preferences button → POST `/v1/users/{id}/onboarding` → toast with `preferences_saved` count and `matched_tracks` summary.

## Decisions made (with reasoning)

- **Onboarding lives at `#/settings/onboarding?user={id}`, not as a modal**, with a picker view at `#/settings/onboarding` and a deep-link from the user-detail page. **Why**: an 8-section form is too tall for a comfortable modal at desktop sizes (would force scrolling inside a small viewport). A page-level form lets us share the settings layout, makes the URL deep-linkable (so a user can bookmark "edit my onboarding"), and naturally reuses the same back-button pattern as `users → user detail → onboarding`. The picker view (no `?user=`) is a graceful fallback if a user lands on `#/settings/onboarding` cold from the topbar tabs.
- **Users detail uses `?user={id}` query param, not a `/settings/users/{id}` path.** **Why**: the existing router enumerates allowed sub-pages per bucket (`SUBPAGES.settings = [...]`), so `users/simon` wouldn't be a valid sub-page slug. Adding parameterised paths would require teaching the router to pattern-match — a much bigger change than just supporting query strings, and one that wasn't strictly necessary. The session-12 cutover task and any future deep-links from Monitor → User Diagnostics use the same `?user=` convention. `parseHash` was extended (backwards-compatible) and `navigate(bucket, subpage, params)` now accepts an optional params object.
- **Extended `versionedConfigShell` with hooks rather than writing a parallel "structured-config shell".** Per session 03's recommendation. The hooks (`renderGroupBody`, `headerExtras`, `topRail`, `extraButtons`, `bodyClass`) are unobtrusive — Algorithm doesn't pass any of them and its behaviour is unchanged. The `ctx` object passed to hooks gives them everything they need: working/saved/defaults snapshots, dotted-path get/set helpers (`pathGet`/`pathSet`), and refresh callbacks (`refresh()`, `refreshGroup(gk)`, `refreshGroupBadge(gk)`, `refreshHeader()`).
- **`fieldsEqual` is now deep-equal for objects/arrays via `JSON.stringify`** (was strict `===` for non-numbers). **Why**: Routing and Backfill working copies are deep-cloned from the active config. With strict-`===` comparison, every field with an array/object value (chain entries, sub-objects like `sources.*`) was reported "dirty" on first load — every group lit up MODIFIED, Save & Apply was always enabled. This is a one-line fix to the shared shell and Algorithm's flat-config behaviour is unaffected (its values are all primitives). The tradeoff: deep-equal on a 78-field config is more expensive than `===`, but the JSON.stringify on a flat object is microseconds and runs only on `groupDirty()` / `anyDirty()`, which fire per render.
- **Master toggle is `cfg.enabled` mutated directly via `ctx.refresh()`** (not via `setWorking + refreshHeader` like normal fields). **Why**: toggling enabled changes the entire host body class (`lbf-disabled` is added/removed), so a full re-render is the cleanest path. The toggle itself stays interactive (it's a checkbox inside `headerExtras`, which never gets the disabled class).
- **Master toggle's "off" mute uses `pointer-events: none`** plus `opacity: 0.6` on `.vc-groups + .vc-retrain-banner`, scoped under `.vc-shell.lbf-disabled`. The header (with the toggle) stays interactive. Save & Apply also stays clickable so the user can save their `enabled = false` change to flip the scheduler off.
- **Down arrow / up arrow buttons for chain reorder, not drag-and-drop.** Per the brief. The CSS thumbs are sized to be easily clickable at 22 × 18 px. A future pass could swap in HTML5 drag-and-drop (`draggable=true` on `.dl-chain-row` plus `dragover` / `drop` listeners) but that's polish work.
- **Down arrow / up arrow for `service_priority` reorder** (LBF) shares the same UX pattern as chain reorder but lives on a single field within the Rate & Schedule group. Could've extracted a `reorderableList` component as the brief suggested, but the two implementations differ enough (full chain entries with toggles + dropdowns vs. just labels) that sharing would have meant adding configuration knobs and lost the locality. If we add a third reorder UI, that's the moment to extract.
- **Connections snapshot fetches once and renders, no polling.** Per the brief. Live polling is Monitor's job (session 07). The "Live health probe lives on Monitor → Integrations" footer makes the boundary explicit so users don't expect this card to update.
- **`integrationCard` is shared between Settings and Monitor.** Built it once here, since it's needed both for Settings (snapshot mode, `snapshot=true` shows the footer) and Monitor (`snapshot=false` will show the live error message). Session 07 extends it with `error` rendering + status colours but the shell is done.
- **No `DELETE /v1/users/{id}` button** — the endpoint doesn't exist (verified via `grep -n "DELETE.*users\|delete_user" app/api/routes/users.py` returned empty). The brief said "if endpoint exists; check current code" — it doesn't. The `Edit user` button is the only mutation surface.
- **`GIQ.components.integrationCard` is exposed via the IIFE inside `settings.js` rather than `components.js`.** This is a slight architectural smell (it's a shared component), but the IIFE pattern means the function is registered on `GIQ.components` at script load time, so it's globally available the moment `settings.js` loads. Session 07 will move this to `components.js` if/when it adds the live-probe variant, since at that point the file lives in two consumers and lifting it up is cleaner.

## Defaults endpoint shapes (read this carefully — sessions 06–08 must follow)

`GET /v1/downloads/routing/defaults`:

```json
{
  "config": {
    "individual": [{ "backend": "spotdl", "enabled": true, "min_quality": null, "timeout_s": 60 }, ...],
    "bulk_per_track": [...],
    "bulk_album": [...],
    "parallel_search_backends": ["spotdl", "streamrip", "spotizerr"],
    "parallel_search_timeout_ms": 5000
  },
  "groups": [
    { "key": "individual", "label": "Individual Downloads", "description": "...", "backends_eligible": ["spotdl","streamrip","spotizerr","slskd"] },
    { "key": "bulk_per_track", ... },
    { "key": "bulk_album", "backends_eligible": ["lidarr","streamrip"] },
    { "key": "parallel_search", "backends_eligible": [...] }
  ]
}
```

Note that `parallel_search_backends` and `parallel_search_timeout_ms` are top-level on `config`, but `groups` includes a `parallel_search` entry — its `backends_eligible` is the source of truth for which checkboxes to render. The `renderGroupBody` callback dispatches on `groupKey` (either a chain key or `'parallel_search'`).

`GET /v1/lidarr-backfill/config/defaults`:

```json
{
  "config": {
    "enabled": false,
    "dry_run": false,
    "max_downloads_per_hour": 10,
    "max_batch_size": 5,
    "poll_interval_minutes": 5,
    "service_priority": ["qobuz", "tidal", "deezer", "soundcloud"],
    "min_quality_floor": "lossless",
    "sources": { "missing": true, "cutoff_unmet": false, "monitored_only": true, "queue_order": "recent_release" },
    "filters": { "artist_allowlist": [], "artist_denylist": [] },
    "match": { "min_artist_similarity": 0.85, "min_album_similarity": 0.80, "require_year_match": false, "require_track_count_match": false, "prefer_album_over_tracks": true, "allow_structural_fallback": false },
    "retry": { "cooldown_hours": 24, "max_attempts": 3, "backoff_multiplier": 2.0 },
    "import_options": { "trigger_lidarr_scan": true, "scan_path": "" }
  },
  "groups": [
    { "key": "sources_filters", "label": "Sources & Filters", "description": "...", "fields": ["enabled", "sources", "filters"] },
    { "key": "rate_schedule", "fields": ["max_downloads_per_hour", "max_batch_size", "poll_interval_minutes"] },
    { "key": "match_quality", "fields": ["service_priority", "min_quality_floor", "match"] },
    { "key": "retry_import", "fields": ["retry", "import_options", "dry_run"] }
  ]
}
```

**Critical detail**: LBF groups carry a `fields: [...]` array, where each entry can be either a top-level scalar key (`"enabled"`, `"max_downloads_per_hour"`) or a top-level *object* key (`"sources"`, `"match"`). The renderer recursively expands object keys into per-leaf `<field>` widgets and groups them in a `<lbf-subgroup>` panel. The Algorithm-style "config[group][field] is always a flat dict" assumption does NOT hold here.

The dotted-path metadata in `LBF_FIELD_META` (`'sources.missing'`, `'match.min_artist_similarity'`, etc.) maps to leaf fields after recursion. The `pathGet` / `pathSet` helpers from `buildCtx` handle the dotted-path mutation.

`POST /v1/lidarr-backfill/preview` body:

```json
{ "limit": 20, "config_override": { ...working_config_or_null... } }
```

Returns `{ candidates: [{ decision, artist, album, matched_album, match_score, picked_service, reasons }, ...], error?: string }`. The decision is one of `"would_queue" | "no_match" | "rejected"` (we render `would_queue` as `vc-badge-active`, `no_match` as `vc-badge-modified`, others as `vc-badge-retrain`).

`GET /v1/integrations/status`:

```json
{
  "integrations": {
    "media_server": { "configured": true, "connected": true, "type": "navidrome", "version": "0.51", "url": "http://navidrome:4533", "details": { ... } },
    "lidarr":       { "configured": true, ... },
    "spotdl_api":   { ... },
    "streamrip_api":{ "configured": false },
    "slskd":        { ... },
    "lastfm":       { "configured": true, "type": "lastfm", "scrobbling": true, "details": { ... } },
    "acousticbrainz_lookup": { ... }
  }
}
```

We use `configured` as the binary state on this page. Session 07 (Monitor → Integrations) will additionally render `connected`, `error`, `status`, etc. for live health.

`GET /v1/users/{id}/profile` returns the user's record including `taste_profile` JSON and a `lastfm` sub-object (or null). For Settings we only use `uid`, `user_id`, `display_name`, `profile_updated_at`, and `lastfm.{username,scrobbling_enabled,synced_at}`. The full `taste_profile` is for Monitor → User Diagnostics.

`GET /v1/users/{id}/onboarding` returns the previously-stored prefs as a flat object — empty `{}` is a valid response when nothing has been saved. Empty-string + empty-array fields collapse to `null` before POST so the server doesn't reject `{}` with the model validator's "at least one preference" rule.

## Gotchas for the next session

- **`fieldsEqual` is now deep-equal.** This was a session 04 bug fix, not a new contract — but if any future page uses the shell with mutable arrays/objects in field values, it now correctly detects dirty. Watch out if you pass large object trees (>50 KB) per field — `JSON.stringify` is O(n) and runs on every dirty check; for normal config sizes this is microseconds.
- **`navigate(bucket, subpage, params)` now serialises a query string.** If the next session adds another deep-link surface, prefer this over hand-rolling hashes — the `parseHash` change is what reads it back. Empty/null params drop out of the serialised URL automatically.
- **Page renderer signature is now `(root, params) => cleanup`.** Existing pages that don't accept `params` keep working (extra args are ignored). Session 06+ pages should accept `params` for `?user=`, `?since=`, etc. deep-link parameters.
- **Master toggle's "off" state mutes pointer-events on the body.** If the next session adds something interactive to the LBF surface that should remain interactive even when off, exempt it via a more specific selector or move it into `headerExtras` (which is not muted).
- **`integrationCard` is in `settings.js` for now** but is intended to be shared with Monitor → Integrations (session 07). Session 07 should either move it to `components.js` (cleaner) or import it from `GIQ.components.integrationCard` (it's already global). Adding the `error` slot for live health is the only API extension expected.
- **`GIQ.api.del` exists** (verified — see `core.js` `GIQ.api.del`). LBF doesn't use it but Connections and Users do (`/v1/users/{id}/lastfm` disconnect). If session 12 adds user deletion and the backend grows a `DELETE /v1/users/{id}` endpoint, it'd plug into the existing button.
- **`alert()` and `confirm()` are still browser dialogs** — same as sessions 03 and 02. Replacing them with custom modals is a polish item, not regression-blocking.
- **The Last.fm "Connect" form is a placeholder.** `POST /v1/users/{id}/lastfm/connect` actually requires a session_key flow with Last.fm's auth.getSession (or username + token from a Last.fm-mobile-app callback). The simple `{ username, session_key }` body works if the user has already gone through Last.fm's auth flow elsewhere. The richer integration is a separate concern.
- **Onboarding's "favourite_tracks" is a textarea of track_ids, not a search-and-add UI.** Power-user friendly but unfriendly for non-engineers. Could be polished in a later session by reusing the track-table component (session 09) as a multi-select picker.
- **Master toggle uses `cb.click()` not `cb.dispatchEvent('change')` in tests** — clicking sends `change` automatically, but if you bind only to `change`, programmatic value-set won't fire it. Watch for this in further automation.
- **Mocked-API harness still needed for verification.** Same gotcha as session 03. The remote backend has no CORS for the static-file preview, and the local machine has no FastAPI runtime, so visual verification uses `preview_eval` to monkey-patch `GIQ.api.{get,post,put,patch,del}` before re-dispatching the route. Documented mocks for: `/v1/downloads/routing[/defaults|/history]`, `/v1/lidarr-backfill/config[/defaults|/history|/preview]`, `/v1/integrations/status`, `/v1/users[?limit=]`, `/v1/users/{id}/profile`, `/v1/users/{id}/onboarding`.

## Open issues / TODOs

- A "View on Monitor" rail at the top of Settings → Users would round out the cross-link story. Skipped for now — the `Monitor · View diagnostics →` jump link on the per-user detail page covers the main use case, and a Settings-list-level rail would be aspirational ("once Monitor → User Diagnostics has a list view").
- The Discovery / Lidarr Backfill jump rail in `lidarr-backfill` deep-links to `#/actions/discovery` (the Discovery action page). Session 05 owns that page; the rail will go to `#/actions/discovery#lidarr-backfill-queue` (or similar deep-link inside Discovery) once the queue surface is built. Acceptable for now.
- `relatedRail` always renders inside `.vc-shell` (page padding handled by the shell). For future split pages outside the shell (e.g. Monitor → Pipeline), the rail will need its own page-padding wrapper.
- `GIQ.components.integrationCard` should move to `components.js` in session 07.
- Connections "Configure" deep-link: each card supports a `configurePath` that would render a "Configure" jump-link. Currently unused (env-var-driven config has nowhere to deep-link to). If future versions add a UI for editing media-server creds, the hook is there.
- The `AlgorithmConfigData.model_validate` permissiveness on import (session 03 hand-off) applies equally to Routing and Backfill imports. Friendly per-field error messages on 422 responses are the same backlog item.
- LBF master toggle does NOT auto-save when flipped — it just dirty-marks the working copy and waits for the user to click Save & Apply. This is consistent with the rest of the shell. Auto-saving would be inconsistent. The old `app.js` had a Pause/Resume button that auto-saved a new versioned config row; the new design leaves that to the queue management surface (Actions, session 05).
- LBF Preview Match modal is full-width on desktop but doesn't yet collapse the table to cards on small viewports. Acceptable for v1; the modal already has horizontal scroll inside.

## Verification screenshots

Captured inline in the session transcript via `mcp__Claude_Preview__preview_screenshot`:

1. **Download Routing** — full layout: related-rail at top, eyebrow `VERSIONED CONFIG · v7`, title `Download Routing · Tuned-2026-04`, 7-button right rail, 4 collapsible groups, Individual Downloads expanded showing 4 chain rows (spotdl ✓, streamrip ✓ Lossless, spotizerr ☐, slskd ☐). MODIFIED badges absent (after the deep-equal fix).
2. **Lidarr Backfill (enabled)** — full layout: related-rail, eyebrow `VERSIONED CONFIG · v3`, "Preview Match →" button visible in the right rail, master toggle ON in lavender, Sources & Filters group expanded showing "Enabled" toggle + sources sub-group (Missing / Cutoff Unmet / Monitored Only / Queue Order dropdown).
3. **Lidarr Backfill (disabled)** — same page after toggling master OFF: toggle moves to grey, `Backfill engine DISABLED` title, sub-line text changes, the four groups + retrain area mute to opacity 0.6 (visibly faded). Save & Apply is now lavender-active because `enabled` is dirty.
4. **Connections** — 7 cards in a 3-col grid (4 configured, 3 unconfigured). Configured cards show URL + `Live health probe lives on Monitor → Integrations` footer. Unconfigured cards have dashed borders and the `.env` hint.
5. **Users (list)** — 3-row table with UID / username / display name / events / last seen / created / View → action.
6. **User detail (simon)** — Identity + Last.fm cards side by side, UID 1 badge, "Profile updated 11s ago" sub-line, "Monitor · View diagnostics →" jump link in top-right, Edit user / Edit onboarding → buttons in Identity card, Sync now / Backfill scrobbles / Disconnect buttons in Last.fm card.
7. **Onboarding (simon)** — 8-section editor: 3 textareas with seeded values, 3 chip groups (4 chips selected: happy, energetic, mobile, desktop), 2 sliders (energy 0.70, danceability 0.60), Save preferences button.
8. **Onboarding (picker)** — 3 user rows linking to their respective `?user={id}` URLs.

Programmatic verifications evidenced via `preview_eval`:
- All 6 Settings pages dispatch without console errors.
- Download Routing: 4 groups with the correct labels; chain rows render with backends `spotdl/streamrip/spotizerr/slskd`; toggling enabled flips MODIFIED + dirty state; Save & Apply hits `PUT /v1/downloads/routing` with the working config; version eyebrow bumps to v8 after save; Discard hides; Save disables.
- Lidarr Backfill: 4 groups with the correct labels, master toggle reflects `working.enabled`, toggling OFF adds `lbf-disabled` to host (verified `host.classList`), Preview Match modal opens, Run preview hits `POST /v1/lidarr-backfill/preview` and renders 2 candidate rows.
- Connections: 7 cards with correct configured/not-configured badges (`configured / configured / configured / not configured / not configured / configured / not configured`).
- Users: list shows 3 rows; click row → URL changes to `#/settings/users?user=simon`; detail page renders Identity + Last.fm cards; Last.fm badge text is "connected" for simon.
- Onboarding: 8 sections render; pre-existing prefs seeded into the form (`Daft Punk\nYann Tiersen` in artists textarea, 4 chips selected, energy slider at `0.7`); save button present.
- Algorithm config (regression check): 2 mocked groups, 24 fields, 0 modified badges, save disabled — no regression from the `fieldsEqual` change.

## Time spent

≈ 130 min: reading session 03 hand-off + components.js shell + old `app.js` Routing/LBF/Users/Connections/Onboarding (35) · shell extension hooks (`renderGroupBody`/`headerExtras`/`topRail`/`extraButtons`/`bodyClass`) + ctx + dotted-path helpers + fieldsEqual fix (25) · Download Routing renderer + chain row component (20) · Lidarr Backfill renderer + master toggle + Preview Match modal (25) · Connections + integrationCard component (10) · Users list + detail + Last.fm card + Edit modal + Add modal (10) · Onboarding picker + editor (5) · CSS for all new components (~470 new lines, components.css) (15) · preview verification + screenshots + bugfix loop (15) · this hand-off note (10).

---

**For the next session to read:** Session 05 — Actions bucket (5 grouped pages), see [docs/rebuild/05-actions.md](docs/rebuild/05-actions.md). Sessions 06–08 (Monitor) are also unblocked and can run in parallel per the master plan.
