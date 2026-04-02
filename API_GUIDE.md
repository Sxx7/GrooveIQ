# GrooveIQ API Guide

Base URL: `http://localhost:8000`

## Authentication

Every request (except `/health`) needs a Bearer token:

```
Authorization: Bearer YOUR_API_KEY
```

The key is whatever you set in `API_KEYS` in your `.env` file.

---

## Endpoints

### Health Check

```
GET /health
```

No auth required. Returns:

```json
{"status": "ok", "service": "grooveiq"}
```

---

### Events

#### Send a single event

```
POST /v1/events
Content-Type: application/json
Authorization: Bearer YOUR_API_KEY
```

**Body:**

```json
{
  "user_id": "alice",
  "track_id": "abc123",
  "event_type": "play_end",
  "value": 0.94,
  "context": "evening",
  "session_id": "sess-001",
  "client_id": "symfonium",
  "timestamp": 1775126504
}
```

| Field        | Type   | Required | Notes |
|---|---|---|---|
| `user_id`    | string | yes | 1–128 chars. Your media server's user ID. |
| `track_id`   | string | yes | 1–128 chars. Your media server's track ID. |
| `event_type` | string | yes | One of the types listed below. |
| `value`      | float  | no  | Meaning depends on event type (see below). |
| `context`    | string | no  | Max 64 chars. Free label like "workout", "sleep". |
| `session_id` | string | no  | Max 64 chars. Groups events from the same session. |
| `client_id`  | string | no  | Max 64 chars. Which app sent this. |
| `timestamp`  | int    | no  | Unix UTC. Defaults to server time. Must be within 24h past / 5min future. |

**Response:** `202 Accepted`

```json
{"accepted": 1, "rejected": 0, "errors": []}
```

---

#### Send a batch of events

```
POST /v1/events/batch
Content-Type: application/json
Authorization: Bearer YOUR_API_KEY
```

**Body:**

```json
{
  "events": [
    {"user_id": "alice", "track_id": "abc123", "event_type": "play_end", "value": 0.94},
    {"user_id": "alice", "track_id": "def456", "event_type": "skip", "value": 8.2},
    {"user_id": "alice", "track_id": "ghi789", "event_type": "like"}
  ]
}
```

- Max 50 events per batch.
- Each event is validated independently — bad ones are rejected, good ones still go through.

**Response:** `202 Accepted`

```json
{"accepted": 2, "rejected": 1, "errors": ["Event 3: invalid event_type 'foo'"]}
```

---

#### Query stored events

```
GET /v1/events
Authorization: Bearer YOUR_API_KEY
```

| Param      | Type   | Default | Notes |
|---|---|---|---|
| `user_id`  | string | —       | Filter by user |
| `track_id` | string | —       | Filter by track |
| `limit`    | int    | 50      | 1–500 |
| `offset`   | int    | 0       | Pagination |

**Example:** `GET /v1/events?user_id=alice&limit=10`

**Response:** `200 OK`

```json
[
  {
    "id": 1,
    "user_id": "alice",
    "track_id": "abc123",
    "event_type": "play_end",
    "value": 0.94,
    "context": null,
    "timestamp": 1775126504,
    "session_id": null
  }
]
```

Results are ordered by timestamp, newest first.

---

### Event Types Reference

| `event_type`   | `value` meaning                | Signal |
|---|---|---|
| `play_end`     | Completion ratio 0.0–1.0      | Positive if > 0.05 |
| `skip`         | Seconds elapsed at skip        | Negative (early skip = stronger) |
| `pause`        | Seconds elapsed                | Neutral |
| `resume`       | Seconds elapsed                | Neutral |
| `like`         | (none)                         | Strong positive |
| `dislike`      | (none)                         | Strong negative |
| `rating`       | Star rating 1–5                | Positive/negative by value |
| `playlist_add` | (none)                         | Strong positive |
| `queue_add`    | (none)                         | Moderate positive |
| `seek_back`    | Seconds jumped backward        | Strong positive (replaying) |
| `seek_forward` | Seconds skipped forward        | Mild negative |
| `repeat`       | (none)                         | Very strong positive |
| `volume_up`    | New volume 0–100               | Implicit positive |
| `volume_down`  | New volume 0–100               | Mild negative |

Notes:
- `play_end` with value < 0.05 is silently dropped (too short to count).
- Duplicate `(user_id, track_id, event_type)` within 2 seconds is accepted but deduplicated.

---

### Users

#### Create a user

```
POST /v1/users
Content-Type: application/json
Authorization: Bearer YOUR_API_KEY
```

**Body:**

```json
{
  "user_id": "alice",
  "display_name": "Alice"
}
```

| Field          | Type   | Required | Notes |
|---|---|---|---|
| `user_id`      | string | yes | 1–128 chars |
| `display_name` | string | no  | Max 255 chars |

**Response:** `201 Created`

```json
{"user_id": "alice", "display_name": "Alice", "created_at": 1775126504, "last_seen": null}
```

Returns `409 Conflict` if the user already exists.

> You don't need to pre-create users. They're auto-created on the first event.

---

#### Get a user

```
GET /v1/users/{user_id}
Authorization: Bearer YOUR_API_KEY
```

**Response:** `200 OK`

```json
{"user_id": "alice", "display_name": null, "created_at": 1775126504, "last_seen": 1775126519}
```

Returns `404` if the user doesn't exist.

---

### Library & Audio Analysis

#### Trigger a library scan

```
POST /v1/library/scan
Authorization: Bearer YOUR_API_KEY
```

No body needed. Starts scanning `MUSIC_LIBRARY_PATH` in the background.

**Response:** `202 Accepted`

```json
{"message": "Scan started", "scan_id": 1, "status": "running"}
```

Only one scan runs at a time. If a scan is already running, returns the existing scan ID.

---

#### Check scan progress

```
GET /v1/library/scan/{scan_id}
Authorization: Bearer YOUR_API_KEY
```

**Response:** `200 OK`

```json
{
  "scan_id": 1,
  "status": "running",
  "files_found": 4823,
  "files_analyzed": 1240,
  "files_failed": 3,
  "started_at": 1775126504,
  "ended_at": null,
  "last_error": null
}
```

Status is one of: `running`, `completed`, `failed`.

---

#### Get track audio features

```
GET /v1/tracks/{track_id}/features
Authorization: Bearer YOUR_API_KEY
```

**Response:** `200 OK`

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
    {"label": "happy", "confidence": 0.61}
  ],
  "analyzed_at": 1775126504,
  "analysis_version": "1.0"
}
```

Returns `404` if the track hasn't been analyzed yet (run a library scan first).

---

#### Get similar tracks

```
GET /v1/tracks/{track_id}/similar
Authorization: Bearer YOUR_API_KEY
```

| Param              | Type | Default | Notes |
|---|---|---|---|
| `limit`            | int  | 10      | 1–50 |
| `include_features` | bool | false   | Include full feature objects in results |

**Example:** `GET /v1/tracks/abc123/similar?limit=5&include_features=true`

Returns tracks ranked by acoustic similarity (BPM, energy, mode, then cosine similarity on embeddings).

---

## Error Responses

All errors follow this format:

```json
{"detail": "Error description here"}
```

| Code | Meaning |
|---|---|
| 401  | Missing or invalid API key |
| 404  | Resource not found |
| 409  | Conflict (e.g. user already exists) |
| 422  | Validation error (bad field values) |
| 429  | Rate limited |

---

## Quick Test

```bash
# Health check
curl http://localhost:8000/health

# Send an event
curl -X POST http://localhost:8000/v1/events \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"alice","track_id":"abc123","event_type":"play_end","value":0.94}'

# Query events
curl "http://localhost:8000/v1/events?user_id=alice" \
  -H "Authorization: Bearer YOUR_API_KEY"

# Trigger library scan
curl -X POST http://localhost:8000/v1/library/scan \
  -H "Authorization: Bearer YOUR_API_KEY"
```
