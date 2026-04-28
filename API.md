# GrooveIQ API Reference

**Base URL:** `http://<host>:8000`
**Auth:** All endpoints except `/health` and `/dashboard` require `Authorization: Bearer <api_key>`.
**Content-Type:** `application/json`

> **Track-ID schema notice (target state — tracking [#37](https://github.com/Sxx7/GrooveIQ/issues/37)).** The Events, Tracks & Library, and Library/sync sections describe the **post-refactor** schema in which `track_id` is a stable internal GrooveIQ identifier (16-char hex) and per-backend IDs (`media_server_id`, `spotify_id`, `qobuz_id`, `tidal_id`, `deezer_id`, `soundcloud_id`, `mb_track_id`) live in their own columns. The new `GET /v1/tracks/lookup` endpoint and the per-backend ID columns will land together with the migration. Build the iOS app against this contract; `track_id` will not be a Navidrome ID.

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
14. [Download Routing Configuration](#download-routing-configuration)
15. [Artists](#artists)
16. [Pipeline & Stats](#pipeline--stats)
17. [Configuration Reference](#configuration-reference)

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

> **Track-ID model — read this first.** Every track has one stable internal GrooveIQ identifier (`track_id`, a 16-char hex hash) and zero or more per-backend external IDs (`media_server_id`, `spotify_id`, `qobuz_id`, `tidal_id`, `deezer_id`, `soundcloud_id`, `mb_track_id`). The events API requires the **internal `track_id`**. Clients that hold a media-server / streaming-service ID resolve it via [`GET /v1/tracks/lookup`](#get-v1trackslookup--resolve-an-external-id-to-the-internal-track_id) and cache the mapping locally. See the schema rework in [#37](https://github.com/Sxx7/GrooveIQ/issues/37) for background.

### `POST /v1/events` — Ingest a single event

```bash
curl -X POST http://localhost:8000/v1/events \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "simon",
    "track_id": "9f3a1b2c4d5e6f70",
    "event_type": "play_end",
    "value": 0.95
  }'
```

**Response** `202 Accepted`

```json
{"accepted": 1, "rejected": 0, "errors": []}
```

A `track_id` that does not match any analysed `TrackFeatures` row is reported in `errors` (the rest of the batch still ingests).

#### Required fields

| Field | Type | Description |
|-------|------|-------------|
| `user_id` | string (1-128) | Stable user identifier. Should match what `GET /v1/users` returns. |
| `track_id` | string (1-128) | **Internal GrooveIQ track ID** — 16-char hex hash. Resolve from a Navidrome / Spotify / Qobuz ID via `GET /v1/tracks/lookup`. |
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
    "track_id": "9f3a1b2c4d5e6f70",
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
      {"user_id": "simon", "track_id": "9f3a1b2c4d5e6f70", "event_type": "play_start"},
      {"user_id": "simon", "track_id": "9f3a1b2c4d5e6f70", "event_type": "play_end", "value": 1.0},
      {"user_id": "simon", "track_id": "8b75e6a53d9c8f17", "event_type": "skip", "value": 3.5}
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

Each row in the response carries the full set of identifiers (the internal `track_id` plus every per-backend external ID currently known for that track):

```json
{
  "tracks": [
    {
      "track_id": "9f3a1b2c4d5e6f70",
      "title": "Never Too Late",
      "artist": "Three Days Grace",
      "album": "One-X",
      "duration": 219.0,
      "bpm": 124.0,
      "energy": 0.81,
      "media_server_id": "iDkrnC4UrRVJ6HHEa83nc9",
      "spotify_id": null,
      "qobuz_id": null,
      "tidal_id": null,
      "deezer_id": null,
      "soundcloud_id": null,
      "mb_track_id": null
    }
  ],
  "total": 58445,
  "limit": 50,
  "offset": 0
}
```

For an iOS / web client that talks directly to Navidrome, the typical bootstrap is to page through `GET /v1/tracks` once and cache `media_server_id → track_id` locally. New tracks discovered later (e.g. from a Navidrome scan) are resolved on demand via [`GET /v1/tracks/lookup`](#get-v1trackslookup--resolve-an-external-id-to-the-internal-track_id).

---

### `GET /v1/tracks/lookup` — Resolve an external ID to the internal `track_id`

Looks up a `TrackFeatures` row by any of its external identifiers. Used by clients (iOS app, web dashboard, Last.fm scrobbler) that hold an ID from a specific backend and need to find the GrooveIQ internal `track_id` before posting events / requesting features / starting radio.

Pass exactly one of the lookup parameters.

| Parameter | Description |
|-----------|-------------|
| `media_server_id` | Navidrome song ID (e.g. `iDkrnC4UrRVJ6HHEa83nc9`) |
| `spotify_id` | Spotify track ID |
| `qobuz_id` | Qobuz track ID |
| `tidal_id` | Tidal track ID |
| `deezer_id` | Deezer track ID |
| `soundcloud_id` | SoundCloud track ID |
| `mb_track_id` | MusicBrainz Recording ID (UUID) |

**Example**

```bash
curl "http://localhost:8000/v1/tracks/lookup?media_server_id=iDkrnC4UrRVJ6HHEa83nc9" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK`

```json
{
  "track_id": "9f3a1b2c4d5e6f70",
  "title": "Never Too Late",
  "artist": "Three Days Grace",
  "album": "One-X",
  "duration": 219.0,
  "media_server_id": "iDkrnC4UrRVJ6HHEa83nc9",
  "spotify_id": null,
  "qobuz_id": null,
  "tidal_id": null,
  "deezer_id": null,
  "soundcloud_id": null,
  "mb_track_id": null
}
```

`404` if no track matches the given external ID. `400` if zero or multiple lookup parameters are supplied.

#### Recommended client pattern

Cache the `external_id → track_id` mapping locally on first encounter. The internal `track_id` is stable for the lifetime of the file (it's a hash of the relative file path); only re-fetch if the file moves on disk.

```swift
// iOS pseudocode
func grooveIQTrackId(forNavidromeId id: String) async throws -> String {
    if let cached = mapping[id] { return cached }
    let r = try await api.get("/v1/tracks/lookup", query: ["media_server_id": id])
    mapping[id] = r.track_id
    return r.track_id
}
```

Batch resolution (avoids one HTTP request per track) — `POST /v1/tracks/lookup`:

```json
{ "media_server_ids": ["iDkr...", "u3dW...", "MY6f..."] }
```

returns

```json
{
  "resolved": {
    "iDkr...": "9f3a1b2c4d5e6f70",
    "u3dW...": "8b75e6a53d9c8f17",
    "MY6f...": null
  }
}
```

`null` for IDs that don't match any track.

---

### `GET /v1/tracks/{track_id}/features` — Audio features

`{track_id}` here is the **internal GrooveIQ track ID**. Use [`GET /v1/tracks/lookup`](#get-v1trackslookup--resolve-an-external-id-to-the-internal-track_id) to resolve from a Navidrome / streaming-service ID first.

**Response** `200 OK`

```json
{
  "track_id": "9f3a1b2c4d5e6f70",
  "title": "Never Too Late",
  "artist": "Three Days Grace",
  "album": "One-X",
  "genre": "Rock",
  "duration": 219.0,
  "bpm": 124.0,
  "key": "G",
  "mode": "minor",
  "energy": 0.81,
  "danceability": 0.42,
  "valence": 0.55,
  "acousticness": 0.04,
  "instrumentalness": 0.01,
  "mood_tags": [
    {"label": "energetic", "confidence": 0.74},
    {"label": "aggressive", "confidence": 0.31}
  ],
  "analyzed_at": 1775774191,
  "analysis_version": "v3",
  "media_server_id": "iDkrnC4UrRVJ6HHEa83nc9",
  "spotify_id": null,
  "qobuz_id": null,
  "tidal_id": null,
  "deezer_id": null,
  "soundcloud_id": null,
  "mb_track_id": null
}
```

The per-backend ID columns (`media_server_id` etc.) are populated as the track is encountered through each integration (sync, download, scrobble). Any of them may be `null`.

**Error** `404` if not yet analyzed.

---

### `GET /v1/tracks/{track_id}/similar` — Similar tracks

| Parameter | Default | Description |
|-----------|---------|-------------|
| `limit` | 10 | 1-50 |
| `include_features` | false | Include full audio features |

Uses FAISS embedding similarity + SQL pre-filter (BPM/energy/mode).

---

### `GET /v1/tracks/map` — 2D music map coordinates

Returns every track that has UMAP-projected `(x, y)` coordinates from the `music_map` pipeline step. Intended for dashboard visualisation (scatter plot where nearby dots sound similar).

| Parameter | Default | Description |
|-----------|---------|-------------|
| `limit` | 5000 | 100-20000 |

**Response**

```json
{
  "count": 1842,
  "tracks": [
    {
      "track_id": "9f3a1b2c4d5e6f70",
      "title": "Strobe",
      "artist": "deadmau5",
      "genre": "electronic",
      "bpm": 128.0,
      "energy": 0.72,
      "mood": "party",
      "x": 0.51,
      "y": 0.33
    }
  ]
}
```

Tracks without coordinates (not yet mapped) are excluded. The step requires `umap-learn` installed and at least 50 analysed tracks.

---

### `GET /v1/tracks/text-search` — Natural-language track search (CLAP)

Encodes a text prompt via the LAION-CLAP text tower and returns the `k` closest tracks in the joint CLAP embedding space.

| Parameter | Type | Description |
|-----------|------|-------------|
| `q` | string (1-256) | Required. Natural-language prompt, e.g. `"melancholic rainy-night jazz"` |
| `limit` | int (1-200) | Default 50 |

**Response**

```json
{
  "query": "melancholic rainy-night jazz",
  "count": 37,
  "tracks": [
    {
      "track_id": "2c4d5e6f70819234",
      "title": "Blue in Green",
      "artist": "Miles Davis",
      "album": "Kind of Blue",
      "bpm": 74.0,
      "energy": 0.22,
      "mood_tags": [{"label": "relaxed", "confidence": 0.81}],
      "similarity": 0.417
    }
  ]
}
```

**Errors**

- `503` — `CLAP_ENABLED=false` or the CLAP FAISS index hasn't been built (no tracks have `clap_embedding` yet).

---

### `POST /v1/tracks/clap/backfill` — Backfill CLAP embeddings (admin)

Queues a background task that computes CLAP audio embeddings for every track missing one. Useful after first enabling CLAP on a library scanned before CLAP was available.

| Parameter | Type | Description |
|-----------|------|-------------|
| `limit` | int (1-50000) | Optional. Cap the number of tracks processed in this call for chunked runs. |

**Response** `202 Accepted`

```json
{"status": "accepted", "pending": 8421, "limit": null}
```

**Errors**

- `400` — `CLAP_ENABLED=false`. Enable CLAP and provide model files first.
- `403` — Not an admin API key.

After the job completes, the CLAP FAISS index is rebuilt automatically.

---

### `GET /v1/tracks/clap/stats` — CLAP coverage

```json
{
  "enabled": true,
  "total_tracks": 9203,
  "with_clap_embedding": 782,
  "coverage": 0.085
}
```

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

Populates `TrackFeatures.media_server_id` (the Navidrome song ID) for every analysed file the matcher can pair up with a Navidrome track. Also imports title / artist / album metadata where the local row has it empty or stale.

> Under the post-#37 schema, sync is a **metadata refresh** — it does **not** rename `track_id` and does **not** cascade across `ListenEvent` / `TrackInteraction`. The internal `track_id` is the SHA-256-prefix hash of the relative file path, computed once at first scan, and immutable thereafter.

Requires `MEDIA_SERVER_TYPE=navidrome`, `MEDIA_SERVER_URL`, and credentials. Plex is no longer supported.

The matcher walks four strategies per row in priority order:

1. **MBID** — `musicbrainz_track_id` exact match (most reliable; survives renames + retags).
2. **AATD** — canonical `(artist, album, title)` tuple, optionally disambiguated by duration (±1.5s). Multi-artist strings are normalised across separator variants (`&` / `and` / `,` / `;` / `/` / `feat` / `ft` / `featuring` / `with` / `x` / `vs`) so the same track tagged differently on each side still keys to the same row.
3. **ATD** — `(album, title)` + strict duration (±1s) fallback. Used when artist canonicalisation can't bridge the gap. Requires a unique candidate after the duration filter and a non-empty album, otherwise skipped.
4. **Path** — normalised relative file path. Last-resort matcher; useful for Navidrome libraries laid out exactly to its templated path.

**Response** `200 OK`

```json
{
  "message": "Sync complete",
  "server_type": "navidrome",
  "tracks_fetched": 57072,
  "tracks_matched": 32062,
  "tracks_matched_by_mbid": 4301,
  "tracks_matched_by_aatd": 27760,
  "tracks_matched_by_path": 1,
  "tracks_aatd_ambiguous": 275,
  "media_server_id_updated": 31194,
  "metadata_updated": 31194,
  "tracks_unmatched": 25010,
  "errors": [],
  "elapsed_seconds": 84.2
}
```

**Error** `400` if no media server configured.

---

### `POST /v1/library/cleanup-stale` — Delete TrackFeatures rows whose files are gone

Admin only. One-shot cleanup for legacy `track_features` rows that point at files no longer on disk — typically the residue of a pre-MBID/AATD sync where Navidrome moved to a new ID format and the old rows can never be re-matched.

For each candidate the endpoint checks whether `file_path` still exists. If the file is gone, the `track_features` row plus its orphaned `track_interactions` and `listen_events` for that `track_id` are deleted. Rows whose files still exist are left alone — the next `POST /v1/library/sync` should pick them up via the MBID/AATD/ATD matcher.

#### Query parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `dry_run` | bool | `true` | Report counts without deleting. Pass `false` to actually delete. |
| `pattern` | string | `legacy_hex` | Stale-id pattern. Currently only `legacy_hex` (16 lowercase-hex chars — pre-Navidrome-0.61 format) is supported. |

```bash
# Dry run first — see how many rows would be touched
curl -X POST "http://localhost:8000/v1/library/cleanup-stale" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Then execute
curl -X POST "http://localhost:8000/v1/library/cleanup-stale?dry_run=false" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200 OK`

```json
{
  "pattern": "legacy_hex",
  "dry_run": false,
  "candidates_total": 928,
  "files_missing": 612,
  "files_present": 316,
  "deleted_track_features": 612,
  "deleted_interactions": 487,
  "deleted_events": 9214,
  "next_step": "Run POST /v1/library/sync to re-match the rows whose files still exist."
}
```

**Errors**

- `400` — Unknown `pattern` value.

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
      "position": 0, "track_id": "2c4d5e6f70819234", "source": "content",
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
    "user_id": "simon", "track_id": "2c4d5e6f70819234",
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
    "seed_track_id": "9f3a1b2c4d5e6f70",
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
| `path` | `seed_track_id` + `params.target_track_id` | **Song Path** — sonic bridge between two tracks. Slerp-interpolates between their 64-dim audio embeddings and picks the nearest unused library track at each waypoint. Both IDs must exist in `track_features` and have an `embedding`. |
| `text` | `params.prompt` | **CLAP Text Prompt** — encodes the prompt via the CLAP text tower and ranks tracks by cosine similarity in joint embedding space. Requires `CLAP_ENABLED=true` and at least some tracks with `clap_embedding` populated. Fails with `503` otherwise. |

**Example — Song Path**

```bash
curl -X POST http://localhost:8000/v1/playlists \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "From Deadmau5 to Chet Baker",
    "strategy": "path",
    "seed_track_id": "strobe-9f3a1b2c4d5e",
    "max_tracks": 15,
    "params": {"target_track_id": "baker-2c4d5e6f7081"}
  }'
```

**Example — CLAP Text**

```bash
curl -X POST http://localhost:8000/v1/playlists \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Rainy Coffee Shop Jazz",
    "strategy": "text",
    "max_tracks": 20,
    "params": {"prompt": "melancholic piano at 2am with light rain"}
  }'
```

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

Download proxy across multiple backends. `POST /v1/downloads` walks the
configured **`individual` priority chain** (see [Download Routing Configuration](#download-routing-configuration))
and returns the first backend that successfully queues the track. Every backend
attempt — successful or not — is appended to the request's `attempts` log.

Backends: **spotdl-api** (YouTube Music), **streamrip-api** (Qobuz/Tidal/Deezer/SoundCloud lossless), **Spotizerr** (legacy fallback), **slskd** (Soulseek). Requires at least one of `SPOTDL_API_URL`, `STREAMRIP_API_URL`, `SPOTIZERR_URL`, or `SLSKD_URL`+`SLSKD_API_KEY`.

### `GET /v1/downloads/search` — Search a single backend (legacy)

Hits the single backend selected by `DEFAULT_DOWNLOAD_CLIENT`. For multi-backend
search, prefer `/v1/downloads/search/multi` below.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `q` | string | yes | Search query |
| `limit` | int | no | 1-100, default 25 |

### `GET /v1/downloads/search/multi` — Multi-agent parallel search

Runs `search` against every backend in `parallel_search_backends` concurrently
(per the active routing config), with a per-backend timeout. Each result
carries an opaque `download_handle` you can POST back to
`/v1/downloads/from-handle` to download that specific result.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `q` | string | required | Search query |
| `limit` | int | 25 | 1-100, hits per backend (a full album fits in 25–30) |
| `backends` | string | from config | Comma-separated override (`spotdl,streamrip,slskd`) |
| `timeout_ms` | int | from config | Per-backend timeout, 500-30000 |

```bash
curl "http://localhost:8000/v1/downloads/search/multi?q=Radiohead+Creep&limit=5" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

**Response** `200`:
```json
{
  "query": "Radiohead Creep",
  "limit": 5,
  "timeout_ms": 5000,
  "groups": [
    {
      "backend": "streamrip",
      "ok": true,
      "results": [
        {
          "backend": "streamrip",
          "title": "Creep",
          "artist": "Radiohead",
          "album": "Pablo Honey",
          "image_url": "https://…",
          "quality": "hires",
          "bitrate_kbps": null,
          "duration_ms": 238000,
          "download_handle": {
            "backend": "streamrip",
            "spotify_id": "33933680",
            "service": "qobuz",
            "service_id": "33933680",
            "artist": "Radiohead",
            "title": "Creep"
          },
          "extra": {"_service": "qobuz", "_quality": "Hi-Res 24bit/96kHz"}
        }
      ]
    },
    {"backend": "spotdl", "ok": false, "error": "not configured", "results": []},
    {"backend": "spotizerr", "ok": false, "error": "timeout after 5.0s", "results": []}
  ]
}
```

Each group has `ok` (bool), `error` (string when `ok=false`), and `results`
(possibly empty). Results from different backends use different
`download_handle` shapes — always pass the handle back verbatim.

### `POST /v1/downloads` — Download a track via the cascade

Walks the active `individual` chain. The track ref accepts a `spotify_id`
(spotdl/spotizerr key directly), or just `artist_name` + `track_title` (each
adapter does an internal search to resolve to its native ID).

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

**Response** `200`:
```json
{
  "id": 42,
  "spotify_id": "70LcF31zb1H0PyJoS1Sx3y",
  "task_id": "abc123",
  "status": "downloading",
  "source": "streamrip",
  "track_title": "Creep",
  "artist_name": "Radiohead",
  "attempts": [
    {"backend": "spotdl", "success": false, "status": "error", "task_id": null,
     "error": "spotdl-api HTTP 503", "quality": null, "duration_ms": 1240, "extra": {}},
    {"backend": "streamrip", "success": true, "status": "downloading", "task_id": "abc123",
     "error": null, "quality": "hires", "duration_ms": 890, "extra": {}}
  ],
  "created_at": 1714128050,
  "updated_at": 1714128052
}
```

If every backend in the chain fails, returns `502` with the last attempted
backend's error message. The DB row is still persisted (with `status=error`
and the full attempts log) so the failure shows up in stats and the history
endpoint.

Status values: `pending`, `downloading`, `queued`, `duplicate`, `completed`, `error`.

### `POST /v1/downloads/from-handle` — Download a specific multi-search result

Bypass the cascade — the user already chose a backend via `/search/multi`.
The `handle` field is the opaque dict returned in each result.

```bash
curl -X POST http://localhost:8000/v1/downloads/from-handle \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "handle": {"backend":"slskd","username":"peer123","filename":"Radiohead/Creep.flac","size":42118921},
    "track_title": "Creep",
    "artist_name": "Radiohead"
  }'
```

Same response shape as `POST /v1/downloads`. The `attempts` array always has a
single entry — the chosen backend.

### `GET /v1/downloads/status/{task_id}` — Check progress

### `GET /v1/downloads` — List download history

| Parameter | Default | Description |
|-----------|---------|-------------|
| `status` | - | Filter: `pending`, `downloading`, `queued`, `duplicate`, `completed`, `error` |
| `limit` | 50 | 1-200 |
| `offset` | 0 | - |

Each row includes the cascade `attempts` log (older rows pre-dating Phase 2
have `attempts: null`).

### `GET /v1/downloads/stats` — Per-backend telemetry

Aggregates `download_requests` over a look-back window into per-backend
counts. Drives the routing-policy GUI's reliability panel.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `days` | 30 | 1-365, look-back window |

```json
{
  "window_days": 30,
  "since_unix": 1711450000,
  "backends": [
    {"backend": "spotdl", "total": 73, "success": 58, "failure": 12, "in_flight": 3, "success_rate": 0.829},
    {"backend": "streamrip", "total": 41, "success": 39, "failure": 2, "in_flight": 0, "success_rate": 0.951},
    {"backend": "soulseek", "total": 6, "success": 4, "failure": 1, "in_flight": 1, "success_rate": 0.8}
  ]
}
```

`success_rate` is `null` when no terminal results are in the window.

---

## Download Routing Configuration

Versioned, DB-persisted policy that decides **which** download backends are
tried, **in what order**, for **which purpose**, and with what quality
threshold. Defaults are seeded on first boot and match the prior env-var-driven
behaviour. Changes take effect on the next request — no restart needed.

Three independent priority chains:

| Purpose | Used by | Default order |
|---------|---------|---------------|
| `individual` | `POST /v1/downloads` (single tracks on demand) | spotdl → streamrip → spotizerr → slskd (slskd disabled) |
| `bulk_per_track` | `charts` auto-add, `bulk_download` (top-tracks) | streamrip → spotdl → spotizerr → slskd (slskd disabled) |
| `bulk_album` | `discovery`, `fill_library` (album-level) | lidarr → streamrip (streamrip disabled) |

Plus `parallel_search_backends` — which backends are queried concurrently by
`/v1/downloads/search/multi` — and `parallel_search_timeout_ms` for the
per-backend timeout.

Each chain entry has:
- `backend`: `spotdl` | `streamrip` | `spotizerr` | `slskd` | `lidarr`
- `enabled`: bool (default true)
- `min_quality`: optional `"lossy_low"` | `"lossy_high"` | `"lossless"` | `"hires"` — backends below the tier are skipped
- `timeout_s`: per-backend timeout for the cascade attempt (5-600s, default 60)

Quality tiers are ordinal (lossy_low < lossy_high < lossless < hires). Each
backend declares an expected quality (spotdl=lossy_high, streamrip=hires,
spotizerr=lossy_high, slskd=lossy_high — variable, evaluated post-search,
lidarr=lossless). Pre-flight gate uses the declared quality; slskd's actual
file quality is also re-checked after the search picks a winner.

**All routing endpoints require admin privileges** (when `ADMIN_API_KEYS` is set).

### `GET /v1/downloads/routing` — Active routing config

```json
{
  "id": 3,
  "version": 3,
  "name": "Bumped slskd ahead of spotizerr",
  "config": {
    "individual": [
      {"backend": "spotdl", "enabled": true, "min_quality": null, "timeout_s": 60},
      {"backend": "streamrip", "enabled": true, "min_quality": "lossless", "timeout_s": 60},
      {"backend": "spotizerr", "enabled": true, "min_quality": null, "timeout_s": 60},
      {"backend": "slskd", "enabled": false, "min_quality": null, "timeout_s": 60}
    ],
    "bulk_per_track": [...],
    "bulk_album": [...],
    "parallel_search_backends": ["spotdl", "streamrip", "spotizerr"],
    "parallel_search_timeout_ms": 5000
  },
  "is_active": true,
  "created_at": 1714128000,
  "created_by": "abc..."
}
```

### `GET /v1/downloads/routing/defaults` — Defaults + group metadata

Returns the default `config` plus `groups` describing the four UI sections
(`individual`, `bulk_per_track`, `bulk_album`, `parallel_search`) — `label`,
`description`, and `backends_eligible`. The dashboard renders the routing GUI
from this response.

### `GET /v1/downloads/routing/history` — Version history

| Parameter | Default | Description |
|-----------|---------|-------------|
| `limit` | 20 | 1-100 |
| `offset` | 0 | - |

### `GET /v1/downloads/routing/{version}` — Specific historical version

### `PUT /v1/downloads/routing` — Save a new version (becomes active)

```bash
curl -X PUT http://localhost:8000/v1/downloads/routing \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Prefer lossless for everything",
    "config": { "individual": [...], "bulk_per_track": [...], "bulk_album": [...], "parallel_search_backends": [...], "parallel_search_timeout_ms": 5000 }
  }'
```

The previous version is deactivated; the new one becomes active immediately.
Validation errors (e.g. unknown backend, out-of-range timeout) return `422`.

### `POST /v1/downloads/routing/reset` — Reset to defaults (creates new version)

### `POST /v1/downloads/routing/activate/{version}` — Roll back to a historical version

### `GET /v1/downloads/routing/export` — JSON download of active config

Sets `Content-Disposition: attachment; filename="grooveiq-routing-v{N}.json"`.

### `POST /v1/downloads/routing/import` — Upload a JSON config

```bash
curl -X POST http://localhost:8000/v1/downloads/routing/import \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d @grooveiq-routing-v3.json
```

Validates against the schema; missing keys get defaults; out-of-range values return `422`.

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

### CLAP Text-Audio Embeddings (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAP_ENABLED` | `false` | Enable CLAP audio + text encoders |
| `CLAP_MODEL_DIR` | `/data/models/clap` | Directory containing exported ONNX models |
| `CLAP_AUDIO_MODEL_FILE` | `clap_audio.onnx` | Audio tower ONNX file (runs in analysis worker) |
| `CLAP_TEXT_MODEL_FILE` | `clap_text.onnx` | Text tower ONNX file (runs in main process) |
| `CLAP_TOKENIZER_FILE` | `clap_tokenizer.json` | HuggingFace tokenizers JSON |
| `CLAP_EMBEDDING_DIM` | `512` | Embedding dimension (must match exported models) |
| `CLAP_AUDIO_SR` | `48000` | Sample rate the audio tower was exported with |
| `CLAP_AUDIO_CLIP_SECONDS` | `10.0` | Clip length fed into the audio tower |

When enabled, the analysis worker computes a 512-dim `clap_embedding` per track (stored base64 on `track_features`) and the main process lazily loads the text tower on first `GET /v1/tracks/text-search`. Both indices are rebuilt by `POST /v1/pipeline/run`. See `POST /v1/tracks/clap/backfill` to populate existing tracks.

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
