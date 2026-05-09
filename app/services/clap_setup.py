"""
GrooveIQ — CLAP model auto-installer (issue #91).

On startup, if ``CLAP_ENABLED=true`` and the three required files aren't
already present in ``CLAP_MODEL_DIR``, fetch them from the Hugging Face
URLs configured in ``settings``:

    clap_text.onnx       <- CLAP_TEXT_MODEL_URL
    clap_audio.onnx      <- CLAP_AUDIO_MODEL_URL
    clap_tokenizer.json  <- CLAP_TOKENIZER_URL

Defaults point to ``Xenova/larger_clap_music_and_speech`` (fp16, ~395 MB
total). Files persist in the ``grooveiq_data`` named volume so subsequent
starts are no-ops.

Failures are non-fatal: a warning is logged and startup continues. The
text-strategy playlist endpoint will then surface a clean 503 instead of
crashing the whole API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Minimum plausible byte size for each file. Catches partial downloads,
# 404 HTML pages saved as .onnx, etc. Sizes are based on Xenova fp16
# weights with ~30% slack to tolerate variant swaps.
_MIN_SIZES = {
    "text": 100 * 1024 * 1024,  # text_model_fp16.onnx is ~251 MB
    "audio": 50 * 1024 * 1024,  # audio_model_fp16.onnx is ~143 MB
    "tokenizer": 1 * 1024 * 1024,  # tokenizer.json is ~2 MB
}


def _file_ok(path: Path, min_size: int) -> bool:
    return path.is_file() and path.stat().st_size >= min_size


def _download_one(url: str, dest: Path, min_size: int, label: str) -> None:
    """Download to a temp file in the same dir, then atomic rename.

    Same-dir temp keeps it on the same filesystem so ``os.replace`` is
    atomic — half-written files never appear at the final path even if
    the process is killed mid-download.
    """
    # Defence-in-depth: settings come from a trusted operator-controlled
    # .env, but reject anything other than https:// to make it impossible
    # for an mis-configured URL to read local files via urllib's file://
    # scheme support.
    if not url.startswith("https://"):
        raise ValueError(f"CLAP setup: refusing to fetch {label} from non-https URL: {url!r}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("CLAP setup: downloading %s from %s", label, url)

    fd, tmp_path = tempfile.mkstemp(prefix=f".{dest.name}.", dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp_path)

    try:
        # httpx (already a project dep) only speaks http/https — file:// is
        # not reachable, which sidesteps the urllib.urlopen surface area.
        # Long timeout for large model files; HF is fast but a 250 MB read
        # over a slow link can plausibly take a couple of minutes.
        timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
        headers = {"User-Agent": "grooveiq/clap-setup"}
        with (
            httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client,
            client.stream("GET", url) as resp,
        ):
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", "0"))
            written = 0
            next_log = 50 * 1024 * 1024  # log every 50 MB
            with tmp.open("wb") as f:
                for buf in resp.iter_bytes(chunk_size=1024 * 256):
                    f.write(buf)
                    written += len(buf)
                    if written >= next_log:
                        if total > 0:
                            logger.info(
                                "CLAP setup: %s — %d / %d MB (%.0f%%)",
                                label,
                                written // (1024 * 1024),
                                total // (1024 * 1024),
                                100.0 * written / total,
                            )
                        else:
                            logger.info("CLAP setup: %s — %d MB", label, written // (1024 * 1024))
                        next_log += 50 * 1024 * 1024

        if not _file_ok(tmp, min_size):
            raise RuntimeError(f"downloaded {label} is too small ({tmp.stat().st_size} bytes < {min_size})")
        os.replace(tmp, dest)
        logger.info("CLAP setup: installed %s -> %s (%d MB)", label, dest, dest.stat().st_size // (1024 * 1024))
    except Exception:
        # Best-effort cleanup of the partial file. Re-raise so the caller can log + skip.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _ensure_clap_models_sync() -> None:
    """Synchronous core. Called from the async wrapper inside a thread."""
    if not settings.CLAP_ENABLED:
        return

    base = Path(settings.CLAP_MODEL_DIR)
    targets = [
        ("text", base / settings.CLAP_TEXT_MODEL_FILE, settings.CLAP_TEXT_MODEL_URL, _MIN_SIZES["text"]),
        ("audio", base / settings.CLAP_AUDIO_MODEL_FILE, settings.CLAP_AUDIO_MODEL_URL, _MIN_SIZES["audio"]),
        (
            "tokenizer",
            base / settings.CLAP_TOKENIZER_FILE,
            settings.CLAP_TOKENIZER_URL,
            _MIN_SIZES["tokenizer"],
        ),
    ]

    missing = [(label, dest, url, ms) for label, dest, url, ms in targets if not _file_ok(dest, ms)]
    if not missing:
        logger.info("CLAP setup: all 3 model files already present at %s", base)
        return

    logger.info(
        "CLAP setup: %d/%d files missing or partial — downloading into %s",
        len(missing),
        len(targets),
        base,
    )
    for label, dest, url, min_size in missing:
        try:
            _download_one(url, dest, min_size, label)
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
            logger.warning(
                "CLAP setup: failed to download %s from %s: %s — text strategy will return 503 until this resolves",
                label,
                url,
                e,
            )


async def ensure_clap_models() -> None:
    """Async entry point. Runs the (potentially slow) blocking download in a thread."""
    if not settings.CLAP_ENABLED:
        return
    await asyncio.to_thread(_ensure_clap_models_sync)
