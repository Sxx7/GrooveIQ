# acousticbrainz-lookup: audio-feature discovery sidecar for GrooveIQ

`acousticbrainz-lookup` is an **optional add-on container** that gives GrooveIQ a
~29.5M-track discovery pool keyed by *audio characteristics*: BPM, energy, mood,
danceability, genre, with **zero external API dependency** after the initial data
download.

On first boot it ingests the [AcousticBrainz](https://acousticbrainz.org/) high-level
data dump into a local SQLite database, then serves a small REST API for
audio-feature **similarity search**. GrooveIQ sends a user's taste profile and gets
back "close enough" tracks the user doesn't own, which it can then route to Lidarr /
spotdl-api for download. It powers GrooveIQ's **Fill Library** pipeline and the
**AcousticBrainz discovery** source.

It runs as its own service in the main GrooveIQ `docker-compose.yml`. It listens on
**port 8200**, is reachable only on the internal Docker network, and has **no auth**.
GrooveIQ's auth layer fronts it. (Whether GrooveIQ *uses* it is gated by
`AB_LOOKUP_ENABLED`; see [§5](#5-deploy).)

```
┌──────────────────────────┐   POST /v1/search (taste profile)   ┌──────────────────────────┐
│  grooveiq                 │  ─────────────────────────────────▶ │  acousticbrainz-lookup   │
│  AB_LOOKUP_ENABLED=true   │   ◀───── matching tracks (MBIDs) ──  │  SQLite (~29.5M tracks)  │
│  AB_LOOKUP_URL=…:8200     │                                      │  port 8200, /data volume │
└──────────────────────────┘                                      └──────────────────────────┘
        same Docker network: no host ports, no auth
```

> **Note:** unlike the download sidecars (spotdl-api / streamrip-api / lyrics-api),
> this service has **no `/music` mount** and **no stale-mount (#123) readiness probe**.
> It operates entirely on its own `/data` SQLite database.

---

## 1. Ingestion (first boot only)

The AcousticBrainz high-level dump contains Essentia-computed audio features (BPM,
key, danceability, 7 mood classifiers, genre, instrumentalness, plus low-level
rhythm/tonal/loudness) keyed by MusicBrainz Recording ID (MBID). Ingestion runs once,
in a **background daemon thread** at startup, and is **resumable**: if the container
restarts mid-ingest it picks up from the next unprocessed archive (tracked in an
`ingestion_state` table).

Pipeline (`ingest.py`):

1. **Download** each zstandard-compressed tar archive from
   `data.metabrainz.org` (streamed to `/data/tmp/`)
2. **Decompress** the `.tar.zst` and iterate entries with `tarfile`
3. **Parse** each high-level JSON file, extract ~18 fields per track
4. **Batch INSERT** into SQLite (5000 rows/batch, `INSERT OR IGNORE` for duplicate MBIDs)
5. **Index** after the bulk load completes (much faster than maintaining during insert)
6. **Cleanup**: delete each archive after processing; remove `/data/tmp/` when done

| Mode | Archives | Download | DB after ingest | Time |
|---|---|---|---|---|
| Full (default) | 30 (`acousticbrainz-highlevel-json-20220623-{0..29}.tar.zst`) | ~39 GB | ~6–8 GB | ~2–4h |
| `SAMPLE_MODE=true` | 1 (sample dump) | ~85 MB | <1 GB | <5 min |

Two derived columns are computed at ingest time:
`energy = 0.3·norm_bpm + 0.4·loudness + 0.3·mood_aggressive`, and
`valence = mood_happy` (probability).

---

## 2. Configuration (env vars)

Only two env vars are read by the container:

| Variable | Default | Description |
|---|---|---|
| `SAMPLE_MODE` | `false` | When truthy (`true`/`1`/`yes`), ingest the 1-archive sample dump instead of the full 30-archive dump. |
| `LOG_LEVEL` | `INFO` | Log level (`DEBUG`/`INFO`/`WARNING`/`ERROR`). |

In the main GrooveIQ `docker-compose.yml`, these are mapped from `AB_SAMPLE_MODE` and
`AB_LOG_LEVEL` in your `.env`.

---

## 3. API endpoints (port 8200)

No auth. All endpoints are served on the internal Docker network only.

### `GET /health`

Status + track count + ingestion progress. Returns `{"status": "ready", "tracks": N,
"ingestion": "completed"}` once ingestion finishes, otherwise `{"status":
"ingesting", "progress": "archive N/30", "tracks_so_far": N}`. (No `ready`/`music`
keys, no 503: a different shape from the download sidecars.)

### `POST /v1/search`

Audio-feature similarity search. Returns **503** while ingestion is still in progress.
All request fields are optional:

```json
{
    "bpm": {"min": 110, "max": 130},
    "energy": {"min": 0.6, "max": 0.9},
    "danceability": {"min": 0.5, "max": 1.0},
    "valence": {"min": 0.4, "max": 0.8},
    "acousticness": {"max": 0.3},
    "instrumentalness": {"max": 0.4},
    "moods": {
        "happy": {"min": 0.3},
        "aggressive": {"max": 0.2}
    },
    "key": "C",
    "mode": "major",
    "genres": ["rock", "pop"],
    "exclude_mbids": ["mbid1", "mbid2"],
    "limit": 50,
    "strategy": "closest"
}
```

- `bpm` / `energy` / `danceability` / `valence` / `acousticness` / `instrumentalness`
  are `{min?, max?}` range filters (`acousticness` maps to the `mood_acoustic` column).
- `moods` accepts per-mood range filters: `happy`, `sad`, `aggressive`, `relaxed`,
  `party`, `acoustic`, `electronic`.
- `key` / `mode` are exact matches; `genres` matches against `genre_dortmund`,
  `genre_rosamerica`, or the comma-joined `genre_tags`.
- `limit` defaults to `50` (1–500).
- `strategy` is `"closest"` (default) or `"random"`. `closest` ranks by weighted
  absolute distance from each range's midpoint (BPM weighted 0.20, energy/danceability
  0.15, the rest 0.10); `random` returns random matches within the filters.

Response:

```json
{
    "results": [
        {
            "mbid": "abc-123",
            "artist": "Artist Name",
            "title": "Track Title",
            "album": "Album Name",
            "bpm": 125.0,
            "key": "C",
            "mode": "major",
            "energy": 0.75,
            "danceability": 0.68,
            "valence": 0.55,
            "mood_happy": 0.55,
            "mood_acoustic": 0.12,
            "instrumentalness": 0.04,
            "genre_dortmund": "rock",
            "distance": 0.12,
            "mb_artist_id": "artist-mbid",
            "mb_album_id": "album-mbid"
        }
    ],
    "total_matches": 1234,
    "query_time_ms": 45.0
}
```

### `GET /v1/track/{mbid}`

Single track lookup by MBID. Returns the raw track row, **404** if not found, **503**
while ingesting.

### `GET /v1/stats`

Database statistics: `total_tracks`, `genre_distribution` (top 20 by `genre_dortmund`),
and `bpm_histogram` (10 bins spanning 60–200 BPM). Returns a partial `{"status":
"ingesting", "total_tracks": N}` while ingestion is still running.

---

## 4. Feature-mapping caveats

AcousticBrainz used the same Essentia classifiers GrooveIQ uses, so most features map
directly. But a few are **derived or proxied**, not native:

| Feature | Source | Match quality |
|---|---|---|
| `bpm`, `key`, `mode` | rhythm / tonal | Direct |
| `danceability`, `instrumentalness`, `mood_*` | Essentia high-level classifiers | Direct (same classifiers) |
| `acousticness` | `mood_acoustic` probability | **Proxy** (different method, same concept) |
| `energy` | derived `0.3·norm_bpm + 0.4·loudness + 0.3·mood_aggressive` | **Approximate** |
| `valence` | `mood_happy` probability | **Rough proxy** |

Treat `energy`, `valence`, and `acousticness` as approximate signals when interpreting
search results.

---

## 5. Deploy

The `acousticbrainz-lookup` service block ships in the main GrooveIQ
`docker-compose.yml`. (Its header comment reads "Uncomment to enable", but the block
beneath it is already active YAML. There is nothing to uncomment.) To enable
discovery against it:

1. In your `.env` on the **grooveiq** container, set:

   ```ini
   AB_LOOKUP_ENABLED=true
   AB_LOOKUP_URL=http://acousticbrainz-lookup:8200
   # AB_SAMPLE_MODE=true   # optional: sample dump for testing (<5 min, <1 GB)
   ```

2. `docker compose up -d`: the sidecar container starts and begins ingesting on
   first boot. Watch progress at the sidecar's `/health`.

> **To skip it entirely** (avoid the ~39 GB ingest), comment out the
> `acousticbrainz-lookup:` service block (and the `ablookup_data` volume) in
> `docker-compose.yml`. With `AB_LOOKUP_ENABLED=false` (the default) GrooveIQ won't
> query it, but the container will still run and ingest if left active in compose.

The compose block builds from `./acousticbrainz-lookup`, runs `read_only: true` with
`cap_drop: [ALL]`, exposes **no host ports** (reachable only at
`acousticbrainz-lookup:8200` on the Docker network), and persists the SQLite DB in the
named volume `ablookup_data:/data`. The container has a non-root `ablookup` user and a
30s/5s/3 healthcheck against `/health` (60s start period). It runs `python -m main`
(uvicorn on `0.0.0.0:8200`).

**Disk:** plan for ~49 GB peak during a full ingest (~39 GB download + ~8 GB DB +
working archive) and ~6–8 GB steady-state once the raw dumps are deleted. Sample mode
needs well under 1 GB.

---

## Dependencies

`requirements.txt`: `fastapi`, `uvicorn[standard]`, `zstandard` (decompress the dump),
`httpx` (download archives), `pydantic`. Base image `python:3.12-slim` (digest-pinned);
no ffmpeg needed.
