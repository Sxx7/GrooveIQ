# Handoff: lyrics (Phases A–C BUILT + deployed; dashboard progress panel DONE; next = Phase D)

**For:** a fresh Claude Code session continuing the lyrics feature.
**Repo:** GrooveIQ backend. **Branch:** `dev` (tip `ddefe2a`, 3 commits ahead of `main`). **Remote:** `origin` → `github.com/Sxx7/GrooveIQ`.
**Prereq reading:** this doc, then the private session memory `project_lyrics.md` (holds the real host/IP/path values the `<placeholders>` below resolve to), `reference_prod_deploy.md`, and `project_test_env.md` (use `.venv-test`).

> **Anonymisation:** this file is committed to the public repo, so infra identifiers are placeholders: `<gpu-vm>` / `<gpu-vm-ip>`, `<prod-host>`, `<library-path>`, `<user>`. The real values are in the private memory and in `/opt/grooveiq/.env` on the box.

---

## 0. Status (2026-06-13)

**Phases A + B + C are built, committed to `dev`, deployed to the `<prod-host>` snapshot + `<gpu-vm>`, and live-verified.** The cascade resolves real lyrics (LRCLIB + embedded + ASR), the drain is backfilling the library **unthrottled**, the per-track lyrics modal is in the dashboard, and the **Monitor → Lyrics** drain-progress panel (§5.1) is wired in.

**Done:**
- Storage + migration `017` (verified on **PostgreSQL**), the cascade, all 3 tiers, the drain + admin routes, the `lyrics-api` GPU sidecar, the per-track display modal, and a test suite (47 new tests, full suite green, ruff clean).
- Pilot run; owner decisions locked (§4).

**Done since (now in the tree, confirmed against current source):**
- **Dashboard live progress panel** (§5.1): built + browser-verified. New **Monitor → Lyrics** sub-page (`#/monitor/lyrics`): coverage % headline + bar, six stat tiles (resolved / remaining / in-queue / ASR-last-hour / ETA / status), by-source (rolled up embedded/lrclib/asr + synced/plain split) and by-status breakdown bars, and a filterable queue table with per-row Retry/Skip/Delete, bulk reset-by-scope, and Run-tick-now. Polls `GET /v1/lyrics/{stats,requests}` every 10 s; admin-403 degrades to an explanatory message. Files: `app/static/js/v2/monitor.js` (`renderLyricsMonitor`, registered as `GIQ.pages.monitor.lyrics`), `app/static/js/v2/router.js` (route + `'lyrics': 'Lyrics'` label), `app/static/css/pages.css` (`.lyr-breakdown` + lyrics-only `.lbf-state-chip` colors).
- **Static cache fix**: `add_security_headers` (`app/main.py:191-192`) now sets `Cache-Control: no-cache` on `/static/*` + `/dashboard` (paired with StaticFiles' ETag → 304 when unchanged). Kills the "hard-refresh to see new JS/CSS after a deploy" footgun noted below; `/v1/*` stays `no-store, private` (`app/main.py:186`).

**NOT done. Pick up here:**
1. **Phase D**: lyric-aware ranker features (§5.2). A `[RETRAIN]` change. **Deferred.** The config switches already exist but stay off: `LYRICS_EMBED_ENABLED=false`, `LYRICS_EMBED_MODEL_DIR=/data/models/lyrics` (`app/core/config.py:207-209`).
2. **Promote to real prod**: `<prod-host>` currently runs `dev` (a snapshot was taken for rollback). Fast-forward `dev → main` + redeploy when satisfied (§3.3).
3. **Phase E**: optional self-hosted LRCLIB mirror (unchanged from the original design; defer).

**Corrections to the original design (confirmed live):**
- Library is **~152k tracks** (not 67k). GPU is an **RTX A4000 16 GB** (not RTX 4000 8 GB). `large-v3` is comfortable.
- **VAD is OFF by default.** Silero VAD (speech-trained) silently dropped ~50 % of *sung* vocals in the pilot; the `instrumentalness` gate already prevents instrumental hallucination (0 % in the pilot), so VAD is redundant here. Configurable via `LYRICS_ASR_VAD`.
- The ASR sidecar mounts `/music` **read-only** and gates `/health` on **readability**, NOT writability (the download sidecars write downloads; the ASR sidecar only reads).
- **No path mapping needed**: both boxes mount the library at `/music`, and GrooveIQ stores `file_path` as `/music/...`, so `LYRICS_API_MUSIC_PATH` stays empty.

---

## 1. What's built (file map)

Cascade `app/services/lyrics.py::resolve_lyrics(track, *, lrclib_client, asr_client, allow_asr, skip_cheap_tiers)` walks the tiers and returns a `LyricsResolution(outcome, source, quality, plain, synced, language, asr_used, cheap_exhausted, ...)`. Quality ladder (higher = better for display): `4 embedded_synced · 3 lrclib_synced · 2 embedded_plain · 1 lrclib_plain · 0 asr_synced · -1 asr_plain`. ASR never overwrites a real source; the instrumentalness gate blocks ASR only.

| Piece | Where |
|---|---|
| Storage cols (`lyrics_plain/synced/source/quality/language`, `is_explicit`, `lyrics_embedding`, `lyrics_version`, `lyrics_fetched_at`) + `LyricsRequest` queue table | `app/models/db.py` |
| Migration (sqlite + postgres) + in-app `_apply_column_migrations` (incl. `BOOLEAN` in the type allow-list) | `migrations/017_add_lyrics_columns.py`, `app/db/session.py` |
| `LYRICS_VERSION = "1.0"` (decoupled from `ANALYSIS_VERSION`) | `app/services/audio_analysis.py` |
| Tier 1: `read_embedded_lyrics()` (non-easy mutagen; USLT/SYLT/Vorbis/MP4); wired into the scan, gated on `LYRICS_ENABLED`; embedded-*plain* deliberately does NOT stamp `lyrics_version` so the drain can seek an LRCLIB synced upgrade | `app/services/metadata_reader.py`, `app/services/analysis_worker.py`, `app/workers/library_scanner.py` (quality-guard in `_upsert_track_features`) |
| Tier 2: LRCLIB client (httpx, UA, throttle, TTL cache, 404→`/search` by ±2 s duration, `ok`-vs-`error`) | `app/services/lrclib.py` |
| Tier 3: ASR client + sidecar | `app/services/lyrics_asr.py` + `lyrics-api/` (Dockerfile, main.py, requirements.txt, docker-compose.yml) |
| Drain (rate-limited tick, cooldown/backoff, `no_lyrics` vs `search_error`, stale-`searching` reaper, GPU-only budget + per-tick smoothing, `cheap_exhausted` fast-path, per-track failure isolation) | `app/services/lyrics_drain.py` |
| Drain scheduler job (gated on `LYRICS_ENABLED`) | `app/workers/scheduler.py` |
| Display endpoint `GET /v1/tracks/{id}/lyrics` + `TrackLyricsResponse` | `app/api/routes/tracks.py`, `app/models/schemas.py` |
| Admin drain routes `GET /v1/lyrics/{stats,requests}`, `POST /v1/lyrics/{run,requests/{id}/retry,requests/{id}/skip,requests/reset}`, `DELETE /v1/lyrics/requests/{id}` | `app/api/routes/lyrics.py` (registered in `app/main.py`) |
| Dashboard per-track modal (Explore→Tracks "♪ Lyrics" row action; synced/plain render + source chip; auto-transcribed marked) | `app/static/js/v2/explore.js`, `app/static/css/pages.css` (`.lyr-*`) |
| Tests | `tests/test_lyrics.py`, `test_lrclib.py`, `test_lyrics_drain.py`, `test_lyrics_sidecar.py` |

Drain statuses: `queued · searching · complete · instrumental · no_lyrics · failed · search_error · permanently_skipped`. Reset scopes: `no_lyrics · failed · search_error · permanently_skipped · instrumental · all`.

---

## 2. Config (`LYRICS_*`, `app/core/config.py`): values currently set on `<prod-host>`

```
LYRICS_ENABLED=true
LYRICS_LRCLIB_ENABLED=true
LYRICS_ASR_ENABLED=true
LYRICS_API_URL=http://<gpu-vm-ip>:8300      # IP, not hostname (container DNS can't resolve .local)
LYRICS_ASR_VAD=false                         # pilot decision (code default false)
LYRICS_ASR_INSTRUMENTAL_MAX=0.5              # gate: skip ASR when instrumentalness >= this
LYRICS_DRAIN_MAX_PER_HOUR=0                  # 0 = unthrottled ASR (owner decision)
LYRICS_DRAIN_BATCH_SIZE=200                  # tracks examined per tick
LYRICS_DRAIN_POLL_MINUTES=5
# LYRICS_API_MUSIC_PATH=                      # left empty: paths match
```
`lyrics_enabled` / `lyrics_lrclib_enabled` / `lyrics_asr_enabled` are `@property` gates. ASR adds **no new Python deps** to the GrooveIQ image (uses existing `httpx`+`mutagen`).

---

## 3. How it's deployed: exact procedure

### 3.1 `lyrics-api` ASR sidecar on `<gpu-vm>` (the part to reproduce carefully)

`<gpu-vm>` already has: an NVIDIA GPU + **nvidia-container-toolkit** (`docker info` shows the `nvidia` runtime), **`docker compose` v2**, and the music library mounted at `<library-path>` (it also runs Plex/Ollama). The sidecar runs from `~/<user>/lyrics-api/`, **separate from the main GrooveIQ stack**.

```bash
# From a dev machine, at the repo root:
ssh <user>@<gpu-vm> 'mkdir -p ~/lyrics-api'
scp lyrics-api/Dockerfile lyrics-api/main.py lyrics-api/requirements.txt lyrics-api/docker-compose.yml \
    <user>@<gpu-vm>:~/lyrics-api/

# Create ~/lyrics-api/.env on the VM (docker compose reads it automatically):
#   MUSIC_PATH=<library-path>     # host path of the library on the VM; compose mounts it :ro at /music
#   LYRICS_API_PORT=8300
#   LYRICS_MODEL=large-v3
#   LYRICS_DEVICE=auto            # -> cuda when CTranslate2 sees a GPU, else cpu
#   MUSIC_MIN_ENTRIES=1           # populated library -> also catch a stale empty mount (#123)
#   LOG_LEVEL=INFO

ssh <user>@<gpu-vm> 'cd ~/lyrics-api && docker compose up -d --build'

# Verify (first /transcribe lazily downloads large-v3 ~3GB to the lyrics_models volume):
ssh <user>@<gpu-vm> 'curl -s localhost:8300/health'
#   expect: {"status":"ok","ready":true,"device":"cuda","model":"large-v3","music":{"readable":true,...}}
```

`docker-compose.yml` mounts `${MUSIC_PATH}:/music:ro` + `lyrics_models:/data/models`, and grants the GPU via `deploy.resources.reservations.devices: [{driver: nvidia, count: all, capabilities: [gpu]}]`. The sidecar is stateless; GrooveIQ owns all state.

- **Redeploy a sidecar code change:** `scp lyrics-api/main.py <user>@<gpu-vm>:~/lyrics-api/ && ssh ... 'cd ~/lyrics-api && docker compose up -d --build'` (cached layers → fast; the model volume persists).
- **Stale `/music` mount (Errno 13 / unhealthy):** `docker compose up -d --force-recreate lyrics-api` re-binds it.
- **Smoke a single transcription:** `curl -s -X POST localhost:8300/transcribe -H 'content-type: application/json' -d '{"path":"/music/<artist>/<album>/<track>.flac","vad":false}' | jq '{language,rtf,text:.text[0:120]}'`.

### 3.2 GrooveIQ on `<prod-host>`

`/opt/grooveiq` is the ansible-managed checkout (PostgreSQL; deploy = git ff + `docker compose build/up`). See `reference_prod_deploy.md`.

```bash
sudo -u ansible git -C /opt/grooveiq fetch origin
sudo -u ansible git -C /opt/grooveiq checkout -B dev origin/dev     # (snapshot box runs dev)

# Append the §2 LYRICS_* block to /opt/grooveiq/.env (back it up first; it's hand-maintained).

cd /opt/grooveiq && docker compose build grooveiq && docker compose up -d --force-recreate grooveiq
```
Migration `017` auto-applies on boot via `_apply_column_migrations()`; `create_all` makes the `lyrics_requests` table. Rebuild is fast (no requirements change). A restart re-triggers the library scan (fine on the snapshot box) and starts the drain job (since `LYRICS_ENABLED=true`).

### 3.3 Promote to real prod

When satisfied: fast-forward `dev → main`, push, then on the real-prod box ff to `origin/main` + rebuild (same as 3.2 but `main`), set the same `LYRICS_*` env, and ensure `<gpu-vm>` is reachable from it. Deploy/keep the sidecar on `<gpu-vm>` per 3.1.

---

## 4. Pilot results + locked decisions

Pilot = 28 tracks (20 voiced + 8 instrumental) POSTed straight to the sidecar (`lyrics-api/pilot.py`, throwaway; recreate from the snippet in the session memory if needed).

- **28/28 transcribed OK.** RTF mean ~0.02 (VAD on) / ~0.1 (VAD off) on the A4000 → whole-library ASR tail is **~days, not weeks** (and most tracks resolve via LRCLIB / the gate, so far fewer reach ASR).
- **0 % hallucination on instrumentals** (VAD + gate).
- **VAD on dropped ~50 % of sung vocals** (Toni Braxton, Asia, Bowie, Elton John → 0–10 chars); VAD off → full clean transcripts. → **`LYRICS_ASR_VAD=false`**.
- Strong multilingual detection (en/fr/de/ja/la).

**Owner decisions:** VAD **off** · **no Demucs** vocal separation for v1 · backfill **unthrottled** (`LYRICS_DRAIN_MAX_PER_HOUR=0`). Model stays **`large-v3`**.

---

## 5. Remaining work

### 5.1 Dashboard live progress panel  ·  **DONE (built, verified, in the tree)**

> Implemented as **Monitor → Lyrics**. See "Done since" in §0. The spec below is retained as the build record. No backend stats tweak was needed (the panel rolls up `by_source` client-side); the optional `Cache-Control` fix in the §5.1 reminder *was* applied.

A backfill-progress view (the read+control side of the drain, mirroring the existing **Lidarr Backfill** sub-tab). The data already exists at `GET /v1/lyrics/stats` and `GET /v1/lyrics/requests`; this is frontend-only plus (optionally) one stats tweak.

**Placement:** a new sub-page under **Monitor** (observability), e.g. `#/monitor/lyrics`. Add it to `app/static/js/v2/monitor.js` (`GIQ.pages.monitor.lyrics`), register the route + nav label, styles in `app/static/css/pages.css`. Mirror the Lidarr-Backfill panel functions for structure.

**Must show (poll `GET /v1/lyrics/stats` every ~10 s while visible):**
- **A coverage progress bar**: `resolved / total_tracks` as a % (the headline the owner asked for), with the raw counts.
- **By-source** breakdown (`embedded · lrclib · asr · instrumental · none`) and **by-status** (`queued · searching · complete · instrumental · no_lyrics · search_error · failed · permanently_skipped`): small bars or chips.
- **ASR pacing**: `asr_used_last_hour`, `asr_capacity_remaining` (null = unthrottled), `eta_hours`, `tick_in_progress`, `last_tick_at`.
- **Queue table** (`GET /v1/lyrics/requests?status=&limit=&offset=`) with a status filter; columns track_id / status / source / attempts / last_error / next_retry.

**Controls (admin):** "Run tick now" → `POST /v1/lyrics/run`; per-row Retry/Skip/Delete → `POST /v1/lyrics/requests/{id}/retry|skip`, `DELETE /v1/lyrics/requests/{id}`; bulk "Reset scope" → `POST /v1/lyrics/requests/reset` body `{"scope": "..."}`. Reuse `GIQ.api` + `GIQ.toast`. (Every `/v1/lyrics/*` route calls `require_admin`, i.e. admin-gated only when admin keys are configured: `ADMIN_API_KEYS` set; otherwise any valid key reaches them.)

**Reminder:** after deploying frontend changes, the owner must **hard-refresh** (no `Cache-Control` on `/static`). Optionally fix that globally by adding `Cache-Control: no-cache` for `/static/*` + `/dashboard` in `add_security_headers` (`app/main.py`).

### 5.2 Phase D: lyric-aware ranker features  ·  `[RETRAIN]`

Layer in cheapest-first; everything must degrade gracefully when lyrics are absent. The columns already exist (`is_explicit`, `lyrics_embedding`, `lyrics_language`).

| Feature | How | Ranker use |
|---|---|---|
| `lyrics_language` | Whisper's detected language, or `langdetect`/`fasttext` on text | language-affinity feature + taste-profile field |
| `is_explicit` | profanity lexicon (e.g. `better-profanity`) | context filter (kids/work) + feature |
| `lyrical_density` | `word_count / duration_sec` | rap-vs-ambient signal; cheap, stable |
| `lyrics_embedding` | **torch-free ONNX** sentence-embedding (e.g. `bge-small`/multilingual-e5-small) via existing `onnxruntime`+`tokenizers`, lazy-loaded like the CLAP text tower (`app/services/clap_text.py` is the template); store base64 in `lyrics_embedding`; optional 2nd FAISS `lyrics_index` | `lyric_similarity` (cosine vs the user's lyrical-taste centroid) → candidate source + feature |
| `text_valence` / topic | lexicon or small ONNX classifier | contrast with audio `valence`; reranking diversity |

Plug into `FEATURE_COLUMNS` in `app/services/feature_eng.py` (39 cols today) + `ranker.py`; tag `[RETRAIN]` and bump the ranker, **never** `ANALYSIS_VERSION`. Keep prod **torch-free** (ONNX only). Recommended first set: `language + is_explicit + lyrical_density + lyric_similarity`; defer sentiment/topic. The gate + model location are already stubbed: `LYRICS_EMBED_ENABLED` (default `false`) and `LYRICS_EMBED_MODEL_DIR` (default `/data/models/lyrics`) in `app/core/config.py:207-209`, both unused until this phase lands.

### 5.3 Phase E: self-hosted LRCLIB mirror (optional, deferred)

Point tier 2 at a self-hosted LRCLIB-dump mirror (the `acousticbrainz-lookup` pattern) by changing `LYRICS_LRCLIB_URL`; no other code change. Only if the live API proves unreliable.

---

## 6. Gotchas / learnings (paid for already)

- **Container DNS:** the grooveiq container could not resolve `<gpu-vm>`'s `.local` hostname. Use the **IP** in `LYRICS_API_URL`.
- **Read-only ASR mount:** the sidecar must mount `/music` `:ro` and gate `/health` on **readability**. The verbatim writability probe from the download sidecars makes a correct read-only mount report 503. (Fixed; don't reintroduce.)
- **VAD ≠ good for music:** see §4. The client must NOT hardcode `vad`; it sends `LYRICS_ASR_VAD`.
- **PostgreSQL `BOOLEAN`:** `_apply_column_migrations`' type allow-list needed `BOOLEAN` added; verified `is_explicit` is a real `boolean` on PG and agrees with the ORM `Boolean`.
- **Embedded-plain doesn't stamp `lyrics_version`** (so the drain reprocesses it for an LRCLIB synced upgrade); embedded-synced does (it's the max). The persistence-layer quality-guard stops a re-scan downgrading a better drain result.
- **macOS `sed`/`timeout`:** no GNU `\b` in BSD `sed`, no `timeout` binary. Use `perl`/SSH `ConnectTimeout` locally; the boxes are Linux.
- **Drain pacing:** ASR is the only budgeted resource (`last_asr_at` sliding window); embedded/LRCLIB are free. `cheap_exhausted` makes ASR retries skip LRCLIB so a reset doesn't re-hammer it.

---

## 7. Verification recipe

```bash
.venv-test/bin/python -m pytest tests/test_lyrics.py tests/test_lrclib.py \
  tests/test_lyrics_drain.py tests/test_lyrics_sidecar.py tests/test_api_endpoints.py -q
.venv-test/bin/ruff check app/ tests/ lyrics-api/
```
(Note: `tests/test_e2e_recommendation.py::test_model_stats_endpoint` fails on this machine, a **pre-existing**, lyrics-unrelated env issue, confirmed failing on clean `main` too.)

Live:
```bash
curl -s <gpu-vm>:8300/health | jq '{status,ready,device,model,music}'
curl -s -H "Authorization: Bearer <key>" https://<grooveiq-host>/v1/lyrics/stats | jq
curl -s -H "Authorization: Bearer <key>" https://<grooveiq-host>/v1/tracks/<track_id>/lyrics | jq '{source,quality,is_synced,language}'
```
Dashboard: hard-refresh `/dashboard` → Explore → Tracks → "♪ Lyrics" on any resolved row.
