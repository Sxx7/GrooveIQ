# GrooveIQ API Reference

All endpoints require `Authorization: Bearer <api_key>` unless noted otherwise.

Interactive docs available at `/docs` when `ENABLE_DOCS=true` (development only).

---

## Health & Dashboard

### `GET /health`

Health check. **No auth required.**

**Response** `200`:
```json
{
  "status": "ok",
  "service": "grooveiq"
}
```

### `GET /dashboard`

Web dashboard UI. **No auth required.** Redirected to from `GET /`.

---

## Events

### `POST /v1/events`

Ingest a single listen event.

**Request body** (`EventCreate`):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | Yes | Media server's user identifier |
| `track_id` | string | Yes | Media server's track identifier |
| `event_type` | string | Yes | One of: `play_start`, `play_end`, `skip`, `pause`, `resume`, `like`, `dislike`, `rating`, `playlist_add`, `playlist_remove`, `queue_add`, `seek_back`, `seek_forward`, `repeat`, `volume_up`, `volume_down`, `reco_impression` |
| `timestamp` | int | No | Unix epoch UTC (default: server time). Rejected if >24h old or >5min future |
| `value` | float | No | Event-specific payload (completion ratio, elapsed seconds, rating, volume) |
| `context` | string | No | Free-text context label, e.g. `"workout"`, `"sleep"` |
| `client_id` | string | No | Which app/integration sent this event |
| `session_id` | string | No | Client-assigned session identifier |
| `surface` | string | No | UI surface: `home`, `search`, `now_playing`, `playlist_view` |
| `position` | int | No | Rank position in recommendation list |
| `request_id` | string | No | Shared ID tying an impression to downstream streams/actions |
| `model_version` | string | No | Recommendation model version that produced this impression |
| `session_position` | int | No | Track's ordinal position in the session (0-based) |
| `dwell_ms` | int | No | Milliseconds listened |
| `pause_duration_ms` | int | No | Inter-track pause in ms |
| `num_seekfwd` | int | No | Forward seek count |
| `num_seekbk` | int | No | Backward seek count |
| `shuffle` | bool | No | Whether shuffle mode was active |
| `context_type` | string | No | Source: `playlist`, `album`, `radio`, `search`, `home_shelf` |
| `context_id` | string | No | ID of the source (playlist ID, album ID, radio session ID, etc.) |
| `context_switch` | bool | No | True if user just switched to a new context |
| `reason_start` | string | No | `autoplay`, `user_tap`, `forward_button`, `external` |
| `reason_end` | string | No | `track_done`, `user_skip`, `error`, `new_track` |
| `device_id` | string | No | Stable device identifier |
| `device_type` | string | No | `mobile`, `desktop`, `speaker`, `car`, `web` |
| `hour_of_day` | int (0-23) | No | Client's local hour |
| `day_of_week` | int (1-7) | No | ISO 8601: 1=Monday ... 7=Sunday |
| `timezone` | string | No | IANA timezone, e.g. `Europe/Zurich` |
| `output_type` | string | No | `headphones`, `speaker`, `bluetooth_speaker`, `car_audio`, `built_in`, `airplay` |
| `output_device_name` | string | No | Friendly name, e.g. `AirPods Pro` |
| `bluetooth_connected` | bool | No | Whether audio is routed over Bluetooth |
| `latitude` | float | No | GPS latitude (-90 to 90) |
| `longitude` | float | No | GPS longitude (-180 to 180) |
| `location_label` | string | No | Semantic label: `home`, `work`, `gym`, `commute` |

**Response** `202`:
```json
{
  "accepted": 1,
  "rejected": 0,
  "errors": []
}
```

### `POST /v1/events/batch`

Ingest up to 50 events at once.

**Request body** (`EventBatch`):
```json
{
  "events": [ { /* EventCreate */ }, ... ]
}
```

**Response** `202`: Same format as single event.

### `GET /v1/events`

Query stored events (admin/debug).

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | — | Filter by user |
| `track_id` | string | — | Filter by track |
| `event_type` | string | — | Filter by event type |
| `device_id` | string | — | Filter by device |
| `context_type` | string | — | Filter by context type |
| `request_id` | string | — | Filter by request ID |
| `limit` | int | 50 | Max results (1-500) |
| `offset` | int | 0 | Pagination offset |

**Response** `200`: Array of event objects.

---

## Library & Tracks

### `POST /v1/library/scan`

Trigger an async library scan. Admin only.

**Response** `202`:
```json
{
  "message": "Scan started",
  "scan_id": 1,
  "status": "running"
}
```

### `GET /v1/library/scan/{scan_id}`

Poll scan progress. Admin only.

**Response** `200`: Scan status object with progress percentage, files processed/total, errors.

### `GET /v1/library/scan/{scan_id}/logs`

Get scan log entries. Admin only.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 50 | Max entries (1-200) |
| `after_id` | int | 0 | Return logs after this ID |

**Response** `200`: Array of scan log entries.

### `POST /v1/library/sync`

Sync track IDs with media server (Navidrome/Plex). Cascades track_id updates across all tables. Admin only.

**Response** `200`:
```json
{
  "server_type": "navidrome",
  "tracks_fetched": 5000,
  "tracks_matched": 4800,
  "tracks_updated": 120
}
```

### `GET /v1/tracks`

List analyzed tracks with filtering and sorting.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 50 | Max results (1-200) |
| `offset` | int | 0 | Pagination offset |
| `sort_by` | string | `bpm` | Sort field: `bpm`, `energy`, `danceability`, `valence`, `key`, `duration`, `analyzed_at`, `analysis_version` |
| `sort_dir` | string | `asc` | Sort direction: `asc`, `desc` |
| `search` | string | — | Search title/artist/album/genre/track_id |
| `min_bpm` | float | — | Minimum BPM |
| `max_bpm` | float | — | Maximum BPM |
| `min_energy` | float | — | Minimum energy |
| `max_energy` | float | — | Maximum energy |
| `key` | string | — | Filter by musical key |
| `mode` | string | — | Filter by mode (major/minor) |
| `mood` | string | — | Filter by mood tag |

**Response** `200`:
```json
{
  "total": 5000,
  "tracks": [ { /* TrackFeatures */ } ]
}
```

### `GET /v1/tracks/{track_id}/features`

Get audio features for a specific track.

**Response** `200`: Track features object with BPM, key, energy, mood, danceability, embedding, etc.

### `GET /v1/tracks/{track_id}/similar`

Get acoustically similar tracks via FAISS + SQL fallback.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 10 | Max results (1-50) |
| `include_features` | bool | false | Include full audio features per result |

**Response** `200`: Array of similar track objects with similarity scores.

---

## Users

### `GET /v1/users`

List all users. Admin only.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 100 | Max results (1-500) |
| `offset` | int | 0 | Pagination offset |

**Response** `200`: Array of user objects with event counts.

### `POST /v1/users`

Register a new user.

**Request body**:
```json
{
  "user_id": "alice",
  "display_name": "Alice"
}
```

**Response** `201`: User object.

### `GET /v1/users/{user_id}`

Get a user by ID.

**Response** `200`: User object.

### `PATCH /v1/users/{uid}`

Update a user (rename, change display name). Cascades user_id changes to all related tables.

**Request body**:
```json
{
  "user_id": "new_id",
  "display_name": "New Name"
}
```

**Response** `200`: Updated user object.

### `GET /v1/users/{user_id}/profile`

Get user taste profile (audio preferences, mood, behaviour, timescale sub-profiles, patterns).

**Response** `200`: User object with `taste_profile` JSON and Last.fm data if linked.

### `GET /v1/users/{user_id}/interactions`

Get per-track interaction scores.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `sort_by` | string | `satisfaction_score` | Sort field: `satisfaction_score`, `play_count`, `last_played_at`, `skip_count` |
| `sort_dir` | string | `desc` | `asc` or `desc` |
| `limit` | int | 50 | Max results (1-200) |
| `offset` | int | 0 | Pagination offset |

**Response** `200`:
```json
{
  "total": 250,
  "interactions": [ { /* satisfaction scores, play/skip/like counts, completion %, audio features */ } ]
}
```

### `GET /v1/users/{user_id}/history`

Get listening history (paginated).

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 50 | Max results (1-200) |
| `offset` | int | 0 | Pagination offset |

**Response** `200`:
```json
{
  "total": 1200,
  "history": [ { /* track, device_type, output_type, completion %, dwell_ms, reason_end */ } ]
}
```

### `GET /v1/users/{user_id}/sessions`

Get materialized listening sessions.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 25 | Max results (1-100) |
| `offset` | int | 0 | Pagination offset |

**Response** `200`:
```json
{
  "total": 85,
  "sessions": [ { /* duration_s, track_count, play/skip/like counts, skip_rate, avg_completion */ } ]
}
```

---

## Onboarding

### `POST /v1/users/{user_id}/onboarding`

Submit onboarding preferences for cold-start taste profile seeding.

**Request body**:
```json
{
  "favourite_artists": ["Radiohead", "Bjork"],
  "favourite_genres": ["electronic", "indie"],
  "favourite_tracks": ["track_id_1"],
  "mood_preferences": ["relaxed", "happy"],
  "listening_contexts": ["home", "commute"],
  "device_types": ["mobile", "desktop"],
  "energy_preference": 0.6,
  "danceability_preference": 0.5
}
```

**Response** `200`:
```json
{
  "preferences_saved": 8,
  "matched_tracks": 12,
  "matched_artists": 2,
  "profile_seeded": true
}
```

### `GET /v1/users/{user_id}/onboarding`

Get stored onboarding preferences.

**Response** `200`: Onboarding preferences object.

---

## Recommendations

### `GET /v1/recommend/{user_id}`

Get ranked track recommendations. Candidates from 8 sources, scored by LightGBM, reranked for diversity.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `seed_track_id` | string | — | Bias recommendations toward a specific track |
| `limit` | int | 25 | Max results (1-100) |
| `device_type` | string | — | `mobile`, `desktop`, `speaker`, `car`, `web` |
| `output_type` | string | — | `headphones`, `speaker`, `bluetooth_speaker`, `car_audio`, `built_in`, `airplay` |
| `context_type` | string | — | `playlist`, `album`, `radio`, `search`, `home_shelf` |
| `location_label` | string | — | `home`, `work`, `gym`, `commute` |
| `hour_of_day` | int | — | Client's local hour (0-23) |
| `day_of_week` | int | — | ISO 8601: 1=Monday ... 7=Sunday |
| `genre` | string | — | Filter candidates by genre (case-insensitive substring) |
| `mood` | string | — | Filter by mood tag (e.g. `happy`, `sad`). Requires confidence > 0.3 |
| `debug` | bool | false | Include debug data (admin only) |

**Response** `200`:
```json
{
  "request_id": "uuid",
  "model_version": "lgbm_v3",
  "tracks": [
    {
      "track_id": "abc123",
      "position": 0,
      "score": 0.87,
      "source": "content",
      "title": "Song Title",
      "artist": "Artist Name"
    }
  ],
  "context": { "device_type": "mobile" }
}
```

When `debug=true`, the response includes an additional `debug` object with:
- `candidates_by_source` — candidates grouped by retrieval source
- `total_candidates` — count before deduplication
- `pre_rerank` — ranked list before reranker with scores
- `reranker_actions` — actions taken: `freshness_boost`, `skip_suppression`, `anti_repetition_exclude`, `short_track_exclude`, `exploration_slot`, `artist_diversity_demote`
- `feature_vectors` — per-candidate dict of all 39 feature values

### `GET /v1/recommend/{user_id}/history`

Get recommendation history with impression-to-stream tracking.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 50 | Max results (1-200) |
| `offset` | int | 0 | Pagination offset |

**Response** `200`:
```json
{
  "total": 500,
  "history": [ { /* track metadata, streamed status */ } ]
}
```

### `GET /v1/recommend/{user_id}/artists`

Get recommended artists from 3 sources: local listening history (satisfaction + recency weighted), Last.fm similar artists, and Last.fm top artists.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 20 | Max results (1-100) |

**Response** `200`:
```json
{
  "user_id": "alice",
  "total": 20,
  "artists": [
    {
      "name": "Radiohead",
      "score": 0.92,
      "source": "listening",
      "in_library": true,
      "play_count": 45,
      "like_count": 12,
      "image_url": "https://...",
      "top_tracks": [
        { "track_id": "abc", "title": "Everything In Its Right Place", "satisfaction_score": 0.95 }
      ],
      "audio_stats": { "avg_energy": 0.6, "avg_valence": 0.4 }
    }
  ]
}
```

### `GET /v1/stats/model`

Get recommendation model stats and offline evaluation metrics. Admin only.

**Response** `200`: Model training info, NDCG@k, skip rate, completion rate, impression-to-stream stats.

---

## Radio

Adaptive, stateful radio sessions inspired by YouTube Music's Radio. Sessions adapt in real-time based on skip/like/dislike feedback.

### `POST /v1/radio/start`

Start a radio session seeded from a track, artist, or playlist.

**Request body**:
```json
{
  "user_id": "alice",
  "seed_type": "track",
  "seed_value": "track_id_123",
  "count": 10,
  "device_type": "mobile",
  "output_type": "headphones",
  "location_label": "commute",
  "hour_of_day": 8,
  "day_of_week": 1
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | Yes | User ID |
| `seed_type` | string | Yes | `track`, `artist`, or `playlist` |
| `seed_value` | string | Yes | Track ID, artist name, or playlist ID |
| `count` | int | No | Tracks to return (default 10, max 50) |
| `device_type` | string | No | Device context |
| `output_type` | string | No | Audio output context |
| `location_label` | string | No | Location context |
| `hour_of_day` | int | No | Local hour (0-23) |
| `day_of_week` | int | No | ISO day (1-7) |

**Response** `201`:
```json
{
  "session_id": "uuid",
  "seed_type": "track",
  "seed_value": "track_id_123",
  "seed_display_name": "Song Title - Artist",
  "tracks": [ { /* track objects */ } ]
}
```

### `GET /v1/radio/{session_id}/next`

Fetch next batch of radio tracks. Context params are updatable per call.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `count` | int | 10 | Tracks to return (1-50) |
| `device_type` | string | — | Updated device context |
| `output_type` | string | — | Updated output context |
| `location_label` | string | — | Updated location |
| `hour_of_day` | int | — | Updated hour |
| `day_of_week` | int | — | Updated day |

**Response** `200`:
```json
{
  "session_id": "uuid",
  "total_served": 20,
  "tracks": [ { /* track objects */ } ]
}
```

### `DELETE /v1/radio/{session_id}`

Stop a radio session and remove it from memory.

**Response** `200`:
```json
{ "status": "stopped", "session_id": "uuid" }
```

### `GET /v1/radio`

List active radio sessions.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | — | Filter by user |

**Response** `200`:
```json
{
  "active_sessions": 3,
  "sessions": [ { /* session metadata */ } ]
}
```

**Radio feedback**: Send skip/like/dislike via `POST /v1/events` with `context_type=radio` and `context_id=<session_id>`. The radio session drift embedding updates immediately.

---

## Playlists

### `POST /v1/playlists`

Generate a new playlist.

**Request body**:
```json
{
  "name": "Morning Flow",
  "strategy": "flow",
  "seed_track_id": "track_123",
  "max_tracks": 30,
  "params": {}
}
```

Strategies: `flow` (BPM/energy chain), `mood` (mood tag + energy arc), `energy_curve` (shaped energy profile: ramp_up, cool_down, steady), `key_compatible` (Camelot wheel harmonic mixing).

**Response** `201`: Playlist object with tracks.

### `GET /v1/playlists`

List all playlists.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 20 | Max results (1-100) |
| `offset` | int | 0 | Pagination offset |
| `strategy` | string | — | Filter by strategy |

**Response** `200`: Array of playlist objects.

### `GET /v1/playlists/{playlist_id}`

Get playlist with track details.

**Response** `200`: Playlist object with tracks.

### `DELETE /v1/playlists/{playlist_id}`

Delete a playlist.

**Response** `204`: No content.

---

## Music Discovery

### `GET /v1/discovery`

List discovery requests.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | — | Filter by user |
| `status` | string | — | Filter by status |
| `limit` | int | 50 | Max results (1-200) |
| `offset` | int | 0 | Pagination offset |

**Response** `200`: Discovery requests with total count.

### `POST /v1/discovery/run`

Trigger discovery pipeline (Last.fm similar artists + optional AcousticBrainz). Admin only.

**Response** `200`: Status and result.

### `GET /v1/discovery/stats`

Discovery statistics.

**Response** `200`: Status counts, daily limits.

---

## Charts

Last.fm-sourced charts with library matching, cover art, and optional auto-download.

### `GET /v1/charts`

List available charts.

**Response** `200`: Array of chart objects (chart_type, scope, entry count, fetched_at).

### `GET /v1/charts/{chart_type}`

Get chart entries.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `scope` | string | `global` | Chart scope (e.g. `global`, genre tag, country name) |
| `limit` | int | 100 | Max results (1-200) |
| `offset` | int | 0 | Pagination offset |

`chart_type` options: `top_tracks`, `top_artists`.

**Response** `200`: Chart data with entries array including library match status and cover art URLs.

### `POST /v1/charts/build`

Trigger chart rebuild. Admin only.

**Response** `200`: Completion status and result.

### `POST /v1/charts/download`

Download a chart track via spotdl-api (or Spotizerr fallback).

**Request body**:
```json
{
  "chart_type": "top_tracks",
  "scope": "global",
  "position": 1
}
```

Or by name:
```json
{
  "artist_name": "Artist",
  "track_title": "Song"
}
```

**Response** `200`: Status, task_id, matched info.

### `GET /v1/charts/stats`

Chart statistics.

**Response** `200`: Total entries, library matches, match rate, last fetch time.

---

## Downloads

Search and download tracks via spotdl-api (primary) or Spotizerr (legacy fallback).

### `GET /v1/downloads/search`

Search for tracks.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `q` | string | Yes | Search query |
| `limit` | int | No | Max results (1-50, default 10) |

**Response** `200`: Array of track objects.

### `POST /v1/downloads`

Download a track.

**Request body**:
```json
{
  "spotify_id": "spotify:track:xxx",
  "track_title": "Song Title",
  "artist_name": "Artist",
  "album_name": "Album",
  "cover_url": "https://..."
}
```

**Response** `201`: Download response with task_id.

### `GET /v1/downloads/status/{task_id}`

Check download progress.

**Response** `200`: Task status, progress percentage, details.

### `GET /v1/downloads`

List download history.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `status` | string | — | Filter by status |
| `limit` | int | 50 | Max results (1-200) |
| `offset` | int | 0 | Pagination offset |

**Response** `200`: Download history with total count.

---

## Last.fm

### `POST /v1/users/{user_id}/lastfm/connect`

Connect Last.fm account.

**Request body**:
```json
{
  "lastfm_username": "user",
  "lastfm_password": "pass"
}
```

**Response** `200`: Connection status.

### `DELETE /v1/users/{user_id}/lastfm`

Disconnect Last.fm account.

**Response** `200`.

### `POST /v1/users/{user_id}/lastfm/sync`

Force-refresh Last.fm profile data (top artists, tracks, genres).

**Response** `200`: Last.fm profile data.

### `POST /v1/users/{user_id}/lastfm/backfill`

Backfill missed scrobbles.

**Response** `200`.

### `GET /v1/users/{user_id}/lastfm/profile`

Get Last.fm profile data.

**Response** `200`: Profile with top artists, tracks, genres.

---

## Artists

### `GET /v1/artists/{name}/meta`

Get rich artist metadata combining Last.fm data with local library info.

**Response** `200`: Bio, tags, similar artists, top tracks, images, library match status.

---

## Personalized News

Reddit-sourced music news feed personalized per user's taste profile. Posts are fetched on a schedule, cached in memory, and scored per-user at query time.

### `POST /v1/news/refresh`

Manually trigger a Reddit news cache refresh.

**Response** `202`:
```json
{ "status": "refresh_started" }
```

### `GET /v1/news/{user_id}`

Get personalized music news feed.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 25 | Max items (1-100) |
| `tag` | string | — | Filter by tag: `FRESH`, `NEWS`, `DISCUSSION` |
| `subreddit` | string | — | Filter to specific subreddit |

**Response** `200`:
```json
{
  "user_id": "alice",
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

---

## Pipeline Control & Observability

### `GET /v1/stats`

Aggregate stats for the dashboard. Admin only.

**Response** `200`: Total events, users, tracks, playlists, event type breakdown, top tracks, latest scan info, library coverage.

### `POST /v1/pipeline/run`

Trigger the recommendation pipeline manually. Admin only.

**Response** `200`.

### `POST /v1/pipeline/reset`

Reset and rebuild the recommendation pipeline from raw events. Admin only.

**Response** `200`.

### `GET /v1/pipeline/status`

Pipeline run history and current state. Admin only.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 10 | Max runs (1-50) |

**Response** `200`: Current run state and history with per-step timing, status, metrics, errors, and `config_version`.

### `GET /v1/pipeline/stream`

SSE stream of real-time pipeline step events. Admin only.

**Event types**: `pipeline_start`, `step_start`, `step_complete`, `step_failed`, `pipeline_end`.

### `GET /v1/pipeline/models`

Readiness status of all ML models (ranker, CF, embeddings, SASRec, GRU, Last.fm cache). Admin only.

**Response** `200`: Per-model trained/not-trained status, key stats, feature importances.

### `GET /v1/pipeline/stats/sessionizer`

Sessionizer aggregate stats — session counts, durations, skip rate distribution. Admin only.

### `GET /v1/pipeline/stats/scoring`

Track scoring stats — score distribution, signal breakdown, top/bottom tracks. Admin only.

### `GET /v1/pipeline/stats/taste_profiles`

Taste profile stats — users with profiles count. Admin only.

### `GET /v1/pipeline/stats/events`

Event ingest rate over last 24h in 15-minute buckets. Admin only.

### `GET /v1/pipeline/stats/activity`

Listening activity timeline grouped by event type. Admin only.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `days` | int | 7 | Lookback window (1-30) |

### `GET /v1/pipeline/stats/engagement`

Per-user engagement leaderboard (last 30 days). Admin only.

---

## Algorithm Configuration

Versioned, DB-persisted configuration for all 78 pipeline tunables. All admin only.

### `GET /v1/algorithm/config`

Get active algorithm configuration.

**Response** `200`: Config version, name, all 78 tunables grouped by section, timestamps.

### `GET /v1/algorithm/config/defaults`

Get default values with group metadata (for GUI rendering, includes ge/le constraints).

### `GET /v1/algorithm/config/history`

Config version history.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 20 | Max results (1-100) |
| `offset` | int | 0 | Pagination offset |

### `GET /v1/algorithm/config/{version}`

Get a specific historical config version.

### `PUT /v1/algorithm/config`

Update config (creates a new version, marks it active).

**Request body**:
```json
{
  "config": {
    "track_scoring": { "w_like": 2.5 },
    "reranker": { "freshness_boost": 0.15 }
  },
  "name": "Tuned for more exploration"
}
```

### `POST /v1/algorithm/config/reset`

Reset config to defaults (creates a new version).

### `POST /v1/algorithm/config/activate/{version}`

Activate a historical config version (rollback).

### `GET /v1/algorithm/config/export`

Export active config as a JSON file download.

### `POST /v1/algorithm/config/import`

Import a JSON config (validates, creates a new version).

**Request body**:
```json
{
  "config": { /* all 7 groups */ },
  "name": "Imported config"
}
```

---

## Integration Health

### `GET /v1/integrations/status`

Probe all external services in parallel. Admin only.

**Response** `200`:
```json
{
  "checked_at": 1712000000,
  "integrations": {
    "spotdl_api": { "configured": true, "connected": true, "version": "1.0", "details": {} },
    "lidarr": { "configured": true, "connected": true, "version": "2.0", "details": {} },
    "acousticbrainz_lookup": { "configured": false, "connected": false },
    "lastfm": { "configured": true, "connected": true },
    "media_server": { "configured": true, "connected": true, "version": "0.53.3", "type": "navidrome" }
  }
}
```
