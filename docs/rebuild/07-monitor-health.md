# Session 07 — Monitor: System Health + User Diagnostics + Integrations

You are continuing the GrooveIQ dashboard rebuild. This is **session 07 of 12**.

Three more Monitor surfaces. Independent of session 06 / 08; can run in parallel.

## Read first

1. `docs/rebuild/README.md`
2. All prior hand-offs (01–02 minimum).
3. `docs/rebuild/components.md` → **Integration card** (build now if not yet built; spec there).
4. `docs/rebuild/api-map.md` → "System Health", "User Diagnostics", "Integrations" rows.
5. `gui-rework-plan.md` § "Dashboard tab → Monitor → Overview + System Health" + § "Users tab → split" + § "Connections tab → split".
6. The current implementations:
   - System Health panels: search `// loadHealthPanels`, `renderEventSparkline`, `renderLibraryCoverage`, `renderActivityTimeline`, `renderEngagementTable` in `app/static/js/app.js`.
   - User detail (taste profile, history, sessions): search `renderUserDetail`.
   - Integration cards: search `// Connections Tab`.

## Goal

Three Monitor pages working:
- `#/monitor/system-health` — event ingest sparkline, library coverage, listening activity timeline, user engagement leaderboard, library scan status (full detail).
- `#/monitor/user-diagnostics` — per-user diagnostic surface. Picks user via dropdown or `?user={id}` query param. Shows taste profile (audio / behaviour / mood / key), Last.fm enrichment, top tracks (interactions), listening history, sessions.
- `#/monitor/integrations` — live health probes for all 7 integrations. Shares `integrationCard` component with Settings → Connections (built in session 04) but with live status data.

## Out of scope

- Settings → Connections (already done session 04). This page is the live counterpart.
- Settings → Users user CRUD (done session 04). This page only shows diagnostic data, no editing.

## Tasks (ordered)

### A. Monitor → System Health

`GIQ.pages.monitor.systemHealth`:

1. **Page header** — eyebrow MONITOR, title "System Health", right-side range toggle (24h / 7d / 30d).
2. **Event ingest sparkline panel** — full version of the Overview chart with finer-grained binning. From `GET /v1/pipeline/stats/events`. Supports range toggle.
3. **Library coverage panel** — progress bar (analyzed / total) + per-version breakdown (`current` / `outdated` / `failed`) + a small list of failed files (truncated to 20). Uses `library_coverage` from `GET /v1/stats`.
4. **Listening activity timeline panel** — stacked area chart by event type over `range`. From `GET /v1/pipeline/stats/activity?days={1|7|30}`. Stack only ~5 most-frequent event types; aggregate the rest into "other". Use a single neutral hue family (different lavender saturations), NOT a rainbow.
5. **User engagement leaderboard panel** — sortable table from `GET /v1/pipeline/stats/engagement`. Columns: user · plays · skip rate · unique tracks · diversity. Default sort: plays desc.
6. **Library scan panel** (full) — same data source as the Overview tile but with all the file-by-file detail: phase indicator, ETA, check rate, analyze rate, currently-processing file path, started / ended timestamps, activity log (auto-poll while running). Port from current `renderScanPanel`.

### B. Monitor → User Diagnostics

`GIQ.pages.monitor.userDiagnostics`:

1. **Page header** — eyebrow MONITOR, title "User Diagnostics", right side: user dropdown (loads from `cachedUsers` global, populated by Overview / on demand).
2. **Routing** — accept `?user={id}` query param or fall back to first user. Update URL when dropdown changes.
3. **Taste Profile section** — 4 panels:
   - Audio preferences (BPM, energy, dance, valence, acoustic, instrumentalness, loudness — Inter Tight 22/600 values in a 4-col grid).
   - Behaviour (total plays, active days, avg session, skip rate, completion — same grid).
   - Mood preferences (bar chart top 8, monochrome lavender).
   - Key preferences (bar chart top 12, monochrome lavender).
   - Source: `GET /v1/users/{id}/profile`.
4. **Multi-timescale audio preferences** — radar chart with 7-day / 30-day / all-time overlays (5 axes). Optional but adds a lot for diagnosing taste drift.
5. **Last.fm enrichment** (if connected) — top artists (3 tabs: 7 days / 1 month / all time), loved tracks, genres tag cloud. From `GET /v1/users/{id}/lastfm/profile`. Read-only here (Connect / Sync are in Settings).
6. **Top tracks (interactions) table** — `GET /v1/users/{id}/interactions`. Use the track-table component (built in session 09 — for now, render an inline table; revise after session 09 lands the shared component if order doesn't allow). Columns: track / score (with bar) / plays / skips / likes / completion / last played.
7. **Listening history table** — `GET /v1/users/{id}/history`. Paginated. Columns: time / artist / title / album / duration / listened / completion / result / device.
8. **Sessions table** — `GET /v1/users/{id}/sessions`. Columns: started / duration / tracks / plays / skips / skip rate / completion / context / device.
9. **"Get Recs" jump** — top-right next to user dropdown: button → `#/explore/recommendations?user={id}` (resolves in session 09).
10. **"Edit user" jump** — link to `#/settings/users/{id}` for admin actions.

### C. Monitor → Integrations

`GIQ.pages.monitor.integrations`:

1. **Page header** — eyebrow MONITOR, title "Integrations", right side: "Re-probe all" button (manual refresh) + last-checked timestamp.
2. Fetch `GET /v1/integrations/status` every 30s (poll) and on manual re-probe.
3. Render the same 7 integration cards as Settings → Connections, but using the **live-status half** of `integrationCard`:
   - Status badge: "Healthy" / "Probing" / "Error" / "Not configured".
   - Live error message (red, if probe failed).
   - Probe latency (mono).
   - Last-checked-at timestamp.
   - **Don't show** configured fields (URL, etc.) — those are the Settings half.
4. Visually distinct from Settings → Connections — bigger status badges, live updating, no config-hint text.

### D. Coordinate with session 04 — `integrationCard`

If session 04 is already done, the `integrationCard` component should already exist with both `mode='configured'` (Settings) and `mode='live'` (Monitor) variants. If session 04 isn't done yet, build it here with both modes; session 04 will reuse.

## Verification

1. Load `#/monitor/system-health`. Range toggle changes data. Library coverage progress bar renders. Activity timeline stacks event types in monochrome.
2. Load `#/monitor/user-diagnostics`. Pick a user from the dropdown — URL updates with `?user=X`. Taste profile loads. Last.fm panels render if user has connected. History pagination works.
3. Load `#/monitor/integrations`. All 7 cards render with live status. Re-probe button refreshes. Force one to fail (e.g. shut down spotdl-api) and verify the card shows error state within 30s.

## Hand-off

Write `handoffs/07-monitor-health.md`. Note:
- Which event types you stacked in the activity timeline + what fell into "other".
- Whether you used the (not-yet-built) shared track-table or rendered inline tables that session 09 will need to refactor.
- Any quirks of `/v1/integrations/status` (probe latency timing, error message format).

Commit: `rebuild: session 07 — monitor: system health + user diagnostics + integrations`.
