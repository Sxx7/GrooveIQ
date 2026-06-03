# Recommendation Modes — Implementation Plan

**Status:** Planned (design only — not yet implemented)
**Created:** 2026-06-02
**Updated:** 2026-06-02 (added confidence-based "proven" definition, app surfacing, dashboard settings, request-only surfaces)
**Estimated effort:** ~5–10 days for the core dial + confidence-based "proven" (Phases 0–2); optional phases beyond

## Overview

GrooveIQ's recommender today has effectively **one mode**: it optimizes a single objective — the per-user `satisfaction_score` — and tries to find the best next track. The major streaming services don't work this way. They expose listener *intent* as a spectrum between two poles:

- **"Play me my proven favorites"** — exploitation of what the user already loves (Spotify Daily Mix, Apple Favorites Mix / Personal Station).
- **"I want to discover new music"** — exploration of the novel and unheard (Spotify Discover Weekly, Apple New Music Mix / Discovery Station).

This plan adds that spectrum to GrooveIQ as a **single continuous "discovery dial"** (`discovery` in [0.0, 1.0]) on the recommendation request, with named presets (`familiar` / `balanced` / `discovery` / `deep_discovery`) as labeled points on the dial.

The dial is best understood as a **UCB-style acquisition function** over two per-(user, track) model outputs — predicted engagement `mu` and uncertainty `sigma` (see section 5). It re-weights knobs that *already exist* in the pipeline plus one new model output (`sigma`). It does **not** require replacing the ranking model.

| Pole | Dial value | What it means here |
|------|-----------|--------------------|
| **Proven favorites** | `discovery = 0.0` | Surface tracks the model is *confident* the user will not skip (high `mu`, low `sigma`). No exploration, no novelty filter, relaxed anti-repetition so loved tracks can recur. |
| **Balanced (today's behaviour)** | `discovery ~ 0.3` | Reproduces the current fixed policy (~15% exploration slots, +10% freshness). Default. |
| **Discovery / Deep discovery** | `discovery -> 1.0` | Deliberately favour high-uncertainty tracks, exclude the *proven* set, lean on discovery-oriented candidate sources. |

> **Product decisions locked (2026-06-02):**
> 1. "Proven" = **the user's own** proven favourites (the *familiarity* / explore-exploit axis), **not** global popularity. Popularity (mainstream/niche) is a future second dial (section 14).
> 2. Primary interface is a **single continuous dial**; presets `familiar / balanced / discovery / deep_discovery`.
> 3. **"Proven" is confidence-based, not play-count-based** — a track is proven when the model has high confidence the user won't skip it (section 5).
> 4. **Radio honours the dial** (section 6.3).
> 5. **Surfaces/"mixes" are request-only** with stale-while-revalidate caching + pre-warm (section 8) — no persisted/scheduled surface tables.
>
> This document is a **plan only**; no code changes are made yet.

---

## 1. The problem

Every stage of the pipeline serves one number: the per-user `satisfaction_score`, a weighted sum of engagement signals (full listen +1.0, like +2.0, repeat +1.5, early skip -0.5, dislike -2.0, ...) min-max normalized to [0,1] per user.

- **Scoring**: `_raw_satisfaction()` then `_normalise_scores()` — `app/services/track_scoring.py:396` and `:561`.
- **Ranker**: LightGBM trained with label = `satisfaction_score` — `app/services/ranker.py:241`; falls back to the raw satisfaction feature when untrained. **Emits a point estimate only — no uncertainty, no explicit skip probability.**
- **Reranker**: a single `rerank()` with no intent parameter — `app/services/reranker.py:49`.

The only "discovery" in the system is **hardcoded and global**:

| Knob | Default | Where |
|------|---------|-------|
| `exploration_fraction` | 0.15 | `app/services/reranker.py:232` (Thompson-flavoured slots; noise prop. to `1/sqrt(plays+1)`) |
| `freshness_boost` | 0.10 | `app/services/reranker.py:101` (+10% for never-played tracks) |

Both apply identically to every user on every request. The candidate-source weights and these knobs live in the **global** `AlgorithmConfig`, returned by a single in-memory singleton `get_config()` (`app/services/algorithm_config.py:41`) that "takes effect on next pipeline run" — so they **cannot be varied per request** as they stand.

Notably, the existing exploration slots already encode the right *intuition* — uncertainty as `1/sqrt(plays+1)` — but as a crude play-count proxy, exactly the thing we want to replace with a real confidence estimate (section 5).

**The levers a dial needs that already exist:**

- `reranker.exploration_fraction`, `exploration_noise_scale`, `freshness_boost` — `app/models/algorithm_config_schema.py:55`
- 8 candidate-source weights (`content`, `content_profile`, `cf`, `session_skipgram`, `lastfm_similar`, `sasrec`, `popular`, `artist_recall`) — `app/models/algorithm_config_schema.py:80`
- A candidate **exclude set** (disliked / heavily-skipped / recently-skipped) — `app/services/candidate_gen.py:91` — the hook for the discovery novelty filter.
- `taste_profile.top_tracks` (top 50 by satisfaction) and the **FAISS effnet index** (`app/services/faiss_index.py`) — used to compute neighbour-density evidence in section 5.

What's missing: the **policy layer** (per-request intent) and a **confidence signal** (`sigma`).

---

## 2. How the majors do it

### Spotify
- **Two-stage funnel** (candidate generation -> ranking -> rerank) — the shape GrooveIQ already has. Discover Weekly fuses CF + NLP-over-text + a CNN over raw audio.
- **Explicit explore/exploit policy:** the Home feed runs **BaRT** ("Bandits for Recommendations as Treatments") — a multi-armed bandit with an **epsilon-greedy** split between *exploitation* (highest predicted engagement) and *exploration* (uncertain items). Published as "Explore, Exploit, Explain."
- **Intent as surfaces:** Daily Mix (exploit/familiar), Discover Weekly (explore/novel — *hard-excludes music you already know*), Release Radar (new from connected artists), Smart Shuffle (in-session blend, ~1 recommended per 3 of yours).

### Apple Music
- Editorial-first; the algorithm personalizes around human curation. Under the hood: collaborative + content/audio + contextual.
- The two poles are separate products: **Favorites Mix** ("the epitome of algorithmic exploitation"), **New Music Mix** (Fridays, discovery), **Discovery Station** (continuous discovery radio that surfaces tracks *not* in your library and rarely repeats), **Personal Station** (familiar).

### The field
- **Two-stage retrieve -> rank -> rerank** is the industry default. GrooveIQ matches it.
- **Explore/exploit** is formalized as multi-armed / **contextual bandits** (epsilon-greedy, UCB, Thompson sampling). UCB picks items by `mu + kappa*sigma` — high predicted reward *or* high uncertainty. **This is exactly the acquisition function our dial implements (section 5).**
- **Calibrated recommendations / popularity-bias control**: the distribution of recommended items should track the user's own history; novelty comes from deliberately re-weighting it.

---

## 3. Comparison

| Capability | Spotify | Apple Music | **GrooveIQ today** |
|---|---|---|---|
| Two-stage retrieve->rank->rerank | yes | yes | yes |
| Multiple candidate models (CF + audio + sequence) | yes CF/NLP/audio | yes CF/content/editorial | yes **rich** (CF, FAISS audio, skip-gram, SASRec, GRU, Last.fm) |
| Explicit explore/exploit **policy** (bandit) | yes BaRT | ~ implicit | no — static 15% slots, no reward loop |
| Per-track **uncertainty / confidence** estimate | yes | ~ | no — point estimate only |
| **Selectable per-request intent** | yes (surfaces) | yes (surfaces) | no |
| Familiar / exploitation surface | Daily Mix | Favorites Mix / Personal Station | no familiar-only mode |
| Discovery surface **with novelty filter** | Discover Weekly | New Music Mix / Discovery Station | no — only a 15% sprinkle |
| In-session blend with a dial | Smart Shuffle | ~ stations | ~ **Radio drift** (closest analog) |
| Context-awareness (time/device/mood) | yes | yes | yes **strong** |
| Per-request tunability | yes via surface | yes via surface | no — global config only |

**Takeaway:** GrooveIQ is not behind on models or features — it's missing the **intent/policy layer** and a **confidence signal**.

---

## 4. Design: the discovery dial

### 4.1 The dial

A single request parameter `discovery` in [0.0, 1.0]. Named presets are labeled anchor points, so clients send either `?discovery=0.7` or `?mode=discovery`:

| `discovery` | Preset | Intent |
|---|---|---|
| 0.0 | `familiar` | "Play me what I love." |
| ~0.3 | `balanced` *(default)* | Today's behaviour, unchanged. |
| ~0.6 | `discovery` | "Mostly new, anchored to my taste." |
| 1.0 | `deep_discovery` | "Surprise me — nothing I've heard." |

### 4.2 What the dial controls (anchor table)

The dial interpolates existing knobs **plus** the new confidence-based acquisition (section 5). Anchors (tunable via the `modes` config, section 7):

| Knob | `d=0.0` (familiar) | `d~0.3` (today) | `d=1.0` (deep_discovery) |
|------|------------------|------------------|----------------------|
| Acquisition `kappa` (uncertainty weight, section 5) | 0.0 | small | large |
| `exploration_fraction` | 0.00 | 0.15 | ~0.50 |
| `freshness_boost` | 0.00 | 0.10 | ~0.30 |
| Novelty filter (exclude *proven* set, section 5.5) | off | off | **on** |
| Anti-repetition window | relaxed (favourites may recur) | 2 h | 2 h |
| Source weights | up content_profile, cf | defaults | up lastfm_similar, sasrec, neighbour-content; down popular, artist_recall |

A linear mapping (e.g. `exploration_fraction = 0.5*d`, `freshness_boost = 0.3*d`) reproduces today's values at `d~0.3`, so `balanced` is exactly current behaviour and nothing regresses.

### 4.3 Request-scoped override mechanism (the key plumbing)

Modes must apply **per request**, but config is a global singleton. **Do not** implement modes by activating different global config versions — that races across concurrent requests and the trained ranker / taste profiles are baked regardless. Instead:

- Add `app/services/request_config.py` exposing a `contextvars.ContextVar` holding an optional overrides dict for the current request.
  - **`contextvars`, not thread-local** — the async-safe primitive: it propagates across `await` inside the request's task and is auto-copied into the fire-and-forget `asyncio.create_task(write_audit(...))`.
- Teach `get_config()` to merge any active override on top of the base config.
- The recommend handler computes overrides from `discovery`, sets the ContextVar, resets it in a `finally`.
- **Deep call sites need no change** — `reranker`/`candidate_gen` keep calling `get_config()`.

### 4.4 Modes as versioned config

Add a `modes` group to `app/models/algorithm_config_schema.py` holding the anchor values and preset->dial mappings, editable in **Settings -> Algorithm** with versioning/history/diff/export for free (section 7). The per-request `discovery` value chooses a point; the `modes` config defines what each point means.

### 4.5 What does *not* change

- **The base ranker and feature engineering are untouched.** One model plus an uncertainty estimate (section 5) serves every dial position.
- `balanced` (default) reproduces today's output exactly.

---

## 5. Defining "proven": confidence, not play count

This is the crux. "Proven" must mean **the model is confident the user won't skip this track** — not merely that it has been played before. A track played once and skipped is *not* proven; a never-heard track sitting deep inside a cluster of the user's loved tracks *can* be.

### 5.1 The acquisition function

Per (user, track) we want two quantities:

- **`mu` (predicted engagement)** — probability the user will *not* early-skip / will complete. High `mu` = a safe bet.
- **`sigma` (uncertainty)** — how unsure the model is about `mu`. Low `sigma` = we can trust it.

The dial is the exploration coefficient `kappa(d)` in a UCB-style score:

```
score(track) = mu  +  kappa(d)*sigma  -  lambda(d)*is_proven(track)
```

- **`familiar` (`d=0`, `kappa=0`)**: rank by `mu` and **restrict the pool to high-`mu`, low-`sigma` tracks** -> confident no-skips. `lambda=0`.
- **`balanced`**: small `kappa` — a gentle exploratory tilt (~ today).
- **`deep_discovery` (`d=1`, large `kappa`)**: reward high `sigma` (things we're unsure about) and subtract `lambda` for tracks already in the proven set -> push genuine novelty.

This is the principled version of the existing `1/sqrt(plays+1)` heuristic, and the natural on-ramp to a real bandit later (section 14).

### 5.2 Estimating mu and sigma — two phases

GrooveIQ's LightGBM ranker emits neither a skip probability nor `sigma` today, so this needs a small, incremental model addition.

**Phase A — proxy from existing signals (ships *with* the dial, no new model):**

- **`mu` proxy:** the ranker's `satisfaction_score` prediction (already a skip-aware composite) blended with the track's own `avg_completion` / `skip_count` when present; for unheard tracks, inherited from nearest neighbours.
- **`sigma` proxy (confidence):** *evidence density* —
  - `personal_evidence`: own plays / completions on this track, recency-weighted.
  - `neighbour_evidence`: weighted count of the user's **proven** tracks within an embedding radius, via the existing FAISS `effnet_index`. Dense cluster of loved tracks nearby -> low `sigma`; isolated track -> high `sigma`.
- This already beats `play_count`: confidence comes from prediction + neighbourhood density, not "heard before."

**Phase B — proper model (high-leverage fast-follow):**

- **Skip/completion head:** a LightGBM **classifier** on the same 39-feature matrix, label = `early_skip(1)` vs `full_listen(0)` from events -> calibrated `P(skip)`, so `mu = 1 - P(skip)`. Bonus: it becomes a new ranker feature that improves ranking generally.
- **Uncertainty via quantile regression:** train two extra LightGBM models on satisfaction with `objective="quantile"` at `alpha~0.15` and `alpha~0.85`; `sigma = q_hi - q_lo`. Native to LightGBM (which you already use) — two small models, no new framework.
- Carries a **[RETRAIN]** implication and slots into the existing pipeline as new training steps.

> **Recommendation:** ship **Phase A with the dial** (so "proven" is confidence-based from day one), then do **Phase B** as the immediate next step — it's cheap given the existing LightGBM stack, improves ranking across all modes, and is the foundation for the eventual bandit.

### 5.3 The "proven set" and the discovery novelty filter

- **Proven set** (used by `familiar` and as the discovery *exclusion*): tracks with `mu >= proven_mu_min` **and** `sigma <= proven_sigma_max`. Both thresholds live in the `modes` config.
- **Discovery filter** (`d->1`): exclude the proven set and the existing proven-*bad* set (disliked / heavily-skipped). Keep **uncertain-but-promising** tracks (high `sigma`, `mu` above a floor). A track skipped once but with weak/ambiguous signal can still appear — which is correct, and impossible to express with `play_count`.

### 5.4 Cold-start interaction

For users with little history, the proven set is tiny and `sigma` is high almost everywhere -> `familiar` would starve. The dial **clamps toward balanced** until enough evidence exists, falling back to `build_seed_profile()` / onboarding. (See section 13.)

---

## 6. App surfacing & UX

### 6.1 Explore -> Recommendations becomes a multi-shelf home

Today Recommendations is a single ranked list. To match the "request as many mixes as needed" model (section 8) and the way Spotify/Apple present intent, restructure it as a **home of shelves**:

```
Recommendations
  Discovery dial:  [ Familiar --- Balanced --- Discovery --- Deep ]   (what's this?)

  On Repeat            (familiar)         -> -> -> -> ->         <- horizontal shelves,
  Your Mix             (balanced)         -> -> -> -> ->            each lazy-loaded from
  Discover             (discovery)        -> -> -> -> ->            the SWR cache (section 8)
  Deep Cuts            (deep_discovery)   -> -> -> -> ->
  More like {artist}   (seed = recent)    -> -> -> -> ->
```

- The **dial** at the top is a 4-stop slider bound to the continuous `discovery` value; it drives a "Custom Mix" list and is also the control end-users reach for. Snap points = the four presets; the raw value is still continuous underneath.
- Each **shelf** is one `GET /v1/recommend/{user_id}?mode=...` call (or seed variant), served instantly from cache and refreshed in the background.
- Changing the dial re-requests the custom list — instant if pre-warmed.

### 6.2 Per-track "reason" chips

Make the mode legible (Spotify's sparkle / "because you listen to X"). The response carries a `reasons` array per track; the UI renders small chips:

- `familiar` -> "Proven favourite", "You complete this 90% of the time"
- `discovery` -> "New to you", "Fans of {artist} like this", "Exploring — unsure you'll like it"

The data already exists: candidate `sources`, the feature vector, and (post-section 5) `mu`/`sigma`. This also flows straight into the existing **recommendation audit** so every served mix is replayable.

### 6.3 Radio honours the dial (recommended)

Radio is the most natural home for the dial — Apple's Discovery Station is literally a discovery-dialed radio.

- Set at session start: `POST /v1/radio/start?discovery=...`; override per `GET /v1/radio/{id}/next?discovery=...`.
- The dial modulates radio's **source weights**, the **novelty filter** (against the session's played set + the proven set), and the **drift step size** (how far each feedback nudge moves the drift embedding — `app/services/radio.py:300`).
- **Cooperation, not conflict:** the dial sets the *baseline posture*; in-session like/skip/dislike feedback drifts *around* that baseline. A `familiar` radio is a low-drift comfort station; a `deep_discovery` radio wanders and rarely repeats.
- UI: a "Comfort / Adventurous" control on the Radio start panel (Explore -> Radio), adjustable mid-session.

---

## 7. Dashboard settings

There are **two distinct levels**, and they live in different buckets of the four-bucket IA:

| Level | Who | Where | What |
|------|-----|-------|------|
| **Runtime dial** | end-user, per request | **Explore -> Recommendations / Radio** | picks `discovery` value (section 6) |
| **Mode definitions** | admin, versioned | **Settings -> Algorithm -> Modes** | defines what each preset *does* |

### Settings -> Algorithm -> Modes editor

A new accordion group ("Modes") in the existing versioned-config dashboard (`app/static/js/v2/settings.js` + the shared config shell), so it inherits sliders, inline validation, **diff**, **history/rollback**, **export/import**.

- One sub-card per preset (`familiar` / `balanced` / `discovery` / `deep_discovery`), each exposing its anchors: `kappa`, `exploration_fraction`, `freshness_boost`, novelty-filter on/off + strength, anti-repetition window, source-weight multipliers, and the proven thresholds `proven_mu_min` / `proven_sigma_max`.
- Global confidence-model settings (Phase B): skip-head + quantile params, carrying **[RETRAIN]** badges (mirrors the existing Ranking Model / Session Embeddings badges).
- A small **"dial->knob" curve preview** so an admin can see how a mid-dial value (e.g. 0.45) interpolates between presets.

The runtime dial in Explore reads these definitions via `GET /v1/algorithm/modes`.

---

## 8. Surfaces: request-only with pre-warming

**Decision:** no persisted/scheduled surface tables. Every "mix" is a normal request; the frontend asks for as many as it wants. To hide generation latency, add **stale-while-revalidate (SWR)** caching + **pre-warm**:

- **Cache** (in-memory, mirrors the `lastfm_candidates` / news cache pattern): key = `(user_id, mode/dial-bucket, context-bucket, model_version, config_version)` -> ranked result + timestamp. First request generates and caches; subsequent reads are instant.
- **Stale-while-revalidate:** if a cached entry is within TTL -> serve instantly. If stale -> serve the stale copy immediately *and* kick off a background regenerate that swaps the entry when done (exactly your "request a new mix in the background and swap it instantly").
- **Pre-warm:** a lightweight hint endpoint the frontend calls on app-open / Recommendations-view (`POST /v1/users/{user_id}/mixes/prewarm`) warms the common shelves (the four presets + a seed or two) in the background so the first paint is instant. Alternatively the backend auto-pre-warms the other presets after the first request of a session.
- **Invalidation:** drop entries on a new pipeline run (model/config version bump) or after TTL (default ~30-60 min, tunable). Because keys include `model_version` + `config_version`, a retrain or config change naturally misses the cache.
- **Auditability is preserved:** request-time generation still flows through the existing `reco_audit` persistence, so any mix can be browsed/replayed after the fact.

Trade-off acknowledged: a cold mix costs one full pipeline run (~100-500 ms). SWR + pre-warm makes that invisible in the common case; the only truly cold path is a brand-new user/mode/context combination.

---

## 9. API changes

```
# Changed — add the dial (+ preset alias) to the existing endpoint
GET /v1/recommend/{user_id}?discovery=0.0..1.0
GET /v1/recommend/{user_id}?mode=familiar|balanced|discovery|deep_discovery
#   existing context params unchanged (seed_track_id, device_type, hour_of_day, genre, mood, ...)
#   response gains: "discovery" (resolved value) + per-track "reasons"[]  (+ mu/sigma after section 5 Phase B)

# Radio honours the dial
POST /v1/radio/start?discovery=0.0..1.0          # sets baseline posture
GET  /v1/radio/{session_id}/next?discovery=...   # optional per-batch override

# New — request-only surfaces: pre-warm hint (SWR cache, section 8)
POST /v1/users/{user_id}/mixes/prewarm           # body: { modes?: [...], seeds?: [...] } -> 202, warms cache in background
GET  /v1/users/{user_id}/mixes                   # optional: menu of suggested shelves (each = a ready-to-call request spec)

# New — modes/dial config CRUD (mirror existing algorithm-config routes)
GET  /v1/algorithm/modes                         # anchors + preset definitions (read by the Explore dial)
GET  /v1/algorithm/modes/defaults
PUT  /v1/algorithm/modes                         # save (versioned, becomes active)
POST /v1/algorithm/modes/reset
```

No new endpoints are required for the confidence model (section 5) — `mu`/`sigma` are produced inside the pipeline and surfaced via the existing recommend/debug/audit responses.

---

## 10. File change summary

| File | Change | Phase |
|------|--------|-------|
| `app/api/routes/recommend.py` | `discovery`/`mode` params; resolve to overrides; set/reset ContextVar; echo dial + `reasons`; SWR cache lookup | 0 |
| `app/services/request_config.py` *(new)* | ContextVar override store | 0 |
| `app/services/algorithm_config.py` | `get_config()` merges per-request override | 0 |
| `app/services/mix_cache.py` *(new)* | SWR cache + pre-warm + invalidation hooks (section 8) | 0 |
| `app/services/candidate_gen.py` | Extend exclude set with the *proven set* when novelty filter active | 0 |
| `app/services/confidence.py` *(new)* | Phase A: `mu`/`sigma` proxies (ranker score + FAISS neighbour density) | 0 |
| `app/services/reranker.py` | UCB acquisition `mu+kappa*sigma`; relax anti-repetition at `familiar` end | 0 |
| `app/services/radio.py` | Honour dial: source weights, novelty filter, drift step | 1 |
| `app/models/algorithm_config_schema.py` | New `modes` group (anchors, presets, proven thresholds) | 1 |
| `app/models/schemas.py` | `discovery` + `reasons` (+ `mu`/`sigma`) in responses | 1 |
| `app/api/routes/algorithm_config.py` | `/v1/algorithm/modes` CRUD | 1 |
| `app/api/routes/users.py` (or recommend) | `.../mixes/prewarm`, `.../mixes` | 1 |
| `app/static/js/v2/explore.js` + css | Multi-shelf home, discovery dial, reason chips, radio posture control | 1 |
| `app/static/js/v2/settings.js` | Modes editor group (section 7) | 1 |
| `app/services/ranker.py` | Phase B: skip/completion head + quantile (`sigma`) models; new ranker feature | 2 |
| `app/workers/scheduler.py` | Phase B: train skip-head + quantile models in the pipeline | 2 |
| `app/services/evaluation.py` | Per-dial metrics (novelty, coverage, intra-list diversity) | 2 |
| `tests/` | `test_modes.py` (dial extremes, override isolation, proven set), confidence tests | 0-2 |

The **base** ranker/feature-eng are untouched in Phase 0-1; Phase 2 *adds* models alongside them.

---

## 11. Effort estimate

Single developer familiar with the codebase. Implementation **deferred** — these size the work for pickup.

| Phase | Scope | Size | Rough |
|-------|-------|------|-------|
| **0** | ContextVar overrides + `discovery`/`mode` params + UCB acquisition + **Phase A confidence proxy** + proven-set novelty filter + SWR cache + tests | **M** | **3-5 days** |
| **1** | `modes` versioned config + dashboard (multi-shelf home, dial, reason chips, modes editor) + radio dial + prewarm endpoint | **M-L** | **4-6 days** |
| **2** | **Phase B confidence model** (skip head + quantile `sigma`) + pipeline training steps + per-dial eval metrics | **M** | **3-5 days** [RETRAIN] |
| **3** | Bandit-driven default feed (BaRT-style), online reward loop from impressions — subsumes the manual default | **XL** | multi-week |

**Core ask — a confidence-based dial from proven favourites to deep discovery, surfaced in the app — is Phases 0-2: ~10-16 days.** A first usable version (dial + Phase-A confidence + shelves) is Phase 0-1: ~1-1.5 weeks.

---

## 12. Evaluation

Judge the poles by different metrics (NDCG alone is insufficient):

- **Familiar (`d->0`)**: completion rate up, skip rate down, and **skip rate on the proven set** (validates the `mu`/`sigma` thresholds — a good proven set should almost never be skipped).
- **Discovery (`d->1`)**: novelty (mean inverse popularity / % never-played), catalog coverage, intra-list diversity, and **save/like rate on newly surfaced tracks**.
- **Confidence calibration (Phase B)**: reliability curve of `P(skip)` vs actual; does low-`sigma` actually mean low skip variance?
- Report per dial-bucket in `app/services/evaluation.py`; the existing `reco_impression` -> stream/skip logging supplies the raw signal.

---

## 13. Risks & decisions

- **Confidence model investment** (the main fork): Phase A proxy only, or commit to Phase B (skip head + quantile `sigma`)? Recommendation: A in v1, B as fast-follow.
- **Proven thresholds** (`proven_mu_min`, `proven_sigma_max`): expose in `modes` config; calibrate against the section 12 "skip rate on proven set" metric.
- **Cold-start**: clamp the effective dial toward balanced until evidence exists; `familiar` falls back to seed/onboarding (section 5.4).
- **Small libraries**: a hard novelty filter at `d=1` can starve candidates — add a floor that relaxes the filter when the post-filter pool is too small.
- **Override leakage**: reset the ContextVar per request (`finally`); test two concurrent requests with different dials don't bleed.
- **Cache correctness**: keys must include `model_version` + `config_version` so retrains/config changes don't serve stale mixes; persist resolved `discovery` into the audit for reproducible replays.

---

## 14. Future extensions

- **Second dial — mainstream / niche (popularity).** `popularity_preference` already exists (`app/services/taste_profile.py:466`) as a passive feature; a second dial would *actively* target a popularity setpoint via a reranker calibration rule ("proven mainstream hits" vs "deep cuts"). Out of scope for v1 per the familiarity-only decision.
- **Close the loop with a real bandit.** Replace the static exploration slots with a contextual bandit (BaRT-style) using the section 5 `mu`/`sigma` and the impression->outcome data already logged — the principled endgame that subsumes the manual default dial.
- **Smart-Shuffle-style in-session blend.** Interleave a familiar queue with N% discovery picks (N from the dial), reusing the Radio machinery.

---

## 15. Resolved decisions & remaining open question

**Resolved (2026-06-02):**
1. Presets: `familiar` / `balanced` / `discovery` / `deep_discovery`.
2. "Proven" = confidence-based (high `mu`, low `sigma`), *not* play-count. (section 5)
3. Radio honours the dial (baseline posture + feedback drift around it). (section 6.3)
4. Surfaces are request-only with SWR + pre-warm; no persisted surface tables. (section 8)
5. Default unparameterized request stays at `balanced` (no regression).

**Remaining open question:**
- **Confidence-model scope for v1** — ship Phase A proxy only and add Phase B (skip head + quantile `sigma`) later, or build A+B together up front? (Recommendation: A in v1, B as fast-follow.) DECIDED: A-now / B-later.

Secondary calibration choices (proven thresholds, dial->knob curve shape, cache TTL) are config values, tunable post-launch rather than blocking decisions.

---

## 16. Sources & related reading

**Spotify** — Explore, Exploit, Explain (Spotify Research): https://research.atspotify.com/publications/explore-exploit-explain-personalizing-explainable-recommendations-with-bandits ; BaRT overview: https://dynamoi.com/learn/faqs/what-is-spotify-bart-algorithm ; Discover Weekly vs Daily Mix vs Release Radar: https://roadtripsandplaylists.medium.com/the-spotify-discover-weekly-and-release-radar-algorithm-explained-32a611df77fc ; Discover Weekly's three models: https://medium.com/the-sound-of-ai/spotifys-discover-weekly-explained-breaking-from-your-music-bubble-or-maybe-not-b506da144123 ; Smart Shuffle: https://newsroom.spotify.com/2023-03-08/smart-shuffle-new-life-spotify-playlists/

**Apple Music** — Algorithmic playlists: https://musosoup.com/blog/apple-music-algorithmic-playlists ; Favorites vs New Music Mix: https://notnoise.co/blog/how-apple-music-algorithm-works ; Personal vs Discovery Station: https://www.makeuseof.com/apple-musics-discovery-station-new-music-playlist-differences/

**Theory** — Two-stage recommenders: https://www.mlwhiz.com/p/the-recommendation-engine-under-the ; Calibrated recommendations survey: https://arxiv.org/html/2507.02643v1 ; Exploration/exploitation in sequential music rec: https://arxiv.org/pdf/1812.03226 ; Controlling popularity bias via calibration: https://arxiv.org/pdf/2007.12230

**In-repo** — `./Telemetry, Features, and Learning-to-Rank Architectures for Music Recommendation.pdf` ; `./LLM_CONTEXT.md` ; `CLAUDE.md` (Recommendation pipeline / serving)
