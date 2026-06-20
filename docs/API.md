# GrooveIQ API Reference

**Base URL:** `http://<host>:8000`
**Auth:** All endpoints except `GET /health` and `GET /dashboard` require `Authorization: Bearer <api_key>`.
**Content-Type:** `application/json`

Interactive docs are available at `/docs` and `/redoc` when `ENABLE_DOCS=true` (development only).

This is the single canonical API reference for GrooveIQ. Source repository:
[github.com/Sxx7/GrooveIQ](https://github.com/Sxx7/GrooveIQ).

---

## Authentication & access control

Three layers of access control apply. Two of them are **no-ops unless explicitly
configured**, so on a minimal single-key deployment every key can reach every
route.

| Layer | What it does | When it is enforced |
|-------|--------------|---------------------|
| **Bearer key** (`require_api_key`) | SHA-256 + HMAC constant-time check of `Authorization: Bearer <key>`. `401` on missing/invalid, `429` on per-key rate limit. | Always, on every `/v1/*` route. (`/health` is open.) |
| **Admin gate** (`require_admin`) | Restricts a route to admin keys. | **Only when `ADMIN_API_KEYS` is configured.** If unset, every valid key is treated as admin, so "admin-gated" routes are reachable by any key. |
| **User scoping** (`check_user_access`) | A key may only act on the `user_id`(s) it is bound to. | **Only when `API_KEY_USERS` bindings are configured.** If unset, any key may act on any user. |

Throughout this doc, **"admin-gated (when admin keys are configured)"** means the
route calls `require_admin`, and **"user-scoped"** means it calls
`check_user_access`.

Rate limits: event-ingest endpoints use `RATE_LIMIT_EVENTS` (default 300/min/key);
all other endpoints use `RATE_LIMIT_DEFAULT` (default 200/min/key).

### `user_id` format (issue #86)

Every endpoint that accepts a `user_id` (path param, query string, or request
body field) validates it against a regex. Non-conforming values are rejected
with **400 Bad Request**:

```json
{"detail": "Invalid user_id 'Alice': must match ^[A-Za-z0-9]{20,22}$. GrooveIQ requires the Navidrome user identifier."}
```

The default pattern (`^[A-Za-z0-9]{20,22}$`) covers Navidrome's identifier output
across versions: `xid` (20 chars, pre-0.50) and `nanoid` (21–22 chars, 0.50+).
Override via `USER_ID_PATTERN`. Plex usernames (which were emails) are no longer
supported. Navidrome is the canonical identity source. Send the Navidrome user
ID exactly as Navidrome surfaces it on `getUser.view`.

### Track identifiers in responses

Every track has one stable internal GrooveIQ `track_id` (a SHA-256-prefix hash of
the relative file path, immutable once scanned) plus zero or more per-backend
external IDs (`media_server_id`, `spotify_id`, `qobuz_id`, `tidal_id`,
`deezer_id`, `soundcloud_id`, `mb_track_id`). Every track-returning endpoint
ships both. Use `media_server_id` to play the track on your media server (Navidrome); use
`track_id` for everything sent back to GrooveIQ (events, features, lookup, audit,
radio). `media_server_id` is `null` for tracks the media server hasn't matched
yet: run `POST /v1/library/sync` to populate.

The **events API also accepts any per-backend external ID** in `track_id`: the
server resolves it to the canonical internal hash at ingest. Events whose
`track_id` matches no `TrackFeatures` row are dropped (no attribution possible).

---

## Table of Contents

1. [Health & Dashboard](#health--dashboard)
2. [Events](#events)
3. [Users](#users)
4. [Onboarding](#onboarding)
5. [Tracks & Library](#tracks--library)
6. [Recommendations](#recommendations)
7. [Recommendation Audit & Replay](#recommendation-audit--replay)
8. [Radio](#radio)
9. [Playlists](#playlists)
10. [Discovery](#discovery)
11. [Fill Library](#fill-library)
12. [Charts](#charts)
13. [Downloads](#downloads)
14. [Download Routing Configuration](#download-routing-configuration)
15. [Soulseek](#soulseek)
16. [Last.fm](#lastfm)
17. [Artists](#artists)
18. [News](#news)
19. [Pipeline & Stats](#pipeline--stats)
20. [Algorithm Configuration](#algorithm-configuration)
21. [Lidarr Backfill](#lidarr-backfill)
22. [Lyrics (operator)](#lyrics-operator)
23. [Integrations](#integrations)
24. [API Call Log](#api-call-log)
25. [Admin](#admin)
26. [Configuration Reference](#configuration-reference)

---

## Health & Dashboard

### `GET /health`

Health check. **No auth required.**

```bash
curl http://localhost:8000/health
```

```json
{"status": "ok", "service": "grooveiq"}
```

### `GET /dashboard`

Single-page web dashboard. **No auth required.** `GET /` redirects here.

---

## Events

### `POST /v1/events`: Ingest a single event

User-scoped. Returns `202 Accepted`.

```bash
curl -X POST http://localhost:8000/v1/events \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "aBcDeFgHiJkLmNoPqRsTu",
    "track_id": "9f3a1b2c4d5e6f70",
    "event_type": "play_end",
    "value": 0.95
  }'
```

```json
{"accepted": 1, "rejected": 0, "errors": []}
```

#### Required fields

| Field | Type | Description |
|-------|------|-------------|
| `user_id` | string | Navidrome user identifier (must match `USER_ID_PATTERN`) |
| `track_id` | string (1-128) | Internal GrooveIQ `track_id`, OR any per-backend ID known to GrooveIQ (resolved at ingest; unmatched IDs are dropped) |
| `event_type` | string | One of the event types below |

#### Event types

`play_start`, `play_end`, `skip`, `pause`, `resume`, `like`, `dislike`, `rating`,
`playlist_add`, `playlist_remove`, `queue_add`, `seek_back`, `seek_forward`,
`repeat`, `volume_up`, `volume_down`, `reco_impression`. (`play_end` with
completion below `MIN_PLAY_PERCENTAGE`, default 5%, is dropped as noise.)

#### Optional core fields

| Field | Type | Description |
|-------|------|-------------|
| `value` | float | Event-specific payload (completion ratio, elapsed seconds, rating, volume) |
| `context` | string (≤64) | Free-text label, e.g. `"workout"` |
| `client_id` | string (≤64) | Which app sent this |
| `session_id` | string (≤64) | Client-assigned session identifier |
| `timestamp` | int | Unix epoch UTC. Default server time. Rejected if >24h old or >5min future |

#### Rich signal fields (all optional)

These improve recommendation quality but are not required.

<details>
<summary>Expand all optional rich-signal fields</summary>

**Impression & exposure**: for learning-to-rank

| Field | Type | Description |
|-------|------|-------------|
| `surface` | string | `home`, `search`, `now_playing`, `playlist_view` |
| `position` | int ≥0 | Rank position in recommendation list |
| `request_id` | string | Ties an impression to downstream actions |
| `model_version` | string | Which recommendation model produced this |

**Session context**

| Field | Type | Description |
|-------|------|-------------|
| `session_position` | int ≥0 | Track's ordinal position in the session (0-based) |

**Satisfaction & engagement proxies**

| Field | Type | Description |
|-------|------|-------------|
| `dwell_ms` | int ≥0 | Milliseconds listened |
| `pause_duration_ms` | int ≥0 | Inter-track pause in ms |
| `num_seekfwd` / `num_seekbk` | int ≥0 | Seek counts |
| `shuffle` | bool | Whether shuffle was active |

**Context / source**

| Field | Type | Description |
|-------|------|-------------|
| `context_type` | string | `playlist`, `album`, `radio`, `search`, `home_shelf` |
| `context_id` | string | ID of the source (playlist ID, radio session ID, …) |
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
| `hour_of_day` | int 0-23 | Client's local hour |
| `day_of_week` | int 1-7 | ISO 8601: 1=Monday, 7=Sunday |
| `timezone` | string | IANA timezone, e.g. `Europe/Zurich` |

**Audio output**

| Field | Type | Description |
|-------|------|-------------|
| `output_type` | string | `headphones`, `speaker`, `bluetooth_speaker`, `car_audio`, `built_in`, `airplay` |
| `output_device_name` | string | e.g. `AirPods Pro` |
| `bluetooth_connected` | bool | Audio routed over Bluetooth |

**Location**

| Field | Type | Description |
|-------|------|-------------|
| `latitude` | float (-90..90) | GPS latitude |
| `longitude` | float (-180..180) | GPS longitude |
| `location_label` | string | `home`, `work`, `gym`, `commute` |

</details>

#### Closing the feedback loop

Echo the `request_id` from a recommendation response back on the resulting
events so impression-to-stream attribution works:

```json
{"user_id": "aBcDeFgHiJkLmNoPqRsTu", "track_id": "2c4d5e6f70819234",
 "event_type": "play_start", "request_id": "a1b2c3d4-..."}
```

### `POST /v1/events/batch`: Ingest multiple events

Send up to `EVENT_BATCH_MAX` (default 50) events; each is validated and
user-scoped independently. Returns `202`.

```json
{"events": [{"user_id": "...", "track_id": "...", "event_type": "play_start"}, ...]}
```

```json
{"accepted": 3, "rejected": 0, "errors": []}
```

### `GET /v1/events`: Query stored events

Debug query. **User-scoped if `user_id` is given, otherwise admin-gated (when
admin keys are configured).**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | – | Filter by user |
| `track_id` | string | – | Filter by track |
| `event_type` | string | – | Filter by event type |
| `device_id` | string | – | Filter by device |
| `context_type` | string | – | Filter by context type |
| `request_id` | string | – | Filter by request ID |
| `limit` | int | 50 | 1-500 |
| `offset` | int | 0 | Pagination offset |

---

## Users

### `GET /v1/users`: List all users

Admin-gated (when admin keys are configured). Paginated.

| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 100 | 1-500 |
| `offset` | 0 | – |

### `POST /v1/users`: Create a user

Auto-created on first event too; explicit creation lets you set `display_name`.

```json
{"user_id": "aBcDeFgHiJkLmNoPqRsTu", "display_name": "Alice"}
```

`201 Created` returns the user object. `409` if `user_id` already exists.

### `GET /v1/users/{user_id}`: Get a user

User-scoped. `404` if not found.

### `PATCH /v1/users/{uid}`: Update a user

User-scoped. **Keys on the stable numeric `uid` (int), not the username string**,
unlike every other `/users/{user_id}` route. Renaming `user_id` cascades to all
related tables. Body (`UserUpdate`, at least one field): `user_id` (1-128),
`display_name`.

```bash
curl -X PATCH http://localhost:8000/v1/users/1 \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"display_name": "Alice D."}'
```

`404` (no user), `409` (user_id taken), `422` (no fields).

### `GET /v1/users/{user_id}/profile`: Taste profile

User-scoped. Returns the computed taste profile (audio preferences;
mood/key/time/device/output/context/location patterns; top tracks; behaviour;
multi-timescale 7d/30d/all-time sub-profiles) plus a Last.fm block if linked.
`taste_profile` is `null` until the pipeline has run.

### `GET /v1/users/{user_id}/interactions`: Track interactions

User-scoped. Paginated (returns `total`). Each row carries `track_id` +
`media_server_id`, satisfaction/play/skip/like/dislike/repeat counts, and track
metadata.

| Param | Default | Options |
|-------|---------|---------|
| `sort_by` | `satisfaction_score` | `satisfaction_score`, `play_count`, `last_played_at`, `skip_count` |
| `sort_dir` | `desc` | `asc`, `desc` |
| `limit` | 50 | 1-200 |
| `offset` | 0 | – |

### `GET /v1/users/{user_id}/history`: Listening history

User-scoped. Paginated. Chronological play history with title/artist/album,
device/output context, completion %, `dwell_ms`, `reason_end`.

| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 50 | 1-200 |
| `offset` | 0 | – |

### `GET /v1/users/{user_id}/sessions`: Listening sessions

User-scoped. Paginated. Materialised sessions (grouped by inactivity gap, default
30 min).

| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 25 | 1-100 |
| `offset` | 0 | – |

### `GET /v1/users/{user_id}/stats`: Engagement summary

User-scoped. Single-user engagement numbers. `total_events`, `unique_tracks`,
`diversity`, `last_active` are over the **last 30 days**; `plays`, `skips`,
`skip_rate` come from the all-time interactions rollup.

```json
{
  "user_id": "aBcDeFgHiJkLmNoPqRsTu",
  "window_days": 30,
  "total_events": 1234, "plays": 890, "skips": 145, "skip_rate": 0.163,
  "unique_tracks": 412, "diversity": 0.334, "last_active": 1712000000
}
```

---

## Onboarding

### `POST /v1/users/{user_id}/onboarding`: Submit onboarding preferences

User-scoped. Full replace (at least one field required). Seeds a cold-start taste
profile.

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

`energy_preference` / `danceability_preference` are floats 0–1.

```json
{"preferences_saved": 8, "matched_tracks": 12, "matched_artists": 2, "profile_seeded": true}
```

### `GET /v1/users/{user_id}/onboarding`: Get onboarding preferences

User-scoped. Returns the stored preferences object.

---

## Tracks & Library

### `GET /v1/tracks`: List / search analyzed tracks

Paginated (returns `total`). Each row carries the internal `track_id` plus every
known per-backend external ID.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 50 | 1-200 |
| `offset` | int | 0 | – |
| `sort_by` | string | `bpm` | `bpm`, `energy`, `danceability`, `valence`, `key`, `duration`, `analyzed_at`, `analysis_version` |
| `sort_dir` | string | `asc` | `asc`, `desc` |
| `search` | string | – | Title / artist / album / genre / track_id |
| `min_bpm` / `max_bpm` | float | – | BPM range |
| `min_energy` / `max_energy` | float | – | Energy range (0-1) |
| `key` | string | – | Musical key, e.g. `C`, `C#` |
| `mode` | string | – | `major` / `minor` |
| `mood` | string | – | One of `happy`, `sad`, `aggressive`, `relaxed`, `party` (confidence > 0.3). Returns `400` for any other value |

```json
{
  "tracks": [
    {
      "track_id": "9f3a1b2c4d5e6f70", "title": "Never Too Late",
      "artist": "Three Days Grace", "album": "One-X",
      "duration": 219.0, "bpm": 124.0, "energy": 0.81,
      "media_server_id": "iDkrnC4UrRVJ6HHEa83nc9",
      "spotify_id": null, "qobuz_id": null, "tidal_id": null,
      "deezer_id": null, "soundcloud_id": null, "mb_track_id": null
    }
  ],
  "total": 58445, "limit": 50, "offset": 0
}
```

### `GET /v1/tracks/lookup`: Resolve ONE external ID → internal `track_id`

Pass **exactly one** of the lookup params; `400` if zero or multiple supplied,
`404` if no match.

| Param | Description |
|-------|-------------|
| `media_server_id` | Navidrome song ID (e.g. `iDkrnC4UrRVJ6HHEa83nc9`) |
| `spotify_id` | Spotify track ID |
| `qobuz_id` | Qobuz track ID |
| `tidal_id` | Tidal track ID |
| `deezer_id` | Deezer track ID |
| `soundcloud_id` | SoundCloud track ID |
| `mb_track_id` | MusicBrainz Recording ID (UUID) |

```bash
curl "http://localhost:8000/v1/tracks/lookup?media_server_id=iDkrnC4UrRVJ6HHEa83nc9" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

Returns the track object (with `track_id` and all per-backend IDs).

### `POST /v1/tracks/lookup`: Batch-resolve external IDs → `track_id`

Body must contain **exactly one** list field:
`media_server_ids` / `spotify_ids` / `qobuz_ids` / `tidal_ids` / `deezer_ids` /
`soundcloud_ids` / `mb_track_ids`.

```json
{ "media_server_ids": ["iDkr...", "u3dW...", "MY6f..."] }
```

```json
{ "resolved": { "iDkr...": "9f3a1b2c4d5e6f70", "u3dW...": "8b75...", "MY6f...": null } }
```

`null` for IDs that match no track.

### `GET /v1/tracks/{track_id}/features`: Audio features

`{track_id}` is the **internal** GrooveIQ ID. `404` if not yet analyzed.

```json
{
  "track_id": "9f3a1b2c4d5e6f70", "title": "Never Too Late",
  "artist": "Three Days Grace", "album": "One-X", "genre": "Rock",
  "duration": 219.0, "bpm": 124.0, "key": "G", "mode": "minor",
  "energy": 0.81, "danceability": 0.42, "valence": 0.55,
  "acousticness": 0.04, "instrumentalness": 0.01,
  "mood_tags": [{"label": "party", "confidence": 0.74}, {"label": "aggressive", "confidence": 0.31}],
  "analyzed_at": 1775774191, "analysis_version": "v3",
  "media_server_id": "iDkrnC4UrRVJ6HHEa83nc9",
  "spotify_id": null, "qobuz_id": null, "tidal_id": null,
  "deezer_id": null, "soundcloud_id": null, "mb_track_id": null
}
```

### `GET /v1/tracks/{track_id}/lyrics`: Track lyrics

Returns lyrics resolved through the cascade (embedded → LRCLIB → ASR). `200` with
an instrumental marker when the track is instrumental; `404` when no lyrics are
available. Requires `LYRICS_ENABLED=true` for acquisition.

### `GET /v1/tracks/{track_id}/similar`: Similar tracks

FAISS embedding similarity + SQL pre-filter (BPM/energy/mode). Each item carries
`track_id` + `media_server_id` plus metadata and a `similarity` score.

| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 10 | 1-50 |
| `include_features` | false | Include full audio features per result |

### `GET /v1/tracks/map`: 2D music-map coordinates

Returns every track with UMAP-projected `(x, y)` from the `music_map` pipeline
step (requires `umap-learn` + ≥50 analysed tracks). Unmapped tracks are excluded.

| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 5000 | 100-20000 |

```json
{
  "count": 1842,
  "tracks": [
    {"track_id": "9f3a...", "media_server_id": "iDkr...", "title": "Strobe",
     "artist": "deadmau5", "bpm": 128.0, "energy": 0.72, "mood": "party", "x": 0.51, "y": 0.33}
  ]
}
```

### `GET /v1/tracks/text-search`: Natural-language search (CLAP)

Encodes the prompt via the LAION-CLAP text tower and returns the closest tracks
in the joint 512-dim space.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `q` | string (1-256) | *(required)* | Prompt, e.g. `"melancholic rainy-night jazz"` |
| `limit` | int | 50 | 1-200 |

```json
{"query": "melancholic rainy-night jazz", "count": 37,
 "tracks": [{"track_id": "2c4d...", "title": "Blue in Green", "artist": "Miles Davis", "similarity": 0.417}]}
```

`503` if `CLAP_ENABLED=false` or the CLAP FAISS index isn't ready.

### `POST /v1/tracks/clap/backfill`: Backfill CLAP embeddings

Admin-gated (when admin keys are configured). Queues a background job to compute
CLAP audio embeddings for tracks missing one; rebuilds the CLAP index on
completion.

| Param | Type | Description |
|-------|------|-------------|
| `limit` | int (1-50000) | Optional cap on tracks processed this call |

`202` `{"status": "accepted", "pending": 8421, "limit": null}`. `400` if
`CLAP_ENABLED=false`.

### `GET /v1/tracks/clap/stats`: CLAP coverage

```json
{"enabled": true, "total_tracks": 9203, "with_clap_embedding": 782, "coverage": 0.085}
```

### `POST /v1/library/scan`: Trigger library scan

Admin-gated (when admin keys are configured). Starts async audio analysis (one
scan at a time). `202`.

```json
{"message": "Scan started", "scan_id": 3, "status": "running"}
```

### `GET /v1/library/scan/{scan_id}`: Scan status

Admin-gated (when admin keys are configured). Status: `pending`, `running`,
`completed`, `failed`.

```json
{"scan_id": 3, "status": "running", "files_found": 1842, "files_analyzed": 523,
 "files_failed": 2, "started_at": 1743638400, "ended_at": null}
```

### `GET /v1/library/scan/{scan_id}/logs`: Scan logs

Admin-gated (when admin keys are configured). Poll-paginated via `after_id`.

| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 50 | 1-200 |
| `after_id` | 0 | Only entries with `id` > this |

### `POST /v1/library/sync`: Sync with media server

Admin-gated (when admin keys are configured). Populates
`TrackFeatures.media_server_id` and refreshes title/artist/album metadata.
Requires `MEDIA_SERVER_TYPE`, `MEDIA_SERVER_URL`, and credentials. The matcher
walks four strategies per row: **MBID** → **AATD** (artist/album/title, duration
disambiguated) → **ATD** (album/title + strict duration) → **path**.

```json
{
  "message": "Sync complete", "server_type": "navidrome",
  "tracks_fetched": 57072, "tracks_matched": 32062,
  "tracks_matched_by_mbid": 4301, "tracks_matched_by_aatd": 27760,
  "tracks_matched_by_path": 1, "tracks_aatd_ambiguous": 275,
  "media_server_id_updated": 31194, "metadata_updated": 31194,
  "tracks_unmatched": 25010, "errors": [], "elapsed_seconds": 84.2
}
```

`400` if no media server configured.

### `POST /v1/library/cleanup-stale`: Delete TrackFeatures rows whose files are gone

Admin-gated (when admin keys are configured). One-shot cleanup of legacy rows
pointing at files no longer on disk. Deletes the `track_features` row plus
orphaned interactions/events; leaves rows whose files still exist.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `dry_run` | bool | `true` | Report counts without deleting; pass `false` to delete |
| `pattern` | string | `legacy_hex` | Stale-ID pattern (currently only `legacy_hex`: 16 lowercase-hex chars) |

```json
{
  "pattern": "legacy_hex", "dry_run": false,
  "candidates_total": 928, "files_missing": 612, "files_present": 316,
  "deleted_track_features": 612, "deleted_interactions": 487, "deleted_events": 9214,
  "next_step": "Run POST /v1/library/sync to re-match the rows whose files still exist."
}
```

`400` for an unknown `pattern`.

---

## Recommendations

### `GET /v1/recommend/{user_id}`: Get recommendations

User-scoped. Candidate-gen → LightGBM rank → diversity rerank, served from a
stale-while-revalidate mix cache. Fires a fire-and-forget audit write.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `seed_track_id` | string | – | Bias results toward this track |
| `limit` | int | 25 | 1-100 |
| `genre` | string | – | Filter by genre (case-insensitive substring) |
| `mood` | string | – | One of `happy`, `sad`, `aggressive`, `relaxed`, `party` (confidence > 0.3); `400` for other values |
| `discovery` | float | – | **Continuous discovery dial 0.0–1.0** (familiar → deep discovery) |
| `mode` | string | – | **Discovery-dial preset**: `familiar`, `balanced`, `discovery`, `deep_discovery`. `422` if unknown |
| `device_type` | string | – | `mobile`, `desktop`, `speaker`, `car`, `web` |
| `output_type` | string | – | `headphones`, `speaker`, `bluetooth_speaker`, `car_audio`, `built_in`, `airplay` |
| `context_type` | string | – | `playlist`, `album`, `radio`, `search`, `home_shelf` |
| `location_label` | string | – | `home`, `work`, `gym`, `commute` |
| `hour_of_day` | int 0-23 | server time | Client's local hour |
| `day_of_week` | int 1-7 | server time | ISO 8601 (1=Mon, 7=Sun) |
| `debug` | bool | false | Include debug data: **forces admin gating (when admin keys are configured)** |

> **Mode vocabulary note.** The recommend/radio discovery dial uses
> `familiar` / `balanced` / `discovery` / `deep_discovery` (plus the continuous
> `?discovery=0..1`). This is **distinct** from the artist/album recommenders,
> which use `familiar` / `balanced` / `discover` (note the different spelling and
> default; see below).

```json
{
  "request_id": "a1b2c3d4-...",
  "model_version": "lgbm-1712000000",
  "user_id": "aBcDeFgHiJkLmNoPqRsTu",
  "context": {"hour_of_day": 8, "device_type": "mobile"},
  "tracks": [
    {"position": 0, "track_id": "2c4d5e6f70819234", "media_server_id": "iDkr...",
     "source": "content", "score": 0.87, "title": "Da Funk", "artist": "Daft Punk",
     "bpm": 118.7, "energy": 0.79, "duration": 329.0}
  ]
}
```

**Candidate sources:** `content`, `content_profile`, `cf`, `session_skipgram`,
`sasrec`, `lastfm_similar`, `artist_recall`, `popular`.

**Debug mode (`?debug=true`)** adds a `debug` object with `candidates_by_source`,
`total_candidates`, `pre_rerank` (scores before reranking), `reranker_actions`
(`freshness_boost`, `skip_suppression`, `anti_repetition_exclude`,
`short_track_exclude`, `exploration_slot`, `artist_diversity_demote`), and
`feature_vectors` (all 39 features per candidate).

### `POST /v1/users/{user_id}/mixes/prewarm`: Prewarm mix cache

User-scoped + per-(key,user) rate-limited. Warms the SWR mix cache for preset
mixes in the background so a later `GET /v1/recommend/{user_id}?mode=...` is
served instantly. Returns `202`.

Body (`PrewarmRequest`, optional):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `modes` | list[string] | all presets | Which preset mixes to warm (unknowns dropped, capped by `MIX_PREWARM_MAX_MODES`) |
| `limit` | int | 25 | 1-100 |

```json
{"status": "warming", "user_id": "aBc...", "modes": ["familiar", "balanced"], "limit": 25}
```

### `GET /v1/users/{user_id}/mixes`: List suggested shelves

User-scoped. Returns ready-to-call recommend specs powering a multi-shelf home
view (On Repeat / Your Mix / Discover / Deep Cuts).

```json
{
  "user_id": "aBc...",
  "mixes": [
    {"id": "on_repeat", "title": "On Repeat", "mode": "familiar", "discovery": 0.0, "endpoint": "/v1/recommend/aBc...?mode=familiar"},
    {"id": "your_mix", "title": "Your Mix", "mode": "balanced", "discovery": 0.3, "endpoint": "/v1/recommend/aBc...?mode=balanced"},
    {"id": "discover", "title": "Discover", "mode": "discovery", "discovery": 0.6, "endpoint": "/v1/recommend/aBc...?mode=discovery"},
    {"id": "deep_cuts", "title": "Deep Cuts", "mode": "deep_discovery", "discovery": 1.0, "endpoint": "/v1/recommend/aBc...?mode=deep_discovery"}
  ]
}
```

### `GET /v1/users/{user_id}/resurfacing`: Recently-engaged tracks

User-scoped. Returns currently-"hot" tracks (just replayed, seeked back into,
finished, or liked), each with a decayed `heat` score, plus a `request_id` tying
the served list to impression/play events.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 20 | 1-50 |
| `stage` | enum | `confirmed` | `candidate` (tryout "Special tracks" card) or `confirmed` ("Keep listening") |

### `POST /v1/users/{user_id}/tracks/{track_id}/suppress`: Suppress from resurfacing

User-scoped. Writes a `suppress` event so the track stops appearing in
resurfacing cards. `track_id` max 128 chars.

### `GET /v1/recommend/{user_id}/history`: Recommendation history

User-scoped. Paginated. Past impressions with impression-to-stream attribution
(`track_id`, `media_server_id`, `position`, `request_id`, `model_version`,
`streamed`, plus metadata).

| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 50 | 1-200 |
| `offset` | 0 | – |

### `GET /v1/recommend/{user_id}/artists`: Recommended artists

User-scoped. Blends content centroid, ranker roll-up, Last.fm similar/top, and
the listening-history heuristic.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 20 | 1-100 |
| `mode` | string | `discover` | `familiar`, `balanced`, `discover` (note spelling; **default `discover`**) |
| `include_discovery` | bool | true | Include FAISS/Last.fm discovery candidates |

```json
{
  "user_id": "aBc...", "total": 20,
  "artists": [
    {"name": "Radiohead", "score": 0.85, "source": "listening", "in_library": true,
     "plays": 142, "likes": 8, "track_count": 45, "avg_satisfaction": 0.78,
     "audio": {"energy": 0.61, "danceability": 0.42, "valence": 0.39, "bpm": 128.3},
     "image_url": "https://...",
     "top_tracks": [{"track_id": "abc", "media_server_id": "Z5dd...", "title": "Everything In Its Right Place", "satisfaction_score": 0.95, "play_count": 23}]},
    {"name": "Portishead", "score": 0.51, "source": "lastfm_similar",
     "similar_to": ["Radiohead", "Massive Attack"], "in_library": false}
  ]
}
```

Per-item `sources`/`reasons`/`signals` enable "Because you listen to X" badges.

### `GET /v1/recommend/{user_id}/albums`: Recommended albums

User-scoped. Library-only roll-up: ranker + coverage + freshness + audio
coherence.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 20 | 1-100 |
| `mode` | string | `discover` | `familiar`, `balanced`, `discover` (default `discover`) |

### `GET /v1/recommend/{user_id}/forgotten-favourites`: Forgotten favourites

User-scoped. Library-only. Surfaces individual tracks the user demonstrably
loved but hasn't played in a long time — the "I forgot how much I liked this"
surface. Score is **multiplicative** — `affinity × dormancy` — so a track only
surfaces when it is *both* a proven favourite *and* dormant. `affinity` blends
the per-user normalised `satisfaction_score`, a like/repeat boost, and a
play-count saturation term; `dormancy` is `1 − exp(−ln2·days_since/halflife)`.

Qualification gates (all tunable via the `forgotten_favourites` algorithm-config
group) keep it favourites-only: the track must have been played (`last_played_at`
set — never-played tracks belong to the new-discovery surface, not here),
`play_count ≥ min_play_count`, `satisfaction_score ≥ min_satisfaction`, and have
been dormant for `≥ min_dormancy_days`. No model is trained.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 25 | 1-100 |

```json
{
  "user_id": "aBc...", "generated_at": 1712000000, "total": 25,
  "tracks": [
    {"track_id": "abc", "title": "The Gem", "artist": "Artist X", "album": "Album Y",
     "media_server_id": "Z5dd...", "duration": 214.0, "score": 0.71,
     "sources": ["satisfaction", "likes", "plays"],
     "reasons": ["you loved this", "you liked it", "not played in 7 months"],
     "signals": {"affinity": 0.88, "dormancy": 0.81, "satisfaction": 0.95,
                 "days_since_last_play": 210, "play_count": 12, "like_count": 2, "repeat_count": 0},
     "last_played_at": 1693000000}
  ]
}
```

Per-item `sources`/`reasons`/`signals` enable "you loved this — last played N
months ago" badges.

### `GET /v1/stats/model`: Recommendation model stats

> **Path note.** This route's real path is **`/v1/stats/model`** (the decorator
> is `@router.get("/stats/model")`, not `/recommend/stats/model`).

Admin-gated (when admin keys are configured). Ranker training info, offline
evaluation metrics (NDCG@k, skip rate, completion), impression-to-stream rates,
and (when `RECO_DIAL_EVAL_ENABLED`) per-dial metrics.

---

## Recommendation Audit & Replay

Always-on persistence of every `/v1/recommend` call and radio batch with its full
candidate pool, for post-hoc "why was X surfaced at position N?" analysis and
offline replay.

### `GET /v1/recommend/audit/sessions`: Audit summaries

Paginated. **User-scoped if `user_id` is given, otherwise admin-gated (when admin
keys are configured).**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | – | Filter by user |
| `surface` | string | – | Filter by surface (e.g. `radio`) |
| `since_days` | int | – | 1-365 lookback |
| `limit` | int | 50 | 1-200 |
| `offset` | int | 0 | – |

### `GET /v1/recommend/audit/stats`: Audit storage stats

Admin-gated (when admin keys are configured). Total counts, storage estimate,
retention/cap settings.

### `GET /v1/recommend/audit/{request_id}`: Full audit detail

User-scoped (against the audited user). All persisted candidates with raw + final
scores, positions, reranker actions, and feature vectors.

### `GET /v1/recommend/audit/{request_id}/track/{track_id}`: Single candidate audit

User-scoped. Feature vector, sources, and reranker actions for one candidate.

### `POST /v1/recommend/audit/{request_id}/replay`: Replay vs current ranker

User-scoped. Re-ranks the audited request against the loaded model.

```json
{"mode": "rerank_only"}
```

`mode` is `rerank_only` (re-score persisted feature vectors, cheap) or `full`
(rebuild features live); `422` for any other value. Returns per-track
`rank_deltas` plus a `summary` (top-10 overlap, Kendall's τ, avg |Δrank|, etc.).

---

## Radio

Adaptive, stateful radio sessions. Sessions adapt in real-time to skip/like/dislike
feedback (sent via `POST /v1/events` with `context_type=radio`,
`context_id=<session_id>`).

### `POST /v1/radio/start`: Start a session

User-scoped (on body `user_id`). Returns the first batch as `201`. Logs
impressions + fire-and-forget audit.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | string | *(required)* | 1-128 |
| `seed_type` | enum | *(required)* | `track`, `artist`, `playlist` |
| `seed_value` | string | *(required)* | Track ID, artist name, or playlist ID (1-512) |
| `count` | int | 10 | 1-50 |
| `discovery` | float | 0.3 | Continuous dial 0.0–1.0 |
| `mode` | string | – | `familiar`, `balanced`, `discovery`, `deep_discovery` |
| `device_type` | string | – | Device context |
| `output_type` | string | – | Audio-output context |
| `location_label` | string | – | Location context |
| `hour_of_day` | int 0-23 | – | Local hour |
| `day_of_week` | int 1-7 | – | ISO day |

```json
{
  "session_id": "uuid", "seed_type": "track", "seed_value": "track_id_123",
  "seed_display_name": "Song Title - Artist", "tracks": [ /* track objects */ ]
}
```

`404` (user/seed not found), `422` (no embedding/candidates).

### `GET /v1/radio/{session_id}/next`: Next batch

User-scoped (session's user). Posture/context params are updatable per call. Logs
impressions + audit. `404` if the session expired.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `count` | int | 10 | 1-50 |
| `discovery` | float | – | 0.0–1.0 |
| `mode` | string | – | `familiar`, `balanced`, `discovery`, `deep_discovery` |
| `device_type` / `output_type` / `location_label` | string | – | Updated context |
| `hour_of_day` | int 0-23 | – | Updated hour |
| `day_of_week` | int 1-7 | – | Updated day |

```json
{"session_id": "uuid", "total_served": 20, "tracks": [ /* track objects */ ]}
```

### `DELETE /v1/radio/{session_id}`: Stop a session

User-scoped. `404` if expired.

```json
{"status": "stopped", "session_id": "uuid"}
```

### `GET /v1/radio`: List active sessions

**User-scoped if `user_id` is given, otherwise admin-gated (when admin keys are
configured).**

| Param | Description |
|-------|-------------|
| `user_id` | Filter by user |

```json
{"active_sessions": 3, "sessions": [ /* session metadata */ ]}
```

---

## Playlists

### `POST /v1/playlists`: Generate a playlist

Daily-idempotent (issue #89): same caller + same body params within a UTC day
returns the existing playlist (`200`) instead of duplicating it (`201` on a fresh
generation). Cache key =
`sha256(api_key_hash | strategy | seed_track_id | params | max_tracks | UTC-day)`;
`name` is excluded. `503` if the strategy is unavailable; `400` for bad input.

| Query param | Default | Effect |
|-------------|---------|--------|
| `refresh` | `false` | `true` bypasses the cache and forces regeneration (always `201`) |

Body (`PlaylistCreate`): `name` (1-255, required), `strategy`, `seed_track_id`,
`params` (dict), `max_tracks` (5-100, default 25).

| Strategy | Required | Description |
|----------|----------|-------------|
| `flow` | `seed_track_id` | Smooth BPM/energy chain from seed |
| `mood` | `params.mood` | Mood tag + energy arc. `params.mood` ∈ `happy`, `sad`, `aggressive`, `relaxed`, `party` |
| `energy_curve` | `params.curve` | Energy profile: `ramp_up`, `cool_down`, `ramp_up_cool_down`, `steady_high`, `steady_low` |
| `key_compatible` | `seed_track_id` | Camelot-wheel harmonic chaining |
| `path` | `seed_track_id` + `params.target_track_id` | **Song Path**: slerp-interpolates between two 64-dim embeddings; nearest unused track at each waypoint. Both tracks must have an `embedding` |
| `text` | `params.prompt` | **CLAP Text Prompt**: ranks by cosine similarity to the encoded prompt. Requires `CLAP_ENABLED=true`; `503` until some tracks have `clap_embedding` |

```bash
curl -X POST http://localhost:8000/v1/playlists \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "Rainy Coffee Shop Jazz", "strategy": "text", "max_tracks": 20,
       "params": {"prompt": "melancholic piano at 2am with light rain"}}'
```

`201`/`200` returns the playlist with a `tracks` array (`position`, `track_id`,
`media_server_id`, audio features, metadata).

### `GET /v1/playlists`: List playlists

Paginated.

| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 20 | 1-100 |
| `offset` | 0 | – |
| `strategy` | – | Filter by strategy |

### `GET /v1/playlists/{playlist_id}`: Get playlist with tracks

`404` if missing.

### `DELETE /v1/playlists/{playlist_id}`: Delete playlist

`204`. Admin required (when admin keys are configured) only if the caller is not
the playlist's creator.

---

## Discovery

### `GET /v1/discovery`: List discovery requests

Paginated.

| Param | Default | Description |
|-------|---------|-------------|
| `user_id` | – | Filter by user |
| `status` | – | Filter by status |
| `limit` | 50 | 1-200 |
| `offset` | 0 | – |

### `POST /v1/discovery/run`: Trigger discovery pipeline

Admin-gated (when admin keys are configured). Last.fm similar artists (+ optional
AcousticBrainz) → Lidarr. Returns `{"status": "error", ...}` with HTTP 200 if not
configured.

### `GET /v1/discovery/stats`: Discovery statistics

Status counts and daily limits.

---

## Fill Library

Queries AcousticBrainz Lookup for tracks matching each user's taste profile,
groups by album, and sends best-matching albums to Lidarr for FLAC download.
Requires `FILL_LIBRARY_ENABLED=true`, `AB_LOOKUP_URL`, `LIDARR_URL`,
`LIDARR_API_KEY`.

### `POST /v1/fill-library/run`: Trigger pipeline

Admin-gated (when admin keys are configured).

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `max_albums` | int | config (20) | 1-100; override max albums per run |

```json
{"status": "completed", "result": {"users_processed": 2, "albums_queued": 5,
 "albums_skipped": 12, "tracks_matched": 347, "tracks_no_album": 23, "errors": 0}}
```

### `GET /v1/fill-library`: List requests

Paginated.

| Param | Default | Description |
|-------|---------|-------------|
| `user_id` | – | Filter by user |
| `status` | – | `pending`, `artist_added`, `album_monitored`, `sent`, `skipped`, `failed` |
| `limit` | 50 | 1-200 |
| `offset` | 0 | – |

### `GET /v1/fill-library/stats`: Statistics

```json
{"enabled": true, "total": 42, "by_status": {"sent": 28, "skipped": 10, "failed": 4},
 "today_count": 5, "max_per_run": 20, "max_distance": 0.15, "avg_distance_sent": 0.0923}
```

---

## Charts

Last.fm-sourced charts with library matching, cover art, daily snapshots, and
optional auto-download. Rebuilt on a daily cron (`CHARTS_CRON`, default
`0 3 * * *` = 03:00 UTC) or on demand. One snapshot is stamped per calendar day.

### `GET /v1/charts`: List available charts

All chart type + scope combos (latest-snapshot entry counts).

### `GET /v1/charts/stats`: Chart statistics

Latest-snapshot stats: total entries, match rate, retained `snapshot_count`,
and the build schedule — `schedule_cron`, `schedule_label` (e.g. `daily 03:00
UTC`), and `next_run_at`.

### `GET /v1/charts/{chart_type}`: Chart entries

Paginated. Serves the latest or a historical snapshot, optionally with position
deltas. (Re-matches against the library when serving the latest snapshot.)

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `chart_type` | path | – | `top_tracks` or `top_artists` |
| `scope` | string | `global` | `global`, genre tag, or country name |
| `limit` | int | 100 | 1-200 |
| `offset` | int | 0 | – |
| `as_of` | date | – | `YYYY-MM-DD` to fetch a historical snapshot |
| `compare` | string | – | `"Nd"` (e.g. `7d`) to include position deltas |

Each entry includes `in_library`, `matched_track_id`, `image_url`, and a
`library` object (`track_id`, `media_server_id`, `cover_url`, metadata) when
matched. Prefer `library.cover_url`, fall back to `image_url`.

When `compare=Nd` is set, the response adds `compared_to` (the snapshot date
diffed against, or `null` if none is old enough) and each entry gains
`position_change` (positive = climbed, `null` when new) and `previously` (prior
position, `null` = new/re-entry). Deltas are relative to the served snapshot, so
`compare` composes with `as_of`.

### `GET /v1/charts/{chart_type}/snapshots`: Distinct snapshot dates

| Param | Default | Description |
|-------|---------|-------------|
| `scope` | `global` | Chart scope |

### `GET /v1/charts/{chart_type}/track/{artist}/{title}/history`: Position trajectory

Position history for one entry over snapshots. (`title` is ignored for
`top_artists`.)

| Param | Default | Description |
|-------|---------|-------------|
| `scope` | `global` | Chart scope |

### `POST /v1/charts/build`: Trigger chart rebuild

Admin-gated (when admin keys are configured). Fetches fresh charts, matches to
library, optionally auto-adds to Lidarr.

### `POST /v1/charts/download`: Download a chart track

By `position`, or by `artist_name` + `track_title`. `503` if no download backend
configured.

```json
{"chart_type": "top_tracks", "scope": "global", "position": 0}
```

Body (`ChartDownloadRequest`): `chart_type` (default `top_tracks`), `scope`
(default `global`), `position` (≥0), `artist_name`, `track_title`: supply either
`position` OR `artist_name`+`track_title`.

---

## Downloads

Download proxy across multiple backends: **spotdl-api** (YouTube Music),
**streamrip-api** (Qobuz/Tidal/Deezer/SoundCloud lossless), **Spotizerr** (legacy
fallback), **slskd** (Soulseek). `POST /v1/downloads` walks the configured
`individual` chain (see [Download Routing Configuration](#download-routing-configuration))
and records every backend attempt in the request's `attempts` log.

### `GET /v1/downloads/search`: Single-backend search (legacy)

Hits the single backend selected by `DEFAULT_DOWNLOAD_CLIENT`. `503` if downloads
are disabled.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `q` | string | *(required)* | Search query |
| `limit` | int | 25 | 1-100 |

### `GET /v1/downloads/search/multi`: Multi-backend parallel search

Runs `search` against every backend in `parallel_search_backends` concurrently
(per-backend timeout). Each result carries an opaque `download_handle` to POST
back to `/v1/downloads/from-handle`. Results are annotated with local-library
matches.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `q` | string | *(required)* | Search query |
| `limit` | int | 25 | 1-100, hits per backend |
| `backends` | string | from config | CSV override (`spotdl,streamrip,slskd`) |
| `timeout_ms` | int | from config | Per-backend timeout, 500-30000 |

```json
{
  "query": "Radiohead Creep", "limit": 5, "timeout_ms": 5000,
  "groups": [
    {"backend": "streamrip", "ok": true, "results": [
      {"backend": "streamrip", "title": "Creep", "artist": "Radiohead", "album": "Pablo Honey",
       "quality": "hires",
       "download_handle": {"backend": "streamrip", "service": "qobuz", "service_id": "33933680", "title": "Creep"}}]},
    {"backend": "spotdl", "ok": false, "error": "not configured", "results": []}
  ]
}
```

### `GET /v1/downloads/search/artist`: Artist search with discography

streamrip-only. Returns artists plus their full album discography.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `q` | string | *(required)* | Search query |
| `limit` | int | 2 | 1-10 artists |
| `albums_per_artist` | int | 100 | 1-300 |
| `backend` | string | `streamrip` | Backend |

### `GET /v1/downloads/album-tracks`: Lazy-load album tracks

streamrip-only. `503` if streamrip not configured.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `service` | string | *(required)* | Streaming service |
| `album_id` | string | *(required)* | Album ID |
| `backend` | string | `streamrip` | Backend |

### `POST /v1/downloads`: Download a track via the cascade

Walks the active `individual` chain. Accepts a `spotify_id` directly, or
`artist_name` + `track_title` (each adapter resolves internally). `503` if no
backend; `502` if every chain backend fails (DB row still persisted with the full
`attempts` log).

```json
{"spotify_id": "70LcF31zb1H0PyJoS1Sx3y", "track_title": "Creep", "artist_name": "Radiohead"}
```

Body (`DownloadCreateRequest`): `spotify_id` (required, 1-64), `track_title`,
`artist_name`, `album_name`, `cover_url`.

```json
{
  "id": 42, "spotify_id": "70Lc...", "task_id": "abc123", "status": "downloading",
  "source": "streamrip", "track_title": "Creep", "artist_name": "Radiohead",
  "attempts": [
    {"backend": "spotdl", "success": false, "status": "error", "error": "spotdl-api HTTP 503", "duration_ms": 1240},
    {"backend": "streamrip", "success": true, "status": "downloading", "task_id": "abc123", "quality": "hires", "duration_ms": 890}
  ]
}
```

Status values: `pending`, `downloading`, `queued`, `duplicate`, `completed`,
`error`.

### `POST /v1/downloads/from-handle`: Download a specific multi-search result

Bypasses the cascade: the user already chose a backend via `/search/multi`. Pass
the opaque `handle` dict verbatim. `502` on failure; the `attempts` array has a
single entry.

```json
{"handle": {"backend": "slskd", "username": "peer123", "filename": "Radiohead/Creep.flac", "size": 42118921},
 "track_title": "Creep", "artist_name": "Radiohead"}
```

Body (`DownloadFromHandleRequest`): `handle` (required dict), `track_title`,
`artist_name`, `album_name`, `cover_url`.

### `GET /v1/downloads/status/{task_id}`: Check progress

Proxies the backend's task progress and opportunistically updates the DB row.

### `GET /v1/downloads/queue`: Live queue snapshot

Buckets: `in_flight` / `recent_completed` / `recent_failed`, with live progress
probes.

| Param | Default | Description |
|-------|---------|-------------|
| `recent_limit` | 10 | 0-50 |
| `in_flight_limit` | 50 | 1-200 |

### `GET /v1/downloads/stats`: Per-backend telemetry

Aggregates `download_requests` over a window into per-backend counts.

| Param | Default | Description |
|-------|---------|-------------|
| `days` | 30 | 1-365 |

```json
{
  "window_days": 30,
  "backends": [
    {"backend": "spotdl", "total": 73, "success": 58, "failure": 12, "in_flight": 3, "success_rate": 0.829},
    {"backend": "streamrip", "total": 41, "success": 39, "failure": 2, "in_flight": 0, "success_rate": 0.951}
  ]
}
```

`success_rate` is `null` when no terminal results are in the window.

### `DELETE /v1/downloads/{download_id}`: Cancel / dismiss a download

Marks the DB row `cancelled` (no upstream abort). `404` if not found.

### `GET /v1/downloads`: Download history

Newest first. Each row includes the cascade `attempts` log (older rows may have
`attempts: null`).

| Param | Default | Description |
|-------|---------|-------------|
| `status` | – | Filter by status |
| `limit` | 50 | 1-200 |
| `offset` | 0 | – |

---

## Download Routing Configuration

Versioned, DB-persisted policy deciding which download backends are tried, in what
order, for which purpose, and at what quality. Defaults are seeded on first boot.
Changes take effect on the next request; no restart. **Every routing endpoint is
admin-gated (when admin keys are configured).**

Three independent priority chains plus parallel-search settings:

| Purpose | Used by | Default order |
|---------|---------|---------------|
| `individual` | `POST /v1/downloads` | spotdl → streamrip → spotizerr → slskd (slskd disabled) |
| `bulk_per_track` | charts auto-add, bulk top-tracks | streamrip → spotdl → spotizerr → slskd (slskd disabled) |
| `bulk_album` | discovery, fill_library | lidarr → streamrip (streamrip disabled) |

Each chain entry: `backend` (`spotdl`/`streamrip`/`spotizerr`/`slskd`/`lidarr`),
`enabled` (default true), `min_quality` (optional `lossy_low`/`lossy_high`/
`lossless`/`hires`), `timeout_s` (5-600, default 60). `parallel_search_backends`
controls `/search/multi`; `parallel_search_timeout_ms` is the per-backend timeout.
Quality tiers are ordinal (`lossy_low < lossy_high < lossless < hires`).

### `GET /v1/downloads/routing`: Active routing config

`404` if none.

```json
{
  "id": 3, "version": 3, "name": "Bumped slskd ahead of spotizerr",
  "config": {
    "individual": [
      {"backend": "spotdl", "enabled": true, "min_quality": null, "timeout_s": 60},
      {"backend": "streamrip", "enabled": true, "min_quality": "lossless", "timeout_s": 60}
    ],
    "bulk_per_track": [], "bulk_album": [],
    "parallel_search_backends": ["spotdl", "streamrip", "spotizerr"],
    "parallel_search_timeout_ms": 5000
  },
  "is_active": true, "created_at": 1714128000
}
```

### `GET /v1/downloads/routing/defaults`: Defaults + group metadata

Default config plus `groups` (the four UI sections: `individual`,
`bulk_per_track`, `bulk_album`, `parallel_search`).

### `GET /v1/downloads/routing/history`: Version history

| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 20 | 1-100 |
| `offset` | 0 | – |

### `PUT /v1/downloads/routing`: Save a new version (becomes active)

Body: `config` (`DownloadRoutingConfigData`), `name` (optional). `422` on
validation error.

### `POST /v1/downloads/routing/reset`: Reset to defaults

Creates a new version.

### `POST /v1/downloads/routing/activate/{version}`: Roll back

`404` if the version doesn't exist.

### `GET /v1/downloads/routing/export`: Export active config (JSON)

Downloadable JSON with `grooveiq_download_routing_config: true`. `404` if none.

### `POST /v1/downloads/routing/import`: Import config (JSON)

Body: `config` (dict), `name` (optional). `422` if invalid.

### `GET /v1/downloads/routing/{version}`: Specific historical version

`404` if not found.

---

## Soulseek

Text-search + file-level download via slskd (distinct from the Spotify-ID
`/downloads` group). All endpoints return `503` if slskd is not configured.

### `GET /v1/soulseek/search`: Search the Soulseek network

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `q` | string | *(required)* | Search query |
| `limit` | int | 20 | 1-100 |

### `POST /v1/soulseek/download`: Queue a peer file

Returns `201`; spawns a transfer watcher. `502` on slskd error.

```json
{"username": "peer123", "filename": "Radiohead/Creep.flac", "size": 42118921,
 "track_title": "Creep", "artist_name": "Radiohead"}
```

Body (`SoulseekDownloadRequest`): `username` (1-256), `filename` (1-1024), `size`
(>0), `track_title`, `artist_name`, `album_name`.

### `GET /v1/soulseek/downloads/{username}/{transfer_id}`: Transfer status

Real-time status from slskd. `404` if not found.

### `DELETE /v1/soulseek/downloads/{username}/{transfer_id}`: Cancel a transfer

`204`; updates the DB row. `404` if not found / already done.

### `GET /v1/soulseek/downloads`: Soulseek download history

Newest first.

| Param | Default | Description |
|-------|---------|-------------|
| `status` | – | Filter by status |
| `limit` | 50 | 1-200 |
| `offset` | 0 | – |

### `POST /v1/soulseek/bulk-download`: Bulk-download top Last.fm artists' tracks

Admin-gated (when admin keys are configured). Background job (`202`). `503` if
slskd / Last.fm off; `409` if a job is already running.

| Param | Default | Description |
|-------|---------|-------------|
| `max_artists` | 500 | 1-1000 |
| `tracks_per_artist` | 20 | 1-50 |

### `GET /v1/soulseek/bulk-download/status`: Bulk job status

Current / most-recent bulk-download job status.

### `POST /v1/soulseek/bulk-download/cancel`: Cancel the bulk job

Admin-gated (when admin keys are configured).

---

## Last.fm

All user-scoped.

### `POST /v1/users/{user_id}/lastfm/connect`: Connect account

Exchanges credentials for a session key; the password is discarded (never
stored). `503` if Last.fm / encryption key not enabled; `401` on auth failure;
`404` user.

```json
{"lastfm_username": "my_lastfm", "lastfm_password": "..."}
```

### `DELETE /v1/users/{user_id}/lastfm`: Disconnect

Clears credentials/cache and purges pending scrobbles.

### `POST /v1/users/{user_id}/lastfm/sync`: Force profile refresh

`503` if not enabled; `404` if no username.

### `POST /v1/users/{user_id}/lastfm/backfill`: Backfill missed scrobbles

Scans past `play_end` events and enqueues missed scrobbles. `503` if not enabled;
`400` on error.

### `GET /v1/users/{user_id}/lastfm/profile`: Get cached profile

Read-only cached Last.fm profile (top artists, tracks, genres). `404` if no
username.

---

## Artists

### `GET /v1/artists/{name}/meta`: Artist metadata

Rich Last.fm metadata (bio, tags, similar artists, top tracks, images) with local
library cross-referencing. `top_tracks` entries carry `matched_track_id` +
`media_server_id` when found locally. `503` if `LASTFM_API_KEY` missing; `404` if
no data.

---

## News

Reddit-sourced music news, fetched on a schedule, cached in memory, and scored
per-user at query time. **Experimental and off by default**: the backend is
implemented but not yet validated, and the dashboard renders a "Coming Soon"
placeholder until you set `NEWS_ENABLED=true`.

### `POST /v1/news/refresh`: Trigger a cache refresh

Background (`202`). `503` if news disabled.

```json
{"status": "refresh_started"}
```

### `GET /v1/news/{user_id}`: Personalized news feed

User-scoped. `503` if news disabled.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 25 | 1-100 |
| `tag` | string | – | `FRESH`, `NEWS`, `DISCUSSION` (≤50 chars) |
| `subreddit` | string | – | Filter to a subreddit (≤100 chars) |

```json
{
  "user_id": "aBc...", "total": 25, "cache_age_minutes": 12, "cache_stale": false,
  "items": [
    {"id": "t3_abc123", "title": "[FRESH] Kendrick Lamar - New Song",
     "url": "https://youtube.com/watch?v=...", "reddit_url": "https://www.reddit.com/r/hiphopheads/comments/abc123/...",
     "subreddit": "hiphopheads", "score": 4521, "num_comments": 342, "created_utc": 1712000000,
     "age_hours": 3.5, "flair": "FRESH", "is_fresh": true, "parsed_artists": ["Kendrick Lamar"],
     "relevance_score": 0.92, "relevance_reasons": ["artist_match", "genre_match", "fresh"]}
  ]
}
```

`relevance_reasons` (`artist_match`, `genre_match`, `fresh`, `high_engagement`)
enables "Because you listen to X" badges.

---

## Pipeline & Stats

All endpoints in this section are admin-gated (when admin keys are configured).

### `GET /v1/stats`: Dashboard aggregate stats

Total events/users/tracks/playlists, 24h/1h event counts, event-type breakdown,
top tracks, latest scan status, library coverage.

### `POST /v1/pipeline/run`: Trigger pipeline manually

Background; won't start if one is already running.

### `POST /v1/pipeline/reset`: Reset and rebuild

Clears sessions/interactions/profiles and rebuilds from raw events.

### `GET /v1/pipeline/status`: Run history

| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 10 | 1-50 |

Current run + recent history with per-step timing, status, metrics, errors, and
`config_version`. Steps (in order): `sessionizer`, `track_scoring`,
`taste_profiles`, `collab_filter`, `ranker`, `session_embeddings`,
`lastfm_cache`, `sasrec`, `session_gru`, `music_map`.

### `GET /v1/pipeline/stream`: SSE stream

`text/event-stream`. Event types: `pipeline_start`, `step_start`, `step_complete`,
`step_failed`, `pipeline_end`. 30s keepalive; `429` on subscriber overflow.

```bash
curl -N http://localhost:8000/v1/pipeline/stream -H "Authorization: Bearer YOUR_API_KEY"
```

### `GET /v1/pipeline/models`: ML model readiness

Trained/not-trained status + key metrics for all models (ranker, collab_filter,
session_embeddings, sasrec, session_gru, lastfm_cache). Includes ranker
`feature_importances`.

### `GET /v1/pipeline/stats/sessionizer`: Session statistics

Total sessions, avg duration/tracks/skip rate, distributions.

### `GET /v1/pipeline/stats/scoring`: Scoring statistics

Score distribution, signal breakdown, top/bottom tracks.

### `GET /v1/pipeline/stats/taste_profiles`: Profile statistics

Users with/without computed profiles.

### `GET /v1/pipeline/stats/events`: Event ingest rate

15-minute buckets over 24h (for sparklines).

### `GET /v1/pipeline/stats/activity`: Activity timeline

Event counts by type.

| Param | Default | Description |
|-------|---------|-------------|
| `days` | 7 | 1-30 |

### `GET /v1/pipeline/stats/engagement`: Engagement leaderboard

Per-user 30-day metrics (top 50): total events, plays, skip rate, unique tracks,
diversity.

---

## Algorithm Configuration

Versioned, DB-persisted configuration for all pipeline tunables. **Every endpoint
is admin-gated (when admin keys are configured) EXCEPT `GET /v1/algorithm/modes`**,
which any authenticated key may read.

### `GET /v1/algorithm/config`: Active config

`404` if none.

### `GET /v1/algorithm/config/defaults`: Defaults + group metadata

Default values with group metadata and ge/le constraints (for GUI rendering).

### `GET /v1/algorithm/modes`: Discovery-dial presets (non-admin)

**Any authenticated key** (not admin). Read-only `modes` group from the in-memory
active config: the discovery-dial preset matrix (`familiar`, `balanced`,
`discovery`, `deep_discovery`, plus `dial_anchors`) used by end-user surfaces to
render mode pickers / shelves.

### `GET /v1/algorithm/config/history`: Version history

| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 20 | 1-100 |
| `offset` | 0 | – |

### `PUT /v1/algorithm/config`: Save a new version (becomes active)

Body: `config` (`AlgorithmConfigData`), `name` (optional).

```json
{"config": {"track_scoring": {"w_like": 2.5}, "reranker": {"freshness_boost": 0.15}}, "name": "More exploration"}
```

### `POST /v1/algorithm/config/reset`: Reset to defaults

Creates a new version.

### `POST /v1/algorithm/config/activate/{version}`: Activate a version (rollback)

`404` if not found.

### `GET /v1/algorithm/config/export`: Export active config (JSON)

Downloadable JSON with `grooveiq_algorithm_config: true`. `404` if none.

### `POST /v1/algorithm/config/import`: Import config (JSON)

Body: `config` (dict), `name` (optional). Missing keys get defaults; invalid
values `422`.

### `GET /v1/algorithm/config/{version}`: Specific version

`404` if not found.

---

## Lidarr Backfill

Drains Lidarr's `/wanted/missing` (and optionally `/wanted/cutoff`) queue through
the streamrip download pipeline. Versioned config + per-album state machine.
**Every endpoint is admin-gated (when admin keys are configured).**

### Config CRUD

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/lidarr-backfill/config` | GET | Active config (`404` if none) |
| `/v1/lidarr-backfill/config/defaults` | GET | Defaults + group metadata |
| `/v1/lidarr-backfill/config/history` | GET | Version history (`limit` 20/1-100, `offset` 0) |
| `/v1/lidarr-backfill/config` | PUT | Save new version; reapplies scheduler |
| `/v1/lidarr-backfill/config/reset` | POST | Reset to defaults; reapplies scheduler |
| `/v1/lidarr-backfill/config/activate/{version}` | POST | Roll back (`404` if missing) |
| `/v1/lidarr-backfill/config/export` | GET | Export JSON (`grooveiq_lidarr_backfill_config: true`) |
| `/v1/lidarr-backfill/config/import` | POST | Import JSON; reapplies scheduler (`422` invalid) |
| `/v1/lidarr-backfill/config/{version}` | GET | Specific version (`404` if missing) |

PUT/import bodies mirror the algorithm-config shape: `config`
(`LidarrBackfillConfigData`) + optional `name`.

### Queue & operations

### `GET /v1/lidarr-backfill/requests`: List rows

| Param | Default | Description |
|-------|---------|-------------|
| `status` | – | Filter by status |
| `artist` | – | Artist substring |
| `limit` | 50 | 1-500 |
| `offset` | 0 | – |

### `POST /v1/lidarr-backfill/requests/{request_id}/retry`: Retry a row

Resets attempts and re-queues. `404` if not found.

### `POST /v1/lidarr-backfill/requests/{request_id}/skip`: Skip a row

Marks it `permanently_skipped`. `404` if not found.

### `DELETE /v1/lidarr-backfill/requests/{request_id}`: Forget a row

Re-picked on the next tick. `404` if not found.

### `POST /v1/lidarr-backfill/requests/reset`: Bulk-reset by scope

Body: `{"scope": ...}` where scope ∈ `failed`, `no_match`, **`search_error`**,
`permanently_skipped`, `all`. `400` if missing/invalid.

### `POST /v1/lidarr-backfill/run`: Run one tick now

Returns metrics.

### `POST /v1/lidarr-backfill/preview`: Preview match decisions

Body (optional): `limit` (default 20, clamped 1-100), `config_override` (dict).
Runs match logic against the live missing queue without persisting (calibration
tool).

### `GET /v1/lidarr-backfill/stats`: Dashboard stats

Queue counts, capacity, ETA, last/next tick.

---

## Lyrics (operator)

Operator side of the lyrics-acquisition drain. **Every endpoint is admin-gated
(when admin keys are configured).** (Per-track display is
`GET /v1/tracks/{track_id}/lyrics`; see [Tracks & Library](#tracks--library).)

### `GET /v1/lyrics/stats`: Drain stats

Queue counts, coverage, capacity, ETA.

### `GET /v1/lyrics/requests`: List drain queue rows

| Param | Default | Description |
|-------|---------|-------------|
| `status` | – | Filter by status |
| `limit` | 50 | 1-200 |
| `offset` | 0 | – |

### `POST /v1/lyrics/run`: Run one drain tick now

### `POST /v1/lyrics/requests/{request_id}/retry`: Re-queue a row

`404` if not found.

### `POST /v1/lyrics/requests/{request_id}/skip`: Permanently skip a row

`404` if not found.

### `DELETE /v1/lyrics/requests/{request_id}`: Forget a row

`404` if not found.

### `POST /v1/lyrics/requests/reset`: Bulk re-queue by scope

Body: `{"scope": ...}` (e.g. `no_lyrics`). `422` if invalid.

---

## Integrations

### `GET /v1/integrations/status`: Integration connectivity

Admin-gated (when admin keys are configured). Probes all external services in
parallel and returns per-service `configured`/`connected` status. Probes **9**
services: `spotdl_api`, `streamrip_api`, `lidarr`, `slskd`,
`acousticbrainz_lookup`, `lastfm`, `media_server`, `lyrics_api`, `lrclib`.

```json
{
  "checked_at": 1712000000,
  "integrations": {
    "spotdl_api": {"configured": true, "connected": true, "version": "1.0.0"},
    "streamrip_api": {"configured": true, "connected": true},
    "lidarr": {"configured": true, "connected": true, "version": "2.8.5.4875"},
    "slskd": {"configured": false},
    "acousticbrainz_lookup": {"configured": false},
    "lastfm": {"configured": true, "connected": true, "scrobbling": true},
    "media_server": {"configured": true, "type": "navidrome", "connected": true},
    "lyrics_api": {"configured": false},
    "lrclib": {"configured": true, "connected": true}
  }
}
```

---

## API Call Log

Per-user HTTP request/response history (issues #79, #81). Surfaced under
Monitor → User Diagnostics.

### `GET /v1/users/{user_id}/api-calls`: Per-user log

User-scoped. Paginated.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 50 | 1-500 |
| `offset` | int | 0 | – |
| `method` | string | – | HTTP method filter |
| `path_contains` | string | – | Path substring filter |
| `status` | int | – | Status code (100-599) |
| `include_events` | bool | true | Include POST `/v1/events*` |
| `since_minutes` | int | – | Time window (≥1) |
| `source` | string | – | `browser`, `mobile`, `cli`, `other` (`422` if invalid) |
| `client_ip_contains` | string | – | Client-IP substring filter |

### `GET /v1/api-calls/{call_id}`: Single call detail

User-scoped on the row's `user_id` (if present). Full request body + response
summary. `404` if not found.

### `GET /v1/api-calls`: List across all users

Admin-gated (when admin keys are configured). Same filters as the per-user log
plus `user_id`.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 100 | 1-1000 |
| `offset` | int | 0 | – |
| `method` / `path_contains` / `status` | – | – | As above |
| `user_id` | string | – | Filter by user |
| `include_events` | bool | true | Include event POSTs |
| `since_minutes` | int | – | Time window (≥1) |
| `source` | string | – | `browser`, `mobile`, `cli`, `other` |
| `client_ip_contains` | string | – | Client-IP substring filter |

### `GET /v1/api-calls/stats/summary`: Log totals + retention

Admin-gated (when admin keys are configured). Totals plus retention settings.

---

## Admin

### `GET /v1/admin/analysis-health`: Library invariant checks

Admin-gated (when admin keys are configured). Runs all library-wide
`track_features` invariants and returns per-check pass/fail plus an overall
status.

```json
{
  "checked_at": 1712000000, "overall_status": "ok",
  "summary": {"total": 12, "ok": 11, "fail": 0, "warn": 1, "skipped": 0, "error": 0},
  "checks": [ /* per-check results */ ]
}
```

---

## Configuration Reference

All settings come from environment variables or `.env`. The canonical, fully
commented list is [`.env.example`](../.env.example). This section summarizes the
groups; defaults below match `app/core/config.py`.

Several settings are **gated**: a feature is only "enabled" when its toggle AND
its required connection settings are present (e.g. `slskd_enabled` requires
`SLSKD_ENABLED=true` AND `SLSKD_URL` AND `SLSKD_API_KEY`; `fill_library_enabled`
requires `FILL_LIBRARY_ENABLED=true` AND `AB_LOOKUP_URL` AND `LIDARR_URL` AND
`LIDARR_API_KEY`; `lastfm_user_enabled` requires `LASTFM_ENABLED=true` AND
`LASTFM_API_KEY` AND `LASTFM_API_SECRET`).

### Core & security

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_ENV` | `production` | `development` / `production`; gates auth enforcement |
| `SECRET_KEY` | *(required in prod)* | Internal signing secret (placeholder values rejected in prod) |
| `API_KEYS` | *(required in prod)* | CSV bearer tokens; each ≥32 chars |
| `ADMIN_API_KEYS` | `""` | CSV admin keys; **empty = every key is admin** |
| `API_KEY_USERS` | `""` | Bind keys to users: `"key1:alice,bob;key2:charlie"` |
| `DISABLE_AUTH` | `false` | Skip auth (dev only; ignored in prod) |
| `RATE_LIMIT_EVENTS` | `300` | Event-ingest req/min/key |
| `RATE_LIMIT_DEFAULT` | `200` | Other req/min/key |
| `REDIS_URL` | `""` | Optional cross-process rate-limit backend |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Host-header allowlist (CSV) |
| `CORS_ORIGINS` | `""` | CORS origins (CSV; empty = same-origin) |
| `ENABLE_DOCS` | `false` | Expose `/docs` + `/redoc` |
| `USER_ID_PATTERN` | `^[A-Za-z0-9]{20,22}$` | Regex enforced on every `user_id` |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./grooveiq.db` | SQLite or `postgresql+asyncpg://...` (Docker images use the `/data/` path) |
| `DB_POOL_SIZE` | `5` | Connection pool size |
| `DB_MAX_OVERFLOW` | `10` | Pool overflow |
| `DB_ECHO` | `false` | Log all SQL |

### Audio analysis

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSIC_LIBRARY_PATH` | `/music` | Library root (read-only bind mount) |
| `ANALYSIS_WORKERS` | CPU count − 1 | Parallel analysis workers |
| `ANALYSIS_BATCH_SIZE` | `50` | Tracks per batch |
| `ANALYSIS_TIMEOUT` | `300` | Per-file timeout (s) |
| `RESCAN_INTERVAL_HOURS` | `6` | Auto-rescan interval |
| `ANALYSIS_GPU` / `ANALYSIS_GPU_BACKEND` | `false` / `""` | GPU ONNX inference (`cuda`/`openvino`) |
| `AUDIO_EXTENSIONS` | `.mp3,.flac,.ogg,.m4a,.wav,.aac,.opus,.wv` | Scanner file extensions |

(Additional GPU/ONNX threading knobs (`ANALYSIS_GPU_BATCH_SIZE`,
`ANALYSIS_GPU_WORKERS`, `ANALYSIS_ONNX_INTRA_THREADS`, `ANALYSIS_ONNX_INTER_THREADS`,
`ANALYSIS_OMP_THREADS`) exist; see `.env.example`.)

### Recommendation pipeline & event ingestion

| Variable | Default | Description |
|----------|---------|-------------|
| `SESSION_GAP_MINUTES` | `30` | Inactivity gap that splits sessions |
| `SESSION_MIN_EVENTS` | `2` | Drop sessions below this |
| `TASTE_PROFILE_DECAY_DAYS` | `30` | Recency half-life |
| `SCORING_INTERVAL_HOURS` | `1` | Pipeline cadence |
| `EVENT_BATCH_MAX` | `50` | Max events per batch POST |
| `EVENT_RETENTION_DAYS` | `365` | Raw-event retention |
| `MIN_PLAY_PERCENTAGE` | `0.05` | Drop `play_end` below this completion |

### CLAP (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAP_ENABLED` | `false` | Enable CLAP audio + text encoders |
| `CLAP_MODEL_DIR` | `/data/models/clap` | ONNX + tokenizer dir |
| `CLAP_EMBEDDING_DIM` | `512` | Embedding dim (must match models) |
| `CLAP_AUDIO_SR` | `48000` | Audio sample rate |
| `CLAP_AUDIO_CLIP_SECONDS` | `10.0` | Clip length fed to the audio tower |

(Plus `CLAP_AUDIO_MODEL_FILE` / `CLAP_TEXT_MODEL_FILE` / `CLAP_TOKENIZER_FILE`
filenames and `CLAP_*_URL` auto-download URLs; see `.env.example`.)

### Lyrics (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `LYRICS_ENABLED` | `false` | Master toggle (drain + embedded read) |
| `LYRICS_LRCLIB_ENABLED` | `true` | Tier-2 LRCLIB lookups |
| `LYRICS_ASR_ENABLED` | `false` | Tier-3 ASR transcription |
| `LYRICS_API_URL` | `""` | lyrics-api (ASR) sidecar base URL |
| `LYRICS_DRAIN_POLL_MINUTES` | `5` | Drain tick cadence |
| `LYRICS_DRAIN_MAX_ATTEMPTS` | `3` | Then permanently skipped |

(Plus LRCLIB URL/UA, ASR timeout/VAD/instrumental/model, and drain
throttle/cooldown/backoff knobs; see `.env.example`.)

### Media server (Navidrome)

Navidrome is the only supported media server. The `plex` value and the
`MEDIA_SERVER_TOKEN` / `MEDIA_SERVER_LIBRARY_ID` fields are legacy and unsupported.

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDIA_SERVER_TYPE` | `""` | `navidrome` (empty = disabled) |
| `MEDIA_SERVER_URL` | `""` | Base URL (SSRF-validated) |
| `MEDIA_SERVER_USER` / `MEDIA_SERVER_PASSWORD` | `""` | Navidrome credentials |
| `MEDIA_SERVER_TOKEN` | `""` | Plex token (legacy/unsupported) |
| `MEDIA_SERVER_LIBRARY_ID` | `1` | Plex section ID (legacy/unsupported) |
| `MEDIA_SERVER_MUSIC_PATH` | `""` | Server's music root (path mapping) |
| `CREDENTIAL_ENCRYPTION_KEY` | `""` | Fernet key for media creds at rest |

### Last.fm

| Variable | Default | Description |
|----------|---------|-------------|
| `LASTFM_API_KEY` / `LASTFM_API_SECRET` | `""` | Last.fm API credentials |
| `LASTFM_ENABLED` | `false` | Per-user Last.fm features |
| `LASTFM_SCROBBLE_ENABLED` | `false` | Scrobbling |
| `LASTFM_SESSION_ENCRYPTION_KEY` | `""` | Fernet key for session keys at rest |
| `LASTFM_REFRESH_HOURS` | `6` | Profile-pull cadence |

### Discovery (Lidarr)

| Variable | Default | Description |
|----------|---------|-------------|
| `LIDARR_URL` / `LIDARR_API_KEY` | `""` | Lidarr connection (SSRF-validated) |
| `LIDARR_QUALITY_PROFILE_ID` / `LIDARR_METADATA_PROFILE_ID` | `1` / `1` | Lidarr profiles |
| `LIDARR_ROOT_FOLDER` | `/music` | Lidarr root folder |
| `DISCOVERY_CRON` | `0 3 * * *` | Discovery schedule |
| `DISCOVERY_MAX_REQUESTS_PER_DAY` | `500` | Daily Lidarr-add cap |
| `DISCOVERY_SIMILAR_LIMIT` | `20` | Similar artists per seed |

### Fill Library (AcousticBrainz + Lidarr)

| Variable | Default | Description |
|----------|---------|-------------|
| `AB_LOOKUP_URL` | `""` | AcousticBrainz Lookup base URL |
| `AB_LOOKUP_ENABLED` | `false` | AB discovery source |
| `AB_DISCOVERY_LIMIT` | `50` | Max tracks discovered per run |
| `FILL_LIBRARY_ENABLED` | `false` | Enable Fill Library |
| `FILL_LIBRARY_MAX_ALBUMS` | `20` | Albums queued per run |
| `FILL_LIBRARY_MAX_DISTANCE` | `0.15` | Max AB distance (lower = stricter) |
| `FILL_LIBRARY_CRON` | `0 4 * * *` | Schedule (4 AM UTC) |
| `FILL_LIBRARY_QUERY_LIMIT` | `500` | Max results per AB query |

### Charts

| Variable | Default | Description |
|----------|---------|-------------|
| `CHARTS_ENABLED` | `false` | Periodic chart builds (also needs `LASTFM_API_KEY`) |
| `CHARTS_CRON` | `0 3 * * *` | Daily build schedule, UTC (wall-clock cron; survives restarts) |
| `CHARTS_INTERVAL_HOURS` | `24` | Freshness window (h) for the Monitor staleness banner; cadence is `CHARTS_CRON` |
| `CHARTS_TOP_LIMIT` | `100` | Entries per chart (max 200) |
| `CHARTS_TAGS` / `CHARTS_COUNTRIES` | `""` | Genre tags / countries (CSV) |
| `CHARTS_LIDARR_AUTO_ADD` / `CHARTS_LIDARR_MAX_ADDS` | `false` / `50` | Auto-add chart artists to Lidarr |
| `CHARTS_SPOTIZERR_AUTO_ADD` / `CHARTS_SPOTIZERR_MAX_ADDS` | `false` / `50` | Auto-download unmatched chart tracks |

### Download backends

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_DOWNLOAD_CLIENT` | `spotdl` | Legacy single-backend toggle: `spotdl`/`streamrip`/`spotizerr` |
| `SPOTDL_API_URL` | `""` | spotdl-api base URL |
| `STREAMRIP_API_URL` | `""` | streamrip-api base URL |
| `SPOTIZERR_URL` / `SPOTIZERR_USERNAME` / `SPOTIZERR_PASSWORD` | `""` | Spotizerr (legacy fallback) |
| `SLSKD_URL` / `SLSKD_API_KEY` / `SLSKD_ENABLED` | `""` / `""` / `false` | Soulseek (slskd) |
| `SLSKD_SEARCH_TIMEOUT` / `SLSKD_PREFER_LOSSLESS` | `15` / `true` | slskd search behaviour |

> Streaming-service credentials (`SPOTIFY_*`, `QOBUZ_*`, `TIDAL_*`, `DEEZER_ARL`,
> `SOUNDCLOUD_CLIENT_ID`) and `STREAMRIP_*` quality/codec knobs are consumed by
> the **sidecar containers** (spotdl-api / streamrip-api), not by GrooveIQ
> itself; set them in `.env`/compose for those containers.

### Recommendation serving, audit & caching

| Variable | Default | Description |
|----------|---------|-------------|
| `RECO_AUDIT_ENABLED` | `true` | Audit-write master switch |
| `RECO_AUDIT_RETENTION_DAYS` | `90` | Auto-purge cutoff |
| `RECO_AUDIT_MAX_CANDIDATES` | `200` | Cap candidates persisted per request |
| `RECO_DIAL_EVAL_ENABLED` | `true` | Per-dial eval metrics on `/v1/stats/model` |
| `MIX_CACHE_ENABLED` | `true` | Serve recommend/mode from the SWR mix cache |
| `MIX_CACHE_FRESH_SECONDS` | `120` | Served as-is within this age |
| `MIX_CACHE_STALE_SECONDS` | `900` | Stale grace + 1 background rebuild |
| `MIX_PREWARM_RATE_LIMIT_PER_MIN` | `12` | Per (key,user) prewarm cap |
| `MIX_PREWARM_MAX_MODES` | `8` | Modes warmed per prewarm request |

(Additional dial-eval and mix-cache tuning knobs exist; see `app/core/config.py`.)

### News (Reddit)

| Variable | Default | Description |
|----------|---------|-------------|
| `NEWS_ENABLED` | `false` | Enable the news feed |
| `NEWS_INTERVAL_MINUTES` | `30` | Reddit fetch cadence |
| `NEWS_MAX_AGE_HOURS` | `48` | Discard older posts at query time |
| `NEWS_DEFAULT_SUBREDDITS` | `Music,hiphopheads,indieheads,...` | Subreddits to fetch (CSV) |
| `NEWS_MAX_POSTS_PER_SUB` | `50` | Posts per subreddit per cycle |

### API call logging

| Variable | Default | Description |
|----------|---------|-------------|
| `API_LOG_ENABLED` | `true` | Middleware-write master switch |
| `API_LOG_RETENTION_DAYS` | `7` | Purge cutoff |
| `API_LOG_INCLUDE_EVENTS` | `true` | Log POST `/v1/events` |
| `API_LOG_MAX_BODY_BYTES` | `4096` | Body-capture cap |
| `API_LOG_MAX_LIST_ITEMS` | `20` | Keep first N list items |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_JSON` | `true` | Structured JSON vs human-readable |
