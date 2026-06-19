# lyrics-api — GPU ASR sidecar for GrooveIQ lyrics

`lyrics-api` is the **tier-3** of GrooveIQ's lyrics cascade: machine transcription
(ASR) for voiced tracks that have no embedded tag and no LRCLIB match. It's a thin
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) wrapper (CTranslate2 /
CUDA — **no PyTorch**) that transcribes one file at a time and returns the text plus
an LRC built from the segment timestamps.

It runs as a **separate, standalone container on a GPU host** — *not* part of the main
GrooveIQ `docker-compose.yml`. That's deliberate:

- It needs an **NVIDIA GPU**, while the GrooveIQ box is typically CPU-only.
- It keeps the GrooveIQ image **PyTorch/CUDA-free** — GrooveIQ only talks to it over HTTP.

```
┌─────────────────────────┐         HTTP POST /transcribe        ┌──────────────────────────┐
│  GrooveIQ box (CPU)      │  ─────────────────────────────────▶ │  GPU host                │
│  docker-compose.yml      │   LYRICS_API_URL=http://gpu:8300     │  lyrics-api (this dir)   │
│  grooveiq, postgres, …   │                                      │  faster-whisper + CUDA   │
└─────────────────────────┘                                      └──────────────────────────┘
        both bind-mount the SAME library; the sidecar reads it read-only
```

If your GrooveIQ box *itself* has a GPU you can run this on the same machine — it's
still its own compose project (see [Single-box](#single-box-same-machine-has-the-gpu)).

---

## 1. Prerequisites (on the GPU host)

- An **NVIDIA GPU** + driver, and the **[nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)** (so `docker info` lists an `nvidia` runtime). `large-v3` needs ~3 GB VRAM; an 8 GB+ card is comfortable.
- **Docker** with the Compose plugin (`docker compose`).
- The **same music library** GrooveIQ scanned, mounted on this host (read access is enough). Plex/Jellyfin boxes usually already have it.

> **The paths must line up.** GrooveIQ transcribes *by path*: it sends the `file_path`
> it stored at scan time (e.g. `/music/Artist/Album/track.flac`) to the sidecar, which
> reads that path inside its own container. The simplest setup is to mount your library
> at **`/music`** in *both* GrooveIQ and this sidecar — then no mapping is needed. If the
> sidecar mounts the library somewhere else, set `LYRICS_API_MUSIC_PATH` on the **GrooveIQ**
> side (see [§4](#4-wire-grooveiq-to-the-sidecar)).

---

## 2. Deploy the sidecar (standalone compose)

From a checkout of this repo, copy the build context to the GPU host and start it:

```bash
# 1. Copy the build context to the GPU host
ssh user@gpu-host 'mkdir -p ~/lyrics-api'
scp lyrics-api/Dockerfile lyrics-api/main.py lyrics-api/requirements.txt lyrics-api/docker-compose.yml \
    user@gpu-host:~/lyrics-api/

# 2. Create ~/lyrics-api/.env on the GPU host (docker compose reads it automatically)
cat > ~/lyrics-api/.env <<'EOF'
MUSIC_PATH=/path/to/your/music   # host path of the library on THIS box; mounted :ro at /music
LYRICS_API_PORT=8300
LYRICS_MODEL=large-v3            # or: large-v3-turbo, medium, small (smaller = faster, lower accuracy)
LYRICS_DEVICE=auto               # auto -> cuda when a GPU is present, else cpu
MUSIC_MIN_ENTRIES=1              # library is populated -> also flag a stale/empty mount as unhealthy
LOG_LEVEL=INFO
EOF

# 3. Build + start (needs the nvidia runtime)
ssh user@gpu-host 'cd ~/lyrics-api && docker compose up -d --build'

# 4. Verify — first call lazily downloads the model (~3 GB) to the lyrics_models volume
ssh user@gpu-host 'curl -s localhost:8300/health'
# => {"status":"ok","ready":true,"model":"large-v3","device":"cuda","compute_type":"float16",
#     "model_loaded":true,"gpu":{...},"max_concurrency":1,"music":{"readable":true,...}}
```

`docker-compose.yml` mounts `${MUSIC_PATH}:/music:ro` + a `lyrics_models` named volume
(so the model download survives recreates) and grants the GPU via
`deploy.resources.reservations.devices`. The service is **stateless** — GrooveIQ owns
all queue/retry state.

**Redeploy after a code change:** re-`scp main.py` then `docker compose up -d --build`
(layers are cached, the model volume persists).

---

## 3. Configuration (sidecar env vars)

| Variable | Default | Description |
|---|---|---|
| `OUTPUT_DIR` | `/music` | Library mount inside the container (leave at `/music`). |
| `MUSIC_PATH` *(compose only)* | `/music` | Host path mounted read-only at `/music`. |
| `LYRICS_MODEL` | `large-v3` | faster-whisper model name. Auto-downloaded on first transcribe. |
| `LYRICS_DEVICE` | `auto` | `auto` \| `cuda` \| `cpu`. |
| `LYRICS_COMPUTE_TYPE` | *(auto)* | `float16` on GPU, `int8` on CPU by default; override e.g. `int8_float16` to save VRAM. |
| `LYRICS_MODEL_DIR` | `/data/models` | Where the model is cached (the named volume). |
| `LYRICS_BEAM_SIZE` | `5` | Decoding beam size. |
| `LYRICS_VAD` | `true` | Default VAD when the caller doesn't specify. GrooveIQ overrides this per request (see note below). |
| `LYRICS_LANGUAGE` | *(empty → auto-detect)* | Forced default transcription language (e.g. `en`) used when a `/transcribe` request omits `language`. Empty lets faster-whisper detect per track. |
| `LYRICS_MAX_CONCURRENCY` | `1` | Concurrent transcriptions (single GPU → keep at 1). |
| `MUSIC_MIN_ENTRIES` | `0` | When > 0, `/health` also requires `/music` to list ≥ N entries — set `1` on a populated library to catch a stale/empty mount. |

### Endpoints

- `GET /health` — readiness + model/device/compute_type + `gpu` (VRAM) block. Returns **503** when `/music` isn't readable (stale mount) so Docker marks the container unhealthy. The `gpu` block is best-effort — it's populated by shelling out to `nvidia-smi`, so its `name`/`memory_total_mb`/`memory_used_mb` come back `null` when `nvidia-smi` is absent (e.g. CPU mode).
- `POST /transcribe` — body fields:

  | Field | Type | Default | Description |
  |---|---|---|---|
  | `path` | str | *(required)* | File path under `/music` to transcribe. |
  | `language` | str? | *(unset → `LYRICS_LANGUAGE`, else auto-detect)* | Force a transcription language (e.g. `en`). |
  | `vad` | bool? | *(unset → `LYRICS_VAD`)* | Per-request VAD override. |
  | `word_timestamps` | bool | `false` | Include per-word timestamps in segments. |
  | `beam_size` | int? | *(unset → `LYRICS_BEAM_SIZE`)* | Per-request decoding beam size. |
  | `temperature` | float | `0.0` | Sampling temperature. |

  Returns `{language, language_probability, duration, text, lrc, segments, model, device, compute_type, rtf, processing_seconds}`.

```bash
curl -s -X POST localhost:8300/transcribe -H 'content-type: application/json' \
  -d '{"path":"/music/Artist/Album/track.flac","vad":false}' | jq '{language, rtf, text: .text[0:120]}'
```

---

## 4. Wire GrooveIQ to the sidecar

On the **GrooveIQ** box, set these in `.env` (see the repo root [`.env.example`](../.env.example)) and restart GrooveIQ:

```ini
LYRICS_ENABLED=true
LYRICS_LRCLIB_ENABLED=true          # tier 2 (free, no key)
LYRICS_ASR_ENABLED=true             # tier 3 (this sidecar)
LYRICS_API_URL=http://gpu-host:8300 # use the IP if the container can't resolve the hostname
# LYRICS_API_MUSIC_PATH=            # only if the sidecar's /music != GrooveIQ's MUSIC_LIBRARY_PATH
LYRICS_ASR_VAD=false                # see "VAD" below
LYRICS_DRAIN_MAX_PER_HOUR=0         # 0 = unthrottled; raise to pace a shared GPU
```

> **Container DNS:** the GrooveIQ container often can't resolve a `.local`/LAN hostname.
> If `LYRICS_API_URL` with a hostname fails, use the GPU host's **IP** instead.

GrooveIQ then backfills the library on a schedule (the lyrics *drain*), walking
embedded tags → LRCLIB → this sidecar, and only sending **voiced** tracks to ASR
(tracks with `instrumentalness ≥ LYRICS_ASR_INSTRUMENTAL_MAX`, default 0.5, are
skipped — no hallucinated lyrics on instrumentals). Watch progress at
`GET /v1/lyrics/stats`.

---

## 5. Tuning notes

- **VAD off for music.** Silero VAD is trained on *speech* and discards a large share of
  *sung* vocals — we measured ~50 % recall loss with it on. Since GrooveIQ's
  instrumentalness gate already prevents ASR on instrumentals, `LYRICS_ASR_VAD=false`
  is recommended (and is the GrooveIQ default). Turn it on only if you see hallucinated
  text on voiced tracks with long instrumental passages.
- **Model size.** `large-v3` is the most accurate and runs at RTF ~0.05–0.15 on a modern
  GPU (a 4-min track in seconds). Drop to `medium`/`small` only if throughput matters more
  than accuracy.
- **Vocal separation (Demucs)** can improve transcription of dense mixes but adds PyTorch
  and 2–3× GPU cost — not included; revisit only if WER is unacceptable.
- **Pacing.** The single GPU is the bottleneck. `LYRICS_DRAIN_MAX_PER_HOUR` (on GrooveIQ)
  caps ASR calls/hour so the GPU stays free for other work (e.g. Plex transcodes); `0`
  runs flat out.

---

## 6. Troubleshooting

- **`/health` is 503 / `music.readable: false`** — the `/music` mount is missing or stale.
  Check `MUSIC_PATH` points at the real library on this host; if the host dir was replaced
  under a running container, rebind with `docker compose up -d --force-recreate lyrics-api`.
- **`404 file not found` from `/transcribe`** — the path GrooveIQ sent doesn't exist inside
  the sidecar. The mounts don't line up: either mount the library at `/music` on both sides,
  or set `LYRICS_API_MUSIC_PATH` on GrooveIQ.
- **`device: cpu` in `/health` though you have a GPU** — the nvidia runtime isn't wired up;
  verify `docker info` lists `nvidia` and that the compose `deploy.resources` block is present.
- **First transcription is slow** — it's downloading the model (~3 GB) to the volume; it's
  cached after that.

---

## Single-box (same machine has the GPU)

If your GrooveIQ host has a GPU, you can run this sidecar on the same machine — still as its
own compose project in `lyrics-api/`. Point `LYRICS_API_URL=http://localhost:8300` (or, from
inside the GrooveIQ container, the host's docker-bridge address). It does **not** need to be
merged into the main `docker-compose.yml`; keeping it separate preserves the PyTorch-free
main image.

## CPU-only (testing)

`LYRICS_DEVICE=cpu` works for trying it out (faster-whisper falls back to CPU automatically
when no GPU is present), but it's far slower — not suitable for a whole-library backfill.
