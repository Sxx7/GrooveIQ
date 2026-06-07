"""
Tests for the download-sidecar /music readiness self-check (GitHub issue #123).

`streamrip-api` and `spotdl-api` are separate Docker images that bind-mount the
host music library at /music. If the host dir backing that mount is replaced
while the long-lived container keeps running, the container holds the old (now
empty, root-owned) inode and every download fails with
``[Errno 13] Permission denied`` — yet a disk-blind /health stayed green. The
``_music_status`` / ``_music_ready`` helpers turn that silent failure into an
unhealthy container.

The sidecars live outside the app package and have their own (optional)
dependencies, so we load each ``main.py`` by file path. streamrip-api pulls in
``tomlkit`` at import time, so it's skipped where that isn't installed; the
readiness code is identical in both, so spotdl-api alone covers the algorithm.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(rel_path: str, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / rel_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_sidecars():
    mods = []
    # spotdl-api imports cleanly (spotdl itself is lazy-loaded).
    try:
        mods.append(pytest.param(_load("spotdl-api/main.py", "spotdl_sidecar_main"), id="spotdl-api"))
    except Exception as exc:  # pragma: no cover - environment dependent
        mods.append(pytest.param(None, id="spotdl-api", marks=pytest.mark.skip(reason=f"import failed: {exc}")))
    # streamrip-api needs tomlkit at import time.
    try:
        import tomlkit  # noqa: F401

        mods.append(pytest.param(_load("streamrip-api/main.py", "streamrip_sidecar_main"), id="streamrip-api"))
    except Exception as exc:
        mods.append(pytest.param(None, id="streamrip-api", marks=pytest.mark.skip(reason=f"import failed: {exc}")))
    return mods


@pytest.fixture(params=_load_sidecars())
def sidecar(request):
    return request.param


def test_music_status_writable_populated(sidecar, tmp_path, monkeypatch):
    d = tmp_path / "music"
    d.mkdir()
    (d / "ArtistA").mkdir()
    monkeypatch.setattr(sidecar, "OUTPUT_DIR", str(d))
    monkeypatch.setattr(sidecar, "MUSIC_MIN_ENTRIES", 0)
    st = sidecar._music_status()
    assert st["exists"] is True
    assert st["writable"] is True
    assert st["entries"] == 1
    assert st["error"] is None
    assert sidecar._music_ready(st) is True
    # The write probe must clean up after itself — no residue in the library dir.
    assert os.listdir(d) == ["ArtistA"]


def test_music_status_missing_dir_is_not_ready(sidecar, tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar, "OUTPUT_DIR", str(tmp_path / "does-not-exist"))
    st = sidecar._music_status()
    assert st["exists"] is False
    assert st["error"]
    assert sidecar._music_ready(st) is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write regardless of permission bits")
def test_music_status_not_writable_is_not_ready(sidecar, tmp_path, monkeypatch):
    """The actual stale-mount signature: dir present but not writable by us."""
    d = tmp_path / "readonly"
    d.mkdir()
    (d / "ArtistA").mkdir()
    os.chmod(d, 0o500)  # r-x — listable but not writable for the owner
    try:
        monkeypatch.setattr(sidecar, "OUTPUT_DIR", str(d))
        st = sidecar._music_status()
        assert st["exists"] is True
        assert st["writable"] is False
        assert "not writable" in (st["error"] or "")
        assert sidecar._music_ready(st) is False
    finally:
        os.chmod(d, 0o700)  # restore so pytest can clean up tmp_path


def test_empty_library_is_ready_by_default_but_gated_when_min_entries_set(sidecar, tmp_path, monkeypatch):
    """A legitimately empty (but writable) library must not false-positive as
    unhealthy by default; operators opt into the emptiness gate via
    MUSIC_MIN_ENTRIES."""
    d = tmp_path / "empty"
    d.mkdir()
    monkeypatch.setattr(sidecar, "OUTPUT_DIR", str(d))

    monkeypatch.setattr(sidecar, "MUSIC_MIN_ENTRIES", 0)
    st = sidecar._music_status()
    assert st["writable"] is True
    assert st["entries"] == 0
    assert sidecar._music_ready(st) is True  # no false positive

    monkeypatch.setattr(sidecar, "MUSIC_MIN_ENTRIES", 1)
    assert sidecar._music_ready(sidecar._music_status()) is False
