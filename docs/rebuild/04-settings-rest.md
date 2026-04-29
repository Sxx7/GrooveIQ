# Session 04 — Settings: Download Routing + Lidarr Backfill + Connections + Users + Onboarding

You are continuing the GrooveIQ dashboard rebuild. This is **session 04 of 12**.

This session finishes the Settings bucket — plugs the two remaining versioned configs into the shell built in session 03, then adds the Connections snapshot and the Users / Onboarding pages.

## Read first

1. `docs/rebuild/README.md`
2. All prior hand-offs (01–03). Especially session 03's notes on the `defaults` payload shape.
3. `docs/rebuild/components.md` → **Versioned-config shell**, **Integration card**.
4. `docs/rebuild/api-map.md` → all "Settings" rows.
5. `gui-rework-plan.md` § "Downloads tab → split", "Discovery → Lidarr Backfill → split", "Connections tab → split", "Users tab → split".
6. `design_handoff_grooveiq_dashboard/page-lidarr.jsx` — `LBSettings` for the cross-link rail pattern.
7. The **current** Download Routing tab in `app/static/js/app.js` — search `// Downloads tab — routing config`. Lift field metadata + chain structure.
8. The **current** Lidarr Backfill settings panel in `app/static/js/app.js` — search `// Lidarr Backfill sub-tab`. Lift field metadata + four-group structure (Sources & Filters / Rate & Schedule / Match Quality / Retry & Import).

## Goal

By session end, all 6 Settings pages work:
- `#/settings/algorithm` — already done (session 03).
- `#/settings/download-routing` — uses versioned-config shell. Special handling for the `priority chains` (drag-reorder), parallel search backends, per-entry quality dropdown.
- `#/settings/lidarr-backfill` — uses versioned-config shell with the 4-group config + Preview Match modal. Cross-link rail to Actions queue + Monitor stats.
- `#/settings/connections` — read-only snapshot of all 7 integration cards (configured / not configured, env-var-driven).
- `#/settings/users` — list + detail. Detail page has CRUD (rename, delete), per-user Last.fm connect/disconnect/sync, and "Edit onboarding" deep-link.
- `#/settings/onboarding` — per-user onboarding preferences editor (sub-page of Users — rendered as `#/settings/onboarding?user=<id>` or via tab inside Users → user detail; pick whichever fits the IA cleanly and document in the hand-off).

## Out of scope

- Live integration health (that's Monitor → Integrations, session 07).
- Lidarr Backfill queue management (that's Actions, session 05).
- Lidarr Backfill stats (that's Monitor, session 08).
- User listening history / taste profile / sessions (that's Monitor → User Diagnostics, session 07).

## Tasks (ordered)

### A. Settings → Download Routing

1. **Plug into shell.** `versionedConfigShell({ kind: 'downloads/routing', title: 'Download Routing', eyebrow: 'VERSIONED CONFIG · v{n}', retrainGroups: [] })`.
2. **Custom group renderers** — Download Routing has 3 chains (`individual` / `bulk_per_track` / `bulk_album`) where each chain entry has `backend` + `enabled` + optional `min_quality` + `timeout_s`. The default field-grid renderer in session 03 won't fit. Instead, override the body slot per group: render each chain entry as a row with up/down reorder arrows, enable toggle, backend label, min-quality dropdown (`lossy_low / lossy_high / lossless / hires`), timeout input (mono numeric), and remove + add buttons. Drag-reorder is nice but pragmatic implementation: up/down arrows is fine for v1.
3. **Parallel-search panel** — at the bottom, a list of backend checkboxes (spotdl / streamrip / spotizerr / slskd) + a numeric `parallel_search_timeout_ms` input.
4. **Side-effect string** on Save: "Routing updated — next download will use the new chain". No jump link (no Monitor surface to redirect to).
5. **Cross-link rail** — at top: links to Actions → Downloads (Search & Download test) and Monitor → Downloads (live queue + telemetry).

### B. Settings → Lidarr Backfill Config

1. **Plug into shell.** `versionedConfigShell({ kind: 'lidarr-backfill', title: 'Lidarr Backfill Config', eyebrow: 'VERSIONED CONFIG · v{n}', retrainGroups: [] })`.
2. **4 groups** per existing schema: Sources & Filters, Rate & Schedule, Match Quality, Retry & Import.
3. **Service-priority field** in Rate & Schedule is a list of services to be reordered (qobuz / tidal / deezer / soundcloud). Render as a small ordered list with up/down arrows. Same UX as Download Routing chain reorder — extract a small `reorderableList` component if both pages need it.
4. **Allow / deny lists** (artist substrings) are textareas, one entry per line.
5. **Preview Match modal** — `POST /v1/lidarr-backfill/preview` with the working (unsaved) config + a `limit` input. Renders top-N candidates with their fuzzy scores and accept/reject reason. Modal is reachable from a "Preview Match →" button in the header button row (between History and Save & Apply).
6. **Side-effect string** on Save: "Backfill policy updated — takes effect on next tick". Jump link to `#/monitor/lidarr-backfill`.
7. **Cross-link rail** — at top: links to Actions → Discovery → Lidarr Backfill Queue and Monitor → Lidarr Backfill.
8. **Master enabled toggle** — `cfg.enabled` is a boolean — render as a prominent toggle just below the page header (not buried in a group). Disabled state visually mutes the rest of the page (opacity 0.6).

### C. Settings → Connections (snapshot)

`GIQ.pages.settings.connections`:

1. Fetch `GET /v1/integrations/status` once (no polling — this is the static snapshot; live probes go on Monitor → Integrations).
2. Render a grid of 7 integration cards using `GIQ.components.integrationCard({ name, icon, type, version, configured, details, configurePath })` (build the component first; spec in `components.md → Integration card`).
3. **Show only the "configured" half**:
   - If configured: header (icon, name, type, version), description, configured fields (URL, etc. — read-only).
   - If not configured: header with status "not configured", description, hint text "Set the required env vars in your `.env` file to enable this integration".
4. Status badge here is "Configured" or "Not configured" (env-driven). **Do not show live health** — that's Monitor.

### D. Settings → Users

`GIQ.pages.settings.users` — list view:

1. `GET /v1/users` → table: UID, username, display name, events, last seen, created, "View →" action.
2. "Add user" button (top-right) → modal with username + display name fields → `POST /v1/users`.
3. Click row → `#/settings/users/{id}` (detail page).

User detail (`GIQ.pages.settings.userDetail` or `users.detail` — pick a name and document):

1. `GET /v1/users/{id}` for header (UID badge, display name, profile updated timestamp).
2. Buttons: "Edit user" (rename modal), "Delete user" (destructive — confirmation, then `DELETE /v1/users/{id}` if endpoint exists; check current code), "Edit onboarding" (jump to onboarding sub-page or open modal).
3. **Last.fm card** — status (connected / not), Connect / Disconnect / Sync Now / Backfill Scrobbles buttons. Endpoints: `POST /v1/users/{id}/lastfm/connect`, `DELETE /v1/users/{id}/lastfm`, `POST /v1/users/{id}/lastfm/sync`. (Backfill scrobbles endpoint per current code; verify in `app/static/js/app.js`.)
4. **Jump link to diagnostics** — top-right of user detail: "View diagnostics →" jumping to `#/monitor/user-diagnostics?user={id}`.
5. NO listening history, taste profile, or sessions on this page — those are Monitor → User Diagnostics.

### E. Settings → Onboarding (per user)

Either as a sub-page (`#/settings/onboarding?user={id}`) or as a modal opened from Users → user detail. Pick one and document in the hand-off.

1. `GET /v1/users/{id}/onboarding` for current values.
2. Form fields per the API spec (favourite artists, genres, tracks, moods, contexts, devices, energy, danceability sliders).
3. Save → `POST /v1/users/{id}/onboarding` → toast.

## Verification

1. Load each Settings sub-page. Each renders without error.
2. Download Routing: reorder a chain entry; toggle enabled on a backend; save → fetch active config and confirm change persisted.
3. Lidarr Backfill: open Preview Match modal; verify candidates render with scores. Toggle master enabled flag.
4. Connections: every integration card renders with "configured" or "not configured". Hint shows for unconfigured ones.
5. Users: list renders, click into detail, edit name, save, return to list, name updated.
6. Onboarding: load existing prefs, edit a slider, save, reload, persisted.
7. All cross-links work (rails on Routing and Backfill jump to Actions / Monitor stubs as expected).

## Hand-off

Write `handoffs/04-settings-rest.md`. Note any nuances about:
- Download Routing chain payload structure
- Lidarr Backfill config groups + preview-match payload shape
- Onboarding placement decision (sub-page vs modal) and why
- Any endpoint quirks discovered

Commit: `rebuild: session 04 — settings: routing + backfill + connections + users + onboarding`.
