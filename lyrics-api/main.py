"""
lyrics-api — Thin REST wrapper around faster-whisper (CTranslate2, CUDA).

Tier 3 of GrooveIQ's lyrics cascade: machine transcription (ASR) for voiced
tracks that have no embedded tag and no LRCLIB match. Deployed as a stateless
sidecar on a GPU VM (the streamrip-api / acousticbrainz-lookup pattern) so the
PyTorch-free prod image never has to carry an ML transcription stack.

GrooveIQ owns all state; this service just transcribes one file at a time and
returns the transcript + an LRC built from the segment timestamps. CTranslate2
(faster-whisper's backend) needs no PyTorch.

Endpoints:
    GET  /health      — readiness (incl. the /music stale-mount probe, #123) +
                        model / device / VRAM.
    POST /transcribe  — {path, language?, vad?, word_timestamps?, beam_size?,
                        temperature?} -> transcript + segments + LRC + rtf.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration (env vars)
# ---------------------------------------------------------------------------

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/music")
# Readiness self-check (GitHub issue #123). When > 0, /health also requires the
# /music mount to contain at least this many entries. 0 (default) gates on
# writability only — zero false positives on a legitimately-empty library.
MUSIC_MIN_ENTRIES = int(os.environ.get("MUSIC_MIN_ENTRIES", "0"))

LYRICS_MODEL = os.environ.get("LYRICS_MODEL", "large-v3")
# auto -> cuda when CTranslate2 sees a GPU, else cpu.
LYRICS_DEVICE = os.environ.get("LYRICS_DEVICE", "auto").lower()
# float16 on GPU, int8 on CPU by default; override with LYRICS_COMPUTE_TYPE.
LYRICS_COMPUTE_TYPE = os.environ.get("LYRICS_COMPUTE_TYPE", "").strip()
LYRICS_MODEL_DIR = os.environ.get("LYRICS_MODEL_DIR", "/data/models")
LYRICS_BEAM_SIZE = int(os.environ.get("LYRICS_BEAM_SIZE", "5"))
LYRICS_VAD = os.environ.get("LYRICS_VAD", "true").strip().lower() in ("1", "true", "yes")
DEFAULT_LANGUAGE = os.environ.get("LYRICS_LANGUAGE", "").strip() or None
# Single GPU — serialise transcriptions. GrooveIQ also throttles upstream.
MAX_CONCURRENCY = max(1, int(os.environ.get("LYRICS_MAX_CONCURRENCY", "1")))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lyrics-api")

app = FastAPI(title="lyrics-api", version="1.0.0")

# ---------------------------------------------------------------------------
# Model (lazy-loaded, single instance)
# ---------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()
_resolved_device: str | None = None
_resolved_compute_type: str | None = None
_load_error: str | None = None
_sema = asyncio.Semaphore(MAX_CONCURRENCY)


def _detect_device() -> str:
    if LYRICS_DEVICE in ("cuda", "cpu"):
        return LYRICS_DEVICE
    # auto
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("CUDA detection failed (%s); falling back to CPU", exc)
    return "cpu"


def _get_model():
    """Lazily load the WhisperModel once. Raises on failure (caller -> 503/500)."""
    global _model, _resolved_device, _resolved_compute_type, _load_error
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from faster_whisper import WhisperModel

            device = _detect_device()
            compute_type = LYRICS_COMPUTE_TYPE or ("float16" if device == "cuda" else "int8")
            logger.info(
                "Loading faster-whisper model=%s device=%s compute_type=%s download_root=%s",
                LYRICS_MODEL, device, compute_type, LYRICS_MODEL_DIR,
            )
            t0 = time.monotonic()
            model = WhisperModel(
                LYRICS_MODEL,
                device=device,
                compute_type=compute_type,
                download_root=LYRICS_MODEL_DIR,
            )
            _resolved_device = device
            _resolved_compute_type = compute_type
            _model = model
            _load_error = None
            logger.info("Model loaded in %.1fs", time.monotonic() - t0)
            return _model
        except Exception as exc:
            _load_error = str(exc)
            logger.error("Model load failed: %s", exc)
            raise


# ---------------------------------------------------------------------------
# /music readiness probe (copied from streamrip-api / acousticbrainz-lookup — #123)
# ---------------------------------------------------------------------------


def _music_status() -> dict[str, Any]:
    """Probe the /music bind-mount: existence, entry count, and writability.

    Detects the stale-bind-mount failure mode (GitHub issue #123): when the host
    library dir backing /music is replaced while this long-lived container keeps
    running, the container holds the old (now empty, root-owned) inode and reads
    fail — yet a disk-blind /health stays green. The probe turns that silent
    failure into an unhealthy container.
    """
    path = OUTPUT_DIR
    out: dict[str, Any] = {"path": path, "exists": False, "entries": None, "writable": False, "error": None}
    try:
        if not os.path.isdir(path):
            out["error"] = "directory does not exist"
            return out
        out["exists"] = True
        try:
            out["entries"] = len(os.listdir(path))
        except OSError as exc:
            out["error"] = f"cannot list: {exc}"
        probe = os.path.join(path, ".grooveiq_write_probe")
        try:
            fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            os.unlink(probe)
            out["writable"] = True
        except FileExistsError:
            with contextlib.suppress(OSError):
                os.unlink(probe)
            out["writable"] = True
        except OSError as exc:
            if out["error"] is None:
                out["error"] = f"not writable: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        out["error"] = str(exc)
    return out


def _music_ready(status: dict[str, Any]) -> bool:
    """Readiness gate for /music: must exist and be writable (the definitive
    stale-mount signal). When MUSIC_MIN_ENTRIES > 0, also require that many
    entries."""
    if not status.get("exists") or not status.get("writable"):
        return False
    if MUSIC_MIN_ENTRIES > 0 and (status.get("entries") or 0) < MUSIC_MIN_ENTRIES:  # noqa: SIM103
        return False
    return True


def _gpu_info() -> dict[str, Any]:
    """Best-effort VRAM/name via nvidia-smi (no torch dependency)."""
    info: dict[str, Any] = {"name": None, "memory_total_mb": None, "memory_used_mb": None}
    try:
        cmd = ["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"]
        proc = subprocess.run(cmd, capture_output=True, timeout=5, check=False)  # noqa: S603
        if proc.returncode == 0:
            line = proc.stdout.decode(errors="replace").strip().splitlines()[0]
            name, total, used = (p.strip() for p in line.split(","))
            info["name"] = name
            info["memory_total_mb"] = int(float(total))
            info["memory_used_mb"] = int(float(used))
    except Exception:
        pass
    return info


# ---------------------------------------------------------------------------
# LRC helpers
# ---------------------------------------------------------------------------


def _ts(seconds: float) -> str:
    if seconds is None or seconds < 0:
        seconds = 0.0
    total_cs = round(seconds * 100)
    minutes = total_cs // 6000
    secs = (total_cs % 6000) // 100
    centis = total_cs % 100
    return f"{minutes:02d}:{secs:02d}.{centis:02d}"


def _segments_to_lrc(segments: list[dict[str, Any]]) -> str:
    lines = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{_ts(seg.get('start') or 0.0)}]{text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TranscribeRequest(BaseModel):
    path: str
    language: str | None = None
    vad: bool | None = None
    word_timestamps: bool = False
    beam_size: int | None = None
    temperature: float = 0.0


def _resolve_path(raw: str) -> Path:
    """Resolve a request path to a real file under OUTPUT_DIR (no traversal)."""
    p = Path(raw)
    if not p.is_absolute():
        p = Path(OUTPUT_DIR) / raw
    resolved = p.resolve()
    root = Path(OUTPUT_DIR).resolve()
    if root not in resolved.parents and resolved != root:
        raise HTTPException(status_code=400, detail="path escapes the music root")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {raw}")
    return resolved


def _do_transcribe(req: TranscribeRequest, audio_path: str) -> dict[str, Any]:
    """Blocking transcription (run in a worker thread)."""
    model = _get_model()
    t0 = time.monotonic()
    segments_iter, info = model.transcribe(
        audio_path,
        language=req.language or DEFAULT_LANGUAGE,
        beam_size=req.beam_size or LYRICS_BEAM_SIZE,
        temperature=req.temperature,
        vad_filter=LYRICS_VAD if req.vad is None else bool(req.vad),
        word_timestamps=bool(req.word_timestamps),
    )

    segments: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for seg in segments_iter:  # iterating runs the actual transcription
        seg_text = (seg.text or "").strip()
        entry: dict[str, Any] = {"start": seg.start, "end": seg.end, "text": seg_text}
        if req.word_timestamps and getattr(seg, "words", None):
            entry["words"] = [
                {"start": w.start, "end": w.end, "word": w.word} for w in seg.words
            ]
        segments.append(entry)
        if seg_text:
            text_parts.append(seg_text)

    elapsed = time.monotonic() - t0
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    text = "\n".join(text_parts)
    return {
        "language": getattr(info, "language", None),
        "language_probability": getattr(info, "language_probability", None),
        "duration": duration,
        "text": text,
        "lrc": _segments_to_lrc(segments),
        "segments": segments,
        "model": LYRICS_MODEL,
        "device": _resolved_device,
        "compute_type": _resolved_compute_type,
        "rtf": round(elapsed / duration, 4) if duration > 0 else None,
        "processing_seconds": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    music = _music_status()
    ready = _music_ready(music)
    device = _resolved_device or _detect_device()
    body: dict[str, Any] = {
        "status": "ok" if ready else "degraded",
        "service": "lyrics-api",
        "ready": ready,
        "music": music,
        "model": LYRICS_MODEL,
        "device": device,
        "compute_type": _resolved_compute_type
        or (LYRICS_COMPUTE_TYPE or ("float16" if device == "cuda" else "int8")),
        "model_loaded": _model is not None,
        "load_error": _load_error,
        "gpu": _gpu_info(),
        "max_concurrency": MAX_CONCURRENCY,
    }
    if not ready:
        # 503 → Docker HEALTHCHECK fails → container shows (unhealthy), turning a
        # silent "every transcription fails" into an obvious signal (issue #123).
        return JSONResponse(status_code=503, content=body)
    return body


@app.post("/transcribe")
async def transcribe(req: TranscribeRequest):
    audio_path = str(_resolve_path(req.path))
    async with _sema:
        try:
            result = await asyncio.get_event_loop().run_in_executor(None, _do_transcribe, req, audio_path)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Transcription failed for %s: %s", req.path, exc)
            raise HTTPException(status_code=500, detail=f"transcription failed: {exc}") from exc
    return result
