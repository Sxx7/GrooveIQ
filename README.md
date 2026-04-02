# GrooveIQ

**Behavioral music recommendation engine for self-hosted libraries.**

GrooveIQ sits alongside your existing media server (Navidrome, Jellyfin, Plex, etc.) and learns *how* you listen — not just *what* you listen to. It collects behavioral signals like skips, replays, volume changes, and completion rates, analyzes your audio files locally, and returns a personalized recommendation feed via a simple REST API.

No cloud. No tracking. Runs on your hardware.

---

## What it does

```
Your music app  ──► POST /v1/events  ──► GrooveIQ  ──► GET /v1/feed
  (Navidrome,           (skip, like,       (learns your      (ranked
   Symfonium,            play_end, etc.)    taste model)       track IDs)
   any Subsonic app)
```

**Phase 1** (this release): Event ingestion — collect behavioral signals, store them securely, expose a query API.

**Phase 3** (this release): Audio analysis — extract BPM, key, energy, mood, and a similarity embedding from every file in your library using [Essentia](https://essentia.upf.edu/).

Phases 2, 4, 5 (recommendation engine, feed API, context awareness) follow in subsequent releases.

---

## Quick start

### Prerequisites
- Docker & Docker Compose
- A music library accessible on the host
- 10 minutes

### 1. Clone and configure

```bash
git clone https://github.com/yourname/grooveiq
cd grooveiq
cp .env.example .env
```

Edit `.env`:

```bash
# Generate a secret key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# → paste as SECRET_KEY

# Generate an API key for your music app
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# → paste as API_KEYS

# Set the path to your music on the host
MUSIC_LIBRARY_PATH=/mnt/music
```

### 2. Start

```bash
docker compose up -d
docker compose logs -f grooveiq
```

### 3. Verify

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"grooveiq"}
```

### 4. Trigger a library scan

```bash
curl -X POST http://localhost:8000/v1/library/scan \
  -H "Authorization: Bearer YOUR_API_KEY"
# {"message":"Scan started","scan_id":1,"status":"running"}

# Check progress
curl http://localhost:8000/v1/library/scan/1 \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### 5. Send your first event

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

---

## API reference

Full interactive docs available at `/docs` when `ENABLE_DOCS=true` (development only).

### Authentication

All endpoints require an API key passed as a Bearer token:

```
Authorization: Bearer your-api-key-here
```

### Event ingestion

#### `POST /v1/events`

Ingest a single behavioral event.

**Request body:**

```json
{
  "user_id":    "alice",
  "track_id":   "abc123",
  "event_type": "play_end",
  "value":      0.94,
  "context":    "evening",
  "session_id": "sess-xyz",
  "timestamp":  1700000000
}
```

| Field        | Required | Description |
|---|---|---|
| `user_id`    | ✓ | Your media server's user identifier |
| `track_id`   | ✓ | Your media server's track identifier |
| `event_type` | ✓ | See event types table below |
| `value`      | –  | Event-specific payload (see table) |
| `context`    | –  | Free-text session label (e.g. "workout") |
| `session_id` | –  | Groups events from the same listening session |
| `timestamp`  | –  | Unix UTC timestamp. Defaults to server time. Must be within 24h. |

**Event types:**

| `event_type`   | `value` meaning               | Notes |
|---|---|---|
| `play_end`     | Completion ratio (0–1)        | Values below 5% are dropped as noise |
| `skip`         | Elapsed seconds at skip time  | Skip at 12s vs 180s carry different weight |
| `pause`        | Elapsed seconds               | |
| `resume`       | Elapsed seconds               | |
| `like`         | (none)                        | Explicit positive signal |
| `dislike`      | (none)                        | Explicit negative signal |
| `rating`       | Star rating (1–5)             | |
| `playlist_add` | (none)                        | Strong positive signal |
| `queue_add`    | (none)                        | Moderate positive signal |
| `seek_back`    | Seconds jumped backward       | User replaying — strong positive |
| `seek_forward` | Seconds skipped forward       | Mild negative |
| `repeat`       | (none)                        | Very strong positive |
| `volume_up`    | New volume (0–100)            | Implicit positive |
| `volume_down`  | New volume (0–100)            | Mild negative |

**Response:** `202 Accepted`

```json
{"accepted": 1, "rejected": 0, "errors": []}
```

---

#### `POST /v1/events/batch`

Send up to 50 events in one request. Recommended for clients that buffer locally and flush every 30 seconds.

```json
{
  "events": [
    {"user_id": "alice", "track_id": "abc123", "event_type": "play_end", "value": 0.94},
    {"user_id": "alice", "track_id": "def456", "event_type": "skip", "value": 8.2}
  ]
}
```

---

#### `GET /v1/events`

Query stored events (admin/debug use).

| Parameter  | Default | Description |
|---|---|---|
| `user_id`  | –       | Filter by user |
| `track_id` | –       | Filter by track |
| `limit`    | 50      | Max results (1–500) |
| `offset`   | 0       | Pagination offset |

---

### Library & audio analysis

#### `POST /v1/library/scan`

Trigger a full library scan. Analyzes new and changed files. Returns immediately with a `scan_id`; the scan runs in the background.

#### `GET /v1/library/scan/{scan_id}`

Poll scan progress.

```json
{
  "scan_id": 1,
  "status": "running",
  "files_found": 4823,
  "files_analyzed": 1240,
  "files_failed": 3,
  "started_at": 1700000000,
  "ended_at": null,
  "last_error": null
}
```

Possible statuses: `running` → `completed` | `failed`

---

#### `GET /v1/tracks/{track_id}/features`

Get extracted audio features for a single track.

```json
{
  "track_id": "abc123",
  "duration": 214.5,
  "bpm": 128.0,
  "key": "A",
  "mode": "minor",
  "energy": 0.82,
  "danceability": 0.74,
  "valence": 0.61,
  "acousticness": 0.12,
  "instrumentalness": 0.03,
  "mood_tags": [
    {"label": "energetic", "confidence": 0.88},
    {"label": "happy",     "confidence": 0.61}
  ],
  "analyzed_at": 1700000000,
  "analysis_version": "1.0"
}
```

---

#### `GET /v1/tracks/{track_id}/similar`

Get acoustically similar tracks (pre-FAISS fallback, available now).

| Parameter          | Default | Description |
|---|---|---|
| `limit`            | 10      | Number of results (1–50) |
| `include_features` | false   | Include full feature objects |

---

### Users

#### `POST /v1/users`

Pre-register a user. Optional — users are auto-created on their first event.

#### `GET /v1/users/{user_id}`

Get user record and last-seen timestamp.

---

## Configuration reference

All settings are read from environment variables (or `.env` file). See `.env.example` for the full list.

| Variable                  | Default         | Description |
|---|---|---|
| `SECRET_KEY`              | **required**    | Random secret for internal signing |
| `API_KEYS`                | **required**    | Comma-separated API keys for clients |
| `DATABASE_URL`            | SQLite          | SQLite or PostgreSQL connection string |
| `MUSIC_LIBRARY_PATH`      | `/music`        | Path to music inside container (mount your library here) |
| `ANALYSIS_WORKERS`        | `2`             | Parallel Essentia worker processes |
| `RESCAN_INTERVAL_HOURS`   | `6`             | Library re-scan frequency |
| `EVENT_RETENTION_DAYS`    | `365`           | Raw event retention before aggregation |
| `EVENT_BATCH_MAX`         | `50`            | Max events per batch request |
| `MIN_PLAY_PERCENTAGE`     | `0.05`          | Minimum completion to count as a real listen |
| `ALLOWED_HOSTS`           | `*`             | Comma-separated host whitelist |
| `CORS_ORIGINS`            | `*`             | Comma-separated CORS origin whitelist |
| `ENABLE_DOCS`             | `false`         | Enable `/docs` and `/redoc` (dev only) |
| `LOG_LEVEL`               | `INFO`          | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_JSON`                | `true`          | Structured JSON logs |

---

## Connecting your music app

### Navidrome + Symfonium

Symfonium supports custom scrobble webhooks. Configure it to POST to:
```
http://your-server:8000/v1/events/batch
```
with `Authorization: Bearer YOUR_KEY`.

### Any Subsonic-compatible app

Apps that expose a "scrobble" or "now playing" webhook can be pointed at GrooveIQ's event endpoint. Map Subsonic events to GrooveIQ event types:

| Subsonic action    | GrooveIQ event_type |
|---|---|
| `scrobble`         | `play_end`          |
| `nowPlaying start` | `play_start`        |
| Skip (app-level)   | `skip`              |
| Star               | `like`              |

### Manual / custom integration

Any HTTP client works. A minimal Python example:

```python
import requests, time

GROOVEIQ = "http://localhost:8000"
HEADERS  = {"Authorization": "Bearer your-key"}

def send_event(user_id, track_id, event_type, value=None, context=None):
    requests.post(f"{GROOVEIQ}/v1/events",
        headers=HEADERS,
        json={"user_id": user_id, "track_id": track_id,
              "event_type": event_type, "value": value,
              "context": context, "timestamp": int(time.time())},
        timeout=5,
    )

# Examples
send_event("alice", "track-123", "play_end", value=0.88)
send_event("alice", "track-123", "like")
send_event("alice", "track-456", "skip", value=9.2, context="workout")
```

---

## HTTPS / public deployment

If you're exposing GrooveIQ on the internet (e.g. to reach it from your phone away from home):

1. **Always use HTTPS.** Uncomment the `caddy` service in `docker-compose.yml` and set your domain in `Caddyfile`.

2. **Set `ALLOWED_HOSTS`** to your domain:
   ```
   ALLOWED_HOSTS=grooveiq.yourdomain.com
   ```

3. **Restrict `CORS_ORIGINS`** if you have a web frontend:
   ```
   CORS_ORIGINS=https://grooveiq.yourdomain.com
   ```

4. **Rotate API keys** periodically. Generate new ones with:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Then update `API_KEYS` in `.env` and `docker compose up -d` to reload.

5. **Firewall.** Expose only port 443 (via Caddy) externally. Keep port 8000 internal.

---

## Development

```bash
# Install dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run with hot reload
APP_ENV=development ENABLE_DOCS=true API_KEYS="" uvicorn app.main:app --reload

# Run tests
pytest tests/ -v

# Build Docker image
docker build -t grooveiq:dev .
```

---

## Upgrade notes

GrooveIQ uses Alembic for database migrations. After pulling a new version:

```bash
docker compose down
docker compose pull
docker compose run --rm grooveiq alembic upgrade head
docker compose up -d
```

---

## License

MIT. See `LICENSE`.
