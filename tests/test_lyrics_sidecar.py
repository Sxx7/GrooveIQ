"""Tests for the lyrics-api sidecar helpers (LRC builder, path guard, readiness).

The sidecar is a standalone FastAPI app under lyrics-api/ (not part of the
``app`` package); we load it by path. faster-whisper / ctranslate2 are imported
lazily inside the model loader, so importing the module needs no GPU stack.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

_SIDE_CAR = Path(__file__).resolve().parent.parent / "lyrics-api" / "main.py"


def _load_sidecar():
    spec = importlib.util.spec_from_file_location("lyrics_api_main", _SIDE_CAR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lyrics_api_main"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sidecar():
    return _load_sidecar()


def test_lrc_timestamp_format(sidecar):
    assert sidecar._ts(0) == "00:00.00"
    assert sidecar._ts(61.23) == "01:01.23"
    assert sidecar._ts(3723.5) == "62:03.50"
    assert sidecar._ts(-5) == "00:00.00"


def test_segments_to_lrc_skips_empty(sidecar):
    segs = [
        {"start": 1.0, "end": 2.0, "text": " Hello "},
        {"start": None, "end": 3.0, "text": ""},
        {"start": 5.5, "end": 6.0, "text": "World"},
    ]
    assert sidecar._segments_to_lrc(segs) == "[00:01.00]Hello\n[00:05.50]World"


def test_music_probe_and_ready(sidecar):
    # The ASR sidecar mounts /music read-only and gates on READABILITY (it only
    # reads to transcribe), not writability like the download sidecars.
    d = tempfile.mkdtemp()
    sidecar.OUTPUT_DIR = d
    status = sidecar._music_status()
    assert status["exists"] and status["readable"]
    assert sidecar._music_ready(status) is True
    # Non-existent dir -> not ready.
    sidecar.OUTPUT_DIR = "/nonexistent/dir/xyz"
    bad = sidecar._music_status()
    assert not bad["exists"] and sidecar._music_ready(bad) is False
    sidecar.OUTPUT_DIR = d


def test_resolve_path_guards(sidecar):
    from fastapi import HTTPException

    d = tempfile.mkdtemp()
    sidecar.OUTPUT_DIR = d
    f = os.path.join(d, "song.flac")
    open(f, "w").close()

    resolved = sidecar._resolve_path("song.flac")
    assert str(resolved) == os.path.realpath(f)

    with pytest.raises(HTTPException) as exc:
        sidecar._resolve_path("../../etc/passwd")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        sidecar._resolve_path("missing.flac")
    assert exc.value.status_code == 404


def test_detect_device_without_gpu(sidecar, monkeypatch):
    # Force the auto path; ctranslate2 may be absent -> cpu.
    monkeypatch.setattr(sidecar, "LYRICS_DEVICE", "auto")
    assert sidecar._detect_device() in ("cpu", "cuda")
    monkeypatch.setattr(sidecar, "LYRICS_DEVICE", "cpu")
    assert sidecar._detect_device() == "cpu"
