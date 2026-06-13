# GrooveIQ

**Self-hosted behavioral music recommendation engine.**

GrooveIQ sits alongside your media server (Navidrome, Jellyfin, Plex) and learns *how* you listen — not just *what*. It collects behavioral signals (skips, replays, volume changes, completion rates), analyzes your audio files locally with [Essentia](https://essentia.upf.edu/), trains ML models on your habits, and serves personalized recommendations via a REST API.

No cloud. No tracking. Runs on your hardware.

## Features

- **Behavioral event ingestion** — 17 event types from any Subsonic-compatible app
- **Local audio analysis** — BPM, key, energy, mood, danceability, 64-dim embeddings via Essentia + ONNX
- **9-step recommendation pipeline** — sessionization, scoring, taste profiles, collaborative filtering, LightGBM ranking, transformer sequential model, diversity reranking
- **8 candidate sources** — content similarity (FAISS), collaborative filtering, session skip-gram, SASRec, Last.fm external CF, artist recall, popularity
- **Context-aware ranking** — device, output, location, time-of-day features
- **Adaptive radio** — stateful sessions seeded from track/artist/playlist with real-time feedback adaptation
- **Playlist generation** — flow, mood, energy curve, and key-compatible (Camelot wheel) strategies
- **Artist recommendations** — multi-source artist discovery from listening history, Last.fm similar, and Last.fm top artists
- **User onboarding** — explicit preference collection for cold-start taste profile seeding
- **Last.fm integration** — scrobbling, profile sync, similar-artist discovery, chart fetching, backfill
- **Personalized news feed** — Reddit-sourced music news scored per user's taste profile
- **Download proxy** — search and download tracks via spotdl-api (YouTube Music) or Spotizerr (legacy fallback)
- **Media server sync** — Navidrome/Plex track ID mapping with cascading updates
- **Algorithm tuning** — 78 pipeline weights/thresholds configurable via REST API with versioning, rollback, and export/import
- **Web dashboard** — real-time pipeline visualization, recommendation debugger, algorithm tuning GUI, system health panels
- **Music discovery** — Last.fm similar artists auto-added to Lidarr, optional AcousticBrainz lookup (29.5M tracks)
- **Lyrics** — read-along/karaoke + algorithm signal via a cascade: embedded tags → LRCLIB → optional GPU ASR sidecar (faster-whisper), instrumental-gated
- **Integration health** — live connectivity probes for all external services

## Quick start

### Prerequisites

- Docker and Docker Compose
- A music library accessible on the host
- (Optional) Navidrome or Plex for track ID sync

### 1. Clone and configure

```bash
git clone https://github.com/Sxx7/GrooveIQ.git
cd grooveiq
cp .env.example .env
```

Edit `.env` — at minimum set these three values:

```bash
# Generate secrets
SECRET_KEY=$(openssl rand -base64 32)
API_KEYS=$(openssl rand -base64 32)

# Point to your music
MUSIC_LIBRARY_PATH=/path/to/your/music
```

### 2. Start

```bash
docker compose up -d
```

### 3. Verify

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"grooveiq"}
```

### 4. Scan your library

```bash
curl -X POST http://localhost:8000/v1/library/scan \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### 5. Send events

```bash
curl -X POST http://localhost:8000/v1/events \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "alice",
    "track_id": "abc123",
    "event_type": "play_end",
    "value": 0.94
  }'
```

### 6. Get recommendations

After the pipeline runs (hourly by default, or trigger manually):

```bash
curl "http://localhost:8000/v1/recommend/alice?limit=10" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

## Architecture

```
Music app  ──►  POST /v1/events  ──►  GrooveIQ  ──►  GET /v1/recommend/{user}
 (Navidrome,      (behavioral           (pipeline        (ranked
  Symfonium,       signals)              trains ML        track IDs)
  Plexamp)                               models)
```

### Recommendation pipeline

Runs every hour (configurable). Each step is error-isolated.

| # | Step | What it does |
|---|------|-------------|
| 1 | **Sessionizer** | Groups events into listening sessions by inactivity gaps |
| 2 | **Track scoring** | Computes per-(user, track) satisfaction scores from engagement signals |
| 3 | **Taste profiles** | Builds multi-timescale audio preference profiles (7d / 30d / all-time) |
| 4 | **Collaborative filtering** | User-user and item-item similarity matrices |
| 5 | **Ranker training** | LightGBM model on 39 features with hard negative weighting |
| 6 | **Session embeddings** | Word2Vec skip-gram on listening sessions |
| 7 | **Last.fm cache** | External CF via Last.fm track.getSimilar |
| 8 | **SASRec** | Transformer decoder for next-track prediction |
| 9 | **Session GRU** | Taste drift modeling across sessions |

### Serving flow

1. **Candidate retrieval** — 8 sources merged and deduplicated
2. **Feature engineering** — 39 features per candidate (audio, behavioral, context, sequential)
3. **Ranking** — LightGBM scores candidates (falls back to satisfaction score)
4. **Reranking** — artist diversity, anti-repetition, freshness boost, skip suppression, ~15% exploration slots
5. **Impression logging** — closes the feedback loop for model improvement

## Tech stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12 |
| Framework | FastAPI (async), Pydantic v2 |
| ORM | SQLAlchemy 2.x async |
| Database | SQLite (default) / PostgreSQL (recommended for production) |
| Audio analysis | Essentia 2.1b6 + ONNX Runtime (Discogs-EffNet) |
| Ranking | LightGBM / scikit-learn fallback |
| Similarity | FAISS (IndexFlatIP, 64-dim) + gensim Word2Vec |
| Scheduler | APScheduler 3.x |
| Packaging | Docker multi-stage build |

## Configuration

All settings via environment variables or `.env` file. See [`.env.example`](.env.example) for the full list with documentation.

### Key variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | *required* | Random secret for internal signing |
| `API_KEYS` | *required* | Comma-separated bearer tokens |
| `MUSIC_LIBRARY_PATH` | `/music` | Host path to music (bind-mounted read-only) |
| `DATABASE_URL` | SQLite | `sqlite+aiosqlite:///...` or `postgresql+asyncpg://...` |
| `SCORING_INTERVAL_HOURS` | `1` | Pipeline run frequency |
| `RESCAN_INTERVAL_HOURS` | `6` | Library rescan frequency |
| `ANALYSIS_WORKERS` | CPU-1 | Parallel audio analysis processes |

### Optional integrations

| Integration | Required variables |
|-------------|-------------------|
| Navidrome/Plex sync | `MEDIA_SERVER_TYPE`, `MEDIA_SERVER_URL`, credentials |
| Last.fm | `LASTFM_API_KEY`, `LASTFM_API_SECRET` |
| Lidarr discovery | `LIDARR_URL`, `LIDARR_API_KEY` |
| spotdl-api downloads | `SPOTDL_API_URL`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` |
| Spotizerr (legacy) | `SPOTIZERR_URL` (optional auth: `SPOTIZERR_USERNAME`, `SPOTIZERR_PASSWORD`) |
| Charts | `CHARTS_ENABLED=true`, `LASTFM_API_KEY` |
| News feed | `NEWS_ENABLED=true` (optional: `NEWS_INTERVAL_MINUTES`, `NEWS_DEFAULT_SUBREDDITS`) |
| AcousticBrainz lookup | `AB_LOOKUP_URL`, `AB_LOOKUP_ENABLED=true` (separate container) |
| CLAP text-to-music search | `CLAP_ENABLED=true` (~395 MB ONNX models auto-download on first start; persisted in the `grooveiq_data` volume) |
| Lyrics (read-along + ASR) | `LYRICS_ENABLED=true` (+ `LYRICS_LRCLIB_ENABLED=true`); optional GPU ASR via `LYRICS_ASR_ENABLED=true` + `LYRICS_API_URL` (separate [`lyrics-api`](lyrics-api/README.md) container) |

## Database

GrooveIQ runs on **SQLite by default** — zero-config, no extra container, ideal for trying it out and for small libraries.

For **production deployments — especially libraries beyond ~50K tracks — use PostgreSQL.** GrooveIQ generates sustained concurrent writes: continuous library scans persisting audio features, the hourly recommendation pipeline, background jobs, and API traffic all hit the database at once. SQLite serializes every write behind a single database-wide lock, so under that load it spends increasing time blocked on busy-timeout waits and can stall with `database is locked`. PostgreSQL handles concurrent writers natively and removes that ceiling.

`docker-compose.yml` already includes a `postgres` service. To use it:

1. Set `POSTGRES_PASSWORD` in `.env`.
2. Point `DATABASE_URL` at it (env-file values aren't variable-expanded, so write the password in full):
   ```
   DATABASE_URL=postgresql+asyncpg://grooveiq:YOUR_POSTGRES_PASSWORD@postgres:5432/grooveiq
   ```
3. `docker compose up -d` — a fresh install builds the schema automatically on first start.

Already running on SQLite? `migrations/002_sqlite_to_postgres.py` copies your data into PostgreSQL — see the script's header comment for usage.

## Connecting your music app

### Navidrome + Symfonium

Symfonium supports custom scrobble webhooks. Point it at:

```
http://your-server:8000/v1/events/batch
```

with `Authorization: Bearer YOUR_KEY`.

### Any Subsonic-compatible app

Map events to GrooveIQ event types:

| App action | GrooveIQ `event_type` |
|------------|----------------------|
| Scrobble | `play_end` |
| Now playing | `play_start` |
| Skip | `skip` |
| Star/heart | `like` |

### Custom integration

```python
import httpx, time

client = httpx.Client(
    base_url="http://localhost:8000",
    headers={"Authorization": "Bearer your-key"},
)

client.post("/v1/events", json={
    "user_id": "alice",
    "track_id": "track-123",
    "event_type": "play_end",
    "value": 0.88,
    "timestamp": int(time.time()),
})
```

## GPU acceleration

Two Dockerfiles are provided for GPU-accelerated audio analysis:

| File | Hardware | Notes |
|------|----------|-------|
| `Dockerfile.gpu` | NVIDIA GPU | Requires nvidia-container-toolkit |
| `Dockerfile.igpu` | Intel iGPU | Requires `/dev/dri` passthrough |

Use the matching compose override:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

## Lyrics (read-along + ASR)

GrooveIQ acquires lyrics through a priority **cascade**, preferring real sources over
machine transcription:

1. **Embedded tags** (USLT/SYLT, Vorbis `LYRICS`, MP4 `©lyr`) — read for free during the library scan.
2. **[LRCLIB](https://lrclib.net)** — free, no API key, returns time-synced LRC.
3. **ASR** (optional) — [faster-whisper](lyrics-api/README.md) transcription for voiced tracks that tiers 1–2 miss.

Lyrics power both read-along/karaoke display (`GET /v1/tracks/{id}/lyrics`, shown in the
dashboard's Tracks view) and — later — algorithm signals. ASR results are tagged
`auto-transcribed` and ranked below real sources, and **instrumentals are gated out**
(no hallucinated lyrics).

### Tiers 1–2 (no GPU)

```ini
LYRICS_ENABLED=true
LYRICS_LRCLIB_ENABLED=true
```
That's it — embedded tags land during scans and a background drain fills in LRCLIB matches.

### Tier 3 — ASR on a separate GPU host

ASR runs as a standalone **[`lyrics-api`](lyrics-api/README.md)** container on a machine
with an NVIDIA GPU — **not** in the main `docker-compose.yml`. This keeps the GrooveIQ
image GPU/PyTorch-free; GrooveIQ just calls it over HTTP. Typical topology: GrooveIQ on a
CPU box, `lyrics-api` on your Plex/GPU box (which already has the library mounted).

1. Deploy the sidecar on the GPU host — see **[`lyrics-api/README.md`](lyrics-api/README.md)**.
2. Point GrooveIQ at it:
   ```ini
   LYRICS_ASR_ENABLED=true
   LYRICS_API_URL=http://gpu-host:8300     # use the IP if the container can't resolve the hostname
   # LYRICS_API_MUSIC_PATH=                # only if the sidecar mounts the library at a different path
   ```

**Mount the library at the same path on both** (e.g. `/music`) so GrooveIQ's stored
`file_path`s resolve inside the sidecar; otherwise set `LYRICS_API_MUSIC_PATH`. Pace the
shared GPU with `LYRICS_DRAIN_MAX_PER_HOUR` (`0` = unthrottled). Full env reference and
troubleshooting: [`lyrics-api/README.md`](lyrics-api/README.md).

## API reference

See [docs/API.md](docs/API.md) for the full endpoint reference with request/response examples.

Interactive docs available at `/docs` when `ENABLE_DOCS=true` (development only).

### Endpoint overview

| Group | Endpoints |
|-------|-----------|
| Health | `GET /health`, `GET /dashboard` |
| Events | `POST /v1/events`, `POST /v1/events/batch`, `GET /v1/events` |
| Library | `POST /v1/library/scan`, `GET /v1/library/scan/{id}`, `GET /v1/library/scan/{id}/logs`, `POST /v1/library/sync` |
| Tracks | `GET /v1/tracks`, `GET /v1/tracks/{id}/features`, `GET /v1/tracks/{id}/similar` |
| Users | `GET/POST /v1/users`, `GET/PATCH /v1/users/{id}`, profile, interactions, history, sessions |
| Onboarding | `POST/GET /v1/users/{id}/onboarding` |
| Recommendations | `GET /v1/recommend/{user_id}`, history, artists, model stats |
| Radio | `POST /v1/radio/start`, `GET /v1/radio/{id}/next`, `DELETE /v1/radio/{id}`, `GET /v1/radio` |
| Playlists | `POST/GET /v1/playlists`, `GET/DELETE /v1/playlists/{id}` |
| Discovery | `GET/POST /v1/discovery`, stats |
| Last.fm | connect, disconnect, sync, backfill, profile per user |
| Charts | list, get, build, download, stats |
| Downloads | search, download, status, history |
| Artists | `GET /v1/artists/{name}/meta` |
| News | `POST /v1/news/refresh`, `GET /v1/news/{user_id}` |
| Pipeline | run, reset, status, SSE stream, model readiness, per-step stats |
| Algorithm | config CRUD, defaults, history, export/import, rollback |
| Integrations | `GET /v1/integrations/status` |

## HTTPS / public deployment

If exposing GrooveIQ on the internet:

1. **Use HTTPS.** Uncomment the `caddy` service in `docker-compose.yml` and configure `Caddyfile` with your domain.
2. **Set `ALLOWED_HOSTS`** to your domain.
3. **Restrict `CORS_ORIGINS`** to your frontend origin.
4. **Rotate API keys** periodically (`openssl rand -base64 32`).
5. **Firewall** — expose only port 443 externally.

## Troubleshooting

### Downloads silently stopped / `[Errno 13] Permission denied`

`streamrip-api` and `spotdl-api` bind-mount your library at `/music`. If the host
directory backing that mount is replaced while the containers keep running (e.g. a
deploy recreates it), the containers keep a reference to the *old* inode — `/music`
looks empty and root-owned inside the container and every download fails, even
though search still works.

Each sidecar's `/health` now self-checks the `/music` mount (existence + a
writability probe) and returns **HTTP 503** (container shows `(unhealthy)`) when it
isn't writable, so the failure is no longer silent. Confirm and fix:

```bash
docker exec streamrip-api sh -c 'ls /music | wc -l'   # ~0 while the host dir is full?
docker compose up -d --force-recreate streamrip-api spotdl-api   # re-bind the mount
```

Better, recreate the sidecars after any deploy step that can swap the library dir.
Set `MUSIC_MIN_ENTRIES=1` on the sidecars to *also* flag a writable-but-empty mount
as unhealthy (leave at `0` if your library can legitimately be empty).

### Lidarr backfill: lots of `no_match`

`no_match` means the streaming catalog was searched successfully and nothing
acceptable was found. (Infra/transient search failures are recorded separately as
`search_error` and re-queue automatically once the backend recovers.) On a large
library the residual `no_match` tail is usually *structural*, not a bug:

- **Singles / remixes / promos** — if Lidarr's metadata profile monitors the
  `Single` (and `EP`) release types, many "missing albums" are really individual
  tracks that only exist *inside* an album on streaming services, so album-level
  matching can't fetch them. **The biggest single fix is to narrow the Lidarr
  metadata profile to `Album` (+ `EP`).** Alternatively, enable
  `match.allow_track_fallback` in the backfill settings to download the single
  track instead.
- **Classical** — Lidarr's album artist is the *composer* (often non-Latin script)
  while services list the *performer / orchestra*, so the artist-similarity check
  rejects an otherwise-correct album. Enable `match.classical_relax_artist` to
  accept when the album title matches strongly **and** the year or track count
  corroborates (the corroboration guards against unrelated albums that merely
  share a common title).

Both options are off by default — use the **Preview Match** modal to calibrate
before enabling, especially for libraries with significant non-Latin content.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run with hot reload (auth disabled)
APP_ENV=development ENABLE_DOCS=true DISABLE_AUTH=true API_KEYS="" \
  uvicorn app.main:app --reload

# Run tests
pytest tests/ -v

# Lint
pip install ruff
ruff check app/ tests/

# Build Docker image (amd64 only — essentia has no ARM wheels)
docker build --platform linux/amd64 -t grooveiq:dev .
```

## CI/CD

GitHub Actions workflow (`.github/workflows/ci.yml`) runs automatically on push and pull requests:

- **Lint** — ruff check + format
- **Test** — pytest with JUnit reporting
- **Security** — pip-audit (dependency CVEs), semgrep (SAST), trivy (container scan)
- **Build** — Docker image pushed to GitHub Container Registry (`ghcr.io`) on main branch + tags

## License

[CC BY-NC 4.0](LICENSE) — free to use, copy, and modify for non-commercial purposes with attribution.
