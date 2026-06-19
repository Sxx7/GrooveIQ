# streamrip-api — lossless/hi-res download sidecar for GrooveIQ

`streamrip-api` is a thin REST wrapper around the
[streamrip](https://github.com/nathom/streamrip) Python library (used as a
library, not the CLI). It downloads tracks and albums in **lossless / hi-res**
quality from **Qobuz, Tidal, Deezer, and SoundCloud**, and exposes a small HTTP
API that GrooveIQ's download cascade calls.

It runs as a **separate container on GrooveIQ's internal Docker network** (port
**8282**, no host ports, **no auth** — GrooveIQ's auth layer fronts it). GrooveIQ
reaches it over HTTP at `http://streamrip-api:8282`.

> **You need a paid subscription to at least one service.** Qobuz, Tidal, and
> Deezer require account credentials; SoundCloud needs a client ID. Without any
> configured service, `/health` still comes up but `available_services` is empty,
> every **search** returns 503, and **downloads** are accepted (queued) but end in
> `status: error`.

---

## Endpoints (7)

No authentication — reachable only on the internal Docker network.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Readiness incl. `/music` write-probe (#123) plus `available_services`, `default_service`, `download_quality`, `download_codec`, `active_tasks`. **503** when `/music` isn't writable. |
| GET | `/search?q=&limit=&service=` | Track search. `q` required; `limit` default 25 (1–100); `service` defaults to `DEFAULT_SERVICE`. 400 unknown service, 503 if service not configured. |
| GET | `/search/artist?q=&limit=&albums_per_artist=&service=` | Artist search → discography (album metadata only, no track lists). `limit` default 2 (1–10); `albums_per_artist` default 100 (1–300). |
| GET | `/album/{album_id}/tracks?service=` | Lazy-load a single album's track list. 400 on invalid `album_id`. |
| POST | `/download` | Download a track or album (see body below). Returns `{task_id, status}` immediately. 400 on bad `entity_type` / missing id. |
| GET | `/status/{task_id}` | Task status; 404 if unknown. |
| GET | `/tasks?status=&limit=` | Recent tasks, newest-first. `limit` default 50 (1–200). |

Valid services everywhere: `qobuz`, `tidal`, `deezer`, `soundcloud`.
Task statuses: `queued | downloading | complete | duplicate | error`.

**`POST /download` body** (`DownloadRequestBody`):

| Field | Default | Notes |
|---|---|---|
| `entity_type` | `"track"` | `track` or `album`. |
| `service_id` | `""` | Service-native id. Albums **require** this + `service ∈ {qobuz,tidal,deezer}`. |
| `service` | — | Target service. |
| `spotify_id` | — | Alternative id; download id resolves to `service_id or spotify_id`. |
| `artist` / `title` | — | Tracks (only) can fall back to an artist+title search when no id is given. |

```bash
curl -s -X POST streamrip-api:8282/download -H 'content-type: application/json' \
  -d '{"entity_type":"track","service":"qobuz","artist":"Artist","title":"Song"}'
# => {"task_id":"...","status":"queued"}
```

---

## Configuration (sidecar env vars)

| Variable | Default | Description |
|---|---|---|
| `OUTPUT_DIR` | `/music` | Download root (streamrip `downloads.folder`). Leave at `/music`. |
| `MUSIC_MIN_ENTRIES` | `0` | When > 0, `/health` also requires `/music` to list ≥ N entries (set `1` on a populated library to catch a stale/empty mount). |
| `DOWNLOAD_QUALITY` | `3` | streamrip quality ladder: `0`=128 kbps, `1`=320 kbps, `2`=16-bit/44.1, `3`=24-bit/96, `4`=24-bit/192. |
| `DOWNLOAD_CODEC` | `FLAC` | Output codec (surfaced in `/health`). |
| `MAX_CONNECTIONS` | `6` | streamrip `downloads.max_connections`. |
| `MAX_THREADS` | `4` | Read but **not currently applied** to streamrip — only `MAX_CONNECTIONS` is patched into the config. |
| `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR. |
| `DEFAULT_SERVICE` | `qobuz` | Default service for search/download. |
| `QOBUZ_EMAIL` | `""` | Qobuz email — **or** numeric user_id when token mode is on (see below). |
| `QOBUZ_PASSWORD` | `""` | Qobuz password — **or** the auth token when token mode is on. |
| `QOBUZ_USE_AUTH_TOKEN` | *(false)* | Truthy (`1`/`true`/`yes`) → treat `QOBUZ_EMAIL` as user_id and `QOBUZ_PASSWORD` as an auth token (Qobuz dropped email/password login). |
| `TIDAL_EMAIL` | `""` | Read, but Tidal needs OAuth — email/password is **not fully wired**. |
| `TIDAL_PASSWORD` | `""` | Same caveat as above. |
| `DEEZER_ARL` | `""` | Deezer ARL cookie (from browser dev tools). Deezer quality is **capped at 16-bit/44.1** (`min(DOWNLOAD_QUALITY, 2)`). |
| `SOUNDCLOUD_CLIENT_ID` | `""` | SoundCloud client ID. |

> **Per-service credential notes.** Qobuz uses token mode when `QOBUZ_USE_AUTH_TOKEN`
> is set. Tidal's email/password fields are read but not fully wired — it really
> needs OAuth. Deezer downloads are quality-limited to 16-bit/44.1 regardless of
> `DOWNLOAD_QUALITY`.

### `/music` readiness (issue #123)

`streamrip-api` bind-mounts the host library read-write at `/music`. If that mount
is replaced under a running container, the container keeps a reference to the old
inode — `/music` appears empty and every download fails with `Permission denied`,
yet a naive health check stays green. To catch this, `/health` runs a **write
probe** (create + delete a dotfile): when `/music` isn't writable it returns
**HTTP 503** with `ready:false`, so the Docker `HEALTHCHECK` flips the container to
`(unhealthy)`. Recover with `docker compose up -d --force-recreate streamrip-api`.

---

## docker-compose wiring

`streamrip-api` is defined in the repo-root `docker-compose.yml`. It builds from
`./streamrip-api`, runs `read_only`, exposes **no host ports**, and persists its
generated streamrip config in a named volume:

```yaml
streamrip-api:
  build:
    context: ./streamrip-api
    dockerfile: Dockerfile
  restart: unless-stopped
  read_only: true
  # No host ports — reachable by grooveiq via Docker network at streamrip-api:8282.
  environment:
    OUTPUT_DIR:       /music
    DOWNLOAD_QUALITY: ${STREAMRIP_QUALITY:-3}
    DOWNLOAD_CODEC:   ${STREAMRIP_CODEC:-FLAC}
    MAX_CONNECTIONS:  ${STREAMRIP_CONNECTIONS:-6}
    MAX_THREADS:      ${STREAMRIP_THREADS:-4}
    DEFAULT_SERVICE:  ${STREAMRIP_DEFAULT_SERVICE:-qobuz}
  volumes:
    - ${MUSIC_LIBRARY_PATH:-/mnt/music}:/music   # read-write
    - streamrip_config:/config                   # named volume for config.toml
```

Add your streaming-service credentials (`QOBUZ_*`, `TIDAL_*`, `DEEZER_ARL`,
`SOUNDCLOUD_CLIENT_ID`) to `.env`, then point GrooveIQ at the sidecar:

```ini
# .env (on the GrooveIQ side)
STREAMRIP_API_URL=http://streamrip-api:8282
```

> **`STREAMRIP_API_URL` is commented out in `docker-compose.yml` by default** — so
> even though the `streamrip-api` service itself is defined, GrooveIQ won't use the
> sidecar until you uncomment/set that line and restart GrooveIQ.

---

## Notes

- **Stateless on the GrooveIQ side.** GrooveIQ owns all download routing, retry,
  and history state via its `individual` / `bulk_per_track` / `bulk_album` cascade
  chains; this sidecar just executes one search/download at a time.
- **Single-worker, serialized downloads.** streamrip is guarded by a global lock
  and the app runs with `--workers 1`. A blocking download can wedge the asyncio
  loop so `/health` *times out* (CPU near 0%, sockets piling up in `CLOSE_WAIT`).
  This is **distinct from the #123 stale-mount case** (which returns a clean 503):
  a plain `docker compose restart streamrip-api` clears the wedge.
