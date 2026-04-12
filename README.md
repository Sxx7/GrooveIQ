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
- **Integration health** — live connectivity probes for all external services

## Quick start

### Prerequisites

- Docker and Docker Compose
- A music library accessible on the host
- (Optional) Navidrome or Plex for track ID sync

### 1. Clone and configure

```bash
git clone https://gitlab.local.devii.ch/simon/grooveiq.git
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
| Database | SQLite (default) / PostgreSQL |
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

GitLab CI pipeline (`.gitlab-ci.yml`) runs automatically on push:

- **Lint** — ruff check + format
- **Test** — pytest with JUnit reporting
- **Security** — pip-audit (dependency CVEs), semgrep (SAST), trivy (container scan)
- **Build** — Docker image pushed to GitLab Container Registry (main branch + tags only)

## License

[CC BY-NC 4.0](LICENSE) — free to use, copy, and modify for non-commercial purposes with attribution.
