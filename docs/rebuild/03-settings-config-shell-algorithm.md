# Session 03 — Settings: versioned-config shell + Algorithm

You are continuing the GrooveIQ dashboard rebuild. This is **session 03 of 12**.

This session builds the **shared versioned-config shell** that Algorithm, Download Routing, and Lidarr Backfill all plug into — then ports the most complex consumer (Algorithm, ~78 fields) onto it. Sessions 04 plugs the other two configs into the same shell.

## Read first

1. `docs/rebuild/README.md`
2. All prior hand-offs: `handoffs/01-foundation.md`, `handoffs/02-overview.md`. Verify state.
3. `docs/rebuild/components.md` → **Versioned-config shell**, **Cross-link "jump" pill**, **"Related →" rail**.
4. `docs/rebuild/api-map.md` → "Settings → Algorithm Config" row.
5. `gui-rework-plan.md` § "Algorithm tab → Settings → Algorithm" and § "Versioned-config shell".
6. `design_handoff_grooveiq_dashboard/page-lidarr.jsx` — `LBSettings` is the structural template (header + cross-link rail + collapsible groups + dashed field stand-ins). Replace the dashed stand-ins with real slider+input controls.
7. The **current** Algorithm tab implementation in `app/static/js/app.js` — search for `// Algorithm Config Tab (Phase B+C)`. Port its behaviour (groups, fields, save/discard/reset/history/diff/export/import) into the new shell. Don't reinvent the field metadata or validation.

## Goal

User navigates to `#/settings/algorithm` and gets a fully working algorithm config UI:
- Header with version badge, name, and right-side button row (History · Diff · Reset · Discard · Save & Apply · Export · Import).
- Optional retrain warning banner when changes affect Ranker / Session Embeddings.
- 7 collapsible config groups with `RETRAIN`/`MODIFIED` badges where appropriate.
- Each field: label + slider + numeric input (synced) + description + per-field default-deviation indicator.
- Save & Apply → toast + jump-link to Monitor → Pipeline (do not auto-redirect).
- History modal lists past versions with Activate / Diff per row.
- Diff modal compares working copy vs saved or any historical version.
- Export downloads JSON. Import accepts JSON file + optional name, validates, creates new version.

## Out of scope

- Plugging Download Routing or Lidarr Backfill into the shell (session 04).
- Connections snapshot (session 04).
- User CRUD / onboarding (session 04).
- Any retraining behaviour beyond surfacing the warning — that's a backend concern.

## Tasks (ordered)

### A. Versioned-config shell component

1. **`GIQ.components.versionedConfigShell({ kind, title, eyebrow, retrainGroups })`**:
   - `kind` is one of `'algorithm' | 'downloads/routing' | 'lidarr-backfill'`.
   - Resolves API paths from `kind` (e.g. `/v1/{kind}/config`, `/v1/{kind}/config/defaults`, `.../history`, `.../export`, `.../import`).
   - Renders header (eyebrow + title + button row), optional retrain banner, groups grid, and modals (history, diff, import).
   - Manages working copy state (`workingConfig`, `savedConfig`, `defaults`, dirty diff).
   - `retrainGroups` is a list of group keys whose changes should trigger the retrain warning.
2. **Group rendering** — collapsible with chevron. Header shows label + RETRAIN badge (if `retrainGroups.includes(group.key)`) + MODIFIED badge (if any field in group is dirty) + sub. Body: 2-col grid (single col below 700px breakpoint) of fields.
3. **Field rendering** — label + slider + numeric input (with +/- spinner buttons) + description + default-deviation indicator. Slider + input are bidirectionally synced. Modified field gets a subtle blue border on the row.
4. **Pydantic ge/le constraints** come back from `/{kind}/config/defaults` per field. Use them for slider min/max/step and to validate the numeric input on blur.
5. **Save & Apply** — `PUT /v1/{kind}/config` with the working copy. On success: toast "Saved. {side-effect} →" with a jump link. For Algorithm specifically, the side-effect is "Pipeline reset triggered" → jump to `#/monitor/pipeline`. Sessions 04's two configs will customise the side-effect string.
6. **Discard** — confirmation, then revert to `savedConfig`.
7. **Reset to Defaults** — confirmation, then `POST /v1/{kind}/config/reset`, refresh.
8. **History modal** — `GET /v1/{kind}/config/history`. Table: version, name, timestamp, active flag, Activate / Diff buttons. Activate → `POST /v1/{kind}/config/activate/{version}` → refresh.
9. **Diff modal** — side-by-side comparison of two configs (working copy vs saved is the default; vs historical version is reachable from History → Diff). Per group, list fields where values differ. Below 700px, stack vertically.
10. **Export** — `GET /v1/{kind}/config/export` → trigger file download.
11. **Import** — file input (hidden, button-triggered) + optional name input. On submit, `POST /v1/{kind}/config/import` with the JSON. On success, refresh.
12. **Add styles** to `components.css` and (where page-specific) `pages.css`.

### B. Settings sub-page nav

Settings has its own sub-page list (Algorithm · Download Routing · Lidarr Backfill · Connections · Users · Onboarding). The topbar already has these from session 01. No work here.

### C. Plug Algorithm into the shell

`GIQ.pages.settings.algorithm`:

1. Fetch `/v1/algorithm/config` (active) and `/v1/algorithm/config/defaults` (with group metadata).
2. Pass to `versionedConfigShell({ kind: 'algorithm', title: 'Algorithm Config', eyebrow: 'VERSIONED CONFIG · v{version}', retrainGroups: ['ranker', 'session_embeddings'] })`.
3. Verify all 7 groups render: `track_scoring`, `reranker`, `candidate_sources`, `taste_profile`, `ranker`, `radio`, `session_embeddings`. Field count totals ~78.
4. Verify side-effect line on Save: "Pipeline reset triggered →" with jump to `#/monitor/pipeline`.

### D. Page header polish

- Right-align the button row.
- Use `--accent` background for `Save & Apply` (primary), bordered/ghost for everything else.
- Disable `Save & Apply` when no changes are dirty.
- Show `Discard` only when changes are dirty.
- Use `JumpLink`-style pill for the "related" links if needed (Algorithm doesn't need a related rail — it's not a split page).

## Verification

1. Load `#/settings/algorithm`. All groups + fields render. Field values match a fresh API call to `/v1/algorithm/config`.
2. Modify a slider on `track_scoring.w_like` from default 2.0 to 2.5. The field row gets a blue border. The MODIFIED badge appears on the group header. The Save & Apply button enables.
3. Click Discard → confirmation → field reverts.
4. Modify a `ranker` field. Retrain banner appears.
5. Click Save & Apply → toast appears with "Pipeline reset triggered →" link. Confirm `/v1/pipeline/reset` was called (check network tab). Click the jump link → URL becomes `#/monitor/pipeline`.
6. Click History → modal lists versions. Click Diff on an old version → diff modal shows differences.
7. Click Export → JSON file downloads.
8. Click Import → upload the just-exported JSON → new version created and active.
9. Reset to Defaults → all fields back to default values (no MODIFIED badges).
10. Take screenshots: full page, group expanded with one modified field, history modal, diff modal.

## Hand-off

Write `handoffs/03-settings-config-shell-algorithm.md`. Critical: document the exact shape of `defaults` payload (group metadata structure, per-field metadata structure including ge/le constraints, RETRAIN tagging) so session 04 doesn't have to re-discover.

Commit: `rebuild: session 03 — settings: versioned-config shell + algorithm`.
