# Hand-off: Session 03 — Settings: versioned-config shell + Algorithm

## Status
- [x] Session goal achieved
- [x] Visual verification done (mocked-API roundtrip captured inline; same approach as session 02 since the remote backend has no CORS for cross-origin static-server preview)
- [x] No regressions in old `/dashboard` (zero modifications to `app/static/index.html`, `js/app.js`, `css/style.css`)
- [x] Committed on `gui-rebuild` branch with message `rebuild: session 03 — settings: versioned-config shell + algorithm`

## What landed

`#/settings/algorithm` now serves a fully-functional Algorithm config UI. The page hosts the new **versioned-config shell** (`GIQ.components.versionedConfigShell`) — a reusable component that handles header + button row, optional retrain banner, collapsible groups, slider+number-input fields with bidirectional sync and FP-clean spinners, dirty tracking with MODIFIED group badges, History / Diff / Reset / Discard / Save & Apply / Export / Import flows, and post-save side-effects (toast with jump-link, no auto-redirect). Two supporting primitives also landed: `GIQ.components.modal` (overlay + esc/outside-click + focus-safe close) and `GIQ.components.jumpLink` (the dashed lavender pill from the design hand-off, ready for session 04's "related →" rails). The four other Settings sub-pages remain stubs and are wired by session 04.

## File inventory after this session

Substantially modified:
- [app/static/js/v2/components.js](app/static/js/v2/components.js) — appended `GIQ.components.modal`, `GIQ.components.jumpLink`, and the full `GIQ.components.versionedConfigShell` factory (~600 lines). The shell is parameterised over `kind`, exposes `mount(host) / refresh() / dispose()`, and accepts `paths` overrides for non-standard backends (download-routing's active config lives at `/v1/downloads/routing` rather than `/v1/downloads/routing/config`, so the default-paths helper special-cases it).
- [app/static/js/v2/settings.js](app/static/js/v2/settings.js) — replaced the algorithm stub with a real renderer that ports `ALGO_FIELD_META` from the old `app.js` verbatim. Other 5 sub-pages still stub but now point to "TBD (sessions 04+)".
- [app/static/css/components.css](app/static/css/components.css) — added modal, jump-link pill, and versioned-config shell styles (~440 new lines): vc-header / vc-btn (ghost + primary) / vc-retrain-banner / vc-group / vc-field / vc-slider / vc-num / vc-num-wrap / vc-spin / vc-table / vc-diff-table / vc-loading / vc-error, plus toast jump-link styling. Single-col responsive rule at 700px.

Doc:
- [docs/rebuild/handoffs/03-settings-config-shell-algorithm.md](docs/rebuild/handoffs/03-settings-config-shell-algorithm.md) — this file.

No new top-level files; everything plugs into the session-01 scaffolding.

## State of the dashboard at end of session

Working at `/static/dashboard-v2.html#/settings/algorithm`:
- Header: eyebrow `VERSIONED CONFIG · v{n}`, title `Algorithm Config` plus muted ` · {name}` suffix, right-aligned button row in this order — History · Diff · Reset · Discard (only when dirty) · Export · Import · Save & Apply (primary).
- Save & Apply is `--accent`-filled and disabled until the working copy diverges from the active config.
- Wine retrain banner appears whenever the dirty diff touches any field in `ranker` or `session_embeddings` — its text reads "Changes include parameters that trigger a full model retrain. Saving will rebuild the affected model on the next pipeline run."
- All 7 groups render, each as a collapsible card with chevron + label + RETRAIN badge (Ranking Model, Session Embeddings) + MODIFIED badge (when its working subtree diverges from saved) + sub-line description. First group (`track_scoring`) auto-expands on first load.
- 74 fields total (the brief said "~78" — the actual schema returned 14+10+8+6+14+16+6 = 74). Each renders: capitalised label, optional inline small `RETRAIN` chip on `[RETRAIN]`-marked fields, full-width slider with lavender thumb on `--line-faint` track, mono numeric input flanked by `−` / `+` spinner buttons, then a description line and a right-aligned `default: {x}` indicator that shows only when the working value differs from the system default.
- Slider input live-syncs the numeric input + working-state without re-rendering the whole tree (focus and scroll preserved). Numeric input clamps to ge/le on blur. Spinners step at the field's `step` value, with FP rounding to step precision so 4 + 0.1 yields exactly `4.1`.
- Save & Apply triggers `PUT /v1/algorithm/config { name, config }`, then runs the configured side-effect (`POST /v1/pipeline/reset`), then a 7-second toast — `Saved. Pipeline reset triggered` + a `Pipeline →` jump-link to `#/monitor/pipeline`. The user is **not** auto-redirected; clicking the link does that.
- Discard restores the saved working copy after a confirm.
- Reset to Defaults posts to `/v1/algorithm/config/reset` and refreshes (creates a new active version).
- History modal lists prior versions (version, name, active flag, created time-ago) with per-row Activate (rolls back via `POST /v1/algorithm/config/activate/{v}`) and Diff (loads the version via `GET /v1/algorithm/config/{v}` and opens a diff modal vs the current working copy).
- Diff modal lays out per-group tables (Field · From · To) with `from` in `--wine` and `to` in `--accent`. Closes on × / Escape / outside click.
- Export downloads `grooveiq-algorithm-v{n}.json` via `GET /v1/algorithm/config/export`.
- Import accepts a JSON file plus an optional name, posts to `/v1/algorithm/config/import`, and refreshes the working copy from the new active row.
- Below 700px the fields grid collapses to a single column. The button row wraps without breaking the layout.

Stubbed:
- The other 5 Settings sub-pages (Download Routing, Lidarr Backfill, Connections, Users, Onboarding) still render placeholder cards with "TBD (sessions 04+)". Session 04 plugs Download Routing and Lidarr Backfill into this same shell.

## Decisions made (with reasoning)

- **`fieldMeta` is a required parameter, not derived from the API.** The brief said "Pydantic ge/le constraints come back from `/{kind}/config/defaults` per field" but the actual `/v1/algorithm/config/defaults` endpoint only emits `{config, groups}` — no per-field metadata, no ge/le, no per-field descriptions. The schema lives client-side in `ALGO_FIELD_META` (ported verbatim from the old `app.js`). The shell takes a `fieldMeta: { [groupKey]: { [fieldKey]: { desc, min, max, step, integer? } } }` parameter so each consumer page supplies its own. Falls back to a sensible default (integer 0–100 step 1, or float 0–10 step 0.1) when a field is missing from `fieldMeta`. **Session 04 must do the same**: lift the `dlFieldMeta`-style tables out of the old `app.js` for download routing + lidarr backfill (or build them from scratch — the routing config has structurally different fields like dropdowns and lists that won't fit slider+number, so consider whether the shell needs an extension point for custom field renderers).
- **`paths` override hook is available but optional.** The default path resolver in `_defaultConfigPaths(kind)` knows that algorithm and lidarr-backfill use `/v1/{kind}/config/...` while `downloads/routing` uses `/v1/downloads/routing/...` (the active GET sits at the bare prefix). Session 04 can pass `kind: 'downloads/routing'` and the right paths drop in automatically; for any future divergence, `paths: { active, defaults, history, version, save, reset, activate, export, import }` is accepted and overrides the defaults wholesale.
- **`saveSideEffect` is a structured callback rather than a hard-coded behaviour.** Algorithm specifies `{ label: 'Pipeline reset triggered', onSave: () => GIQ.api.post('/v1/pipeline/reset'), jumpHash: '#/monitor/pipeline', jumpLabel: 'Pipeline →' }`. If `onSave` throws, a separate `warning` toast is shown instead of failing the whole save. For session 04: download-routing has no side-effect (just a toast); lidarr-backfill probably wants to call `apply_lidarr_backfill_config` to re-register scheduler jobs — but that already happens server-side on PUT, so the front-end probably just needs a "Save & Apply" with no jump.
- **The shell mounts into a host element, returns `{ mount, refresh, dispose }`.** Page renderers create a `<div class="vc-shell">`, append it to `root`, then call `shell.mount(host)`. The page renderer returns `() => shell.dispose()` for the router's `cleanup` hook so any open modals get closed on navigation away. (The shell's modal lifecycle already closes on Escape / outside-click / × button — this is just the safety net.)
- **Slider drag does NOT trigger a full re-render.** Each `setValue(v)` call mutates the working state and updates exactly: the row's `value` attributes (slider + num), the row's `dirty` class, the field's "default:" indicator, the group's MODIFIED badge (added/removed in place), and the page header (re-rendered to update Save/Discard button state and the retrain banner visibility). This keeps focus/scroll stable across long sliders. Full re-renders happen only on Save / Discard / Reset / Activate / Import to refresh from server data.
- **FP cleanup via `roundToStep`.** When stepping or syncing, the value is rounded to the step's decimal count (`step=0.1` → 1 decimal, `step=0.005` → 3 decimals). Prevents the classic `0.30000000000000004` from showing in the numeric input.
- **Modals are siblings of `#app`, attached to `document.body`.** Avoids stacking-context bugs when the shell sits inside a scrolling parent. The modal opens with a `modal-fade-in` animation; close removes the listeners and node synchronously.
- **`confirm()`/`prompt()` are still browser dialogs.** The old app uses them too. Replacing them with custom modals is design polish for a later session; not in this session's scope.
- **Toast with jump-link** is implemented as a separate inline toast (rather than going through `GIQ.toast()`) because the existing toast doesn't support an arbitrary trailing element. The DOM is rendered inline with a `.toast-with-jump` class and a `.toast-jump` `<a>` whose `href` is `cfg.saveSideEffect.jumpHash`. Clicking the link dismisses the toast and lets the hash router handle navigation. CSS lives in `components.css`.

## Defaults endpoint shape (read this carefully — sessions 04 must follow)

`GET /v1/algorithm/config/defaults` and the equivalent `/v1/lidarr-backfill/config/defaults`, `/v1/downloads/routing/defaults` endpoints all return:

```json
{
  "config": {
    "<group_key>": {
      "<field_key>": <default_value>,
      ...
    },
    ...
  },
  "groups": [
    {
      "key": "<group_key>",
      "label": "Human Label",
      "description": "What the group does",
      "retrain_required": true | false
    },
    ...
  ]
}
```

**What's NOT in the response:**
- Per-field `min` / `max` / `step` / `ge` / `le` / `gt` / `lt` (the Pydantic constraints are on the schema class but not exposed via the endpoint).
- Per-field descriptions (the docstrings on the Pydantic class fields).
- Per-field `integer` flag (you have to infer from `Number.isInteger(default_value)` or from the field name).
- Per-field RETRAIN tagging (the description string in `ALGO_FIELD_META` includes `[RETRAIN]` as a sentinel — this is a **client-side convention**, not from the API).

**What IS in the response:**
- All default values, keyed by group → field.
- Group-level metadata: key, label, description, and a `retrain_required: bool` flag. The shell consults this flag (in addition to the optional `retrainGroups` constructor option) when deciding whether to show the retrain banner. Either source can flip it on.

`GET /v1/{kind}/config` returns the active row:

```json
{
  "id": <int>,
  "version": <int>,
  "name": <string | null>,
  "config": { ... same shape as defaults.config ... },
  "is_active": true,
  "created_at": <unix ts>,
  "created_by": <string | null>
}
```

Note `is_active`, `created_at` (Unix epoch seconds), and the `name` field is nullable.

`GET /v1/{kind}/config/history?limit=N` returns an array of these rows (newest first), each with `is_active` reflecting the current row.

`PUT /v1/{kind}/config` takes `{ name?: string | null, config: {...} }` and returns the new active row. Server creates a new version and marks it active atomically.

`POST /v1/{kind}/config/reset` and `POST /v1/{kind}/config/activate/{v}` both return the new active row.

`GET /v1/{kind}/config/export` returns the JSON file (Content-Disposition: attachment), with body `{ grooveiq_algorithm_config: true, version, name, config, exported_at }`.

`POST /v1/{kind}/config/import` takes `{ name: string, config: {...} }` and returns the new active row. Imports validate via the Pydantic schema (missing keys filled with defaults, invalid values 422).

**Activate path semantics:** activating a historical version creates a *new* version (with `name = "{old_name} (rolled back)"` per the algo route), it does not mutate the historical row. The shell calls `POST /v1/{kind}/config/activate/{old_version}` and re-fetches the active row + history.

**The "kind" string in the shell maps to the URL prefix differently for download-routing.** Algorithm and lidarr-backfill: `/v1/{kind}/config/{action}`. Download routing: `/v1/{kind}/{action}` (no trailing `/config`). The default-paths helper in `components.js` (`_defaultConfigPaths`) special-cases `kind === 'downloads/routing'`.

## Gotchas for the next session

- **Session 04 will need custom field renderers.** Download routing and lidarr backfill have nested objects + lists (chains of `{ backend, enabled, min_quality, timeout_s }`, parallel-search backend lists, drag-reorderable service priority arrays, allow/deny textarea lists, etc.) — these don't map onto slider+number controls. The current `versionedConfigShell` is **slider+number only**. Session 04 should either:
  1. Extend the shell with a `renderField({ groupKey, fieldKey, value, defaultValue, meta, onChange })` opt-in callback that, when supplied, wins over the default slider+number renderer.
  2. Or write a parallel "structured-config shell" for those two pages that shares the header/banner/groups/modals but swaps the field grid for richer panels.

  Option 1 is leaner and reuses everything. Option 2 is cleaner if the field UIs end up wildly different. **Recommend option 1**: pass `renderField` per-group or per-field, default to the slider+number when not provided.
- **`AlgorithmConfigData.model_validate` is permissive on import** — missing keys get defaults, invalid values raise 422. The import handler shows a generic "Import failed: {error}" toast on 422; the user has to read the message to know which field broke. Acceptable for v1 but worth a friendlier UI in a later polish session.
- **`Save & Apply` does NOT poll for the pipeline-reset side-effect to finish.** It fires `POST /v1/pipeline/reset` and immediately renders the toast. In practice the reset is fast, but if it took 30s+, the user might click the jump link before the new run shows up in `#/monitor/pipeline`. Not a regression — the old app had the same fire-and-forget behaviour — but worth noting.
- **The shell's first-render auto-expands the first group.** This was added to make the empty page feel less empty. If that's wrong for session 04 (e.g. for download-routing where each chain is a separate group and you want them all collapsed by default), pass an `initialExpanded: []` opt — wait, that's not implemented. If session 04 wants different first-load behaviour, add it.
- **Browser `confirm()` / `prompt()` blocks the page during testing.** I had to monkey-patch them in `preview_eval` to test the save/reset/activate flows without the dialog stalling Playwright. The old `app.js` has the same confirms; not new.
- **CORS on the remote backend is still off.** The remote `10.10.50.5:8000` backend doesn't allow cross-origin requests, so static-server preview testing requires either (a) running uvicorn locally so the same origin serves both the static files and the API, (b) rsyncing `gui-rebuild` to the dev box and previewing `http://10.10.50.5:8000/static/dashboard-v2.html`, or (c) mocking the API in `preview_eval` as I did this session. None of these are great. Worth flagging as a recurring tax for sessions 04+. (For visual verification only, mocking is fine; for behaviour verification, push the branch to the dev box once.)
- **Field labels are auto-CSS-capitalised via `text-transform: capitalize`** because the API field keys are snake_case (`w_full_listen` → `W Full Listen`). If a future field has multiple "words" that should not be cased (e.g. `n_estimators` becoming "N Estimators"), you'll need a manual label override in `fieldMeta`.
- **The `algorithm/config/defaults` endpoint does not actually return per-field constraints**, but the brief promised it would. If session 04 wants to make this true, that's a *backend* change (extend the schema-export to include `Field(ge=…, le=…, description=…)` metadata). **Do not do that during the rebuild** — instead, pass `fieldMeta` from the page renderer like Algorithm does.
- **Dirty-rendering depth.** `groupDirty(gk)` and `anyDirty()` use `JSON.stringify` for cheap-and-correct deep-equality on small flat objects. For session 04's deep-nested routing config (chains of arrays of objects), `JSON.stringify` still works as long as object key order is stable across the working copy and the saved copy. Both come from the same JSON parse path, so ordering should match. If you ever build the working copy from a different source (e.g. Pydantic schema dump), be aware key ordering may diverge.
- **Modal lifecycle**: `GIQ.components.modal` adds a `keydown` listener on `document` for the Escape key, and a click listener on the overlay. Both are removed on `close()`. Don't forget to call `close()` if you open a modal from outside the shell — leaking the listeners is silent but accumulates over many opens.
- **Toast jump links rely on `GIQ._dismissToast`** (private helper exposed by `core.js` since session 01). Don't refactor that helper without checking call-sites.

## Open issues / TODOs

- The session-04 split-page rails (Download Routing → Monitor Downloads, Lidarr Backfill → Actions queue + Monitor stats) need the `GIQ.components.jumpLink` pill plus a `<div class="related-rail">` style. The CSS for the rail itself is **not** in `components.css` yet — session 04 should add it. The pill style is done.
- The shell currently always opens the History modal showing a flat list. If history grows large, pagination would help — but the API already supports `?limit=&offset=` and the current `?limit=50` ceiling is fine for self-hosted volumes.
- The retrain banner appears whenever a retrain-flagged group has a dirty diff. It does NOT distinguish between "tweaked a non-retrain field within a retrain group" (e.g. `weight_disliked` in `ranker` is NOT marked `[RETRAIN]` but IS in the retrain group) — currently the whole group counts as retrain-triggering. The old `app.js` behaved the same way. Worth a polish later: only flip the banner when a `[RETRAIN]`-tagged field changes, even within a retrain-required group.
- `/v1/algorithm/config/defaults` should ideally expose per-field metadata so the front-end doesn't need `ALGO_FIELD_META`. Out-of-scope for the rebuild (would touch backend); flag for future.
- The 5 other Settings sub-pages (`download-routing`, `lidarr-backfill`, `connections`, `users`, `onboarding`) all still render the session-01 stub. Session 04 fills these in.
- Save's `prompt()` for an optional version name is a browser dialog — replacing with an inline modal would be a small UX win.
- `confirm()` for Discard / Reset / Activate is the same — could move to inline confirm components later.

## Verification screenshots

Captured inline in the session transcript via `preview_screenshot` (mocked-API state since the remote backend has no CORS for the static-served preview):

1. Initial load — `#/settings/algorithm`, eyebrow "VERSIONED CONFIG · v14", title "Algorithm Config · Default", 6-button right rail (History · Diff · Reset · Export · Import · Save & Apply), Track Scoring group expanded showing a 2-col grid of slider+input fields with descriptions.
2. Modified state — `w_like` slider dragged to 2.5 (lavender border, "default: 2" indicator), Track Scoring header showing MODIFIED badge, Discard appearing in the right rail, Save & Apply lavender-active, plus a wine retrain banner at the top after also bumping `n_estimators`.
3. History modal — table with 3 versions (v14 active, v13/v12 inactive). Per-row Diff button + Activate (only on inactive rows).
4. Diff modal — "Diff · working copy vs v13 (Pre-tune)" with Track Scoring + Ranking Model groups, each with field/from/to columns, `from` in wine, `to` in lavender.
5. Post-save — eyebrow updated to v17 "auto-test" (the prompt-supplied name), Save & Apply disabled, Discard hidden.

Programmatic verifications evidenced via `preview_eval`:
- 7 groups · 74 fields · 2 RETRAIN groups (`Ranking Model`, `Session Embeddings`).
- Slider drag updates dirty class / numeric input / default indicator / group MODIFIED badge / Save button state.
- Modifying `ranker.n_estimators` flips on the retrain banner.
- Save sequence: `PUT /v1/algorithm/config` → `GET history` → `POST /v1/pipeline/reset` (the side-effect), toast text is `"Saved. Pipeline reset triggered"` with link href `#/monitor/pipeline` and label `"Pipeline →"`.
- Reset sequence: `POST /v1/algorithm/config/reset` → all 74 fields drop their default-deviation indicators, all MODIFIED badges clear.
- Export: `URL.createObjectURL` called with a 2.4 KB `application/json` Blob, `<a download>` set to `grooveiq-algorithm-v18.json`.
- Import: file + name → `POST /v1/algorithm/config/import` with body `{ name, config }`, modal closes, version bumps and name updates.
- Discard: dirty-modify → click Discard → field reverts to saved value, dirty class drops, Discard button hides, Save disables.
- Spinner FP: 4 + 0.1 = `4.1` (no FP drift), 4.1 - 0.2 = `3.9`.
- Single-column responsive at <700px viewport (computed `grid-template-columns: 370px`).
- No console errors throughout.

## Time spent

≈ 95 min: reading prior docs / API shape / old `app.js` Algorithm tab (20) · shell + modal + jumpLink components (35) · settings/algorithm renderer + field meta port (10) · CSS (15) · preview verification + mock harness + screenshots (15).

---

**For the next session to read:** Session 04 — Settings: Routing + Backfill + Connections + Users + Onboarding, see [docs/rebuild/04-settings-rest.md](docs/rebuild/04-settings-rest.md). Sessions 05–08 are also unblocked by this one; they don't depend on the shell.
