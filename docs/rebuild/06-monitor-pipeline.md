# Session 06 — Monitor: Pipeline + Models + Recs Debug

You are continuing the GrooveIQ dashboard rebuild. This is **session 06 of 12**.

This is the heaviest visualisation session. Three Monitor surfaces with deep visualisation needs: the pipeline flow diagram + step detail, the models readiness page, and the recommendation debug + audit + replay surfaces.

This session is **independent of sessions 03 / 04 / 05** but depends on session 02 (which built the SSE bus and stat tile / panel components).

## Read first

1. `docs/rebuild/README.md`
2. All prior hand-offs (01–02 minimum; others if done).
3. `docs/rebuild/components.md`
4. `docs/rebuild/api-map.md` → "Pipeline", "Models", "Recs Debug" rows.
5. `gui-rework-plan.md` § "Pipeline tab → Monitor → Pipeline + Models" + § "Audit → Monitor → Recs Debug → Recommendation Audit".
6. The current Pipeline tab in `app/static/js/app.js` — search `// Pipeline View` and `// Pipeline Step Detail Views`. Port behaviour faithfully.
7. The current Audit view — search `// Audit View — recommendation request audit & replay`.
8. The current Recommendations Debug view — search `// Recommendations View` and look for the `?debug=true` rendering branch.

## Goal

Three pages fully working:
- `#/monitor/pipeline` — flow diagram, run header, model readiness summary, errors panel, run history. SSE-driven live updates.
- `#/monitor/models` — extracted model readiness as its own surface (full detail, not just the 6 rows on Overview).
- `#/monitor/recs-debug` — three sub-views: Audit Sessions list, Request Detail, Replay. Plus `Debug Recs` (live `?debug=true` trace) and per-step pipeline detail surfaces (sessionizer / scoring / taste / ranker rich views).

## Out of scope

- System Health (next session).
- User Diagnostics (next session).
- Integrations (next session).

## Tasks (ordered)

### A. Monitor → Pipeline

`GIQ.pages.monitor.pipeline`:

1. **Page header** — eyebrow MONITOR, title "Pipeline", right side: SSE Connect/Disconnect toggle (the SSE-pill in the topbar is informational; this button forces (re)connection), "Run Pipeline" button (jumps to `#/actions/pipeline-ml`), "Reset Pipeline" button (jumps to same).
2. **Run header card** — current/last run: trigger badge (manual / startup / auto), status badge (completed / failed / running), config_version badge, run ID mono, duration, started timestamp. From `GET /v1/pipeline/status?limit=1`.
3. **Flow diagram** — horizontal flow of 10 step nodes with arrows between them. Each node:
   - Background `--paper`, border `1px solid --line-soft`, radius 10px, padding `10px 12px`.
   - Step icon + step label + duration (or `running…` if active) + key metric (e.g. `1,420 sessions`).
   - Status colour: pending = `--ink-3`, running = pulsing `--accent`, completed = static `--accent`, failed = `--wine`.
   - Click → expands a detail panel below the diagram.
4. **Step detail panel** — when a node is selected:
   - Header: step icon + label + description (from STEP_META in current code).
   - Status badge.
   - Metrics grid: all `step.metrics` as `key: value` pairs.
   - Started + duration timestamps.
   - Error block (`<pre>`) if `step.status === 'failed'`.
   - **Per-step rich detail views** (port from current code; behaviour-equivalent, design-language updated):
     - sessionizer: 4 stat cards (total sessions / avg duration / avg tracks per session / avg skip rate %) + skip-rate distribution bar chart + sessions-per-user histogram. Endpoint `GET /v1/pipeline/stats/sessionizer`.
     - track_scoring: stat cards + satisfaction distribution (10-bin histogram with monochrome lavender bars) + signal breakdown (full_listens / likes / repeats / playlist_adds / early_skips / dislikes — bar chart) + top 10 / bottom 10 scored tracks. Endpoint `GET /v1/pipeline/stats/scoring`.
     - taste_profiles: user dropdown + radar chart (5 axes: energy / valence / dance / acoustic / instrumentalness) with 7-day overlay + 24-cell time-of-day heatmap + mood / device / context / output / location bar charts + behaviour summary. Endpoint `GET /v1/pipeline/stats/taste_profiles` (and `/v1/users/{id}/profile` for the per-user radar).
     - ranker: training stats + feature importance horizontal bar chart (top 20) + NDCG@10 vs baselines bar comparison + impression-to-stream funnel. Endpoint `GET /v1/recommend/stats/model`.
5. **Models readiness summary** — same 6-row component as on Overview (already built). Action link "See all →" → `#/monitor/models`.
6. **Errors panel** — collapsible, lists last 20 errors across runs. Each entry: step name + timestamp + run ID, click to expand full traceback.
7. **Run history table** — `GET /v1/pipeline/status?limit=20`. Columns: run ID (mono first 8 chars), status badge, trigger, config_version badge, mini step-status dots (10 dots, colour per step status), duration, timestamp. Click row → load that run into the flow diagram + step detail.
8. **Live SSE updates** — subscribe to all 5 events from `GIQ.sse`. Update node statuses on `step_start` / `step_complete` / `step_failed`; refresh full status on `pipeline_start` / `pipeline_end`. Don't open another EventSource — reuse the bus.

### B. Monitor → Models

`GIQ.pages.monitor.models`:

1. `GET /v1/pipeline/models`.
2. Render 6 model cards in a 2-col grid:
   - Ranker (LightGBM)
   - Collaborative Filter
   - Session Embeddings
   - SASRec
   - Session GRU
   - Last.fm Cache
3. Each card: name + status (✓ Ready or ✗ Not trained) + key stats (training_samples / n_features / engine / vocab_size / users / tracks / seeds_cached / cache_age_seconds / model_version / trained_at).
4. For Ranker, also show feature importance (top 20) and last-evaluation metrics (NDCG@10/50, baseline comparison) — same data as the rich detail view in Pipeline, but as the page's primary content here.

### C. Monitor → Recs Debug

`GIQ.pages.monitor.recsDebug`. This is a multi-view page with internal sub-navigation (NOT a sub-page of the topbar). Three modes:

#### Mode 1: Audit Sessions list (default)

1. Filter bar: User dropdown / Surface dropdown (recommend / radio / home / search / all) / Since dropdown (24h / 7d / 30d / 90d / all) / Apply button.
2. Stats button (top-right) → modal showing `GET /v1/recommend/audit/stats` (total counts, retention, storage estimate).
3. Sessions table from `GET /v1/recommend/audit/sessions`. Columns: time · user · surface · top track · seed · candidates count · model badge · config badge · duration · "View" button → mode 2.
4. Pagination prev / next.

#### Mode 2: Request Detail

1. From `GET /v1/recommend/audit/{request_id}`.
2. Header: request_id mono + surface badge + user + timestamp + model + config_version.
3. Request context chips row: device_type, output_type, context_type, location_label, hour_of_day, day_of_week, seed_type, seed_value, genre, mood — only chips for present values.
4. Replay buttons: "Replay (rerank only)" / "Replay (full)" → mode 3.
5. **Candidate sources panel** — bar chart of `candidates_by_source` with percentages.
6. **Reranker actions panel** — summary chip + bar chart of action types (freshness_boost / skip_suppression / etc.).
7. **Candidates table** with a row per candidate. Columns: rank arrow (↑/↓ delta from `pre_rerank_position` to `final_position`), track, source chips (typographic, never colour-filled), score, "Why?" button per row. "Why?" expands the full feature_vector dict sorted by magnitude.

#### Mode 3: Replay

1. `POST /v1/recommend/audit/{request_id}/replay` body `{"mode": "rerank_only" | "full"}`.
2. Summary card: top-10 overlap %, Kendall's τ, avg |Δrank|, new_in_top10, dropped_from_top10.
3. Side-by-side rank delta table: original vs new. Colour-code: green = moved up, red = moved down, green-with-NEW-badge = newly in top 10, red-with-DROP-badge = dropped out.
4. Toggle to switch modes (rerank_only ↔ full).

#### Live Debug Recs (separate sub-mode reachable from Recommendations page)

1. Reachable from `?debug=true` deep link from Explore → Recommendations (session 09).
2. Render: candidates_by_source bar, reranker actions summary, two-column rank comparison, click-to-expand feature vector inspector.
3. Same visual treatment as Mode 2 above; identical components reused.

### D. Component reuse opportunity

The candidate sources / reranker actions / candidates-with-feature-vector views appear in both Audit Detail and Live Debug. Extract `GIQ.components.candidatePanel({ candidates, sourcesByCount, rerankerActions })` so the two surfaces share rendering.

## Verification

1. Load `#/monitor/pipeline`. Run a pipeline from Actions and watch the flow diagram update live (within ~1s per step) via SSE.
2. Click sessionizer node — rich detail loads with histograms.
3. Click ranker node — feature importance chart renders.
4. Force a step to fail (or pick a historical failed run) — error panel shows the traceback, run history shows the failure dot.
5. Load `#/monitor/models`. All 6 cards render. Stale models (e.g. SASRec) correctly show wine-coloured badge.
6. Load `#/monitor/recs-debug`. Filter sessions by user, sort by time. Click into a request detail. Verify candidates table renders with rank arrows. Click "Why?" on a candidate — feature vector expands.
7. Click Replay (rerank only) — summary card + rank delta table render. Toggle to Replay (full) — rebuilds and refreshes.
8. From Explore → Recommendations (still stub at this point), the `debug→` deep link will resolve here in session 09. For now, manually navigate to `#/monitor/recs-debug?debug={request_id}` and verify the Live Debug mode renders.

## Hand-off

Write `handoffs/06-monitor-pipeline.md`. Critical notes for sessions 09 / 10 / 11:
- The `debug→` deep-link pattern (URL fragment + handler in monitor.js).
- The shape of `feature_vector` — many fields, dynamic per ranker version.
- Any quirks of `GET /v1/recommend/audit/sessions` filtering.
- Any SSE event you encountered that's NOT in the 5-event list (`pipeline_start`, `pipeline_end`, `step_start`, `step_complete`, `step_failed`).

Commit: `rebuild: session 06 — monitor: pipeline + models + recs debug`.
