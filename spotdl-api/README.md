# spotdl-api — Spotify-matched downloads for GrooveIQ

`spotdl-api` is a thin REST wrapper around the [spotDL](https://github.com/spotDL/spotify-downloader)
Python library. It searches **Spotify** for track metadata and downloads the matching
audio from **YouTube Music**, so GrooveIQ can trigger Spotify-matched downloads over
HTTP without embedding spotDL as a direct dependency.

It runs as a **separate container** alongside GrooveIQ in the main
`docker-compose.yml`, reachable only on the internal Docker network at
`spotdl-api:8181`. It has **no auth of its own** — it is never exposed on a host
port; GrooveIQ's auth layer fronts it. Both GrooveIQ and this sidecar bind-mount the
**same music library** at `/music` (read-write here, since this container writes
downloads into it).

```
┌─────────────────────────┐    HTTP (internal Docker network)    ┌──────────────────────────┐
│  grooveiq                │  ─────────────────────────────────▶ │  spotdl-api (this dir)   │
│  SPOTDL_API_URL=         │   http://spotdl-api:8181             │  spotDL + ffmpeg         │
│   http://spotdl-api:8181 │                                      │  writes audio to /music  │
└─────────────────────────┘                                      └──────────────────────────┘
        both bind-mount the SAME library at /music (read-write)
```

---

## 1. Prerequisites

- A free **Spotify app client id + secret** from the
  [Spotify Developer Dashboard](https://developer.spotify.com/dashboard). spotDL ships
  with fallback credentials, but you should supply your own to avoid shared-quota rate
  limits. Set them as `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`.
- **Docker** with the Compose plugin (`docker compose`).
- The container needs `ffmpeg` for transcoding — it's baked into the image.

---

## 2. Deploy

This sidecar is part of GrooveIQ's main `docker-compose.yml` (service `spotdl-api`,
built from `./spotdl-api`). You don't run it separately — bring up the stack and it
comes with it:

```bash
docker compose up -d --build spotdl-api
```

Verify it's healthy (no host port — probe it from inside the network):

```bash
docker compose exec spotdl-api curl -s localhost:8181/health
# => {"status":"ok","service":"spotdl-api","ready":true,"music":{"writable":true,...}}
```

The compose service is `read_only: true` with `cap_drop: [ALL]` and **no host ports**.
A `/tmp` tmpfs (128 MB) covers spotDL's lazy-init config/cache writes (`HOME` is set to
`/tmp/spotdl-home` in the image). The library is mounted **read-write** at `/music`
(`${MUSIC_LIBRARY_PATH}:/music`) so downloads land directly in your library.

---

## 3. Configuration (env vars)

Compose passes most of these from the repo-root `.env` (the `SPOTDL_*` host vars are
mapped onto the container vars below).

| Variable | Default | Description |
|---|---|---|
| `OUTPUT_DIR` | `/music` | Library mount root inside the container (leave at `/music`). |
| `MUSIC_MIN_ENTRIES` | `0` | When > 0, `/health` also requires `/music` to list ≥ N entries — set `1` on a populated library to catch a stale/empty mount. `0` gates on writability only. |
| `OUTPUT_FORMAT` | `opus` | spotDL output format (compose: `SPOTDL_FORMAT`). |
| `BITRATE` | `auto` | spotDL bitrate (compose: `SPOTDL_BITRATE`). |
| `OUTPUT_TEMPLATE` | `{artist}/{album}/{artists} - {title}.{output-ext}` | spotDL output path template (compose: `SPOTDL_TEMPLATE`). |
| `MAX_THREADS` | `4` | Thread-pool size + spotDL download threads (compose: `SPOTDL_THREADS`). |
| `SPOTIFY_CLIENT_ID` | `""` | Spotify app client id. Empty → spotDL uses its own fallback credentials. |
| `SPOTIFY_CLIENT_SECRET` | `""` | Spotify app client secret. |
| `LOG_LEVEL` | `INFO` | Log level. |

---

## 4. Endpoints

No auth. The container listens on `:8181`. These five endpoints are the entire surface:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Readiness + `/music` stale-mount probe (see §5). Returns **503** when `/music` isn't writable; `200` otherwise. |
| `GET` | `/search?q=&limit=` | Search Spotify for tracks (metadata via Spotify, audio via YouTube Music). `q` required (min length 1); `limit` default `10`, range 1–50. Returns a list of results. `503` if spotDL isn't initialized, `502` on search error. |
| `POST` | `/download` | Body `{"spotify_id": "..."}`. Builds `https://open.spotify.com/track/{spotify_id}`, runs the download in a thread pool, and returns `{task_id, status}` immediately. `503` if spotDL isn't initialized. |
| `GET` | `/status/{task_id}` | Progress for one task; `404` if unknown. |
| `GET` | `/tasks?status=&limit=` | Recent tasks, newest first. Optional `status` filter; `limit` default `50`, range 1–200. |

Each search result carries: `spotify_id`, `title`, `artist`, `artists[]`, `album?`,
`album_artist?`, `duration?`, `cover_url?`, `url`. Task `status` is one of
`queued`, `downloading`, `complete`, `error`. Tasks live in memory only (ephemeral).

```bash
# Search
docker compose exec spotdl-api curl -s 'localhost:8181/search?q=daft+punk&limit=3'

# Download by Spotify track id, then poll
docker compose exec spotdl-api curl -s -X POST localhost:8181/download \
  -H 'content-type: application/json' -d '{"spotify_id":"0DiWol3AO6WpXZgp0goxAV"}'
docker compose exec spotdl-api curl -s localhost:8181/status/<task_id>
```

---

## 5. `/music` readiness probe (issue #123)

This is a long-lived container holding an open reference to the `/music` bind mount. If
the host directory backing that mount is **replaced** while the container keeps running,
the container holds the old (now empty, root-owned) inode: every download then fails
with `[Errno 13] Permission denied`, yet a disk-blind health check would stay green.

To turn that silent failure into an obvious one, `/health` probes `/music` for
existence, entry count, and **writability** (it creates and deletes a
`.grooveiq_write_probe` dotfile). When `/music` isn't writable — the definitive
stale-mount signal — `/health` returns **HTTP 503** with `ready: false` and a `music`
detail block, so the Docker `HEALTHCHECK` flips the container to `(unhealthy)`.

Writability (not emptiness) is the gate because it's the direct cause of the
`Permission denied` failures and has no false positives on a legitimately-empty library.
Set `MUSIC_MIN_ENTRIES=1` to additionally flag the rarer "writable but empty/wrong
inode" case on a library you know is populated.

**If it goes unhealthy:** re-bind the mount with
`docker compose up -d --force-recreate spotdl-api` (best done after any deploy step that
can swap the library dir).

---

## 6. Wire GrooveIQ to the sidecar

GrooveIQ reaches the sidecar via `SPOTDL_API_URL`. In the main `docker-compose.yml` this
is already set to the internal address, and the Spotify credentials are passed through
from the repo-root `.env`:

```ini
# .env (repo root)
SPOTDL_API_URL=http://spotdl-api:8181   # set in compose; only override for an external host
SPOTIFY_CLIENT_ID=YOUR_CLIENT_ID
SPOTIFY_CLIENT_SECRET=YOUR_CLIENT_SECRET
```

With those set and the service up, GrooveIQ's download cascade can route track downloads
through spotDL (see the **Download proxy + routing cascade** section in the main README /
`CLAUDE.md`).
