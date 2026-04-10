# GrooveIQ API Reference

**Base URL:** `http://<host>:8000`
**Auth:** All endpoints except `/health` require `Authorization: Bearer <api_key>`.
**Content-Type:** `application/json`

---

## Table of Contents

1. [Health Check](#health-check)
2. [Events](#events)
3. [Users](#users)
4. [Tracks & Library](#tracks--library)
5. [Recommendations](#recommendations)
6. [Playlists](#playlists)
7. [Discovery](#discovery)
8. [Last.fm](#lastfm)
9. [Charts](#charts)
10. [Downloads](#downloads)
11. [Artists](#artists)
12. [Stats & Pipeline Control](#stats--pipeline-control)
13. [Configuration Reference](#configuration-reference)

---

## Health Check

### `GET /health`

No authentication required. Use this to verify the server is running.

```bash
curl http://localhost:8000/health
```

**Response** `200 OK`

```json
{
  "status": "ok",
  "service": "grooveiq"
}
```

---

## Events

### `POST /v1/events` — Ingest a single event

Send one behavioral event from your music player.

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
{
  "accepted": 1,
  "rejected": 0,
  "errors": []
}
```

#### Required fields

| Field | Type | Description |
|-------|------|-------------|
| `user_id` | string (1–128 chars) | Your media server's user identifier |
| `track_id` | string (1–128 chars) | Your media server's track identifier |
| `event_type` | string | One of the event types below |

#### Event types

| Type | Value meaning |
|------|---------------|
| `play_start` | Playback started |
| `play_end` | Track finished. `value` = completion ratio (0.0–1.0) |
| `skip` | User skipped. `value` = seconds elapsed |
| `pause` | Playback paused. `value` = seconds elapsed |
| `resume` | Playback resumed after pause |
| `like` | Explicit thumbs-up / heart |
| `dislike` | Explicit thumbs-down |
| `rating` | Star rating. `value` = 1–5 |
| `playlist_add` | User added track to a playlist |
| `playlist_remove` | User removed track from a playlist |
| `queue_add` | User manually added to queue |
| `seek_back` | User scrubbed backward. `value` = seconds jumped back |
| `seek_forward` | User scrubbed forward. `value` = seconds skipped |
| `repeat` | User hit repeat on a single track |
| `volume_up` | Significant volume increase. `value` = 0–100 |
| `volume_down` | Significant volume decrease. `value` = 0–100 |
| `reco_impression` | Track was shown as a recommendation |

#### Optional core fields

| Field | Type | Description |
|-------|------|-------------|
| `value` | float | Event-specific payload (see table above) |
| `context` | string (max 64) | Free-text label: `"workout"`, `"sleep"`, `"commute"` |
| `client_id` | string (max 64) | Which app/integration sent this |
| `session_id` | string (max 64) | Client-assigned session identifier |
| `timestamp` | int | Unix epoch UTC. Defaults to server time. Rejected if >24h old or >5min future |

#### Rich signal fields (all optional)

These improve recommendation quality but are not required. Existing clients work without them.

<details>
<summary>Click to expand all optional fields</summary>

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
| `pause_duration_ms` | int >= 0 | Inter-track pause in ms before this track started |
| `num_seekfwd` | int >= 0 | Forward seek count during this track |
| `num_seekbk` | int >= 0 | Backward seek count during this track |
| `shuffle` | bool | Whether shuffle mode was active |

**Context / source**

| Field | Type | Description |
|-------|------|-------------|
| `context_type` | string | `playlist`, `album`, `radio`, `search`, `home_shelf` |
| `context_id` | string | ID of the source (playlist ID, album ID, etc.) |
| `context_switch` | bool | True if user just switched to a new context |

**Start / end reason codes**

| Field | Type | Description |
|-------|------|-------------|
| `reason_start` | string | `autoplay`, `user_tap`, `forward_button`, `external` |
| `reason_end` | string | `track_done`, `user_skip`, `error`, `new_track` |

**Device & cross-device identity**

| Field | Type | Description |
|-------|------|-------------|
| `device_id` | string | Stable device identifier |
| `device_type` | string | `mobile`, `desktop`, `speaker`, `car`, `web` |

**Local time context** (client-side)

| Field | Type | Description |
|-------|------|-------------|
| `hour_of_day` | int (0–23) | Client's local hour |
| `day_of_week` | int (1–7) | ISO 8601: 1=Monday … 7=Sunday |
| `timezone` | string | IANA timezone, e.g. `Europe/Zurich` |

**Audio output**

| Field | Type | Description |
|-------|------|-------------|
| `output_type` | string | `headphones`, `speaker`, `bluetooth_speaker`, `car_audio`, `built_in`, `airplay` |
| `output_device_name` | string | e.g. `AirPods Pro`, `Sonos Living Room` |
| `bluetooth_connected` | bool | Whether audio is routed over Bluetooth |

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
    "device_id": "iphone-abc123",
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

Send up to 50 events in one request. Each event is validated independently — one invalid event does not reject the others.

```bash
curl -X POST http://localhost:8000/v1/events/batch \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {
        "user_id": "simon",
        "track_id": "nav-uuid-001",
        "event_type": "play_start"
      },
      {
        "user_id": "simon",
        "track_id": "nav-uuid-001",
        "event_type": "play_end",
        "value": 1.0
      },
      {
        "user_id": "simon",
        "track_id": "nav-uuid-002",
        "event_type": "skip",
        "value": 3.5
      }
    ]
  }'
```

**Response** `202 Accepted`

```json
{
  "accepted": 3,
  "rejected": 0,
  "errors": []
}
```

---

### `GET /v1/events` — Query stored events

Retrieve events with optional filters. Useful for debugging and auditing.

```bash
# Get last 10 events for a user
curl "http://localhost:8000/v1/events?user_id=simon&limit=10" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Get skips for a specific track
curl "http://localhost:8000/v1/events?track_id=nav-uuid-001&event_type=skip" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Filter by device and context
curl "http://localhost:8000/v1/events?device_id=iphone-abc123&context_type=playlist&limit=20" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `user_id` | string | — | Filter by user |
| `track_id` | string | — | Filter by track |
| `event_type` | string | — | Filter by event type |
| `device_id` | string | — | Filter by device |
| `context_type` | string | — | Filter by context type |
| `request_id` | string | — | Filter by recommendation request ID |
| `limit` | int | 50 | Results per page (1–500) |
| `offset` | int | 0 | Pagination offset |

**Response** `200 OK`

```json
[
  {
    "id": 42,
    "user_id": "simon",
    "track_id": "nav-uuid-001",
    "event_type": "play_end",
    "value": 0.95,
    "context": null,
    "client_id": null,
    "session_id": null,
    "timestamp": 1743638400,
    "surface": null,
    "position": null,
    "request_id": null,
    "model_version": null,
    "session_position": null,
    "dwell_ms": 245000,
    "pause_duration_ms": null,
    "num_seekfwd": 0,
    "num_seekbk": 0,
    "shuffle": false,
    "context_type": "playlist",
    "context_id": "my-chill-mix",
    "context_switch": null,
    "reason_start": "user_tap",
    "reason_end": "track_done",
    "device_id": "iphone-abc123",
    "device_type": "mobile",
    "hour_of_day": 22,
    "day_of_week": 5,
    "timezone": null,
    "output_type": "headphones",
    "output_device_name": null,
    "bluetooth_connected": null,
    "latitude": null,
    "longitude": null,
    "location_label": null
  }
]
```

---

## Users

### `POST /v1/users` — Create a user

Register a new user. The server assigns a stable numeric `uid` that never changes even if the username is updated later.

```bash
curl -X POST http://localhost:8000/v1/users \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "simon",
    "display_name": "Simon"
  }'
```

**Response** `201 Created`

```json
{
  "uid": 1,
  "user_id": "simon",
  "display_name": "Simon",
  "created_at": 1743638400,
  "last_seen": null
}
```

**Error** `409 Conflict` if `user_id` already exists.

> **Note:** Users are also auto-created when events arrive for unknown `user_id` values. Explicit creation lets you set `display_name` upfront.

---

### `GET /v1/users` — List all users

```bash
curl "http://localhost:8000/v1/users?limit=50&offset=0" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 100 | Results per page (1–500) |
| `offset` | int | 0 | Pagination offset |

**Response** `200 OK`

```json
[
  {
    "uid": 1,
    "user_id": "simon",
    "display_name": "Simon",
    "created_at": 1743638400,
    "last_seen": 1743724800,
    "event_count": 1523
  }
]
```

---

### `GET /v1/users/{user_id}` — Get a user

```bash
curl http://localhost:8000/v1/users/simon \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK`

```json
{
  "uid": 1,
  "user_id": "simon",
  "display_name": "Simon",
  "created_at": 1743638400,
  "last_seen": 1743724800
}
```

**Error** `404 Not Found` if user does not exist.

---

### `PATCH /v1/users/{uid}` — Update a user

Update username and/or display name. Uses the **stable numeric `uid`** (not the mutable `user_id`).

Renaming `user_id` cascades the change to all related tables (listen_events, listen_sessions, track_interactions).

```bash
# Rename user
curl -X PATCH http://localhost:8000/v1/users/1 \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "simon_new",
    "display_name": "Simon D."
  }'
```

**Response** `200 OK`

```json
{
  "uid": 1,
  "user_id": "simon_new",
  "display_name": "Simon D.",
  "created_at": 1743638400,
  "last_seen": 1743724800
}
```

**Errors:**
- `404 Not Found` — no user with that `uid`
- `409 Conflict` — another user already has the requested `user_id`
- `422 Unprocessable Entity` — must provide at least one of `user_id` or `display_name`

---

### `GET /v1/users/{user_id}/profile` — Get taste profile

Returns the user's computed taste profile (audio preferences, mood, key distributions, behavior stats). Updated hourly by the background scoring pipeline.

```bash
curl http://localhost:8000/v1/users/simon/profile \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK`

```json
{
  "uid": 1,
  "user_id": "simon",
  "display_name": "Simon",
  "profile_updated_at": 1743724800,
  "taste_profile": {
    "audio_preferences": {
      "bpm": {"mean": 122.5, "std": 15.3},
      "energy": {"mean": 0.72, "std": 0.12},
      "danceability": {"mean": 0.65, "std": 0.18},
      "valence": {"mean": 0.55, "std": 0.2},
      "acousticness": {"mean": 0.15, "std": 0.1},
      "instrumentalness": {"mean": 0.3, "std": 0.25}
    },
    "mood_preferences": {
      "energetic": 0.35,
      "happy": 0.25,
      "chill": 0.2,
      "melancholic": 0.12,
      "dark": 0.08
    },
    "key_preferences": {
      "C major": 0.15,
      "G major": 0.12,
      "A minor": 0.1
    },
    "top_tracks": [
      {"track_id": "nav-uuid-001", "score": 0.95},
      {"track_id": "nav-uuid-042", "score": 0.88}
    ],
    "behaviour": {
      "avg_session_length": 12.3,
      "skip_rate": 0.18,
      "avg_completion": 0.82,
      "total_plays": 1523,
      "active_days": 45
    },
    "time_patterns": {
      "8": 0.05, "9": 0.08, "17": 0.12, "20": 0.15, "21": 0.18, "22": 0.2
    },
    "device_patterns": {
      "mobile": 0.65,
      "desktop": 0.35
    },
    "output_patterns": {
      "headphones": 0.55,
      "speaker": 0.30,
      "bluetooth_speaker": 0.15
    },
    "context_type_patterns": {
      "playlist": 0.45,
      "album": 0.30,
      "radio": 0.25
    },
    "location_patterns": {
      "home": 0.60,
      "commute": 0.25,
      "gym": 0.15
    }
  }
}
```

Returns `null` for `taste_profile` if the pipeline hasn't run yet.

---

### `GET /v1/users/{user_id}/interactions` — Get track interactions

Per-track aggregated engagement scores. Each row represents one (user, track) pair with play/skip/like counts and a satisfaction score (0–1).

```bash
# Top tracks by satisfaction
curl "http://localhost:8000/v1/users/simon/interactions?sort_by=satisfaction_score&sort_dir=desc&limit=20" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Most played tracks
curl "http://localhost:8000/v1/users/simon/interactions?sort_by=play_count&sort_dir=desc" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Most skipped tracks
curl "http://localhost:8000/v1/users/simon/interactions?sort_by=skip_count&sort_dir=desc&limit=10" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Options |
|-----------|------|---------|---------|
| `sort_by` | string | `satisfaction_score` | `satisfaction_score`, `play_count`, `skip_count`, `last_played_at` |
| `sort_dir` | string | `desc` | `asc`, `desc` |
| `limit` | int | 50 | 1–200 |
| `offset` | int | 0 | — |

**Response** `200 OK`

```json
{
  "total": 342,
  "interactions": [
    {
      "track_id": "nav-uuid-001",
      "play_count": 15,
      "skip_count": 1,
      "like_count": 1,
      "dislike_count": 0,
      "repeat_count": 3,
      "playlist_add_count": 2,
      "queue_add_count": 1,
      "satisfaction_score": 0.95,
      "updated_at": 1743724800
    }
  ]
}
```

---

### `GET /v1/users/{user_id}/sessions` — Get listening sessions

Materialised listening sessions, grouped by inactivity gaps (default 30 minutes).

```bash
curl "http://localhost:8000/v1/users/simon/sessions?limit=10" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 25 | 1–100 |
| `offset` | int | 0 | — |

**Response** `200 OK`

```json
{
  "total": 89,
  "sessions": [
    {
      "id": 42,
      "session_key": "simon_1743638400",
      "user_id": "simon",
      "started_at": 1743638400,
      "ended_at": 1743642000,
      "event_count": 12,
      "event_id_min": 100,
      "event_id_max": 112
    }
  ]
}
```

---

## Tracks & Library

### `GET /v1/tracks` — List analyzed tracks

Browse analyzed tracks with filtering and sorting.

```bash
# All tracks sorted by BPM
curl "http://localhost:8000/v1/tracks?sort_by=bpm&sort_dir=asc&limit=20" \
  -H "Authorization: Bearer YOUR_API_KEY"

# High-energy tracks only
curl "http://localhost:8000/v1/tracks?min_energy=0.8&sort_by=energy&sort_dir=desc" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Filter by key and BPM range
curl "http://localhost:8000/v1/tracks?key=C&mode=major&min_bpm=120&max_bpm=130" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Filter by mood
curl "http://localhost:8000/v1/tracks?mood=happy&limit=50" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 50 | 1–200 |
| `offset` | int | 0 | — |
| `sort_by` | string | `bpm` | `bpm`, `energy`, `danceability`, `valence`, `key`, `duration`, `analyzed_at` |
| `sort_dir` | string | `asc` | `asc`, `desc` |
| `min_bpm` | float | — | Minimum BPM |
| `max_bpm` | float | — | Maximum BPM |
| `min_energy` | float | — | Minimum energy (0–1) |
| `max_energy` | float | — | Maximum energy (0–1) |
| `key` | string | — | Musical key: `C`, `C#`, `D`, etc. |
| `mode` | string | — | `major` or `minor` |
| `mood` | string | — | Mood tag: `happy`, `energetic`, `chill`, `dark`, etc. |

**Response** `200 OK`

```json
{
  "total": 1842,
  "tracks": [
    {
      "track_id": "nav-uuid-001",
      "title": "Around the World",
      "artist": "Daft Punk",
      "album": "Homework",
      "file_path": "/music/Daft Punk/Homework/Around the World.flac",
      "duration": 428.5,
      "bpm": 121.3,
      "key": "C",
      "mode": "minor",
      "energy": 0.82,
      "danceability": 0.91,
      "valence": 0.65,
      "acousticness": 0.02,
      "instrumentalness": 0.85,
      "mood_tags": [
        {"label": "energetic", "confidence": 0.87},
        {"label": "happy", "confidence": 0.62}
      ],
      "analyzed_at": 1743552000,
      "analysis_version": "essentia-2.1b6"
    }
  ]
}
```

---

### `GET /v1/tracks/{track_id}/features` — Get audio features

```bash
curl http://localhost:8000/v1/tracks/nav-uuid-001/features \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK`

```json
{
  "track_id": "nav-uuid-001",
  "duration": 428.5,
  "bpm": 121.3,
  "key": "C",
  "mode": "minor",
  "energy": 0.82,
  "danceability": 0.91,
  "valence": 0.65,
  "acousticness": 0.02,
  "instrumentalness": 0.85,
  "mood_tags": [
    {"label": "energetic", "confidence": 0.87},
    {"label": "happy", "confidence": 0.62}
  ],
  "analyzed_at": 1743552000,
  "analysis_version": "essentia-2.1b6"
}
```

**Error** `404 Not Found` if the track hasn't been analyzed yet.

---

### `GET /v1/tracks/{track_id}/similar` — Find similar tracks

Returns tracks acoustically similar to the given track, ranked by combined BPM/energy/embedding similarity.

```bash
# Basic similar tracks
curl "http://localhost:8000/v1/tracks/nav-uuid-001/similar?limit=10" \
  -H "Authorization: Bearer YOUR_API_KEY"

# With full feature details
curl "http://localhost:8000/v1/tracks/nav-uuid-001/similar?limit=5&include_features=true" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 10 | 1–50 |
| `include_features` | bool | false | Include full audio features per result |

**Response** `200 OK`

```json
[
  {
    "track_id": "nav-uuid-042",
    "title": "Da Funk",
    "artist": "Daft Punk",
    "album": "Homework",
    "file_path": "/music/Daft Punk/Homework/Da Funk.flac",
    "bpm": 118.7,
    "key": "G",
    "mode": "minor",
    "energy": 0.79,
    "danceability": 0.85,
    "mood_tags": [{"label": "energetic", "confidence": 0.82}],
    "similarity": 0.934
  }
]
```

---

### `POST /v1/library/scan` — Trigger library scan

Starts an asynchronous audio analysis scan of the music library. Only one scan can run at a time. Files are analyzed with Essentia to extract BPM, key, energy, mood, and a 64-dim embedding.

```bash
curl -X POST http://localhost:8000/v1/library/scan \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `202 Accepted`

```json
{
  "message": "Scan started",
  "scan_id": 3,
  "status": "running"
}
```

---

### `GET /v1/library/scan/{scan_id}` — Get scan status

Poll the progress of a library scan.

```bash
curl http://localhost:8000/v1/library/scan/3 \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK`

```json
{
  "scan_id": 3,
  "status": "running",
  "files_found": 1842,
  "files_analyzed": 523,
  "files_failed": 2,
  "started_at": 1743638400,
  "ended_at": null,
  "last_error": null
}
```

Status values: `pending`, `running`, `completed`, `failed`.

---

### `GET /v1/library/scan/{scan_id}/logs` — Get scan logs

Stream log entries for a running or completed scan. Use `after_id` for polling new entries.

```bash
# Initial fetch
curl "http://localhost:8000/v1/library/scan/3/logs?limit=20" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Poll for new entries
curl "http://localhost:8000/v1/library/scan/3/logs?after_id=150&limit=50" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 50 | 1–200 |
| `after_id` | int | 0 | Only return log entries with `id` > this value |

**Response** `200 OK`

```json
[
  {
    "id": 151,
    "timestamp": 1743638500,
    "level": "INFO",
    "filename": "Artist/Album/Track.flac",
    "message": "Analyzed successfully (BPM=121.3, key=Cm)"
  }
]
```

---

### `POST /v1/library/sync` — Sync track IDs with media server

Synchronise GrooveIQ's internal track IDs with your Navidrome or Plex server. Matches tracks by normalised file paths and replaces hash-based IDs with the media server's native IDs. Also imports title/artist/album metadata.

Requires `MEDIA_SERVER_TYPE`, `MEDIA_SERVER_URL`, and credentials to be configured.

```bash
curl -X POST http://localhost:8000/v1/library/sync \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK`

```json
{
  "message": "Sync complete: 1842 fetched, 1838 matched, 1838 updated, 1838 metadata, 4 unmatched.",
  "server_type": "navidrome",
  "tracks_fetched": 1842,
  "tracks_matched": 1838,
  "tracks_updated": 1838,
  "tracks_metadata": 1838,
  "tracks_unmatched": 4,
  "errors": [],
  "elapsed_seconds": 3.42
}
```

**Error** `400 Bad Request` if no media server is configured.

---

## Recommendations

### `GET /v1/recommend/{user_id}` — Get recommendations

Returns ranked track recommendations for a user. Candidates come from multiple sources: acoustic similarity (FAISS), collaborative filtering, artist recall, and popularity fallback. Results are scored by a trained ranker and diversified.

Each call logs `reco_impression` events for feedback loop training.

```bash
# Basic recommendations
curl "http://localhost:8000/v1/recommend/simon?limit=10" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Seed from a specific track
curl "http://localhost:8000/v1/recommend/simon?seed_track_id=nav-uuid-001&limit=15" \
  -H "Authorization: Bearer YOUR_API_KEY"

# With full context (Phase 5) — all context params are optional
curl "http://localhost:8000/v1/recommend/simon?limit=10&device_type=mobile&output_type=headphones&context_type=playlist&location_label=commute&hour_of_day=8&day_of_week=1" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `seed_track_id` | string | — | Bias results toward this track |
| `limit` | int | 25 | 1–100 |
| `debug` | bool | false | Include debug info: candidates by source, feature vectors, reranker actions |
| `device_type` | string | — | `mobile`, `desktop`, `speaker`, `car`, `web` |
| `output_type` | string | — | `headphones`, `speaker`, `bluetooth_speaker`, `car_audio`, `built_in`, `airplay` |
| `context_type` | string | — | `playlist`, `album`, `radio`, `search`, `home_shelf` |
| `location_label` | string | — | `home`, `work`, `gym`, `commute` |
| `hour_of_day` | int | server time | Client's local hour (0-23) |
| `day_of_week` | int | server time | Client's local day (1=Mon, 7=Sun) |

All context parameters are optional. The more context the client sends, the better the personalisation. When omitted, the system uses server local time for hour/day and ignores device/output/location context.

**Response** `200 OK`

```json
{
  "request_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "model_version": "lgbm-1712000000",
  "user_id": "simon",
  "seed_track_id": null,
  "context": {
    "hour_of_day": 8,
    "day_of_week": 1,
    "device_type": "mobile",
    "output_type": "headphones",
    "context_type": "playlist",
    "location_label": "commute"
  },
  "tracks": [
    {
      "position": 0,
      "track_id": "nav-uuid-042",
      "source": "content",
      "score": 0.8734,
      "title": "Da Funk",
      "artist": "Daft Punk",
      "album": "Homework",
      "file_path": "/music/Daft Punk/Homework/Da Funk.flac",
      "bpm": 118.7,
      "key": "G",
      "mode": "minor",
      "energy": 0.79,
      "danceability": 0.85,
      "valence": 0.55,
      "mood_tags": [{"label": "energetic", "confidence": 0.82}],
      "duration": 329.0
    }
  ]
}
```

**Errors:**
- `404 Not Found` — user does not exist
- `404 Not Found` — `seed_track_id` not found in analyzed tracks

**Candidate sources in response:**

| Source | Description |
|--------|-------------|
| `content` | FAISS-based acoustic similarity from seed track |
| `content_profile` | Acoustic similarity from user's taste centroid |
| `cf` | Collaborative filtering ("users who liked X also liked Y") |
| `session_skipgram` | Word2Vec behavioral co-occurrence similarity |
| `sasrec` | Transformer sequential next-track prediction |
| `lastfm_similar` | External CF via Last.fm track.getSimilar |
| `artist_recall` | Tracks from recently listened artists |
| `popular` | Globally popular tracks (fallback) |

**Context-aware behaviour (Phase 5):**

- Context params feed 6 features into the ranking model: `device_affinity`, `output_affinity`, `context_type_affinity`, `location_affinity`, `is_mobile`, `is_headphones`
- Affinity features are computed from the user's historical patterns (e.g., if the user listens 60% on mobile, `device_affinity` = 0.6 when `device_type=mobile`)
- When `device_type` is `car`/`speaker` or `output_type` is `car_audio`/`bluetooth_speaker`/`speaker`, tracks shorter than 90 seconds are suppressed
- Context is logged on `reco_impression` events, so the ranking model learns from context over time
- The `context` object is echoed in the response so the client can confirm what context was applied

**Debug mode (`?debug=true`):**

When `debug=true`, the response includes an additional `debug` field:

```json
{
  "tracks": [...],
  "debug": {
    "candidates_by_source": {
      "content": [{"track_id": "...", "score": 0.85}],
      "cf": [...],
      "session_skipgram": [...],
      "sasrec": [...],
      "lastfm_similar": [...],
      "artist_recall": [...],
      "popular": [...]
    },
    "total_candidates": 150,
    "pre_rerank": [{"track_id": "...", "score": 0.92, "position": 0}],
    "reranker_actions": [
      {"track_id": "...", "action": "freshness_boost", "score_before": 0.5, "score_after": 0.55},
      {"track_id": "...", "action": "skip_suppression", "score_before": 0.7, "score_after": 0.35},
      {"track_id": "...", "action": "artist_diversity_demote", "from_position": 3, "to_position": 12},
      {"track_id": "...", "action": "exploration_slot", "noise_added": 0.15}
    ],
    "feature_vectors": {
      "track_id_1": {"bpm": 120.0, "energy": 0.8, "satisfaction_score": 0.72, "...all 39 features...": "..."}
    }
  }
}
```

Reranker action types: `freshness_boost`, `skip_suppression`, `anti_repetition_exclude`, `short_track_exclude`, `exploration_slot`, `artist_diversity_demote`.

**Closing the feedback loop:**

When the user plays a recommended track, include the `request_id` from the recommendation response in the `play_start` event. This links impressions to streams and improves the ranking model:

```bash
curl -X POST http://localhost:8000/v1/events \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "simon",
    "track_id": "nav-uuid-042",
    "event_type": "play_start",
    "request_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "device_type": "mobile",
    "output_type": "headphones"
  }'
```

---

### `GET /v1/recommend/{user_id}/history` — Recommendation history

See past recommendations and whether the user streamed them (attribution via shared `request_id`).

```bash
curl "http://localhost:8000/v1/recommend/simon/history?limit=20" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 50 | 1–200 |
| `offset` | int | 0 | — |

**Response** `200 OK`

```json
{
  "total": 150,
  "history": [
    {
      "timestamp": 1743724800,
      "track_id": "nav-uuid-042",
      "position": 0,
      "request_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "model_version": "phase4-candidate-gen-v1",
      "streamed": true,
      "file_path": "/music/Daft Punk/Homework/Da Funk.flac",
      "bpm": 118.7,
      "energy": 0.79
    },
    {
      "timestamp": 1743724800,
      "track_id": "nav-uuid-099",
      "position": 1,
      "request_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "model_version": "phase4-candidate-gen-v1",
      "streamed": false,
      "file_path": "/music/Artist/Album/Track.flac",
      "bpm": 125.0,
      "energy": 0.65
    }
  ]
}
```

---

### `GET /v1/stats/model` — Model stats & evaluation

Returns ranker training info, offline evaluation metrics (NDCG, etc.), and impression-to-stream conversion rates.

```bash
curl http://localhost:8000/v1/stats/model \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK` — structure varies based on model state.

---

## Playlists

### `POST /v1/playlists` — Generate a playlist

Create a playlist using one of four generation strategies. Tracks are selected from the analyzed library.

```bash
# Flow: smooth BPM/energy transitions from a seed track
curl -X POST http://localhost:8000/v1/playlists \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Friday Night Flow",
    "strategy": "flow",
    "seed_track_id": "nav-uuid-001",
    "max_tracks": 25
  }'

# Mood: filter by mood tag, order by energy arc
curl -X POST http://localhost:8000/v1/playlists \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Chill Evening",
    "strategy": "mood",
    "params": {"mood": "chill"},
    "max_tracks": 20
  }'

# Energy curve: match tracks to a target energy profile
curl -X POST http://localhost:8000/v1/playlists \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Workout Ramp",
    "strategy": "energy_curve",
    "params": {"curve": "ramp_up"},
    "max_tracks": 30
  }'

# Key-compatible: Camelot wheel harmonic chaining
curl -X POST http://localhost:8000/v1/playlists \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "DJ Set",
    "strategy": "key_compatible",
    "seed_track_id": "nav-uuid-001",
    "max_tracks": 40
  }'
```

#### Strategies

| Strategy | Required params | Description |
|----------|----------------|-------------|
| `flow` | `seed_track_id` | Greedy chain from seed, smooth BPM/energy transitions |
| `mood` | `params.mood` | Filter by mood tag, order by energy arc |
| `energy_curve` | `params.curve` | Match tracks to a target energy profile |
| `key_compatible` | `seed_track_id` | Chain harmonically compatible keys (Camelot wheel) |

#### Energy curve options

`ramp_up`, `cool_down`, `ramp_up_cool_down`, `steady_high`, `steady_low`

**Response** `201 Created`

```json
{
  "id": 5,
  "name": "Friday Night Flow",
  "strategy": "flow",
  "seed_track_id": "nav-uuid-001",
  "params": null,
  "track_count": 25,
  "total_duration": 5842.3,
  "created_at": 1743724800,
  "tracks": [
    {
      "position": 0,
      "track_id": "nav-uuid-001",
      "title": "Around the World",
      "artist": "Daft Punk",
      "album": "Homework",
      "file_path": "/music/Daft Punk/Homework/Around the World.flac",
      "bpm": 121.3,
      "key": "C",
      "mode": "minor",
      "energy": 0.82,
      "danceability": 0.91,
      "valence": 0.65,
      "mood_tags": [{"label": "energetic", "confidence": 0.87}],
      "duration": 428.5
    }
  ]
}
```

---

### `GET /v1/playlists` — List playlists

```bash
# All playlists
curl "http://localhost:8000/v1/playlists" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Filter by strategy
curl "http://localhost:8000/v1/playlists?strategy=flow&limit=10" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 20 | 1–100 |
| `offset` | int | 0 | — |
| `strategy` | string | — | Filter by strategy type |

**Response** `200 OK`

```json
[
  {
    "id": 5,
    "name": "Friday Night Flow",
    "strategy": "flow",
    "seed_track_id": "nav-uuid-001",
    "params": null,
    "track_count": 25,
    "total_duration": 5842.3,
    "created_at": 1743724800
  }
]
```

---

### `GET /v1/playlists/{playlist_id}` — Get playlist with tracks

```bash
curl http://localhost:8000/v1/playlists/5 \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK` — same structure as the `POST` response above (includes `tracks` array).

**Error** `404 Not Found` if playlist does not exist.

---

### `DELETE /v1/playlists/{playlist_id}` — Delete a playlist

```bash
curl -X DELETE http://localhost:8000/v1/playlists/5 \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `204 No Content`

**Error** `404 Not Found` if playlist does not exist.

---

## Discovery

### `GET /v1/discovery` — List discovery requests

List music discovery requests with optional filters.

```bash
curl "http://localhost:8000/v1/discovery?limit=20" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `user_id` | string | — | Filter by user |
| `status` | string | — | Filter by status |
| `limit` | int | 50 | 1-200 |
| `offset` | int | 0 | — |

---

### `POST /v1/discovery/run` — Trigger discovery pipeline

Manually trigger the music discovery pipeline. Finds similar artists via Last.fm API and auto-adds them to Lidarr.

Requires `LASTFM_API_KEY`, `LIDARR_URL`, and `LIDARR_API_KEY` to be configured.

```bash
curl -X POST http://localhost:8000/v1/discovery/run \
  -H "Authorization: Bearer YOUR_API_KEY"
```

---

### `GET /v1/discovery/stats` — Discovery statistics

```bash
curl http://localhost:8000/v1/discovery/stats \
  -H "Authorization: Bearer YOUR_API_KEY"
```

---

## Last.fm

### `POST /v1/users/{user_id}/lastfm/connect` — Connect Last.fm

Connect a user's Last.fm account. The password is exchanged for a session key via the Last.fm API and then discarded (never stored).

```bash
curl -X POST http://localhost:8000/v1/users/simon/lastfm/connect \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"lastfm_username": "my_lastfm", "lastfm_password": "..."}'
```

**Response** `200 OK`

```json
{
  "status": "connected",
  "username": "my_lastfm",
  "scrobbling_enabled": true
}
```

---

### `DELETE /v1/users/{user_id}/lastfm` — Disconnect Last.fm

```bash
curl -X DELETE http://localhost:8000/v1/users/simon/lastfm \
  -H "Authorization: Bearer YOUR_API_KEY"
```

---

### `POST /v1/users/{user_id}/lastfm/sync` — Force-refresh Last.fm profile

Manually trigger a sync of the user's Last.fm top artists and tracks into their taste profile.

```bash
curl -X POST http://localhost:8000/v1/users/simon/lastfm/sync \
  -H "Authorization: Bearer YOUR_API_KEY"
```

---

### `GET /v1/users/{user_id}/lastfm/profile` — Get Last.fm profile

```bash
curl http://localhost:8000/v1/users/simon/lastfm/profile \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK`

```json
{
  "username": "my_lastfm",
  "scrobbling_enabled": true,
  "synced_at": 1743724800,
  "profile": { ... }
}
```

---

## Charts

Last.fm-sourced charts with automatic library matching. Charts are rebuilt periodically (default: every 24h) or on demand.

### `GET /v1/charts` — List available charts

```bash
curl http://localhost:8000/v1/charts \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK`

```json
{
  "charts": [
    {
      "chart_type": "top_tracks",
      "scope": "global",
      "entries": 100,
      "fetched_at": 1743724800
    },
    {
      "chart_type": "top_artists",
      "scope": "tag:rock",
      "entries": 50,
      "fetched_at": 1743724800
    }
  ]
}
```

---

### `GET /v1/charts/{chart_type}` — Get chart entries

Returns chart entries for the given type. Each entry includes an `image_url` from Last.fm and, for library-matched entries, a `cover_url` pointing to your media server's cover art.

```bash
curl "http://localhost:8000/v1/charts/top_tracks?scope=global&limit=50" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Path parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `chart_type` | string | `top_tracks` or `top_artists` |

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `scope` | string | `global` | Chart scope: `global`, `tag:<name>`, `geo:<country>` |
| `limit` | int | 100 | 1–200 |
| `offset` | int | 0 | — |

**Response** `200 OK` — `top_tracks` example:

```json
{
  "chart_type": "top_tracks",
  "scope": "global",
  "total": 100,
  "fetched_at": 1743724800,
  "entries": [
    {
      "position": 0,
      "track_title": "Creep",
      "artist_name": "Radiohead",
      "playcount": 123456789,
      "listeners": 2345678,
      "in_library": true,
      "matched_track_id": "nav-uuid-042",
      "image_url": "https://lastfm.freetls.fastly.net/i/u/300x300/...",
      "library": {
        "track_id": "nav-uuid-042",
        "title": "Creep",
        "artist": "Radiohead",
        "album": "Pablo Honey",
        "genre": "Alternative Rock",
        "bpm": 92.4,
        "energy": 0.61,
        "duration": 238.0,
        "cover_url": "http://navidrome:4533/rest/getCoverArt.view?id=nav-uuid-042&size=300&u=admin&t=...&s=...&v=1.16.1&c=grooveiq"
      }
    },
    {
      "position": 1,
      "track_title": "Bohemian Rhapsody",
      "artist_name": "Queen",
      "playcount": 98765432,
      "listeners": 1876543,
      "in_library": false,
      "matched_track_id": null,
      "image_url": "https://lastfm.freetls.fastly.net/i/u/300x300/...",
      "lidarr_status": "downloading"
    }
  ]
}
```

**Response** `200 OK` — `top_artists` example:

```json
{
  "chart_type": "top_artists",
  "scope": "tag:electronic",
  "total": 50,
  "fetched_at": 1743724800,
  "entries": [
    {
      "position": 0,
      "artist_name": "Daft Punk",
      "playcount": 234567890,
      "listeners": 5678901,
      "in_library": true,
      "matched_track_id": "nav-uuid-007",
      "image_url": "https://lastfm.freetls.fastly.net/i/u/300x300/...",
      "library_track_count": 42,
      "library": {
        "track_id": "nav-uuid-007",
        "title": "Around the World",
        "artist": "Daft Punk",
        "album": "Homework",
        "genre": "Electronic",
        "bpm": 121.3,
        "energy": 0.85,
        "duration": 428.0,
        "cover_url": "http://navidrome:4533/rest/getCoverArt.view?id=nav-uuid-007&size=300&..."
      }
    }
  ]
}
```

#### Entry fields

**Common fields** (both `top_tracks` and `top_artists`):

| Field | Type | Description |
|-------|------|-------------|
| `position` | int | 0-based chart position |
| `artist_name` | string | Artist name from Last.fm |
| `playcount` | int | Total plays on Last.fm |
| `listeners` | int | Unique listeners on Last.fm |
| `in_library` | bool | Whether the track/artist exists in your library |
| `matched_track_id` | string\|null | Library track ID if matched |
| `image_url` | string\|null | Last.fm image URL (300x300 preferred, may be null for older entries) |

**`top_tracks` only:**

| Field | Type | Description |
|-------|------|-------------|
| `track_title` | string | Track title from Last.fm |

**`top_artists` only:**

| Field | Type | Description |
|-------|------|-------------|
| `library_track_count` | int | Number of tracks by this artist in your library |

**When `in_library` is false:**

| Field | Type | Description |
|-------|------|-------------|
| `lidarr_status` | string\|null | `downloading`, `in_lidarr`, `pending`, `failed`, or `null` |

**When matched (`library` object):**

| Field | Type | Description |
|-------|------|-------------|
| `library.track_id` | string | Local library track ID |
| `library.title` | string | Title from library metadata |
| `library.artist` | string | Artist from library metadata |
| `library.album` | string | Album from library metadata |
| `library.genre` | string | Genre tags |
| `library.bpm` | float | BPM |
| `library.energy` | float | Energy (0.0–1.0) |
| `library.duration` | float | Duration in seconds |
| `library.cover_url` | string\|null | Media server cover art URL (Navidrome/Plex). Null if no media server configured or track has no external ID |

#### Image URL priority for frontends

Use `library.cover_url` when available (local, fast, album-specific). Fall back to `image_url` (Last.fm, external CDN, artist/track image).

**Error** `400` if `chart_type` is not `top_tracks` or `top_artists`.
**Error** `404` if no chart data exists for the given scope (includes available scopes in the error message).

---

### `POST /v1/charts/build` — Trigger chart rebuild

Fetches fresh charts from Last.fm, matches to library, and optionally sends missing artists to Lidarr or tracks to spotdl-api/Spotizerr for download. Requires admin API key.

```bash
curl -X POST http://localhost:8000/v1/charts/build \
  -H "Authorization: Bearer YOUR_ADMIN_API_KEY"
```

**Response** `200 OK`

```json
{
  "status": "completed",
  "result": {
    "status": "completed",
    "charts_built": 5,
    "total_entries": 350,
    "library_matches": 87,
    "artists_sent_to_lidarr": 12,
    "tracks_sent_to_download": 45,
    "errors": 0
  }
}
```

---

### `POST /v1/charts/download` — Download a chart track

Trigger download of a specific chart track via spotdl-api (primary) or Spotizerr (legacy fallback). Provide either a chart `position` or `artist_name` + `track_title`.

Requires `SPOTDL_API_URL` or `SPOTIZERR_URL` to be configured.

```bash
curl -X POST http://localhost:8000/v1/charts/download \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"chart_type": "top_tracks", "scope": "global", "position": 0}'
```

Or by artist + title:

```bash
curl -X POST http://localhost:8000/v1/charts/download \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"artist_name": "Radiohead", "track_title": "Creep"}'
```

#### Request body

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `chart_type` | string | `top_tracks` | Chart type |
| `scope` | string | `global` | Chart scope |
| `position` | int\|null | — | Chart position (0-based) |
| `artist_name` | string\|null | — | Artist name (alternative to position) |
| `track_title` | string\|null | — | Track title (required with artist_name) |

Must provide either `position` or both `artist_name` + `track_title`.

**Response** `200 OK`

```json
{
  "status": "downloading",
  "task_id": "abc123-def456",
  "artist_name": "PinkPantheress",
  "track_title": "Stateside + Zara Larsson",
  "spotify_id": "4iV5W9uYEdYUVa79Axb7Rh",
  "matched_artist": "PinkPantheress",
  "matched_title": "Stateside (feat. Zara Larsson)"
}
```

**Error** `503` if no download backend configured (neither spotdl-api nor Spotizerr).
**Error** `404` if chart entry not found or no Spotify match.
**Error** `422` if neither position nor artist+title provided.

---

### `GET /v1/charts/stats` — Chart statistics

```bash
curl http://localhost:8000/v1/charts/stats \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK`

```json
{
  "total_entries": 350,
  "library_matches": 87,
  "match_rate": 0.249,
  "chart_count": 5,
  "last_fetched_at": 1743724800
}
```

---

## Downloads

Download proxy. GrooveIQ acts as a gateway — frontend apps search and download tracks through GrooveIQ without needing direct access to the download backend. Uses spotdl-api (primary) or Spotizerr (legacy fallback). The factory function `get_download_client()` in `app/services/spotdl.py` selects the backend automatically. Download history is persisted in the database.

Requires `SPOTDL_API_URL` (preferred) or `SPOTIZERR_URL` to be configured. Returns `503` if neither is set.

### `GET /v1/downloads/search` — Search for tracks

Searches for tracks via the configured download backend (spotdl-api or Spotizerr). Returns results so the user can pick which track to download.

```bash
curl "http://localhost:8000/v1/downloads/search?q=Radiohead+Creep&limit=5" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `q` | string | (required) | Search query |
| `limit` | int | 10 | 1–50 |

**Response** `200 OK`

```json
{
  "query": "Radiohead Creep",
  "limit": 5,
  "results": [
    {
      "id": "70LcF31zb1H0PyJoS1Sx3y",
      "name": "Creep",
      "artists": [{"name": "Radiohead", "id": "4Z8W4fKeB5YxbusRsdQVPb"}],
      "album": {"name": "Pablo Honey", "images": [{"url": "https://..."}]},
      "duration_ms": 238640
    }
  ]
}
```

---

### `POST /v1/downloads` — Download a track

Trigger download of a specific track by Spotify ID. The user should first search via `GET /v1/downloads/search` and select a result. The download is persisted in the database.

```bash
curl -X POST http://localhost:8000/v1/downloads \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "spotify_id": "70LcF31zb1H0PyJoS1Sx3y",
    "track_title": "Creep",
    "artist_name": "Radiohead",
    "album_name": "Pablo Honey",
    "cover_url": "https://i.scdn.co/image/..."
  }'
```

#### Request body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `spotify_id` | string | yes | Spotify track ID (from search results) |
| `track_title` | string | no | Track title (stored in download history) |
| `artist_name` | string | no | Artist name (stored in download history) |
| `album_name` | string | no | Album name (stored in download history) |
| `cover_url` | string | no | Cover art URL (stored in download history) |

**Response** `200 OK`

```json
{
  "id": 1,
  "spotify_id": "70LcF31zb1H0PyJoS1Sx3y",
  "task_id": "abc123-def456",
  "status": "downloading",
  "track_title": "Creep",
  "artist_name": "Radiohead",
  "album_name": "Pablo Honey",
  "cover_url": "https://i.scdn.co/image/...",
  "error_message": null,
  "created_at": 1743811200,
  "updated_at": 1743811200
}
```

Status values: `pending`, `downloading`, `duplicate`, `completed`, `error`.

**Error** `503` if no download backend configured.
**Error** `502` if download backend request failed.

---

### `GET /v1/downloads/status/{task_id}` — Check download progress

Proxies download backend task progress. Also opportunistically updates the DB record if one exists for this task.

```bash
curl http://localhost:8000/v1/downloads/status/abc123-def456 \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK`

```json
{
  "task_id": "abc123-def456",
  "status": "downloading",
  "progress": 0.45,
  "details": {
    "status": "downloading",
    "progress": 0.45,
    "track": "Creep",
    "artist": "Radiohead"
  }
}
```

The `details` field contains the raw status payload from the download backend (format varies by backend and version).

---

### `GET /v1/downloads` — List download history

Returns persisted download requests, newest first.

```bash
# All downloads
curl "http://localhost:8000/v1/downloads?limit=20" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Filter by status
curl "http://localhost:8000/v1/downloads?status=downloading" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `status` | string | — | Filter by status: `pending`, `downloading`, `duplicate`, `completed`, `error` |
| `limit` | int | 50 | 1–200 |
| `offset` | int | 0 | — |

**Response** `200 OK`

```json
{
  "total": 42,
  "downloads": [
    {
      "id": 1,
      "spotify_id": "70LcF31zb1H0PyJoS1Sx3y",
      "task_id": "abc123-def456",
      "status": "downloading",
      "track_title": "Creep",
      "artist_name": "Radiohead",
      "album_name": "Pablo Honey",
      "cover_url": "https://i.scdn.co/image/...",
      "error_message": null,
      "created_at": 1743811200,
      "updated_at": 1743811200
    }
  ]
}
```

---

## Artists

### `GET /v1/artists/{name}/meta` — Get artist metadata

Returns rich artist metadata from Last.fm combined with local library info. Includes bio, tags, similar artists, top tracks, and an image URL.

```bash
curl "http://localhost:8000/v1/artists/Radiohead/meta" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK`

```json
{
  "name": "Radiohead",
  "mbid": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
  "image_url": "https://lastfm.freetls.fastly.net/i/u/300x300/...",
  "bio": "Radiohead are an English rock band from Abingdon, Oxfordshire...",
  "bio_full": "...",
  "tags": ["alternative", "rock", "alternative rock", "indie", "electronic"],
  "similar_artists": [
    {"name": "Thom Yorke", "match": 1.0},
    {"name": "Atoms for Peace", "match": 0.85}
  ],
  "top_tracks": [
    {"name": "Creep", "playcount": 123456789, "listeners": 2345678},
    {"name": "Karma Police", "playcount": 98765432, "listeners": 1876543}
  ],
  "stats": {
    "playcount": 500000000,
    "listeners": 6000000
  },
  "library": {
    "in_library": true,
    "track_count": 85,
    "albums": ["OK Computer", "Kid A", "In Rainbows"]
  }
}
```

**Error** `404` if artist not found on Last.fm.
**Error** `503` if `LASTFM_API_KEY` is not configured.

---

## Stats & Pipeline Control

### `GET /v1/stats` — Dashboard aggregate stats

```bash
curl http://localhost:8000/v1/stats \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK`

```json
{
  "total_events": 15230,
  "total_users": 3,
  "total_tracks_analyzed": 1842,
  "total_playlists": 5,
  "events_last_24h": 423,
  "events_last_1h": 28,
  "event_types_24h": {
    "play_end": 180,
    "play_start": 175,
    "skip": 32,
    "like": 15,
    "pause": 12,
    "resume": 9
  },
  "top_tracks_24h": [
    {"track_id": "nav-uuid-001", "events": 8},
    {"track_id": "nav-uuid-042", "events": 6}
  ],
  "latest_scan": {
    "scan_id": 3,
    "status": "completed",
    "files_found": 1842,
    "files_analyzed": 1842,
    "files_skipped": 0,
    "files_failed": 2,
    "percent_complete": 100.0,
    "elapsed_seconds": 3600,
    "eta_seconds": null,
    "rate_per_sec": 0.51,
    "current_file": null,
    "started_at": 1743552000,
    "ended_at": 1743555600
  }
}
```

---

### `POST /v1/pipeline/run` — Trigger recommendation pipeline

Manually trigger the full recommendation pipeline (sessionizer -> track scoring -> taste profiles -> collaborative filtering -> ranker training). Returns immediately; pipeline runs in the background. Will not start a second run if one is already in progress.

```bash
curl -X POST http://localhost:8000/v1/pipeline/run \
  -H "Authorization: Bearer YOUR_API_KEY"
```

---

### `POST /v1/pipeline/reset` — Reset and rebuild pipeline

Reset all pipeline state (sessions, interactions, taste profiles) and rebuild from raw events. Use this after major data changes.

```bash
curl -X POST http://localhost:8000/v1/pipeline/reset \
  -H "Authorization: Bearer YOUR_API_KEY"
```

---

### `GET /v1/pipeline/status` — Pipeline run history and current state

Returns the currently running pipeline (if any) and the last N completed runs, each with per-step timing, status, metrics, and errors.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 10 | Number of historical runs to return (1–50) |

```bash
curl http://localhost:8000/v1/pipeline/status?limit=5 \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response:**

```json
{
  "current": null,
  "history": [
    {
      "run_id": "a1b2c3d4e5f6",
      "started_at": 1743600000.0,
      "ended_at": 1743600045.2,
      "status": "completed",
      "duration_ms": 45200,
      "trigger": "scheduled",
      "steps": [
        {
          "name": "sessionizer",
          "status": "completed",
          "started_at": 1743600000.1,
          "ended_at": 1743600002.3,
          "duration_ms": 2200,
          "error": null,
          "metrics": {"events_processed": 150, "sessions_created": 12}
        },
        {
          "name": "track_scoring",
          "status": "completed",
          "started_at": 1743600002.4,
          "ended_at": 1743600005.1,
          "duration_ms": 2700,
          "error": null,
          "metrics": {"interactions_created": 85, "interactions_updated": 23}
        }
      ]
    }
  ]
}
```

Step names (in execution order): `sessionizer`, `track_scoring`, `taste_profiles`, `collab_filter`, `ranker`, `session_embeddings`, `lastfm_cache`, `sasrec`, `session_gru`.

Step status values: `pending`, `running`, `completed`, `failed`, `skipped`.

Run status values: `running`, `completed`, `failed` (failed if any step failed).

---

### `GET /v1/pipeline/stream` — SSE stream of pipeline events

Server-Sent Events stream that emits real-time pipeline step events. Connect before triggering a pipeline run to watch it execute live.

```bash
curl -N http://localhost:8000/v1/pipeline/stream \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Event types:**

| Event | Data fields | Description |
|-------|-------------|-------------|
| `pipeline_start` | `run_id`, `trigger`, `timestamp` | Pipeline run has begun |
| `step_start` | `run_id`, `step`, `timestamp` | A step is starting |
| `step_complete` | `run_id`, `step`, `duration_ms`, `metrics`, `timestamp` | A step finished successfully |
| `step_failed` | `run_id`, `step`, `duration_ms`, `error`, `timestamp` | A step failed with error |
| `pipeline_end` | `run_id`, `status`, `duration_ms`, `timestamp` | Pipeline run is complete |

**Example event stream:**

```
: connected

event: pipeline_start
data: {"run_id": "a1b2c3d4e5f6", "trigger": "manual", "timestamp": 1743600000.0}

event: step_start
data: {"run_id": "a1b2c3d4e5f6", "step": "sessionizer", "timestamp": 1743600000.1}

event: step_complete
data: {"run_id": "a1b2c3d4e5f6", "step": "sessionizer", "duration_ms": 2200, "metrics": {"sessions_created": 12}, "timestamp": 1743600002.3}

event: step_failed
data: {"run_id": "a1b2c3d4e5f6", "step": "lastfm_cache", "duration_ms": 150, "error": "Traceback ...", "timestamp": 1743600030.5}

event: pipeline_end
data: {"run_id": "a1b2c3d4e5f6", "status": "failed", "duration_ms": 45200, "timestamp": 1743600045.2}
```

Sends a keepalive comment (`: keepalive`) every 30s to prevent proxy/browser timeouts.

---

### `GET /v1/pipeline/models` — ML model readiness status

Returns the training status and key metrics for every ML model subsystem in the pipeline.

```bash
curl http://localhost:8000/v1/pipeline/models \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response:**

```json
{
  "ranker": {
    "trained": true,
    "training_samples": 1200,
    "n_features": 39,
    "model_version": "lgbm-20260406-120000",
    "engine": "lgbm",
    "trained_at": 1743600005,
    "saved_path": "/data/models/ranker_lgbm.pkl",
    "feature_importances": {
      "satisfaction_score": 450,
      "play_count": 320,
      "energy": 210,
      "...": "..."
    }
  },
  "collab_filter": {
    "trained": true,
    "users": 3,
    "tracks": 450
  },
  "session_embeddings": {
    "trained": true,
    "vocab_size": 380
  },
  "sasrec": {
    "trained": true,
    "vocab_size": 380
  },
  "session_gru": {
    "trained": false
  },
  "lastfm_cache": {
    "built": true,
    "seeds_cached": 50,
    "cache_age_seconds": 3600
  }
}
```

### `GET /v1/pipeline/stats/sessionizer` — Sessionizer statistics

Aggregate stats about materialised listening sessions.

```bash
curl http://localhost:8000/v1/pipeline/stats/sessionizer \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response:**

```json
{
  "total_sessions": 142,
  "avg_duration_s": 1823.5,
  "avg_tracks_per_session": 8.3,
  "avg_skip_rate": 0.187,
  "skip_rate_distribution": {
    "0-10%": 45,
    "10-25%": 38,
    "25-50%": 32,
    "50%+": 27
  },
  "sessions_per_user": [
    {"user_id": "alice", "sessions": 85},
    {"user_id": "bob", "sessions": 57}
  ]
}
```

### `GET /v1/pipeline/stats/scoring` — Track scoring statistics

Score distribution, top/bottom tracks, and signal counts from track interactions.

```bash
curl http://localhost:8000/v1/pipeline/stats/scoring \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response:**

```json
{
  "total_interactions": 1200,
  "score_distribution": [
    {"range": "0.0-0.1", "count": 42},
    {"range": "0.1-0.2", "count": 85},
    "..."
  ],
  "top_tracks": [
    {"track_id": "abc123", "score": 0.98, "plays": 15, "title": "Song", "artist": "Artist"}
  ],
  "bottom_tracks": [
    {"track_id": "xyz789", "score": 0.02, "plays": 1, "title": "Track", "artist": "Artist"}
  ],
  "signal_counts": {
    "likes": 120,
    "dislikes": 15,
    "repeats": 45,
    "early_skips": 230,
    "full_listens": 890,
    "playlist_adds": 67
  }
}
```

### `GET /v1/pipeline/stats/taste_profiles` — Taste profile statistics

How many users have computed taste profiles.

```bash
curl http://localhost:8000/v1/pipeline/stats/taste_profiles \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response:**

```json
{
  "total_users": 5,
  "users_with_profiles": 3
}
```

### `GET /v1/pipeline/stats/events` — Event ingest rate (15-min buckets)

Event counts over the last 24 hours in 15-minute buckets. Useful for sparkline visualisation.

```bash
curl http://localhost:8000/v1/pipeline/stats/events \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response:**

```json
{
  "bucket_size_seconds": 900,
  "buckets": [
    {"timestamp": 1743513600, "count": 12},
    {"timestamp": 1743514500, "count": 8},
    "..."
  ]
}
```

### `GET /v1/pipeline/stats/activity` — Listening activity timeline

Hourly event counts grouped by event type over a configurable window.

```bash
curl "http://localhost:8000/v1/pipeline/stats/activity?days=7" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int 1-30 | 7 | Number of days of history |

**Response:**

```json
{
  "bucket_size_seconds": 3600,
  "days": 7,
  "buckets": [
    {"timestamp": 1743513600, "play_start": 5, "play_end": 4, "skip": 1},
    {"timestamp": 1743517200, "play_start": 12, "play_end": 11, "like": 2},
    "..."
  ]
}
```

### `GET /v1/pipeline/stats/engagement` — User engagement leaderboard

Per-user engagement metrics over the last 30 days, ranked by total events.

```bash
curl http://localhost:8000/v1/pipeline/stats/engagement \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response:**

```json
{
  "users": [
    {
      "user_id": "alice",
      "total_events": 1250,
      "plays": 480,
      "skip_rate": 0.142,
      "unique_tracks": 320,
      "diversity": 0.256,
      "last_active": 1743600000
    }
  ]
}
```

---

## Configuration Reference

All settings via environment variables or `.env` file.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | (auto) | Random secret for internal signing. **Set in production.** |
| `API_KEYS` | — | Comma-separated bearer tokens for clients |
| `APP_ENV` | `production` | `development` or `production` |
| `ENABLE_DOCS` | `false` | Set `true` to enable `/docs` and `/redoc` |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./grooveiq.db` | SQLite or `postgresql+asyncpg://...` |
| `DB_POOL_SIZE` | 5 | Connection pool size |
| `DB_MAX_OVERFLOW` | 10 | Max overflow connections |

### Security

| Variable | Default | Description |
|----------|---------|-------------|
| `RATE_LIMIT_EVENTS` | 300 | Max event requests per minute per key |
| `RATE_LIMIT_DEFAULT` | 200 | Max other requests per minute per key |
| `ALLOWED_HOSTS` | `*` | Comma-separated allowed hosts |
| `CORS_ORIGINS` | `*` | Comma-separated CORS origins |

### Audio Analysis

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_LIBRARY_PATH` | `/music` | Path to music library (read-only) |
| `ANALYSIS_WORKERS` | 2 | Parallel Essentia workers |
| `ANALYSIS_BATCH_SIZE` | 10 | Tracks per job batch |
| `ANALYSIS_TIMEOUT` | 120 | Seconds per file timeout |
| `RESCAN_INTERVAL_HOURS` | 6 | Auto-rescan interval |
| `AUDIO_EXTENSIONS` | `.mp3,.flac,.ogg,.m4a,.wav,.aac,.opus,.wv` | File types to analyze |

### Recommendation Pipeline

| Variable | Default | Description |
|----------|---------|-------------|
| `SESSION_GAP_MINUTES` | 30 | Inactivity gap that splits sessions |
| `SESSION_MIN_EVENTS` | 2 | Drop sessions with fewer events |
| `TASTE_PROFILE_DECAY_DAYS` | 30 | Half-life for recency weighting |
| `SCORING_INTERVAL_HOURS` | 1 | How often the pipeline runs |

### Event Ingestion

| Variable | Default | Description |
|----------|---------|-------------|
| `EVENT_BATCH_MAX` | 50 | Max events per batch request |
| `EVENT_RETENTION_DAYS` | 365 | Auto-delete events older than this |
| `MIN_PLAY_PERCENTAGE` | 0.05 | Drop `play_end` events below this completion |

### Media Server Integration

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDIA_SERVER_TYPE` | — | `navidrome` or `plex` |
| `MEDIA_SERVER_URL` | — | e.g. `http://navidrome:4533` |
| `MEDIA_SERVER_USER` | — | Navidrome username |
| `MEDIA_SERVER_PASSWORD` | — | Navidrome password |
| `MEDIA_SERVER_TOKEN` | — | Plex `X-Plex-Token` |
| `MEDIA_SERVER_LIBRARY_ID` | `1` | Plex library section ID |
| `MEDIA_SERVER_MUSIC_PATH` | — | Media server's music root (if different from `MUSIC_LIBRARY_PATH`) |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_JSON` | `true` | `true` for structured JSON, `false` for human-readable |
