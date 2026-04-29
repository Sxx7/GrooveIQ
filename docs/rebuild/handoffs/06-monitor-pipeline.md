# Hand-off: Session 06 — Monitor: Pipeline + Models + Recs Debug

## Status
- [x] Session goal achieved
- [x] Visual verification done (mocked-API harness in `preview_eval`; same approach as sessions 02–05 since the local machine has no FastAPI runtime and the remote backend has no CORS for cross-origin static-server preview)
- [x] No regressions in old `/dashboard` (zero modifications to `app/static/index.html`, `js/app.js`, `css/style.css`)
- [x] Overview (session 02) regression-checked — still renders with 6 stat tiles, no console errors
- [x] Committed on `gui-rebuild` branch with message `rebuild: session 06 — monitor: pipeline + models + recs debug`

## What landed

Three new Monitor surfaces are now real:

- `#/monitor/pipeline` — **the heaviest page in the build**. Page header (eyebrow + title + Connect/Disconnect SSE toggle + "Run Pipeline" / "Reset Pipeline" jump links to Actions); run header card (TRIGGER · STATUS · CONFIG · RUN ID · DURATION · STARTED chips); horizontal flow diagram with 10 nodes + arrows (each node carries icon + label + duration + key metric, with status-driven colours: pending=`--ink-3`, running=pulsing `--accent`, completed=static `--accent`, failed=`--wine`); click-to-select step detail panel with metrics grid + error block; **rich detail views** for sessionizer / track_scoring / taste_profiles / ranker (stat tiles + bar charts + radar + heatmap + feature importance ports); models readiness summary panel (re-uses Overview's 6-row component, with "See all →" → `#/monitor/models`); collapsible errors panel (last 20 errors with `<details>` per entry); 7-column run history table with mini step-status dots; and **live SSE-driven node updates** that mutate the selected run in place on `step_start` / `step_complete` / `step_failed` (and full refresh on `pipeline_start` / `pipeline_end`). The page subscribes to the shared `GIQ.sse` bus — never opens its own EventSource.

- `#/monitor/models` — extracted models readiness as a primary surface. 6 model cards in a 2-col grid (Ranker / Collaborative Filter / Session Embeddings / SASRec / Session GRU / Last.fm Cache), each with its name + READY/NOT TRAINED/NO DATA badge (lavender for ready, wine for stale) + a sub-line + a stats list (training_samples / n_features / engine / vocab_size / users / tracks / seeds_cached / cache_age_seconds / model_version / trained_at). Below the cards, the full Ranker rich detail (NDCG@10 stat tile with `+lift% vs popularity` delta, top-20 feature importance bar chart, NDCG vs baselines comparison, impression-to-stream funnel) — same data as the rich detail in Pipeline, but here as the page's primary content.

- `#/monitor/recs-debug` — multi-view page with internal sub-navigation, three modes:
  1. **Sessions list** (default): filter bar (User · Surface · Since · Apply), Storage stats button (top-right opens modal showing total counts, retention, storage estimate), paginated audit-sessions table with When · User · Surface · Top track · Seed · Cands · Model · Cfg · Duration columns and a "View →" button per row.
  2. **Request detail**: header card with REQUEST · SURFACE eyebrow + mono request_id + user/timestamp/model/cfg badges + request-context chips (only chips for present values from `device_type, output_type, context_type, location_label, hour_of_day, day_of_week, seed_type, seed_value, genre, mood`); "Replay (rerank only)" / "Replay (full)" buttons → mode 3; reused **`GIQ.components.candidatePanel({...})`** shows candidate sources bar chart, reranker actions summary + bar chart, and the candidates table with rank delta arrows ↑/↓, source/action chips (typographic, never colour-filled), per-row "Why?" feature-vector inspector.
  3. **Replay**: 5 stat tiles (Top-10 overlap %, Kendall τ, Avg |Δrank|, NEW in top 10, DROPPED from top 10), side-by-side rank-delta table colour-coded green=up / red=down / NEW badge / DROP badge with strikethrough.

Plus a **Live Debug Recs** sub-mode reachable via deep link. Two `?debug=` shapes are supported:
- `?debug=<request_id>` — loads the persisted audit row and jumps into the Request detail mode (re-uses the candidate panel rendering).
- `?debug=user:<user_id>` — fires a fresh `GET /v1/recommend/{user_id}?debug=true` and renders the live trace into the same candidate panel by reshaping `candidates_by_source / pre_rerank / reranker_actions / feature_vectors` into the audit "candidates" array.

Session 09 (Explore → Recommendations) will deep-link into the second form when the user clicks "Debug Recs" on a recommendations result.

A new shared component **`GIQ.components.candidatePanel({ candidatesByCount, candidates, candidatesTotal, limitRequested })`** lives in `components.js` and renders the three reusable panels (sources bar chart, reranker actions, candidates-with-feature-vector) used by both Audit Detail and Live Debug. Component pattern matches the rest of the rebuild — function returns a DOM element, no inline handlers.

## File inventory after this session

Substantially modified:
- [app/static/js/v2/monitor.js](app/static/js/v2/monitor.js) — went from 648 lines (Overview only) to ~1390 lines. Three new top-level page renderers (`renderPipeline`, `renderModels`, `renderRecsDebug`) plus internal helpers: `STEP_META` / `STEP_ORDER` / `RICH_DETAIL_STEPS` constants; `renderRunHeader`, `renderFlow`, `renderStepDetail`, `renderRichDetail`, `renderSessionizerRich`, `renderScoringRich`, `renderTasteRich`, `renderTasteForUser`, `renderRankerRich`; `renderModelsSummary`, `renderErrors`, `collectErrors`, `renderHistory`; `buildRecsDebugRight`, `showAuditStatsModal`, `renderRDSessions`, `fetchSessions`, `renderRDDetail`, `drawDetail`, `renderRDReplay`, `drawReplay`, `renderRDLiveDebug`, `drawLiveDebug`; helper utilities `fmtMs`, `firstMetricKey`, `fmtMetricLabel`, `fmtMetricVal`, `barList`, `buildRadarChart`, `buildModelCard`, `buildModelsSummaryList`, `chip`, `labeled`. Stub list narrowed from 10 → 7 sub-pages (only system-health, user-diagnostics, integrations, downloads, lidarr-backfill, discovery, charts remain stubs for sessions 07–08).
- [app/static/js/v2/components.js](app/static/js/v2/components.js) — appended `GIQ.components.candidatePanel` + private `buildFeatureInspector` helper (~190 new lines).
- [app/static/css/pages.css](app/static/css/pages.css) — appended ~600 new lines covering: `.pipeline-body / .pipeline-bottom-grid / .pipeline-empty`, `.run-header-grid / .run-chip`, `.pipeline-flow / .pipe-arrow / .pipe-node` (with `status-{pending,running,completed,failed,skipped}` variants and `pipeNodePulse` keyframes), `.step-detail-body / .step-status-chip / .step-metric-grid / .step-metric-cell / .step-error-block`, `.rich-detail-wrap / .rich-stats-row / .rich-two-col / .rich-track-list / .rich-track-row / .rich-track-score`, `.bar-list / .bar-row / .bar-fill-{accent,wine,muted}`, `.taste-rich-wrap / .taste-rich-head / .taste-user-select / .radar-wrap / .radar-svg / .radar-axis-label / .radar-legend / .radar-swatch`, `.taste-heatmap / .taste-heat-cell`, `.errors-list / .error-entry / .error-step / .error-time`, `.history-table / .history-row / .status-chip / .step-dots / .step-dot`, `.models-page-body / .models-cards-grid / .model-card / .model-card-{head,name,state,sub,stats,stat-row}`, `.recs-debug-body / .rd-filter / .rd-filter-field / .rd-select / .rd-list-host / .rd-sessions-table / .rd-row / .rd-head / .rd-truncate / .rd-chip / .rd-source-chip / .rd-pagination`, `.rd-detail-wrap / .rd-detail-header / .rd-detail-id / .rd-detail-meta / .rd-context-chips / .rd-context-chip / .rd-detail-actions / .rd-action-summary`, `.candidate-panel-wrap / .candidate-panel-grid / .rd-candidates-table / .rd-cand-rank / .rd-cand-sources / .rd-cand-actions / .rd-cand-why / .rd-feature-inspector / .rd-feature-grid / .rd-feature-cell`, `.rd-up / .rd-down / .rd-new-badge / .rd-drop-badge`, `.rd-replay-wrap / .rd-replay-header / .rd-replay-summary / .rd-delta-table` with `delta-{up,down,new,drop}` row variants, `.audit-stats-modal / .audit-stats-row`, `.vc-btn-sm`. Plus three responsive media-query blocks at 700/900/1100px.

Doc:
- [docs/rebuild/handoffs/06-monitor-pipeline.md](docs/rebuild/handoffs/06-monitor-pipeline.md) — this file.

No new top-level files. The `dashboard-v2.html` script-tag list is unchanged.

## State of the dashboard at end of session

Working at `/static/dashboard-v2.html`:

`#/monitor/pipeline`:
- Page header eyebrow `MONITOR`, title "Pipeline", right rail: SSE Connect/Disconnect ghost btn (dynamic label), Run Pipeline (primary lavender, `<a>` to `#/actions/pipeline-ml`), Reset Pipeline (ghost, same target).
- Run header panel with 6 chips. Status chip recolours via `.run-chip.{good,wine,muted}` modifier classes.
- Flow panel with 10 nodes in a horizontally-scrolling row; each node clickable; running nodes pulse with both border + box-shadow; completed nodes get a faint lavender border; failed get a wine border; skipped go to 0.55 opacity.
- Click any node → step detail panel below shows status chip, duration, started_at, metrics grid, optional error `<pre>`. Click again to deselect.
- Click sessionizer/track_scoring/taste_profiles/ranker → rich detail panel below the step detail loads from the corresponding `/v1/pipeline/stats/*` endpoint. Sessionizer renders 4 stat tiles + skip-rate distribution + sessions-per-user histogram. Scoring renders score distribution + signal breakdown + top/bottom score tables. Taste profiles renders user dropdown + radar chart (3 series: all-time/short/long) + 24-cell heatmap + mood/device/context/output/location bar charts + behaviour stat tiles. Ranker renders training stats + feature importance top-20 + NDCG vs baselines + i2s funnel.
- Models summary panel at right of the bottom grid (lavender/wine dots, READY/STALE chips), "See all →" jumps to `#/monitor/models`.
- Errors panel collapsible — uses native `<details>`, click summary to expand the wine-tinted traceback `<pre>`.
- Run history table — 7 columns, mini step-status dots per row, click a row to load that run into the flow + detail.
- SSE: subscribes to all 5 events from the bus (`pipeline_start`, `pipeline_end`, `step_start`, `step_complete`, `step_failed`) plus the bus's own `connected` / `disconnected` events to keep the toggle button in sync. Per-step events mutate the in-memory step in the selected run and re-render — within ~1 frame of the SSE message.

`#/monitor/models`:
- Page header `MONITOR / Models`, no right-rail.
- 2-col grid of 6 model cards (collapses to 1-col below 1100px). Each card has name + state badge, sub-line (engine/sub-type), and a stats list with the per-model stats from `/v1/pipeline/models`. Empty stats fall to "No stats yet." per-card.
- Below the cards, the full Ranker rich detail block (same component as Pipeline's rich detail). Stat tile NDCG@10 carries a delta line `+51% vs popularity` in lavender (or wine for negative).

`#/monitor/recs-debug`:
- Page header `MONITOR / Recs Debug`, right-rail: Storage stats (always present), `← Back to sessions` / `← Back to detail` (only in non-default modes).
- Sessions mode: filter bar (User select / Surface select / Since select / Apply primary). Sessions table with rank arrow + colour-coded by source. Empty state explains how to populate. Pagination prev/next when `len === limit`.
- Detail mode: header card with eyebrow + request_id mono + meta row of badges + context chips row. Replay buttons in the top-right of the header. Below: candidate panel grid (sources + reranker actions, 1-col below 1100px) + candidates table with per-row Why? expandable feature inspector.
- Replay mode: header card mirrors detail's, with replay-mode toggle buttons (current mode disabled). 5 stat tiles. Rank-delta table with colour-coded rows.
- `?debug=<request_id>` deep-link routes to detail mode (uses persisted audit). `?debug=user:<userId>` routes to Live Debug mode (fires `?debug=true`).

Still stubbed (sessions 07–08):
- system-health, user-diagnostics, integrations, downloads, lidarr-backfill, discovery, charts (all under Monitor) — render the foundation-session "TBD" placeholder.

## Decisions made (with reasoning)

- **Pipeline page is one big `renderPipeline` closure with shared `state` and a single `renderAll()` re-render entry-point.** The page has 7 panels that all need to update on the same data set (run change, step click, SSE event). Rather than multiple disjoint timers, I keep `state` private to the closure, render to fixed host elements, and call `renderAll()` whenever any input changes. SSE events for step lifecycles mutate `state` in place and call `renderAll()`. This avoids re-fetching `/v1/pipeline/status` for every SSE event while still keeping the UI consistent.
- **Rich detail panels load lazily on step selection, not on every refresh.** When the user clicks `sessionizer`, only then does the page hit `/v1/pipeline/stats/sessionizer`. Switching steps fetches the new endpoint. Re-rendering the parent page (e.g. SSE event) doesn't refetch the rich detail unless the selected step changed. This keeps the SSE update path cheap.
- **Step ID drift between code surfaces.** The current pipeline code emits `lastfm_candidates` (per `app/services/lastfm_candidates.py` and the scheduler step list), but `/v1/pipeline/models` returns the cache state under `lastfm_cache`. I added both keys to `STEP_META` so the flow diagram tolerates either. The 10-step `STEP_ORDER` uses `lastfm_candidates`. If the backend ever flips the step name, change the order array; meta is keyed by both.
- **`pipe-arrow` element is non-interactive and lives between nodes**, not as a `::before` on the node. This makes alignment in a flex row trivial and lets `running` arrows pulse independently of the node they precede.
- **Errors panel uses native `<details>`** rather than custom expand/collapse logic. Simpler, accessible, and matches the design hand-off's "click to expand full traceback" spec.
- **`barList()` helper takes a row-array `{label, value, colorClass?}` shape and a `max` value.** Single helper handles all the bar-chart needs across sessionizer / scoring / ranker / candidate-sources / reranker-actions / NDCG-comparison / i2s funnel. Accepts an `opts.fmtVal` override for non-integer formatting (e.g. `0.7842` for NDCG, `1240` for feature importance scores).
- **Radar chart is custom SVG, not Canvas.** Canvas adds DPI scaling complexity for crisp text and would be heavier than the data warrants (5 axes × 3 series). The SVG version is ~80 lines, scales cleanly, and matches the rest of the dashboard's all-SVG charting style (area chart in `components.js`).
- **Recs Debug uses `state.mode` for sub-navigation, not nested router slugs.** The router enumerates allowed sub-pages per bucket and `recs-debug` is one slug. Sub-modes (`sessions` / `detail` / `replay` / `debug`) are page-internal state. URL params let detail and Live Debug modes be deep-linkable: `?request=<id>`, `?debug=<id>`, `?debug=user:<uid>`. The session-list filter inputs are NOT in the URL — they're page-local state, since persisting them would clutter the URL on every keystroke. (Session 12 may want to rethink this for shareable filter URLs.)
- **`GIQ.components.candidatePanel({...})` is a single function returning a DOM container with all three sub-panels appended**, rather than three separate components or a config-driven layout. The three panels always appear together with the same data shape (audit candidates), and pulling them apart would just add boilerplate at every call site. Both Audit Detail and Live Debug call it once with the same props (Live Debug reshapes its raw data into the audit-candidate shape first).
- **Live Debug deep-link supports two shapes: `?debug=<request_id>` and `?debug=user:<userId>`.** Re-using the persisted audit (first form) is cheaper than re-running the pipeline trace, especially since the persisted audit carries the full feature vector. The user-shorthand form is reserved for the "Debug Recs" button on a fresh recommendations request from session 09 — it fires `GET /v1/recommend/{user}?debug=true` and reshapes the live response. Both forms render the same `candidatePanel`. Decision: don't auto-detect — explicit prefix avoids ambiguity if a request_id ever starts with `user:`.
- **Live Debug raw response → audit-candidate shape reshape.** The live `?debug=true` response carries `candidates_by_source` as `{source: [{track_id, score}, ...]}` arrays, but the audit detail uses `{source: count}` counts. The Live Debug code computes counts from the arrays and also walks each source's array to build a `track_id → sources[]` reverse index used for the candidates table source chips. The pre-rerank position comes from `debug.pre_rerank`. The feature vector comes from `debug.feature_vectors[track_id]`. Reranker actions come from `debug.reranker_actions` filtered by track_id.
- **SSE handler signatures vary across event types.** `pipeline_start` / `pipeline_end` carry `{run_id, status, ...}`. `step_start` / `step_complete` / `step_failed` carry `{run_id, step, status?, duration_ms?, metrics?, error?, timestamp}`. The pipeline page's `onStepEvent` walker uses presence-based dispatch (`error` field present → failed; `duration_ms` present → completed; otherwise running) rather than a separate handler per event type. This is robust against future event-name additions.
- **"Run Pipeline" / "Reset Pipeline" buttons are `<a>` tags, not `<button>` with onClick.** Per the brief, they "jump to `#/actions/pipeline-ml`" — same hash navigation pattern used everywhere else in the rebuild. The Actions page has the actual trigger buttons (session 05). This avoids cross-page coupling.

## Gotchas for the next session

- **`debug→` deep-link pattern**: from a future Recommendations page (session 09), navigate to `#/monitor/recs-debug?debug=user:<userId>` to launch Live Debug for that user. Or `#/monitor/recs-debug?debug=<request_id>` to inspect a specific persisted audit. The handler is `renderRDLiveDebug` in `monitor.js`. The router-level mechanism is already in place via `params.debug`.
- **`feature_vector` is dynamic per-ranker-version.** The persisted shape is `{[feature_name]: number}` but the keys vary as the ranker evolves (currently 39 fields per session 5 of the master plan, but the backend can grow this). The inspector sorts by `Math.abs(value)` desc and renders all keys — never assume a fixed schema. Keys can include `audio_centroid_sim`, `sequential_score`, `taste_drift_score`, `delta_*` short/long deltas, `is_mobile`, `is_headphones`, `device_affinity`, etc.
- **`/v1/recommend/audit/sessions` filtering quirks**:
  - When `user_id` is omitted, the backend requires admin (the API key must be flagged admin in the backend). Non-admin keys must pass `user_id` to scope to themselves.
  - `since_days=0` means "all time" — translated client-side by NOT sending the param at all.
  - The `surface` enum is `recommend_api | radio | home | search`. Other surfaces aren't currently emitted but the filter dropdown is open-ended.
  - `limit` capped at 200 server-side (we use 50). `offset` is 0-based.
  - The list is sorted by `created_at DESC` server-side (newest first). Pagination is offset-based.
  - `r.top_track` is NOT always populated. When it's null, render `—`.
  - `r.context_id` (radio session_id) and `r.seed_track_id` are mutually exclusive — radio carries `context_id`, non-radio carries `seed_track_id`.
- **`/v1/pipeline/status` `current` is null when no pipeline is running.** Don't assume it's always populated. The history is always at least `[]`. The selected run defaults to `current ?? history[0] ?? null`.
- **`/v1/pipeline/models` returns sub-objects keyed by service name** (`ranker`, `collab_filter`, `session_embeddings`, `sasrec`, `session_gru`, `lastfm_cache`). The top-level also carries `latest_evaluation` and `impressions` — these are NOT under `ranker`. The Ranker rich detail block reads from both locations.
- **SSE events emitted that are NOT in the 5-event spec list:** I encountered no extra event names beyond `pipeline_start`, `pipeline_end`, `step_start`, `step_complete`, `step_failed`. The `connected` / `disconnected` events come from the `GIQ.sse` bus itself (synthetic — emitted on connection state change), not from the server. If the backend ever adds a `pipeline_progress` event (or similar), the bus will faithfully relay it to subscribers, and pages can opt in.
- **The SSE Connect/Disconnect button is a HARD toggle, not a transient retry.** Clicking it sets `manualDisconnect=true` on the bus, so it stays disconnected even if the API key is still valid. Clicking again clears the flag and reconnects. The topbar SSE pill is informational only — it shows the connection state but doesn't drive it.
- **The flow diagram horizontally scrolls below 1100px width.** All 10 nodes always render; if the viewport is narrow, the panel scrolls. Don't mistake the scroll cue for a layout bug.
- **`renderRichDetail` clears its host on every step click**, then re-fetches. If a user rapidly clicks between steps, you can race two in-flight fetches. The current code doesn't AbortController-cancel — the second response just overwrites the first. Acceptable for a self-hosted dashboard; production would want abortable fetches.
- **The pipeline page bypasses `state.range` toggle entirely.** Pipeline data is per-run, not windowed; the range toggle exists only on Overview / System Health.
- **Models card "NO DATA" state vs "NOT TRAINED".** `NO DATA` means the response key doesn't exist (network error, partial response). `NOT TRAINED` means the key is present but `trained === false && built === false`. Treat them differently — NO DATA is a transport-level issue, NOT TRAINED is a model-state issue.
- **`barList`'s `bar-label` column is fixed `minmax(80px, 140px)`.** Long labels (e.g. `lastfm_similar_track_score_normalized`) will ellipsis. If a future panel needs a wider label column, pass `opts.labelWidth` and add a CSS variant. Not blocking; just a minor polish item.
- **Mocked-API harness still required for verification** — same gotcha as sessions 02–05. Documented mocks for: `/v1/pipeline/status?limit=20`, `/v1/pipeline/models`, `/v1/pipeline/stats/sessionizer`, `/v1/recommend/audit/sessions`, `/v1/recommend/audit/{request_id}`, `/v1/recommend/audit/{request_id}/replay`, `/v1/recommend/audit/stats`, `/v1/recommend/stats/model`. The shapes mirror the real API exactly per `app/api/routes/stats.py`, `app/api/routes/reco_audit.py`, and the existing `app/static/js/app.js` consumer code.
- **`collectErrors` walks all `history` runs** (up to 20 by default) and flattens each run's `steps[].error`. Sorted DESC by `step.ended_at || step.started_at || run.started_at`. Capped at 20. If a single run has more than 20 step errors (unlikely), the cap stays per-page, not per-run.
- **`/v1/recommend/audit/{request_id}/replay` body is `{"mode": "rerank_only" | "full"}`**, not just `mode` as a query param. The page POSTs the JSON body. Replay can be slow for "full" mode (rebuilds features) — there's no progress indicator, just a "Running replay…" empty state. If replay takes >5s, consider adding a polling or SSE-driven progress indicator in a polish session.
- **The Replay rank-delta table colour rules**: `delta > 0` → `delta-up` (lavender background tint), `delta < 0` → `delta-down` (wine tint), `original_position == null` → `delta-new` (lavender left border + NEW badge), `new_position == null` → `delta-drop` (wine left border + DROP badge + line-through). These are stacked CSS classes, mutually exclusive on a row.

## Open issues / TODOs

- The "Live Debug Recs" with `?debug=user:<userId>` shape is wired but the **caller (Recommendations page in session 09)** doesn't exist yet. Session 09 will add a "Debug Recs" button/link on its results page that builds this URL.
- The Pipeline page does not expose a "select run" via URL param. If a user wants to share a deep-link to a specific historical run, they currently can't. Add `?run=<run_id>` if/when needed; the current state.selectedRunId mechanism is ready for it.
- Replay loading state shows "Running replay…" but no spinner or progress. For long "full" replays this could feel unresponsive. Polish item: add a spinner or a per-step progress (the backend doesn't currently emit progress, so this would be visual-only).
- Errors panel doesn't paginate — capped at 20. If a long-running deployment accumulates hundreds of errors, only the most recent 20 surface. Acceptable for self-hosted.
- The Audit Stats modal uses an inline body element (no separate panel/section). If we want to make stats a standalone surface, easy to lift into its own page.
- The `?debug=user:<uid>` Live Debug mode does not currently persist its trace to the audit table — it just runs `?debug=true`. The persisted audit table only catches actual production calls. If we want Live Debug traces to be replayable, the backend would need to fire-and-forget a write_audit on debug calls too. Out of scope for this session.
- The flow diagram's pulse animation (`pipeNodePulse`) uses `box-shadow` on the running node. This can cause minor reflow on slow GPUs at high refresh rates. Acceptable; if it ever becomes a perf issue, switch to a `transform: scale()` pulse on a `::before` overlay.
- `STEP_META.lastfm_candidates` and `STEP_META.lastfm_cache` are duplicates with the same icon/label/desc. If the backend ever stabilizes on one name, dedupe.

## Verification screenshots

Captured inline in the session transcript via `mcp__Claude_Preview__preview_screenshot` (mocked-API state):

1. **Pipeline (initial)** — Run header with 6 chips, flow diagram with 10 nodes (sasrec/session_gru/music_map scrolled off-screen), models summary panel with 6 rows, errors panel showing 1 Session GRU error, no rich detail (no step selected).
2. **Pipeline → Sessionizer rich detail** — Sessionizer node selected (lavender border), step detail shows COMPLETED chip + duration + metric grid, rich detail block below shows 4 stat tiles (1.420 sessions / 30m 20s / 8.2 / 21.0%) plus skip-rate distribution + sessions-per-user bar charts.
3. **Models page** — 6 cards in 2-col grid (Ranker, Collaborative Filter, Session Embeddings, SASRec [NOT TRAINED in wine], Session GRU [NOT TRAINED in wine], Last.fm Cache). Below: Ranker rich detail with NDCG@10 stat tile (`0.7842` + `+51% vs popularity` in lavender) and feature importance bar chart.
4. **Recs Debug → Sessions list** — filter bar + Storage stats button, table with 2 audit sessions (simon/recommend_api, simon/radio) showing top track, seed/ctx, candidate count, model, cfg, duration.
5. **Recs Debug → Request detail** — eyebrow `REQUEST · RECOMMEND_API`, request_id mono, meta row, 7 context chips (device_type/output_type/context_type/hour_of_day/day_of_week/seed_type/seed_value), Replay buttons; Candidate sources + Reranker actions panels in 2-col grid; Candidates table with rank arrows, source chips, raw/final scores, action chips, Why? buttons.
6. **Recs Debug → Replay (rerank only)** — header card with `REPLAY · MODE RERANK_ONLY` eyebrow + replay-mode toggle (rerank disabled, full enabled); 5 summary tiles (70% / 0.482 / 2.40 / 1 / 1); rank delta table with NEW badge (lavender) and the strikethrough DROP row in wine.

Programmatic verifications evidenced via `preview_eval`:
- 10 pipe nodes render in the flow.
- Click failed step (Session GRU) → step detail shows FAILED chip + RuntimeError traceback `<pre>`.
- Click sessionizer → 4 stat tiles + 2 bar lists render in rich detail.
- 6 model cards render with correct READY/NOT TRAINED states.
- 2 audit sessions render with all columns; click View → detail panel renders.
- 7 context chips, 2 candidate panels, 4 candidate rows.
- Click Why? on a candidate → 6 feature cells render in the inspector grid; toggle re-collapses.
- Click Replay (rerank only) → 5 summary stat tiles + 5 delta rows + NEW badge + DROP badge + strikethrough row all render.
- Storage stats modal shows 7 rows.
- Live Debug deep link `?debug=<request_id>` resolves to detail mode.
- Overview regression check: 6 stat tiles still render, no console errors.
- No errors in browser console throughout.

## Time spent

≈ 130 min: reading session 01–05 hand-offs / `app.js` Pipeline / Audit / Recs-Debug surfaces / API route shapes (35) · monitor.js extension (renderPipeline + step detail + rich detail + 4 detail variants) (35) · monitor.js Models page (10) · monitor.js Recs Debug 4 modes + Live Debug shape-reshape (25) · candidatePanel + feature inspector in components.js (10) · pages.css (~600 new lines) (15) · preview verification + screenshots + bug-fix loop (10) · this hand-off note (10).

---

**For the next session to read:** Session 07 — Monitor: System Health + User Diagnostics + Integrations, see [docs/rebuild/07-monitor-health.md](docs/rebuild/07-monitor-health.md). Sessions 08, 09 are also unblocked (08 is independent of 06–07; 09 will deep-link into the Live Debug mode added in this session via `?debug=user:<userId>` on `#/monitor/recs-debug`).
