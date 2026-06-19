# Recommendation Modes — Build Sequence (Phase A)

**Status:** ✅ Phase A complete — shipped
**Created:** 2026-06-02
**Companion to:** [`recommendation-modes-plan.md`](./recommendation-modes-plan.md) (design)
**Scope:** "A-now" — the confidence-based discovery dial using a Phase-A confidence *proxy* (no new model). Phase B (skip head + quantile sigma) is outlined at the end and remains **future / deferred**.

> **Reconciliation note:** All 10 Phase-A chunks below have shipped. The chunk write-ups are retained verbatim as the historical build record; a per-chunk **Status** line maps each to the module/endpoint that implements it. Only **Phase B** (real confidence model) is still future work.

### Phase A status at a glance

| # | Chunk | Status | Implemented by |
|---|-------|--------|----------------|
| 1 | Request-scoped config override mechanism | ✅ DONE | `app/services/request_config.py` (`apply_overrides`, `set_overrides`, `current_overrides`); `get_config()` deep-merge in `app/services/algorithm_config.py` |
| 2 | Modes config group (schema + defaults) | ✅ DONE | `PresetConfig` / `ModesConfig` in `app/models/algorithm_config_schema.py` (`modes` group on `AlgorithmConfigData`); read-only `GET /v1/algorithm/modes` |
| 3 | Phase-A confidence proxy (mu / sigma) | ✅ DONE | `app/services/confidence.py` (`compute_confidence`, `ConfidenceScore`, `is_proven`) |
| 4 | UCB acquisition + proven set + novelty filter | ✅ DONE | `app/services/reranker.py` + `app/services/candidate_gen.py` (override-driven; default `balanced` == today) |
| 5 | Endpoint wiring: discovery/mode params + dial + reasons | ✅ DONE | `discovery`/`mode` query params + `reasons[]` in `app/api/routes/recommend.py`; whitelisted `resolve_dial` in `app/services/modes.py` |
| 6 | SWR mix cache + prewarm / mixes endpoints | ✅ DONE | `app/services/mix_cache.py` (`get_or_build`, semaphore); `POST /v1/users/{user_id}/mixes/prewarm`, `GET /v1/users/{user_id}/mixes` |
| 7 | Radio honours the dial | ✅ DONE | `app/services/radio.py` (dial drift scaling); `discovery`/`mode` accepted on `POST /v1/radio/start` and `GET /v1/radio/{session_id}/next` |
| 8 | Frontend: multi-shelf home + dial + reason chips | ✅ DONE | `app/static/js/v2/explore.js` (Explore → Recommendations shelves + dial) |
| 9 | Frontend: modes editor (Settings) + radio posture | ✅ DONE | `app/static/js/v2/settings.js` (modes group in the versioned-config editor); read-only `GET /v1/algorithm/modes` |
| 10 | Per-dial evaluation metrics | ✅ DONE | per-dial-bucket metrics in `app/services/evaluation.py`, surfaced on the model-stats endpoint |

> **Path note:** the model-stats endpoint is registered as `GET /v1/stats/model` (the `recommend.py` decorator is `@router.get("/stats/model")` with no router prefix), not `/v1/recommend/stats/model`. The chunk text below refers to it by its older name; the live path is `/v1/stats/model`.

---

## How to use this plan

Each **chunk** below is sized to be picked up by a single fresh Claude Code session without context overflow: it lists exactly which files to read first, what to change, the tests to add, the security requirements, and a concrete **Definition of Done** that ends in *real* validation (behavior, not "it compiles").

- Work them in order. Dependencies are noted; chunks marked **(parallelizable)** can be done alongside their siblings.
- Branch: work on `dev` (per `CLAUDE.md` workflow), one branch/PR per chunk or per small group.
- A chunk is **not done** until its Definition of Done passes *and* the global checklists below are satisfied.

### Global conventions (apply to every chunk)

- **Tests:** `pytest tests/ -v` must be green. New tests use the repo conventions — `asyncio_mode = auto` (`pytest.ini`), in-memory SQLite, async fixtures like the existing `tests/`. Add focused tests in the named file; don't bloat unrelated suites.
- **Lint/format:** `ruff check .` and `ruff format --check .` clean (config in `ruff.toml`).
- **Type hints & Pydantic v2** for all new schemas; SQLAlchemy 2.x async style.
- **Structured logging** (`structlog`) — never log secrets, tokens, or full request bodies.
- **No behavior regression:** the default unparameterized `GET /v1/recommend/{user_id}` must produce the *same* output as before this work (the `balanced` preset reproduces today's policy). Add a regression test asserting this in Chunk 5.

### Global security checklist (verify in every chunk that adds/changes an endpoint)

1. **Auth present:** every new `/v1/*` route depends on `require_api_key` (from `app/core/security.py`). Only `/health` and `/dashboard` are unauthenticated.
2. **Admin gating:** any route that *mutates global config* (modes definitions) depends on `require_admin`, exactly like `app/api/routes/algorithm_config.py` and the `debug=true` path in `recommend.py`.
3. **User-scoped access:** routes under `/v1/users/{user_id}/...` enforce the existing user-access check (same pattern `recommend.py` uses around its user lookup) — a non-admin key may only act on its permitted user(s).
4. **Input validation:** all user-supplied values validated at the edge — `discovery` clamped to [0,1] via `Query(ge=0.0, le=1.0)`; `mode` validated against an enum; config writes validated by Pydantic `ge`/`le`. Invalid input -> `422`, never a 500.
5. **Override whitelist (critical):** the per-request override mechanism must accept **only** a fixed, hard-coded set of safe keys with clamped values. User input maps to overrides through a pure resolver function — never pass raw request data into the config merge. A crafted request must not be able to set arbitrary `AlgorithmConfig` fields.
6. **Resource bounds:** background work (SWR revalidate, prewarm) is bounded — cap concurrent regenerations, rate-limit `prewarm`, and never spawn unbounded `create_task`s from a single request.
7. **No secret leakage:** new response fields (`reasons`, `mu`/`sigma`, debug) contain no internal secrets; respect the existing API-call-log redaction.

### Definition-of-Done template (each chunk restates the specifics)

```
[ ] Code change complete, ruff clean, type-hinted
[ ] Unit tests added and green (named below)
[ ] Security checklist items relevant to this chunk verified (incl. an auth/authz test where an endpoint changed)
[ ] Behavioral validation performed (commands + expected results below)
[ ] No regression: full `pytest tests/ -v` green
```

---

## Dependency graph

```
1 override-mechanism --+
2 modes-config --------+--> 4 acquisition+proven+novelty --> 5 endpoint+dial --> 6 SWR cache+prewarm
3 confidence-proxy ----+                                   +-> 7 radio dial (after 4)
                                                           +-> 8 frontend home/dial/chips (after 5,6)
2 --> 9 frontend modes editor + radio posture (after 7)
4 --> 10 per-dial eval metrics (parallelizable)
```

Chunks **1, 2, 3 are independent** and can be built in parallel. **4** joins them. **5** exposes it. **6/7/8/9/10** layer on.

---

## Chunk 1 — Request-scoped config override mechanism

**Status:** ✅ DONE — `app/services/request_config.py` (`apply_overrides` context manager, `set_overrides`/`reset_overrides`, `current_overrides`); `get_config()` in `app/services/algorithm_config.py` deep-merges the active override on top of the cached config.

**Depends on:** none. **(parallelizable** with 2, 3.)

**Goal:** a tested primitive that lets request-handling code apply per-request overrides on top of the global `AlgorithmConfig`, with strict isolation and reset. No endpoint or behavior change yet.

**Read first:** `app/services/algorithm_config.py`, `app/models/algorithm_config_schema.py` (skim `AlgorithmConfigData`).

**Changes:**
- New `app/services/request_config.py`:
  - A `contextvars.ContextVar[dict | None]` holding the current request's override dict.
  - `set_overrides(overrides)`, `reset_overrides(token)`, and an async-safe `apply_overrides(overrides)` context manager that sets on enter and resets on exit.
  - `current_overrides() -> dict | None`.
- Modify `get_config()` in `algorithm_config.py`: if an override is active, return a **copy** of the active config deep-merged with the override (do not mutate the cached singleton). Keep the no-override path allocation-free/fast (return cached object as today).

**Unit tests** (`tests/test_request_config.py`):
- override applies: inside `apply_overrides({"reranker": {"freshness_boost": 0.99}})`, `get_config().reranker.freshness_boost == 0.99`; outside, back to default.
- reset on exception: override cleared even if the block raises.
- **async isolation:** two concurrent `asyncio` tasks with different overrides each see their own value (no bleed) — the key correctness test.
- `create_task` inheritance: a task spawned inside an override block inherits it (documents the audit-task behavior).
- merge does not mutate the cached singleton.

**Security:** internal only; the untrusted-input boundary is Chunk 5 — note it here.

**Definition of Done / validation:**
- `pytest tests/test_request_config.py -v` green, including the concurrency test.
- `pytest tests/ -v` green (no regression to existing config behavior).

**Context fit:** ~2 files, small.

---

## Chunk 2 — Modes config group (schema + defaults)

**Status:** ✅ DONE — `PresetConfig` + `ModesConfig` in `app/models/algorithm_config_schema.py`; `modes: ModesConfig` added to `AlgorithmConfigData` with `default_preset="balanced"`. Read-only serving access via `GET /v1/algorithm/modes` (any authenticated key — no admin). Implemented as a config group on the existing versioned `AlgorithmConfig` (not a separate table).

**Depends on:** none. **(parallelizable** with 1, 3.)

**Goal:** add a `modes` group to the versioned `AlgorithmConfig` describing each preset and the dial->knob interpolation. Data only — no behavior wired yet. Reuses the existing versioned-config infra (history/diff/export/rollback) — **we implement modes as a config group, not a separate table** (simplification over the design doc's separate-endpoint sketch).

**Read first:** `app/models/algorithm_config_schema.py` (all groups + `get_defaults()`), `app/services/algorithm_config.py` (seed/migration on load).

**Changes:**
- In `algorithm_config_schema.py`, add:
  - `PresetConfig` (per preset): `kappa` (>=0), `exploration_fraction` (0-0.5), `freshness_boost` (0-1), `novelty_filter` (bool) + `novelty_strength` (0-1), `repeat_window_hours` (>=0), `proven_mu_min` (0-1), `proven_sigma_max` (0-1), and `source_weight_mult: dict[str,float]` (per-source multipliers, each 0-5).
  - `ModesConfig`: the four presets (`familiar`, `balanced`, `discovery`, `deep_discovery`) + `default_preset: str = "balanced"` + dial->preset anchor positions (`familiar=0.0, balanced~0.3, discovery~0.6, deep_discovery=1.0`).
  - Add `modes: ModesConfig` to `AlgorithmConfigData` and to `get_defaults()`.
- **Calibrate `balanced` to reproduce today:** `exploration_fraction=0.15`, `freshness_boost=0.10`, `kappa~small`, `novelty_filter=false` — so default serving is unchanged.
- Ensure first-boot seed + existing-DB load tolerate the new group (an active config without `modes` should fill from defaults, mirroring how new fields are handled today).

**Unit tests** (`tests/test_modes_config.py`):
- defaults present for all four presets; `default_preset == "balanced"`.
- `balanced` anchors equal current live values (`0.15` / `0.10`) — guards the no-regression contract.
- Pydantic constraints reject out-of-range values (e.g. `proven_mu_min=2.0` -> ValidationError).
- A config dict missing `modes` loads and back-fills defaults (migration safety).

**Security:** constraints (`ge`/`le`) on every field — editable via the admin API in Chunk 9.

**Definition of Done / validation:**
- `pytest tests/test_modes_config.py -v` green.
- Boot dev server (`APP_ENV=development ENABLE_DOCS=true DISABLE_AUTH=true API_KEYS="" uvicorn app.main:app --reload`) -> `GET /v1/algorithm/config` includes a populated `modes` group with `balanced` matching today.
- `pytest tests/ -v` green.

**Context fit:** 2 files, small.

---

## Chunk 3 — Phase-A confidence proxy (mu / sigma)

**Status:** ✅ DONE — `app/services/confidence.py` (`compute_confidence(...) -> dict[str, ConfidenceScore]`; `ConfidenceScore` carries `mu`/`sigma`/`is_proven`/`evidence`; `is_proven = mu >= proven_mu_min and sigma <= proven_sigma_max`, thresholds read from the active preset). No new model.

**Depends on:** none. **(parallelizable** with 1, 2.)

**Goal:** a service that returns, per candidate track for a given user, a predicted-engagement `mu` and an uncertainty `sigma` using only existing signals (ranker score, interaction history, FAISS neighbour density). No new model.

**Read first:** `app/services/faiss_index.py` (effnet index + search), `app/services/feature_eng.py` (`build_features`), `app/services/ranker.py` (`score_candidates`), `app/services/taste_profile.py` (top_tracks / interactions shape).

**Changes:**
- New `app/services/confidence.py` with `compute_confidence(user, candidates, *, interactions, faiss_index) -> dict[str, ConfidenceScore]` where `ConfidenceScore = {mu, sigma, is_proven, evidence}`:
  - `mu`: blend of the ranker's predicted satisfaction with the track's own `avg_completion`/`skip_count` when interaction exists; for unheard tracks, inherit `mu` from the satisfaction of nearest FAISS neighbours.
  - `sigma`: decreasing in *evidence density* — `personal_evidence` (recency-weighted plays/completions on the track) + `neighbour_evidence` (weighted count of the user's high-`mu` tracks within an embedding radius via FAISS). Isolated/unheard -> high sigma; dense loved-cluster -> low sigma.
  - `is_proven`: `mu >= proven_mu_min and sigma <= proven_sigma_max` (thresholds read from `get_config().modes` for the active preset; default to `balanced`'s if not provided).
- Keep it pure/deterministic given inputs (no global RNG; pass any needed seed explicitly).

**Unit tests** (`tests/test_confidence.py`) with synthetic fixtures:
- A track played 20x with high completion -> high `mu`, low `sigma`, `is_proven=True`.
- A never-heard track surrounded (in embedding space) by the user's loved tracks -> reasonable `mu`, low-ish `sigma`, can be proven (the key behaviour vs play_count).
- A track played once then early-skipped -> low `mu`, `is_proven=False`.
- An isolated unheard track far from any liked cluster -> high `sigma`, `is_proven=False`.
- Determinism: same inputs -> same outputs.

**Security:** internal compute; ensure logs don't dump user track lists at INFO.

**Definition of Done / validation:**
- `pytest tests/test_confidence.py -v` green, all four scenarios asserting the intended ordering.
- `pytest tests/ -v` green.

**Context fit:** 1 new file + reading 3-4 services. Medium; keep the read targeted.

---

## Chunk 4 — UCB acquisition + proven set + novelty filter

**Status:** ✅ DONE — acquisition term + proven-set novelty exclusion live in `app/services/reranker.py` and `app/services/candidate_gen.py`, driven entirely by `get_config()` so Chunk-1 overrides flow through. Default (`balanced`, no override) reproduces the pre-change ranking.

**Depends on:** 2 (modes anchors), 3 (confidence).

**Goal:** make the pipeline *behave* differently by dial position — implement `score = mu + kappa(d)*sigma - lambda*is_proven`, the proven-set novelty exclusion at the discovery end, and relaxed anti-repetition at the familiar end. Still no endpoint param (driven by overrides, default `balanced` = today).

**Read first:** `app/services/reranker.py` (`rerank` @49, freshness @101, exploration slots @232), `app/services/candidate_gen.py` (exclude set @91, merge @343), `app/services/confidence.py` (Chunk 3), Chunk 1's `request_config`.

**Changes:**
- `candidate_gen.py`: when the active preset has `novelty_filter=true`, extend the existing exclude set with the **proven set** (from `confidence.compute_confidence`), scaled by `novelty_strength`. Keep proven-*bad* exclusions as-is.
- `reranker.py`: blend the ranker score with the acquisition term — final `score = mu + kappa*sigma` (kappa from preset), then apply existing diversity/skip rules; at the `familiar` end relax/skip the anti-repetition window per `repeat_window_hours` override; gate exploration-slot injection on `exploration_fraction` (already config-driven). Apply source-weight multipliers from the preset.
- All reads via `get_config()` so Chunk-1 overrides flow through automatically.

**Unit tests** (`tests/test_modes_behavior.py`):
- `apply_overrides(familiar)` -> results dominated by proven/high-`mu` tracks; proven set NOT excluded; favourites may recur (anti-repetition relaxed).
- `apply_overrides(deep_discovery)` -> proven set excluded; high-`sigma` candidates rise; results disjoint from the proven set.
- **Regression:** `balanced` (default, no override) -> byte-for-byte same ranking as the pre-change reranker on a fixed fixture.
- Monotonicity: increasing the dial strictly increases mean `sigma` / novelty of the output on a fixed candidate set.

**Security:** none new (no endpoint yet); overrides still come only from server code.

**Definition of Done / validation:**
- `pytest tests/test_modes_behavior.py -v` green incl. the regression + monotonicity tests.
- `pytest tests/ -v` green (existing `test_reranker.py` / `test_candidates.py` still pass).

**Context fit:** 2 core files + 2 deps. Medium-large but bounded; the behavioral heart.

---

## Chunk 5 — Endpoint wiring: discovery/mode params + dial resolution + reasons

**Status:** ✅ DONE — `GET /v1/recommend/{user_id}` accepts `discovery: float (0–1)` and `mode: str` (familiar/balanced/discovery/deep_discovery; `mode` wins; unknown → 422). The whitelisted pure resolver `resolve_dial(...)` lives in `app/services/modes.py`; candidate-gen/rank/rerank run inside `apply_overrides(...)`. Response carries the resolved `discovery` value and per-track `reasons[]`.

**Depends on:** 1, 2, 4.

**Goal:** expose the dial on `GET /v1/recommend/{user_id}`; validate input; resolve dial->overrides through a pure whitelisted resolver; set/reset the ContextVar; add `discovery` + per-track `reasons` to the response.

**Read first:** `app/api/routes/recommend.py` (handler @54, user-access @~89, response build @373), `app/core/security.py` (`require_api_key`/`require_admin`), Chunk 1 + Chunk 2 outputs.

**Changes:**
- Add params: `discovery: float | None = Query(None, ge=0.0, le=1.0)` and `mode: str | None = Query(None)` (validate against the preset enum; define precedence — `mode` wins if both given). Default when neither given -> `default_preset` (`balanced`).
- New **pure resolver** `resolve_dial(discovery, mode, modes_cfg) -> overrides_dict` in `app/services/request_config.py` (or a `modes.py` helper): interpolates preset anchors -> the **whitelisted** override keys only. This is the untrusted-input -> override boundary.
- Wrap candidate-gen + rank + rerank in `apply_overrides(resolve_dial(...))`.
- Response: add resolved `discovery` value and `reasons: list[str]` per track (derive from candidate `sources` + `is_proven`/`sigma`: e.g. `"proven_favourite"`, `"new_to_you"`, `"exploring"`).

**Unit tests** (`tests/test_recommend_modes_api.py`):
- `?mode=familiar` vs `?mode=deep_discovery` -> materially different track lists for a seeded user.
- `?discovery=0.0` ~ `?mode=familiar`; default (no param) ~ `?mode=balanced`.
- **Validation:** `?mode=bogus` -> 422; `?discovery=2.0` -> 422.
- **Security/whitelist:** assert the resolver only ever emits keys in the allowed set (table-driven test feeding all presets + random dial values; intersect output keys with the whitelist). A test confirming an extra/raw key cannot reach `get_config()`.
- **Authz:** request without API key -> 401/403 (run with auth enabled, not `DISABLE_AUTH`); a non-admin key cannot use `debug=true` (existing behaviour preserved).

**Security:** items 1, 3, 4, 5 of the global checklist all exercised here — the most security-sensitive chunk.

**Definition of Done / validation:**
- `pytest tests/test_recommend_modes_api.py -v` green.
- Manual: run dev server **with auth on** (`API_KEYS=testkey ...`, omit `DISABLE_AUTH`); `curl -H "Authorization: Bearer testkey" '.../v1/recommend/<uid>?mode=discovery'` returns a list with `discovery` + `reasons`; the same without the header returns 401/403.
- `pytest tests/ -v` green incl. the Chunk-4 regression.

**Context fit:** 1 route + 1 helper + tests. Medium.

---

## Chunk 6 — SWR mix cache + prewarm / mixes endpoints

**Status:** ✅ DONE — `app/services/mix_cache.py` (`get_or_build(...)`, version-keyed, single-flight background rebuild bounded by an `asyncio.Semaphore`). Endpoints: `POST /v1/users/{user_id}/mixes/prewarm` (202, rate-limited, user-scoped) and `GET /v1/users/{user_id}/mixes` (suggested-shelf specs, user-scoped).

**Depends on:** 5.

**Goal:** hide generation latency. Add a stale-while-revalidate cache around mode requests, a background-bounded `prewarm` hint, and an optional `mixes` menu — all request-only (no persisted surface tables).

**Read first:** `app/services/lastfm_candidates.py` (in-memory cache pattern), `app/api/routes/recommend.py` (post-Chunk-5), `app/services/algorithm_config.py` (to read `config_version`), how `model_version` is obtained in `ranker.py`.

**Changes:**
- New `app/services/mix_cache.py`: dict keyed by `(user_id, preset|dial_bucket, context_bucket, model_version, config_version)` -> `{result, built_at}`, guarded by a lock. `get_or_build(...)` serves fresh instantly; if stale-within-grace, serves stale and schedules **one** bounded background rebuild that swaps the entry; invalidates on version change. Cap concurrent rebuilds (semaphore).
- Integrate into the recommend handler (cache lookup before generation).
- New routes (in `recommend.py` or `users.py`):
  - `POST /v1/users/{user_id}/mixes/prewarm` -> 202, warms `{modes?, seeds?}` in bounded background; `require_api_key` + user-access scope; **rate-limited**.
  - `GET /v1/users/{user_id}/mixes` -> menu of suggested shelves (each a ready-to-call request spec); `require_api_key` + user-access scope.

**Unit tests** (`tests/test_mix_cache.py`):
- cache hit returns identical payload without re-running generation (assert the generator fn is called once across two hits).
- stale entry -> stale served immediately + a single background rebuild scheduled (assert one rebuild, not N).
- key includes versions -> bumping `config_version` misses the cache (no stale serve after a config change).
- concurrency cap respected (N>cap simultaneous misses -> at most `cap` concurrent builds).
- API: `prewarm` returns 202 and populates entries; both endpoints reject missing auth and cross-user access.

**Security:** checklist items 3 (user scope on both endpoints), 6 (rate-limit `prewarm`, bounded background tasks, semaphore), 1.

**Definition of Done / validation:**
- `pytest tests/test_mix_cache.py -v` green incl. the "one rebuild" and concurrency-cap tests.
- Manual: with auth on, `prewarm` then immediately `GET ...?mode=discovery` returns fast (warm); a second user's key cannot prewarm/list this user's mixes (403).
- `pytest tests/ -v` green.

**Context fit:** 1 new service + 1 route file + tests. Medium.

---

## Chunk 7 — Radio honours the dial

**Status:** ✅ DONE — `POST /v1/radio/start` and `GET /v1/radio/{session_id}/next` accept `discovery` (and a `mode` preset); `app/services/radio.py` applies the same preset anchors via overrides and scales the drift step by dial position (`_dial_drift_scale`), with in-session feedback drift preserved on top of the baseline.

**Depends on:** 2, 3, 4. **(parallelizable** with 6 — different files.)

**Goal:** apply the dial to radio as a baseline posture that in-session feedback drifts around.

**Read first:** `app/services/radio.py` (`get_next_tracks`, `_update_drift_embedding` @300, source weights), `app/api/routes/radio.py` (start/next), Chunks 2-4.

**Changes:**
- `POST /v1/radio/start` accepts `discovery: float = Query(0.3, ge=0, le=1)` (default `balanced`); persist on the session. `GET /v1/radio/{id}/next` accepts an optional override.
- In `get_next_tracks`: wrap candidate scoring in `apply_overrides(resolve_dial(...))` so the same preset anchors apply; additionally modulate (a) source weights, (b) the novelty filter against `played + proven`, (c) the **drift step size** in `_update_drift_embedding` (scale by dial).
- Keep feedback drift intact — the dial sets the baseline; likes/skips still move the vector.

**Unit tests** (`tests/test_radio_modes.py`):
- `familiar` session -> low drift step, proven-leaning, repeats allowed; `deep_discovery` -> larger drift, proven set excluded, no repeats within session.
- feedback still drifts around the baseline (a like still pulls the vector regardless of dial).
- param validation (`discovery` out of range -> 422); session ownership enforced.

**Security:** auth on radio routes (existing); validate `discovery`; enforce session/user ownership.

**Definition of Done / validation:**
- `pytest tests/test_radio_modes.py -v` green.
- Manual (auth on): start a `deep_discovery` radio, pull two `next` batches -> no track repeats and outputs avoid the user's proven favourites; start a `familiar` radio -> batches stay close to the seed/favourites.
- `pytest tests/ -v` green.

**Context fit:** 1 service + 1 route + tests. Medium.

---

## Chunk 8 — Frontend: multi-shelf home + discovery dial + reason chips

**Status:** ✅ DONE — Explore → Recommendations in `app/static/js/v2/explore.js` renders the multi-shelf home, the 4-stop discovery slider, and per-track reason chips; prewarms via the mixes endpoint on view load.

**Depends on:** 5, 6.

**Goal:** surface the dial in **Explore -> Recommendations** as a multi-shelf home with a 4-stop slider and per-track reason chips. Validation is behavioral via the running dashboard (no JS unit harness in the repo).

**Read first:** `app/static/js/v2/explore.js`, `app/static/js/v2/components.js`, `app/static/index.html`, `app/static/css/pages.css` (+ `components.css`).

**Changes:**
- Recommendations view: render shelves (On Repeat = familiar, Your Mix = balanced, Discover, Deep Cuts, plus a seed shelf), each calling `?mode=...`; call `.../mixes/prewarm` on view-load.
- A 4-stop discovery slider bound to the continuous value, driving a "Custom Mix" list; re-request on change (served from cache instantly when warm).
- Render `reasons[]` as small chips on each track card.
- Keep all admin-only controls hidden for non-admin sessions.

**Unit tests:** none practical (no JS test harness per `CLAUDE.md`). If you extract a pure helper (e.g. dial->label mapping), add a tiny test only if a JS harness is introduced; otherwise validate behaviorally.

**Security:** the page must call only `require_api_key`-level endpoints for end users; never expose modes-write controls here (those live in Settings, admin-gated, Chunk 9).

**Definition of Done / validation:**
- Run the app (`/run` skill or the dev `uvicorn` command), open `/dashboard` -> Explore -> Recommendations:
  - shelves render and load; switching the dial visibly changes the list; deep-discovery contains tracks the user hasn't engaged with; familiar shows known favourites.
  - reason chips appear and read sensibly.
  - capture a screenshot of each dial extreme (preview/browser tooling) as the validation artifact.
- No console errors; network panel shows `?mode=` requests + a `prewarm` 202.

**Context fit:** 3-4 static files. Medium; keep backend files out of the read set.

---

## Chunk 9 — Frontend: modes editor (Settings) + radio posture control

**Status:** ✅ DONE — the `modes` config group renders in Settings → Algorithm via the existing versioned-config editor (`app/static/js/v2/settings.js`); writes go through the admin-gated `PUT /v1/algorithm/config`. The read-only `GET /v1/algorithm/modes` (any authenticated key) serves the preset defs to the Explore dial. Radio posture control wired into the radio panel.

**Depends on:** 2, 7.

**Goal:** let an admin tune what each preset *does*, reusing the existing versioned-config editor; add the radio posture control.

**Read first:** `app/static/js/v2/settings.js`, `app/static/js/v2/components.js` (versioned-config shell), `app/api/routes/algorithm_config.py` (so the new `modes` group saves through the existing PUT), `app/static/js/v2/explore.js` (radio panel).

**Changes:**
- The new `modes` config group renders in **Settings -> Algorithm** via the existing grouped accordion (mostly free since it's a config group); add per-preset sub-cards for the anchors + proven thresholds + source-weight multipliers, and a small dial->knob curve preview.
- Add a read-only convenience `GET /v1/algorithm/modes` (returns just the preset defs) for the Explore dial to consume without fetching the whole config — `require_api_key` (read-only).
- Radio start panel: a "Comfort / Adventurous" control mapping to `discovery`.

**Unit tests** (`tests/test_modes_endpoint.py` for the backend bit):
- `GET /v1/algorithm/modes` returns the active preset defs; requires auth.
- saving modes via the existing `PUT /v1/algorithm/config` requires **admin**, creates a new version, and the change is reflected by `get_config().modes` immediately (serving-time, no pipeline run needed).

**Security:** modes **writes** go through the admin-gated config PUT (item 2); the new read endpoint is `require_api_key`; confirm a non-admin key gets 403 on write.

**Definition of Done / validation:**
- `pytest tests/test_modes_endpoint.py -v` green incl. the admin-gating test.
- Manual (auth on, admin key): edit `discovery.kappa`, Save & Apply -> version bumps, diff shows the change; reload Explore and confirm the discovery shelf shifts accordingly. Non-admin key cannot save (403).
- `pytest tests/ -v` green.

**Context fit:** 2-3 static files + 1 route + tests. Medium.

---

## Chunk 10 — Per-dial evaluation metrics

**Status:** ✅ DONE — per-dial-bucket metrics (intra-list diversity, catalog coverage, novelty, proven-set skip-rate) computed in `app/services/evaluation.py` and surfaced on the model-stats endpoint (`GET /v1/stats/model`).

**Depends on:** 4. **(parallelizable.)**

**Goal:** measure each pole correctly (NDCG alone is insufficient).

**Read first:** `app/services/evaluation.py`, `app/api/routes/recommend.py` (`/v1/recommend/stats/model`).

**Changes:**
- Add per dial-bucket metrics: novelty (mean inverse popularity / % never-played), catalog coverage, intra-list diversity, and (familiar) skip-rate-on-proven-set. Surface via the existing model-stats endpoint.

**Unit tests** (`tests/test_eval_modes.py`):
- novelty/coverage/diversity computed correctly on a fixture; proven-set skip-rate sane on synthetic events.

**Security:** stats endpoint stays admin-gated as today.

**Definition of Done / validation:**
- `pytest tests/test_eval_modes.py -v` green; metrics visible on the model-stats endpoint for a seeded dataset. `pytest tests/ -v` green.

**Context fit:** 1 file + tests. Small.

---

## Phase B (future / deferred) — real confidence model

**Status:** ⏳ NOT STARTED — Phase A (the proxy) has shipped; Phase B replaces the proxy internals with a trained model and remains future work. Outlined now; sequence in detail when the proxy's "proven" set has been felt against the ear.

- **B1 — Skip/completion head.** LightGBM **classifier** on the existing 39-feature matrix (label `early_skip` vs `full_listen`) -> calibrated `P(skip)`. Add as a pipeline training step (`scheduler.py`) and a new ranker feature. Tests: trains on fixture, calibration sane, persisted/loaded. [RETRAIN]
- **B2 — Quantile uncertainty.** Two extra LightGBM models (`objective="quantile"`, alpha~0.15/0.85) -> `sigma = q_hi - q_lo`. Swap `confidence.py` to consume real `mu = 1 - P(skip)` and quantile `sigma` behind the same interface (Chunk 3's signature unchanged -> minimal blast radius). [RETRAIN]
- **B3 — Calibration eval + dashboard badges.** Reliability curve of `P(skip)` vs actual; wire [RETRAIN] badges on the new params in the modes/ranker editor.

Because Chunk 3 fixed the `compute_confidence` interface, Phase B is a drop-in replacement of the *internals* plus training wiring — no changes to Chunks 4-10.

---

## At-a-glance sequencing & sizing

All 10 chunks have shipped (`Status` column). New tests listed below all exist in `tests/`.

| # | Chunk | Status | Depends | Size | New tests |
|---|-------|--------|---------|------|-----------|
| 1 | Override mechanism | ✅ DONE | — | S | `test_request_config.py` |
| 2 | Modes config group | ✅ DONE | — | S | `test_modes_config.py` |
| 3 | Confidence proxy (mu/sigma) | ✅ DONE | — | M | `test_confidence.py` |
| 4 | Acquisition + proven + novelty | ✅ DONE | 2,3 | M-L | `test_modes_behavior.py` |
| 5 | Endpoint + dial + reasons | ✅ DONE | 1,2,4 | M | `test_recommend_modes_api.py` |
| 6 | SWR cache + prewarm/mixes | ✅ DONE | 5 | M | `test_mix_cache.py` |
| 7 | Radio dial | ✅ DONE | 2,3,4 | M | `test_radio_modes.py` |
| 8 | Frontend home/dial/chips | ✅ DONE | 5,6 | M | (behavioral) |
| 9 | Frontend modes editor + radio posture | ✅ DONE | 2,7 | M | `test_modes_endpoint.py` |
| 10 | Per-dial eval metrics | ✅ DONE | 4 | S | `test_eval_modes.py` |

**First usable slice:** Chunks 1-5 (backend dial works end-to-end, fully tested, secured). **Demoable in the app:** + 6, 8. **Admin-tunable + radio:** + 7, 9. **Measured:** + 10.

**Phase A is complete.** The next sequence is **Phase B** (real confidence model) above — still future / deferred.
