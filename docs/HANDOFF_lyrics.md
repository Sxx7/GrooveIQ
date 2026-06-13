# Handoff — lyrics acquisition + lyric-aware recommendation

**For:** a fresh Claude Code session (or developer) picking up the lyrics feature.
**Repo:** GrooveIQ backend (this repo). **Branch:** `dev`. **Canonical remote:** `origin` → `github.com/Sxx7/GrooveIQ` (PRs via `gh`; the GitLab remotes are historical only).
**Prereq reading:** `CLAUDE.md` § *Audio pipeline*, § *Recommendation serving*, § *Sidecar `/music` readiness (issue #123)*, and § *Lidarr backfill via streamrip* (the drain/queue pattern we copy). Skim `app/services/analysis_worker.py::_analyze_file`, `app/services/metadata_reader.py`, and the `acousticbrainz-lookup/` + `streamrip-api/` sidecars before touching anything.

---

## 0. TL;DR

GrooveIQ has **no lyrics anywhere today** (greenfield — `grep -ri lyric app/` is empty). We want lyrics for **two** purposes, which have opposite quality bars:

- **Display** (read-along / karaoke) → needs *real, near-verbatim* lyrics, ideally time-synced.
- **Algorithm signal** (taste profile + ranker) → tolerant of imperfect text; language / sentiment / topic / a "lyrical fingerprint" embedding all degrade gracefully.

Because we want **both**, lyrics are acquired through a **priority cascade** — real sources first, machine transcription only to fill genuine gaps:

| Tier | Source | Where it runs | Cost | Display-grade? |
|---|---|---|---|---|
| 1 | **Embedded tags** (USLT/SYLT, Vorbis `LYRICS`, MP4 `©lyr`) | prod, in-scan | ~free | ✅ (as-tagged) |
| 2 | **LRCLIB** (`lrclib.net`, free, no key, returns synced LRC) | prod, HTTP | ~free | ✅ |
| 3 | **ASR fallback** (faster-whisper) | **GPU VM sidecar** | GPU-heavy | ⚠️ approximate |

**Decided with the owner (this session):**
- **Goal = both** (display + algorithm). → cascade above; ASR never overwrites a tier-1/2 hit.
- **"Free + reliable, API lookups OK."** → **LRCLIB live API is the primary online source**; a self-hosted LRCLIB mirror is an *optional* later reliability upgrade (§ 9), not a prerequisite.
- **Compute:** prod (`<prod-host>`) is **CPU-only**; a separate **VM with an RTX 4000** (8 GB) is available. → ASR lives on the **GPU VM as a stateless sidecar** GrooveIQ drives over HTTP (the `streamrip-api` / `acousticbrainz-lookup` pattern). Prod stays PyTorch-free.
- **Scope = whole library** (~67k tracks) — but **still gate out instrumentals** via the existing `instrumentalness` head (no fake lyrics on instrumentals; saves ~10–20 % of the GPU drain).
- **Rollout = pilot ~300 tracks first** to lock model size / decide on vocal separation before committing a GPU-week.

**This document is the design only — no code yet.** Build order recommendation: **Pilot → A → B → C → D** (§ 5).

---

## 1. Context — current state & what's reusable

### 1.1 The analysis path today (what we extend)

`library_scanner.py` walks the library → `AnalysisWorkerPool.analyze(path, cached)` → `_analyze_file()` in a long-lived **spawn** subprocess. Inside `_analyze_file`:

- **`metadata_reader.read_metadata(path)`** — mutagen in `easy=True` mode, reads a fixed tag map (title/artist/album/…). **Does *not* read embedded lyrics** (EasyID3 doesn't expose `USLT`). → we extend it.
- **`audio = es.MonoLoader(filename=path, sampleRate=16000)`** — the file is decoded **once to 16 kHz mono**. `_EFFNET_SR = 16000`. **This is exactly Whisper's input rate** — relevant if we ever want an inline/CPU ASR path, but for our GPU-sidecar design the sidecar re-decodes independently (clean separation).
- **`voice_instrumental-discogs-effnet-1.onnx`** head → `result["instrumentalness"]` (col 0 = instrumental). **This is our ASR gate** (skip transcription when `instrumentalness ≥ threshold`).
- Results are batch-upserted into `TrackFeatures` via `_upsert_track_features`; post-scan rebuilds FAISS.
- **`ANALYSIS_VERSION`** (`audio_analysis.py`, currently `"2.8"`) gates re-analysis. **Lyrics get their own `lyrics_version`** — we must NOT couple lyric re-fetch to an Essentia version bump (different cost class entirely).

### 1.2 Reusable infrastructure (inventory — search hints, not pinned lines)

| Component | File | What we reuse |
|---|---|---|
| Stateless sidecar pattern | `streamrip-api/`, `acousticbrainz-lookup/` (Dockerfile + `main.py` + `requirements.txt`) | Template for the new `lyrics-api/` container. |
| Sidecar HTTP client pattern | `app/services/streamrip.py`, `ab_lookup.py`, `spotdl.py` | Template for `LyricsAsrClient`. |
| `/music` readiness self-check | `streamrip-api/main.py` `_music_status()`/`_music_ready()` (issue #123) | Copy verbatim into `lyrics-api` `/health`. |
| External-API client pattern | `app/services/lastfm_client.py` | Template for `LrclibClient` (httpx, UA header, TTL cache). |
| Drain / queue / retry state machine | `app/services/lidarr_backfill.py` + `lidarr_backfill_config.py` + `LidarrBackfillRequest` model + scheduler jobs + `app/api/routes/lidarr_backfill.py` | **The whole transcription drain copies this pattern** (rate-limited tick, cooldown/backoff, terminal statuses, GUI). |
| Second FAISS index | `app/services/faiss_index.py` (`clap_index`, 512-dim) | Template for an optional `lyrics_index` (text-embedding similarity). |
| Optional ONNX model, lazy load | `app/services/clap_text.py` + `_init_clap_audio_session` in `analysis_worker.py`; torch-stub trick | Template for a **torch-free ONNX text-embedding** model (sentence-embedding) in the main process. |
| Ranker features | `app/services/feature_eng.py` (`FEATURE_COLUMNS`, 39) + `ranker.py` | Where lyric-derived features plug in (a `[RETRAIN]` change). |
| Column migration | `migrations/` + `_apply_column_migrations()` in `app/db/session.py` | Add the new nullable columns for existing DBs. |
| Env-flag feature gate | `CLAP_ENABLED` / `CLAP_MODEL_DIR` in `app/core/config.py` | Template for `LYRICS_*` settings. |
| Versioned config + GUI | `algorithm_config_schema.py` / `algorithm_config.py`; `lidarr_backfill_config.py` | If we want GUI-tunable drain/blend knobs (vs plain env). |

### 1.3 Data available (`app/models/db.py`)

- `TrackFeatures`: `artist`, `album`, `album_artist`, `title`, `duration` (sec), `duration_ms`, `musicbrainz_track_id`, `file_path`, `file_hash`, `instrumentalness`, `embedding` (64-dim), `clap_embedding` (optional). **`duration` is the LRCLIB disambiguation key.** No lyric columns yet (§ 4 adds them).
- We can therefore call LRCLIB with `(artist, title, album, duration)` — the exact tuple its `/api/get` wants.

---

## 2. Why this design (read once, then skip)

- **"Both" forces real-source-first.** ASR is good enough to *feed an algorithm*, not good enough to *show a user*. So the cascade puts embedded + LRCLIB ahead of ASR, and ASR results are tagged `asr` with a lower display-quality rank so the UI can label/segregate them. ASR is never allowed to overwrite a real source.
- **Automatic Lyric Transcription (ALT) reality.** Singing over a backing mix gives Whisper roughly **40–80 % WER** on full mixes; vocal separation (Demucs) first cuts that materially but **2–3×'s GPU cost and reintroduces PyTorch**. Whisper also **hallucinates text on instrumental/non-vocal audio** — which is why the `instrumentalness` gate + faster-whisper's built-in **VAD filter** are mandatory, not optional. The pilot (§ 5) measures real WER on *this* library before we pick `large-v3` vs `medium` and before we decide whether separation is worth it.
- **Single-GPU economics.** ASR is GPU-bound; the VM's older CPU barely matters (decode is cheap ffmpeg). One RTX 4000 (8 GB) holds `large-v3` (~3 GB) comfortably, even batched. **Whole-library cost is real:** `large-v3` ≈ **RTF 0.05–0.1** on Turing → ~12–35 s per 4-min track → **~1–2 weeks of continuous GPU time for ~67k tracks**, as a one-time drain (thereafter only new files). VAD + instrumental-gate + (optionally) `medium` are the levers.
- **Prod stays PyTorch-free.** The image deliberately ships a no-op `torch` stub (see `_get_clap_feature_extractor`). Keeping ASR off-box preserves that. The only ML we add *on prod* is an optional **ONNX** text-embedding model (torch-free, runs on the existing `onnxruntime` + `tokenizers` deps).
- **Legal note (public repo + distributed app).** Lyrics are copyrighted. Storing third-party lyrics locally and showing them to the owning user (what Navidrome lyric plugins do) is the accepted self-host norm; ASR transcripts are a derivative work. For a single-user self-hosted instance this is low-risk, but **do not** add any redistribution/sharing surface, and keep lyric text out of public logs/exports. Flag this in the README when the feature ships.

---

## 3. The acquisition cascade (design + contracts)

`app/services/lyrics.py` exposes `resolve_lyrics(session, track) -> LyricsResult`, walking the tiers in order and stopping at the first acceptable hit. Each tier returns `{plain, synced|None, language|None, source, quality_rank, is_explicit|None}`.

```
quality_rank (higher = better for DISPLAY):
  4  embedded_synced     3  lrclib_synced
  2  embedded_plain      1  lrclib_plain
  0  asr_synced (approx) -1 asr_plain (approx)
 None  instrumental / none
```

### 3.1 Tier 1 — embedded tags (extend `metadata_reader.py`) · effort **S**

New helper `read_embedded_lyrics(path) -> {plain, synced, source}` using **non-easy** mutagen:
- **ID3** (`mutagen.id3.ID3`): `tags.getall("USLT")` → plain; `tags.getall("SYLT")` → synced (rare; convert to LRC).
- **FLAC/OGG/Opus** (VorbisComment): `audio.get("lyrics")` / `audio.get("unsyncedlyrics")` / `audio.get("syncedlyrics")`.
- **MP4/M4A**: `audio.tags.get("\xa9lyr")`.
- Strip NUL bytes (same hygiene as `read_metadata`), cap length, treat whitespace-only as absent.
- Wire it into `_analyze_file` so embedded lyrics land during the normal scan at ~zero cost. **This is the only tier that runs inside the worker.**

### 3.2 Tier 2 — LRCLIB (`app/services/lrclib.py`) · effort **S**

httpx client modelled on `lastfm_client.py`:
- `GET https://lrclib.net/api/get?artist_name=&track_name=&album_name=&duration=` → `200 {plainLyrics, syncedLyrics, instrumental, …}` or `404`. Fall back to `/api/search` (fuzzy) on 404, pick the candidate whose `duration` is within ±2 s.
- **Required `User-Agent`** identifying the app + repo URL (LRCLIB asks for this; no key needed). Be polite: small concurrency, short cache TTL, honour 429 with backoff.
- `instrumental: true` in the response → record `source="instrumental"`, no text.
- Runs on prod, off the hot scan path (a pipeline step or the same drain that schedules ASR — see § 4.3).

### 3.3 Tier 3 — ASR fallback (`lyrics-api` sidecar + `LyricsAsrClient`) · effort **L**

Only invoked when tiers 1–2 miss **and** the track is voiced (`instrumentalness < LYRICS_ASR_INSTRUMENTAL_MAX`, default `0.5` — calibrate in the pilot). See § 4.2 for the sidecar contract.

---

## 4. Component specs

### 4.1 Storage — new `TrackFeatures` columns (+ migration)

All nullable; add via a `migrations/00X_add_lyrics_columns.py` and an `_apply_column_migrations()` entry (mirror the CLAP/music-map columns):

| Column | Type | Notes |
|---|---|---|
| `lyrics_plain` | `Text` | newline-joined plain lyrics |
| `lyrics_synced` | `Text` | LRC (`[mm:ss.xx] line`), nullable |
| `lyrics_source` | `String(16)` | `embedded` \| `lrclib` \| `asr` \| `instrumental` \| `none` |
| `lyrics_quality` | `Integer` | the `quality_rank` above (drives display pick) |
| `lyrics_language` | `String(8)` | ISO 639-1, from Whisper or text LID |
| `is_explicit` | `Boolean` | profanity-lexicon flag (§ 6) |
| `lyrics_embedding` | `Text` | base64 float32, optional ONNX text vector (§ 6) |
| `lyrics_version` | `String(16)` | acquisition pipeline version (decoupled from `ANALYSIS_VERSION`) |
| `lyrics_fetched_at` | `Integer` | unix ts |

> **MVP shortcut:** the drain can be column-only (select work by `lyrics_version != current AND instrumentalness < t AND lyrics_source IS NULL`). A dedicated queue table (below) is the fuller, telemetry-friendly option — recommended given how well the Lidarr-backfill pattern fits.

### 4.2 `lyrics-api` sidecar (new top-level dir `lyrics-api/`, deployed on the GPU VM)

Thin FastAPI wrapper around **faster-whisper** (CTranslate2, CUDA — **no PyTorch**). Mirrors `streamrip-api/`.

- **Image:** `python:3.12` + `faster-whisper` + CUDA `ctranslate2` + `ffmpeg`; runs with `--gpus all` (nvidia-container-toolkit on the VM). Model auto-downloaded on first start to a named volume (`LYRICS_MODEL=large-v3` default).
- **Library mount:** bind-mount the music library read-only at `/music` (same as the other sidecars) so the service transcribes by path. If the VM's mount path differs from prod's, GrooveIQ maps it via `LYRICS_API_MUSIC_PATH` (mirror `MEDIA_SERVER_MUSIC_PATH`).
- **`GET /health`** — copy `acousticbrainz-lookup` / `streamrip-api` `_music_ready()` writability probe → 503 on stale mount (issue #123); include model + device (`cuda`/`cpu`) + VRAM in the body.
- **`POST /transcribe`** — body `{path}` (preferred; reads `/music/...`) *or* multipart audio. Params: `language?` (else auto-detect), `vad=true`, `word_timestamps=true`, `beam_size`, `temperature=0`.
  - Returns `{language, language_probability, duration, text, segments:[{start,end,text,words?}], model, rtf}`.
  - Service builds **LRC** from `segments`/`words` so ASR tracks still get (approximate) synced display.
  - `vad_filter=true` (built-in Silero VAD) trims non-vocal spans → less hallucination + lower cost.
- **Stateless** — no DB. GrooveIQ owns all state. Concurrency capped to 1–2 (single GPU); GrooveIQ throttles upstream.

### 4.3 GrooveIQ orchestration — the transcription/lyrics drain

Copy the **Lidarr-backfill** shape (`app/services/lidarr_backfill.py` + config + routes + two scheduler jobs):

- **`TranscriptionRequest`** model (mirror `LidarrBackfillRequest`): `track_id`, `status` (`queued|searching|downloading|complete|no_lyrics|failed|search_error|instrumental`), `attempts`, `last_error`, `next_retry_at`, `created_at` (rate-limit window key), `source_resolved`.
- **Tick job** (`run_lyrics_tick`): select the next batch of un-resolved, **voiced** tracks under a per-hour cap; for each, walk the cascade — embedded (already in DB from scan) → LRCLIB → ASR via `LyricsAsrClient`; write the winning tier back to `TrackFeatures`; record attempt. Rate-limited so the single GPU isn't swamped (sliding-window `created_at > now-1h`, exactly like backfill).
- **Instrumental gate:** `instrumentalness ≥ LYRICS_ASR_INSTRUMENTAL_MAX` → mark `instrumental`, never call ASR. (Tiers 1–2 still apply — an instrumental can legitimately have embedded "lyrics" e.g. spoken-word; trust a real tag over the gate.)
- **Retry semantics:** reuse the `no_match` vs `search_error` distinction from issue #122 — a LRCLIB 404 / ASR "no speech" is terminal-ish (`no_lyrics`, long cooldown); a sidecar timeout / 5xx / unreachable is a re-queueable `search_error` (short cooldown, doesn't burn attempts). Don't let a GPU-VM outage permanently bury tracks.
- **Config:** env first (`LYRICS_*`, § 7). Promote to a versioned `LyricsConfig` + GUI later if knob-tuning warrants it (the backfill config is the template).

### 4.4 Display surface

- **`GET /v1/tracks/{track_id}/lyrics`** → `{plain, synced, source, quality, language, is_synced, is_explicit}`. 404 when none; 200 with `source:"instrumental"` for instrumentals (so clients show "instrumental", not "no lyrics").
- **Dashboard:** a lyrics panel on the track/Tracks view (synced view highlights the active line if `synced` present; else plain). Show a small `source` chip (`embedded`/`lrclib`/`auto-transcribed`).
- **iOS:** the synced LRC is karaoke-ready; the existing app can consume the same endpoint.

---

## 5. Implementation phases

**Recommended order: Pilot → A → B → C → D.** A ships real lyrics on prod with zero GPU. B is the GPU-week drain. D is the "introduce lyrics to the algorithm" payoff.

### Phase 0 — Pilot (~300 tracks) · **do first, throwaway**
Stand up `lyrics-api` on the VM. Transcribe a representative ~300-track sample (mix of genres/languages, include some instrumentals to verify the gate). Measure: WER spot-check against known-good lyrics for ~30 tracks, RTF/throughput on the RTX 4000, language-detection accuracy, hallucination rate on instrumentals. **Output:** decision on model size (`large-v3` vs `medium`) and whether vocal separation is needed. No GrooveIQ schema changes — a standalone script that POSTs to the sidecar and dumps a CSV.

### Phase A — Acquisition core (embedded + LRCLIB + display) · **no GPU**
Storage columns + migration (§ 4.1); `read_embedded_lyrics` in `metadata_reader.py` wired into the scan; `lrclib.py` client; `lyrics.py` cascade (tiers 1–2 only); `GET /v1/tracks/{id}/lyrics`. A lightweight tick (or pipeline step) that resolves embedded+LRCLIB for the library. **Acceptance:** popular tracks show real (often synced) lyrics on prod with no GPU; instrumentals report `instrumental`; tests cover tag parsing per container format + LRCLIB 404/duration-disambiguation + cascade precedence.

### Phase B — ASR sidecar + drain · **the GPU-week**
`lyrics-api/` container (§ 4.2) on the VM; `LyricsAsrClient`; `TranscriptionRequest` + tick/poll scheduler jobs + instrumental gate + path mapping + retry semantics (§ 4.3). Kick off the whole-library drain. **Acceptance:** voiced tracks missing tier-1/2 lyrics get ASR text + approx synced LRC, tagged `asr`; the drain is rate-limited, resumable, and survives a VM outage (`search_error` re-queue); a stale `/music` mount flips the sidecar unhealthy (503).

### Phase C — Display GUI + iOS · **small**
Dashboard lyrics panel (synced rendering + source chip); confirm iOS consumes the endpoint. **Acceptance:** synced lyrics scroll/highlight; `auto-transcribed` is visually distinguishable from real sources.

### Phase D — Algorithm features (the payoff) · **`[RETRAIN]`**
Layer in, cheapest-first (§ 6): `lyrics_language` + `is_explicit` + `lyrical_density` (trivial) → then the **ONNX lyrics embedding** (torch-free) → optional `lyrics_index` FAISS → new candidate source + ranker features in `feature_eng.py` (`lyric_similarity`, `lyrics_language_affinity`, `is_explicit`, `lyrical_density`, optional `text_valence_delta`). Add weights to a config group. This is a ranker-retrain change — tag it and bump the ranker, not `ANALYSIS_VERSION`. **Acceptance:** new features appear in the ranker's importance chart; a held-out check shows lyric-similarity surfacing thematically-related tracks that audio similarity alone misses; everything degrades gracefully when lyrics are absent.

### Phase E — self-hosted LRCLIB mirror · **optional, deferred** (§ 9)

---

## 6. Lyric-derived features (the menu for Phase D)

Pick a subset; don't build all at once. Cheapest → most involved:

| Feature | How | Robust at high WER? | Ranker use |
|---|---|---|---|
| `lyrics_language` | Whisper's detected language, or `fasttext`/`langdetect` on text | ✅ very | language-affinity feature + taste-profile field |
| `is_explicit` | profanity lexicon (e.g. a wordlist; `better-profanity`) | ✅ | context filter (kids/work) + feature |
| `lyrical_density` | `word_count / duration_sec` | ✅ | rap-vs-ambient signal; cheap, stable |
| `lyrics_embedding` | **ONNX** sentence-embedding (e.g. `bge-small` / multilingual-e5-small) via existing `onnxruntime`+`tokenizers`; lazy-load like CLAP text tower | ⚠️ degrades | `lyric_similarity` (cosine vs user lyrical-taste centroid) → candidate source + feature |
| `text_valence` / sentiment | lexicon or a small ONNX classifier | ⚠️ | contrast with audio `valence` (sad sound + happy words) |
| topic tags | zero-shot or keyword lexicons | ⚠️ | reranking diversity / "because you like X-themed songs" |

**Torch-free guarantee:** every on-prod option above runs on libs already in `requirements.txt` (`onnxruntime`, `tokenizers`, `numpy`) or stdlib. No PyTorch on prod.

---

## 7. Config sketch (`app/core/config.py` env, CLAP-style)

```
LYRICS_ENABLED=false                  # master switch
LYRICS_LRCLIB_ENABLED=true            # tier-2 online lookups
LYRICS_LRCLIB_USER_AGENT="GrooveIQ/<ver> (+github.com/Sxx7/GrooveIQ)"
LYRICS_ASR_ENABLED=false              # tier-3 (needs the GPU sidecar)
LYRICS_API_URL=                       # e.g. http://<gpu-vm>:8300
LYRICS_API_MUSIC_PATH=                # path-map if VM mount != prod mount
LYRICS_ASR_INSTRUMENTAL_MAX=0.5       # skip ASR when instrumentalness >= this
LYRICS_ASR_MODEL=large-v3             # or medium (pilot decides)
LYRICS_DRAIN_MAX_PER_HOUR=0           # 0 = unthrottled; raise to pace the GPU
LYRICS_EMBED_ENABLED=false            # Phase D ONNX text embedding
LYRICS_EMBED_MODEL_DIR=/data/models/lyrics
```

Add `lyrics_enabled` etc. as `settings` properties. Promote the drain/blend knobs to a versioned `LyricsConfig` + Settings GUI only if hand-tuning proves necessary (mirror `lidarr_backfill_config.py`).

---

## 8. Guardrails (apply to every task)

- **Test env:** `.venv-test/bin/python -m pytest <files> -q` (Python 3.12 — default `.venv` is 3.9 and self-skips full-app tests). Lint: `.venv-test/bin/ruff check app/ tests/ lyrics-api/`.
- **Additive & backward-compatible:** new nullable columns + new endpoints only. The scan, ranker, and existing `/recommend` paths must behave identically when `LYRICS_ENABLED=false`.
- **Graceful degradation:** no embedded tag → try LRCLIB → try ASR → `none`. No GPU sidecar → tiers 1–2 still work. No lyrics at all → ranker features are absent/neutral, never errors. A fresh library returns `none`, not a 500.
- **Decouple versions:** lyric re-fetch keys off `lyrics_version`, **never** `ANALYSIS_VERSION` (don't trigger a full Essentia re-scan to refresh lyrics).
- **Config, not constants:** thresholds/weights via settings/`get_config()`, read per-call.
- **Public repo hygiene:** no real hostnames/IPs/keys in committed files or commits — use `<prod-host>`, `<gpu-vm>`, `alice`, `user@example.com`. (Real infra values live in `.env` only.) Keep lyric **text** out of logs/exports (copyright).
- **No `Co-Authored-By` trailers** on commits (owner's convention).
- Match surrounding code style and comment density.

---

## 9. Optional: self-hosted LRCLIB mirror (Phase E)

If LRCLIB live lookups ever feel unreliable or you want full offline independence, mirror its open dataset the same way `acousticbrainz-lookup/` mirrors AcousticBrainz: a sidecar that ingests the LRCLIB DB dump once and serves `/api/get` locally. The `lyrics.py` cascade just points tier-2 at the mirror URL instead of `lrclib.net` — no other change. Defer until there's a concrete reliability need; the live API is free and adequate to start.

---

## 10. Verification recipe (run after each task)

```bash
.venv-test/bin/python -m pytest \
  tests/test_lyrics.py tests/test_lrclib.py tests/test_transcription_drain.py \
  tests/test_api_endpoints.py -q
.venv-test/bin/ruff check app/ tests/ lyrics-api/
```

Sidecar smoke (on the VM):
```bash
curl -s http://<gpu-vm>:8300/health | jq '{model, device, music}'
curl -s -X POST http://<gpu-vm>:8300/transcribe \
  -H 'content-type: application/json' \
  -d '{"path":"/music/<artist>/<album>/<track>.flac","vad":true,"word_timestamps":true}' \
  | jq '{language, rtf, text: .text[0:160]}'
```

Display smoke (dev server, `DISABLE_AUTH=true`):
```bash
curl -s 'http://<grooveiq-host>:8000/v1/tracks/<track_id>/lyrics' | jq '{source, quality, language, is_synced}'
```

---

## 11. Decisions for the owner (resolve before/while building)

1. **Pilot model target** — start the pilot on `large-v3` (best WER, fits 8 GB) and only drop to `medium` if throughput hurts? (Recommended: yes.)
2. **Vocal separation** — leave Demucs out unless the pilot WER on full mixes is unacceptable for display (it adds PyTorch on the VM + 2–3× GPU). (Recommended: out for v1.)
3. **Instrumental "lyrics"** — trust a real embedded/LRCLIB tag even when the `instrumentalness` gate says instrumental (spoken-word, skits)? (Recommended: yes — gate only blocks *ASR*, not real sources.)
4. **Drain pacing** — run the whole-library backfill unthrottled (finish in ~1–2 weeks) or cap per-hour so the VM stays free for other work? (Set `LYRICS_DRAIN_MAX_PER_HOUR`.)
5. **Phase-D feature set** — which lyric features actually go into the ranker first? (Recommended: `language` + `is_explicit` + `lyrical_density` + `lyric_similarity` embedding; defer sentiment/topic.)
6. **Display labelling** — how prominently to mark `auto-transcribed` lyrics so users don't mistake ASR errors for the real thing.
```
