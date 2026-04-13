# GrooveIQ API Reference

**Base URL:** `http://<host>:8000`
**Auth:** All endpoints except `/health` and `/dashboard` require `Authorization: Bearer <api_key>`.
**Content-Type:** `application/json`

---

## Table of Contents

1. [Health](#health)
2. [Events](#events)
3. [Users](#users)
4. [Tracks & Library](#tracks--library)
5. [Recommendations](#recommendations)
6. [News (planned)](#news-planned)
7. [Integrations](#integrations)
8. [Playlists](#playlists)
9. [Discovery](#discovery)
10. [Fill Library](#fill-library)
11. [Last.fm](#lastfm)
12. [Charts](#charts)
13. [Downloads](#downloads)
14. [Artists](#artists)
15. [Pipeline & Stats](#pipeline--stats)
16. [Configuration Reference](#configuration-reference)

---

## Health

### `GET /health`

No authentication required.

```bash
curl http://localhost:8000/health
```

```json
{"status": "ok", "service": "grooveiq"}
```

### `GET /dashboard`

No authentication required. Serves the single-page web dashboard.

---

## Events

### `POST /v1/events` — Ingest a single event

```bash
curl -X POST http://localhost:8000/v1/events \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "simon",
    "track_id": "nav-uuid-001",
    "event_type": "play_end",
    "value": 0.95
  }'
```

**Response** `202 Accepted`

```json
{"accepted": 1, "rejected": 0, "errors": []}
```

#### Required fields

| Field | Type | Description |
|-------|------|-------------|
| `user_id` | string (1-128) | Media server's user identifier |
| `track_id` | string (1-128) | Media server's track identifier |
| `event_type` | string | One of the event types below |

#### Event types

| Type | `value` meaning |
|------|----------------|
| `play_start` | Playback started |
| `play_end` | Completion ratio (0.0-1.0). Below 5% is dropped as noise |
| `skip` | Seconds elapsed at skip time |
| `pause` / `resume` | Seconds elapsed |
| `like` / `dislike` | Explicit signal (no value) |
| `rating` | Star rating (1-5) |
| `playlist_add` / `playlist_remove` | No value |
| `queue_add` | No value |
| `seek_back` / `seek_forward` | Seconds jumped |
| `repeat` | No value |
| `volume_up` / `volume_down` | New volume (0-100) |
| `reco_impression` | Track was shown as a recommendation |

#### Optional core fields

| Field | Type | Description |
|-------|------|-------------|
| `value` | float | Event-specific payload |
| `context` | string (max 64) | Free-text label: `"workout"`, `"sleep"` |
| `client_id` | string (max 64) | Which app sent this |
| `session_id` | string (max 64) | Client-assigned session identifier |
| `timestamp` | int | Unix epoch UTC. Defaults to server time. Rejected if >24h old or >5min future |

#### Rich signal fields (all optional)

These improve recommendation quality but are not required. Existing clients work without them.

<details>
<summary>Expand all optional fields</summary>

**Impression & exposure** — for learning-to-rank

| Field | Type | Description |
|-------|------|-------------|
| `surface` | string | UI surface: `home`, `search`, `now_playing`, `playlist_view` |
| `position` | int >= 0 | Rank position in recommendation list |
| `request_id` | string | Ties an impression to downstream actions |
| `model_version` | string | Which recommendation model produced this |

**Session context**

| Field | Type | Description |
|-------|------|-------------|
| `session_position` | int >= 0 | Track's ordinal position in the session (0-based) |

**Satisfaction & engagement proxies**

| Field | Type | Description |
|-------|------|-------------|
| `dwell_ms` | int >= 0 | Milliseconds listened |
| `pause_duration_ms` | int >= 0 | Inter-track pause in ms |
| `num_seekfwd` | int >= 0 | Forward seek count |
| `num_seekbk` | int >= 0 | Backward seek count |
| `shuffle` | bool | Whether shuffle was active |

**Context / source**

| Field | Type | Description |
|-------|------|-------------|
| `context_type` | string | `playlist`, `album`, `radio`, `search`, `home_shelf` |
| `context_id` | string | ID of the source |
| `context_switch` | bool | True if user just switched context |

**Start / end reason codes**

| Field | Type | Description |
|-------|------|-------------|
| `reason_start` | string | `autoplay`, `user_tap`, `forward_button`, `external` |
| `reason_end` | string | `track_done`, `user_skip`, `error`, `new_track` |

**Device**

| Field | Type | Description |
|-------|------|-------------|
| `device_id` | string | Stable device identifier |
| `device_type` | string | `mobile`, `desktop`, `speaker`, `car`, `web` |

**Local time** (client-side)

| Field | Type | Description |
|-------|------|-------------|
| `hour_of_day` | int (0-23) | Client's local hour |
| `day_of_week` | int (1-7) | ISO 8601: 1=Monday, 7=Sunday |
| `timezone` | string | IANA timezone, e.g. `Europe/Zurich` |

**Audio output**

| Field | Type | Description |
|-------|------|-------------|
| `output_type` | string | `headphones`, `speaker`, `bluetooth_speaker`, `car_audio`, `built_in`, `airplay` |
| `output_device_name` | string | e.g. `AirPods Pro`, `Sonos Living Room` |
| `bluetooth_connected` | bool | Audio routed over Bluetooth |

**Location**

| Field | Type | Description |
|-------|------|-------------|
| `latitude` | float (-90 to 90) | GPS latitude |
| `longitude` | float (-180 to 180) | GPS longitude |
| `location_label` | string | `home`, `work`, `gym`, `commute` |

</details>

**Full example with rich signals:**

```bash
curl -X POST http://localhost:8000/v1/events \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "simon",
    "track_id": "nav-uuid-001",
    "event_type": "play_end",
    "value": 0.95,
    "dwell_ms": 245000,
    "reason_start": "user_tap",
    "reason_end": "track_done",
    "device_type": "mobile",
    "output_type": "headphones",
    "context_type": "playlist",
    "context_id": "my-chill-mix",
    "hour_of_day": 22,
    "day_of_week": 5,
    "shuffle": false
  }'
```

---

### `POST /v1/events/batch` — Ingest multiple events

Send up to 50 events in one request. Each event is validated independently.

```bash
curl -X POST http://localhost:8000/v1/events/batch \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"user_id": "simon", "track_id": "nav-uuid-001", "event_type": "play_start"},
      {"user_id": "simon", "track_id": "nav-uuid-001", "event_type": "play_end", "value": 1.0},
      {"user_id": "simon", "track_id": "nav-uuid-002", "event_type": "skip", "value": 3.5}
    ]
  }'
```

**Response** `202 Accepted`

```json
{"accepted": 3, "rejected": 0, "errors": []}
```

---

### `GET /v1/events` — Query stored events

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `user_id` | string | - | Filter by user |
| `track_id` | string | - | Filter by track |
| `event_type` | string | - | Filter by event type |
| `device_id` | string | - | Filter by device |
| `context_type` | string | - | Filter by context type |
| `request_id` | string | - | Filter by recommendation request ID |
| `limit` | int | 50 | 1-500 |
| `offset` | int | 0 | Pagination offset |

```bash
curl "http://localhost:8000/v1/events?user_id=simon&limit=10" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

---

## Users

### `POST /v1/users` — Create a user

Users are also auto-created on first event. Explicit creation lets you set `display_name`.

```bash
curl -X POST http://localhost:8000/v1/users \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "simon", "display_name": "Simon"}'
```

**Response** `201 Created`

```json
{"uid": 1, "user_id": "simon", "display_name": "Simon", "created_at": 1743638400, "last_seen": null}
```

**Error** `409` if `user_id` already exists.

---

### `GET /v1/users` — List all users

| Parameter | Default | Description |
|-----------|---------|-------------|
| `limit` | 100 | 1-500 |
| `offset` | 0 | - |

---

### `GET /v1/users/{user_id}` — Get a user

**Error** `404` if user does not exist.

---

### `PATCH /v1/users/{uid}` — Update a user

Uses the stable numeric `uid`. Renaming `user_id` cascades to all related tables.

```bash
curl -X PATCH http://localhost:8000/v1/users/1 \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "simon_new", "display_name": "Simon D."}'
```

**Errors:** `404` (no user), `409` (user_id taken), `422` (no fields provided).

---

### `GET /v1/users/{user_id}/profile` — Taste profile

Returns computed taste profile: audio preferences, mood/key/time/device/output/context/location patterns, top tracks, behaviour stats. Multi-timescale sub-profiles (7d/30d/all-time).

Updated hourly by the pipeline. Returns `null` for `taste_profile` if the pipeline hasn't run yet.

---

### `GET /v1/users/{user_id}/interactions` — Track interactions

Per-track aggregated engagement scores.

| Parameter | Default | Options |
|-----------|---------|---------|
| `sort_by` | `satisfaction_score` | `satisfaction_score`, `play_count`, `skip_count`, `last_played_at` |
| `sort_dir` | `desc` | `asc`, `desc` |
| `limit` | 50 | 1-200 |
| `offset` | 0 | - |

---

### `GET /v1/users/{user_id}/history` — Listening history

Paginated raw listening history.

---

### `GET /v1/users/{user_id}/sessions` — Listening sessions

Materialised sessions grouped by inactivity gaps (default 30 min).

| Parameter | Default |
|-----------|---------|
| `limit` | 25 (1-100) |
| `offset` | 0 |

---

## Tracks & Library

### `GET /v1/tracks` — List/search analyzed tracks

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 50 | 1-200 |
| `offset` | int | 0 | - |
| `sort_by` | string | `bpm` | `bpm`, `energy`, `danceability`, `valence`, `key`, `duration`, `analyzed_at` |
| `sort_dir` | string | `asc` | `asc`, `desc` |
| `search` | string | - | Search by title/artist/album |
| `min_bpm` / `max_bpm` | float | - | BPM range |
| `min_energy` / `max_energy` | float | - | Energy range (0-1) |
| `key` | string | - | Musical key: `C`, `C#`, `D`, etc. |
| `mode` | string | - | `major` or `minor` |
| `mood` | string | - | Mood tag: `happy`, `energetic`, `chill`, `dark`, etc. |

---

### `GET /v1/tracks/{track_id}/features` — Audio features

Returns BPM, key, mode, energy, danceability, valence, acousticness, instrumentalness, mood tags, and analysis metadata.

**Error** `404` if not yet analyzed.

---

### `GET /v1/tracks/{track_id}/similar` — Similar tracks

| Parameter | Default | Description |
|-----------|---------|-------------|
| `limit` | 10 | 1-50 |
| `include_features` | false | Include full audio features |

Uses FAISS embedding similarity + SQL pre-filter (BPM/energy/mode).

---

### `POST /v1/library/scan` — Trigger library scan

Starts async audio analysis. One scan at a time.

**Response** `202 Accepted`

```json
{"message": "Scan started", "scan_id": 3, "status": "running"}
```

---

### `GET /v1/library/scan/{scan_id}` — Scan status

Status values: `pending`, `running`, `completed`, `failed`.

```json
{
  "scan_id": 3, "status": "running",
  "files_found": 1842, "files_analyzed": 523, "files_failed": 2,
  "started_at": 1743638400, "ended_at": null
}
```

---

### `GET /v1/library/scan/{scan_id}/logs` — Scan logs

| Parameter | Default | Description |
|-----------|---------|-------------|
| `limit` | 50 | 1-200 |
| `after_id` | 0 | Only entries with `id` > this (for polling) |

---

### `POST /v1/library/sync` — Sync with media server

Maps file-path-based track IDs to Navidrome/Plex native IDs. Cascades updates across all tables. Also imports title/artist/album metadata.

Requires `MEDIA_SERVER_TYPE`, `MEDIA_SERVER_URL`, and credentials.

**Error** `400` if no media server configured.

---

## Recommendations

### `GET /v1/recommend/{user_id}` — Get recommendations

```bash
# Basic
curl "http://localhost:8000/v1/recommend/simon?limit=10" \
  -H "Authorization: Bearer YOUR_API_KEY"

# With context
curl "http://localhost:8000/v1/recommend/simon?limit=10&device_type=mobile&output_type=headphones&context_type=playlist&location_label=commute&hour_of_day=8" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Debug mode
curl "http://localhost:8000/v1/recommend/simon?debug=true" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `seed_track_id` | string | - | Bias results toward this track |
| `limit` | int | 25 | 1-100 |
| `genre` | string | - | Filter by genre (case-insensitive substring) |
| `mood` | string | - | Filter by mood tag (confidence > 0.3) |
| `debug` | bool | false | Include debug info |
| `device_type` | string | - | `mobile`, `desktop`, `speaker`, `car`, `web` |
| `output_type` | string | - | `headphones`, `speaker`, `bluetooth_speaker`, `car_audio`, `built_in`, `airplay` |
| `context_type` | string | - | `playlist`, `album`, `radio`, `search`, `home_shelf` |
| `location_label` | string | - | `home`, `work`, `gym`, `commute` |
| `hour_of_day` | int | server time | Client's local hour (0-23) |
| `day_of_week` | int | server time | ISO 8601 (1=Mon, 7=Sun) |

#### Response

```json
{
  "request_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "model_version": "lgbm-1712000000",
  "user_id": "simon",
  "context": {"hour_of_day": 8, "device_type": "mobile", "...": "..."},
  "tracks": [
    {
      "position": 0, "track_id": "nav-uuid-042", "source": "content",
      "score": 0.87, "title": "Da Funk", "artist": "Daft Punk",
      "bpm": 118.7, "energy": 0.79, "duration": 329.0
    }
  ]
}
```

**Candidate sources:** `content`, `content_profile`, `cf`, `session_skipgram`, `sasrec`, `lastfm_similar`, `artist_recall`, `popular`

#### Debug mode (`?debug=true`)

Adds a `debug` object with: `candidates_by_source`, `total_candidates`, `pre_rerank` (scores before reranking), `reranker_actions` (freshness_boost, skip_suppression, artist_diversity_demote, etc.), and `feature_vectors` (all 39 features per candidate).

#### Closing the feedback loop

Include the `request_id` from the recommendation response in subsequent events:

```bash
curl -X POST http://localhost:8000/v1/events \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "simon", "track_id": "nav-uuid-042",
    "event_type": "play_start",
    "request_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
  }'
```

---

### `GET /v1/recommend/{user_id}/history` — Recommendation history

Past recommendations with impression-to-stream attribution.

| Parameter | Default |
|-----------|---------|
| `limit` | 50 (1-200) |
| `offset` | 0 |

---

### `GET /v1/recommend/{user_id}/artists` — Recommended artists

Returns a ranked list of recommended artists derived from listening behavior, Last.fm similarity, and Last.fm top artists.

| Parameter | Default |
|-----------|---------|
| `limit` | 20 (1-100) |

**Response:**

```json
{
  "user_id": "simon",
  "total": 20,
  "artists": [
    {
      "name": "Radiohead",
      "score": 0.8523,
      "source": "listening",
      "plays": 142,
      "likes": 8,
      "track_count": 45,
      "avg_satisfaction": 0.7812,
      "last_played": 1712000000,
      "in_library": true,
      "audio": {
        "energy": 0.612,
        "danceability": 0.423,
        "valence": 0.385,
        "bpm": 128.3
      },
      "image_url": "https://i.scdn.co/image/ab67616100005174...",
      "top_tracks": [
        {
          "track_id": "abc123",
          "title": "Everything In Its Right Place",
          "album": "Kid A",
          "duration": 250,
          "satisfaction_score": 0.9512,
          "play_count": 23
        },
        {
          "track_id": "def456",
          "title": "Idioteque",
          "album": "Kid A",
          "duration": 309,
          "satisfaction_score": 0.8901,
          "play_count": 18
        }
      ]
    },
    {
      "name": "Portishead",
      "score": 0.5100,
      "source": "lastfm_similar",
      "similar_to": ["Radiohead", "Massive Attack"],
      "mbid": "8f6bd1e4-...",
      "in_library": false,
      "plays": 0,
      "likes": 0,
      "track_count": 0,
      "image_url": null,
      "top_tracks": []
    }
  ]
}
```

**Sources:**
- `listening` — artists from the user's played/liked tracks, ranked by satisfaction + recency
- `lastfm_similar` — similar artists to the user's top artists via Last.fm API
- `lastfm_top` — user's Last.fm top artists (if connected)

---

### `GET /v1/recommend/stats/model` — Model stats

Ranker training info, offline evaluation metrics (NDCG), impression-to-stream rates.

---

## News (planned)

> **Not yet implemented.** See `CLAUDE.md` section "Personalized Music News Feed" for the full implementation plan.

### `GET /v1/news/{user_id}` — Personalized music news feed

Returns Reddit music posts ranked by personal relevance to the user's taste profile.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `limit` | 25 (1-100) | Max items to return |
| `tag` | - | Filter by tag: `FRESH`, `NEWS`, `DISCUSSION` |
| `subreddit` | - | Filter to a specific subreddit |

**Response:**

```json
{
    "user_id": "simon",
    "total": 25,
    "cache_age_minutes": 12,
    "cache_stale": false,
    "items": [
        {
            "id": "t3_abc123",
            "title": "[FRESH] Kendrick Lamar - New Song",
            "url": "https://youtube.com/watch?v=...",
            "reddit_url": "https://www.reddit.com/r/hiphopheads/comments/abc123/...",
            "subreddit": "hiphopheads",
            "score": 4521,
            "num_comments": 342,
            "created_utc": 1712000000,
            "age_hours": 3.5,
            "flair": "FRESH",
            "thumbnail": "https://...",
            "domain": "youtube.com",
            "is_fresh": true,
            "parsed_artists": ["Kendrick Lamar"],
            "relevance_score": 0.92,
            "relevance_reasons": ["artist_match", "genre_match", "fresh"]
        }
    ]
}
```

**Sources:** Posts fetched from genre-relevant subreddits every 30 min (configurable). Scored per-user: 55% personal relevance (artist match, genre-subreddit match, [FRESH] bonus), 25% recency (12h half-life), 20% popularity (log-scaled Reddit score).

**`relevance_reasons`** values: `artist_match`, `genre_match`, `fresh`, `high_engagement` — enables "Because you listen to X" UI badges.

**Config:** Set `NEWS_ENABLED=true` in `.env`. See Configuration Reference for all news settings.

---

## Integrations

### `GET /v1/integrations/status` — Integration connectivity status

Probes all 5 external services in parallel (5s timeout each). Returns connected/error/not-configured status.

**Response:**

```json
{
  "checked_at": 1712000000,
  "integrations": {
    "spotdl_api": {
      "configured": true,
      "url": "http://spotdl-api:8181",
      "connected": true,
      "version": "1.0.0",
      "details": { "output_format": "opus", "active_tasks": 0 }
    },
    "lidarr": {
      "configured": true,
      "url": "http://lidarr:8686",
      "connected": true,
      "version": "2.8.5.4875"
    },
    "acousticbrainz_lookup": { "configured": false },
    "lastfm": {
      "configured": true,
      "connected": true,
      "scrobbling": true
    },
    "media_server": {
      "configured": true,
      "type": "navidrome",
      "url": "http://navidrome:4533",
      "connected": true
    }
  }
}
```

---

## Playlists

### `POST /v1/playlists` — Generate a playlist

```bash
curl -X POST http://localhost:8000/v1/playlists \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Friday Night Flow",
    "strategy": "flow",
    "seed_track_id": "nav-uuid-001",
    "max_tracks": 25
  }'
```

#### Strategies

| Strategy | Required | Description |
|----------|----------|-------------|
| `flow` | `seed_track_id` | Smooth BPM/energy transitions from seed |
| `mood` | `params.mood` | Filter by mood tag + energy arc |
| `energy_curve` | `params.curve` | Match target energy profile (`ramp_up`, `cool_down`, `ramp_up_cool_down`, `steady_high`, `steady_low`) |
| `key_compatible` | `seed_track_id` | Camelot wheel harmonic chaining |

**Response** `201 Created` — includes `tracks` array with position, audio features, and metadata.

---

### `GET /v1/playlists` — List playlists

| Parameter | Default |
|-----------|---------|
| `limit` | 20 (1-100) |
| `offset` | 0 |
| `strategy` | - (filter) |

### `GET /v1/playlists/{playlist_id}` — Get playlist with tracks

### `DELETE /v1/playlists/{playlist_id}` — Delete playlist

---

## Discovery

### `GET /v1/discovery` — List discovery requests

| Parameter | Default |
|-----------|---------|
| `user_id` | - (filter) |
| `status` | - (filter) |
| `limit` | 50 (1-200) |
| `offset` | 0 |

### `POST /v1/discovery/run` — Trigger discovery pipeline

Finds similar artists via Last.fm, auto-adds to Lidarr. Requires `LASTFM_API_KEY` + Lidarr credentials.

### `GET /v1/discovery/stats` — Discovery statistics

---

## Fill Library

Queries AcousticBrainz Lookup for tracks matching each user's taste profile (by audio characteristics — BPM, energy, mood, danceability, etc.), groups results by album, and sends the best-matching albums to Lidarr for FLAC download. Unlike discovery (which adds whole artist discographies), Fill Library targets specific albums containing taste-matched tracks.

Requires `FILL_LIBRARY_ENABLED=true`, `AB_LOOKUP_URL`, `LIDARR_URL`, and `LIDARR_API_KEY`.

### `POST /v1/fill-library/run` — Trigger Fill Library pipeline

Admin only. Queries AB Lookup per user taste profile, groups matched tracks by album, deduplicates against library/Lidarr/previous runs, and sends top albums to Lidarr.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_albums` | int | config default (20) | Override max albums per run (1-100) |

```bash
curl -X POST "http://localhost:8000/v1/fill-library/run?max_albums=5" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response:**

```json
{
  "status": "completed",
  "result": {
    "status": "completed",
    "users_processed": 2,
    "albums_queued": 5,
    "albums_skipped": 12,
    "tracks_matched": 347,
    "tracks_no_album": 23,
    "errors": 0
  }
}
```

### `GET /v1/fill-library` — List fill-library requests

| Parameter | Default | Description |
|-----------|---------|-------------|
| `user_id` | - | Filter by user |
| `status` | - | Filter: `pending`, `artist_added`, `album_monitored`, `sent`, `skipped`, `failed` |
| `limit` | 50 (1-200) | Results per page |
| `offset` | 0 | Pagination offset |

**Response:**

```json
{
  "total": 42,
  "requests": [
    {
      "id": 1,
      "user_id": "simon",
      "artist_name": "Boards of Canada",
      "artist_mbid": "69158f97-...",
      "album_name": "Music Has the Right to Children",
      "album_mbid": "a3e7c2f1-...",
      "matched_tracks": 8,
      "avg_distance": 0.087,
      "best_distance": 0.042,
      "status": "sent",
      "lidarr_artist_id": 234,
      "lidarr_album_id": 567,
      "error_message": null,
      "created_at": 1712000000
    }
  ]
}
```

### `GET /v1/fill-library/stats` — Fill Library statistics

```json
{
  "enabled": true,
  "total": 42,
  "by_status": {
    "sent": 28,
    "skipped": 10,
    "failed": 4
  },
  "today_count": 5,
  "max_per_run": 20,
  "max_distance": 0.15,
  "avg_distance_sent": 0.0923
}
```

---

## Last.fm

### `POST /v1/users/{user_id}/lastfm/connect` — Connect account

```bash
curl -X POST http://localhost:8000/v1/users/simon/lastfm/connect \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"lastfm_username": "my_lastfm", "lastfm_password": "..."}'
```

Password is exchanged for a session key and discarded (never stored).

### `DELETE /v1/users/{user_id}/lastfm` — Disconnect

### `POST /v1/users/{user_id}/lastfm/sync` — Force profile refresh

### `GET /v1/users/{user_id}/lastfm/profile` — Get profile data

---

## Charts

Last.fm-sourced charts with automatic library matching. Rebuilt periodically (default 24h) or on demand.

### `GET /v1/charts` — List available charts

Returns all chart type + scope combinations with entry counts and fetch timestamps.

### `GET /v1/charts/{chart_type}` — Get chart entries

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `chart_type` | path | - | `top_tracks` or `top_artists` |
| `scope` | string | `global` | `global`, `tag:<name>`, `geo:<country>` |
| `limit` | int | 100 | 1-200 |
| `offset` | int | 0 | - |

Each entry includes `in_library` status, `matched_track_id`, `image_url`, and a `library` object with `cover_url` when matched.

**Image priority for frontends:** use `library.cover_url` when available (local, fast), fall back to `image_url` (Last.fm CDN).

### `POST /v1/charts/build` — Trigger chart rebuild (admin)

Fetches fresh charts, matches to library, optionally auto-adds to Lidarr/spotdl-api.

### `POST /v1/charts/download` — Download a chart track

```bash
# By position
curl -X POST http://localhost:8000/v1/charts/download \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"chart_type": "top_tracks", "scope": "global", "position": 0}'

# By artist + title
curl -X POST http://localhost:8000/v1/charts/download \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"artist_name": "Radiohead", "track_title": "Creep"}'
```

Requires `SPOTDL_API_URL` or `SPOTIZERR_URL`. **Error** `503` if neither configured.

### `GET /v1/charts/stats` — Chart statistics

---

## Downloads

Download proxy via spotdl-api (primary) or Spotizerr (legacy fallback). Requires `SPOTDL_API_URL` or `SPOTIZERR_URL`.

### `GET /v1/downloads/search` — Search for tracks

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `q` | string | yes | Search query |
| `limit` | int | no | 1-50, default 10 |

### `POST /v1/downloads` — Download a track

```bash
curl -X POST http://localhost:8000/v1/downloads \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "spotify_id": "70LcF31zb1H0PyJoS1Sx3y",
    "track_title": "Creep",
    "artist_name": "Radiohead"
  }'
```

Status values: `pending`, `downloading`, `duplicate`, `completed`, `error`.

### `GET /v1/downloads/status/{task_id}` — Check progress

### `GET /v1/downloads` — List download history

| Parameter | Default | Description |
|-----------|---------|-------------|
| `status` | - | Filter: `pending`, `downloading`, `duplicate`, `completed`, `error` |
| `limit` | 50 | 1-200 |
| `offset` | 0 | - |

---

## Artists

### `GET /v1/artists/{name}/meta` — Artist metadata

Returns Last.fm metadata (bio, tags, similar artists, top tracks, images) with local library cross-referencing.

```bash
curl "http://localhost:8000/v1/artists/Radiohead/meta" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Error** `404` if not found on Last.fm. **Error** `503` if `LASTFM_API_KEY` not configured.

---

## Pipeline & Stats

### `GET /v1/stats` — Dashboard aggregate stats

Total events, users, analyzed tracks, playlists, 24h/1h event counts, event type breakdown, top tracks, latest scan status, library coverage.

### `POST /v1/pipeline/run` — Trigger pipeline manually

Returns immediately. Will not start if one is already running.

### `POST /v1/pipeline/reset` — Reset and rebuild

Clears all pipeline state (sessions, interactions, profiles) and rebuilds from raw events.

### `GET /v1/pipeline/status` — Pipeline run history

| Parameter | Default |
|-----------|---------|
| `limit` | 10 (1-50) |

Returns current run (if any) and recent history with per-step timing, status, metrics, and errors.

Step names (in order): `sessionizer`, `track_scoring`, `taste_profiles`, `collab_filter`, `ranker`, `session_embeddings`, `lastfm_cache`, `sasrec`, `session_gru`.

### `GET /v1/pipeline/stream` — SSE stream

Real-time Server-Sent Events for pipeline execution. Connect before triggering a run.

Event types: `pipeline_start`, `step_start`, `step_complete`, `step_failed`, `pipeline_end`.

Sends keepalive (`: keepalive`) every 30s.

```bash
curl -N http://localhost:8000/v1/pipeline/stream \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### `GET /v1/pipeline/models` — ML model readiness

Status and key metrics for all 6 ML models: ranker, collab_filter, session_embeddings, sasrec, session_gru, lastfm_cache. Includes `feature_importances` for the ranker.

### `GET /v1/pipeline/stats/sessionizer` — Session statistics

Total sessions, avg duration/tracks/skip rate, skip rate distribution, sessions per user.

### `GET /v1/pipeline/stats/scoring` — Scoring statistics

Score distribution histogram, signal breakdown, top/bottom tracks.

### `GET /v1/pipeline/stats/taste_profiles` — Profile statistics

Users with/without computed profiles.

### `GET /v1/pipeline/stats/events` — Event ingest rate

15-minute buckets over 24h. Useful for sparkline charts.

### `GET /v1/pipeline/stats/activity` — Listening activity timeline

Hourly event counts by type.

| Parameter | Default |
|-----------|---------|
| `days` | 7 (1-30) |

### `GET /v1/pipeline/stats/engagement` — User engagement leaderboard

Per-user 30-day metrics: total events, plays, skip rate, unique tracks, diversity.

---

## Configuration Reference

All settings via environment variables or `.env` file. See [`.env.example`](.env.example) for the full list.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | *required* | Random secret for internal signing |
| `API_KEYS` | *required* | Comma-separated bearer tokens |
| `APP_ENV` | `production` | `development` or `production` |
| `ENABLE_DOCS` | `false` | Enable `/docs` and `/redoc` |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./grooveiq.db` | SQLite or `postgresql+asyncpg://...` |
| `DB_POOL_SIZE` | 5 | Connection pool size |
| `DB_MAX_OVERFLOW` | 10 | Max overflow connections |

### Security

| Variable | Default | Description |
|----------|---------|-------------|
| `RATE_LIMIT_EVENTS` | 300 | Max event requests/min/key |
| `RATE_LIMIT_DEFAULT` | 200 | Max other requests/min/key |
| `ALLOWED_HOSTS` | `*` | Comma-separated allowed hosts |
| `CORS_ORIGINS` | `*` | Comma-separated CORS origins |

### Audio Analysis

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_LIBRARY_PATH` | `/music` | Path to music library (read-only) |
| `ANALYSIS_WORKERS` | 2 | Parallel Essentia workers |
| `ANALYSIS_BATCH_SIZE` | 10 | Tracks per batch |
| `ANALYSIS_TIMEOUT` | 120 | Per-file timeout (seconds) |
| `RESCAN_INTERVAL_HOURS` | 6 | Auto-rescan interval |

### Recommendation Pipeline

| Variable | Default | Description |
|----------|---------|-------------|
| `SESSION_GAP_MINUTES` | 30 | Inactivity gap splitting sessions |
| `SESSION_MIN_EVENTS` | 2 | Minimum events per session |
| `TASTE_PROFILE_DECAY_DAYS` | 30 | Half-life for recency weighting |
| `SCORING_INTERVAL_HOURS` | 1 | Pipeline run frequency |

### Event Ingestion

| Variable | Default | Description |
|----------|---------|-------------|
| `EVENT_BATCH_MAX` | 50 | Max events per batch |
| `EVENT_RETENTION_DAYS` | 365 | Auto-delete threshold |
| `MIN_PLAY_PERCENTAGE` | 0.05 | Drop play_end below this |

### Media Server

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDIA_SERVER_TYPE` | - | `navidrome` or `plex` |
| `MEDIA_SERVER_URL` | - | Server base URL |
| `MEDIA_SERVER_USER` | - | Navidrome username |
| `MEDIA_SERVER_PASSWORD` | - | Navidrome password |
| `MEDIA_SERVER_TOKEN` | - | Plex X-Plex-Token |
| `MEDIA_SERVER_LIBRARY_ID` | `1` | Plex library section ID |
| `MEDIA_SERVER_MUSIC_PATH` | - | Server's music root path |

### Fill Library

| Variable | Default | Description |
|----------|---------|-------------|
| `FILL_LIBRARY_ENABLED` | `false` | Enable Fill Library pipeline |
| `FILL_LIBRARY_MAX_ALBUMS` | `20` | Max albums to queue per run |
| `FILL_LIBRARY_MAX_DISTANCE` | `0.15` | Max AB distance threshold (lower = stricter match) |
| `FILL_LIBRARY_CRON` | `0 4 * * *` | Cron schedule (default: 4 AM daily UTC) |
| `FILL_LIBRARY_QUERY_LIMIT` | `500` | Max tracks per AB Lookup query |

### Music News Feed (planned)

| Variable | Default | Description |
|----------|---------|-------------|
| `NEWS_ENABLED` | `false` | Enable Reddit news feed |
| `NEWS_INTERVAL_MINUTES` | `30` | Fetch frequency |
| `NEWS_MAX_AGE_HOURS` | `48` | Discard posts older than this |
| `NEWS_DEFAULT_SUBREDDITS` | `Music,hiphopheads,indieheads,...` | Comma-separated subreddits |
| `NEWS_MAX_POSTS_PER_SUB` | `50` | Posts per subreddit per cycle |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_JSON` | `true` | Structured JSON or human-readable |
