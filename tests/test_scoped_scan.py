"""GrooveIQ – Tests for path-scoped library scans (issue #150).

Covers the pure/decision logic that keeps a completed download off the
hours-long full-library walk:

  - download_watcher._album_dir_for_scan  — resolve an album folder from a
    backend-reported file path (or fall back to a full scan)
  - media_server._plex_partial_path       — map one album dir to a Plex-visible
    partial-refresh path
  - library_scanner.trigger_scan           — coalesce concurrent scoped/full
    requests instead of dropping them

Run with:  .venv-test/bin/pytest tests/test_scoped_scan.py -v
"""

from __future__ import annotations

import app.services.download_watcher as dw
import app.services.media_server as ms
import app.workers.library_scanner as ls
from app.core.config import settings

# ---------------------------------------------------------------------------
# download_watcher._album_dir_for_scan
# ---------------------------------------------------------------------------


def test_album_dir_resolves_parent(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MUSIC_LIBRARY_PATH", str(tmp_path), raising=False)
    fp = str(tmp_path / "Boards of Canada" / "Geogaddi" / "01 - Music Is Math.flac")
    assert dw._album_dir_for_scan(fp) == str(tmp_path / "Boards of Canada" / "Geogaddi")


def test_album_dir_none_when_no_path(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MUSIC_LIBRARY_PATH", str(tmp_path), raising=False)
    assert dw._album_dir_for_scan(None) is None
    assert dw._album_dir_for_scan("") is None


def test_album_dir_none_when_outside_library(monkeypatch, tmp_path):
    # A path that isn't under MUSIC_LIBRARY_PATH must not scope a scan (→ full).
    monkeypatch.setattr(settings, "MUSIC_LIBRARY_PATH", str(tmp_path / "library"), raising=False)
    fp = str(tmp_path / "elsewhere" / "Album" / "track.flac")
    assert dw._album_dir_for_scan(fp) is None


def test_album_dir_none_for_track_at_library_root(monkeypatch, tmp_path):
    # A loose track directly at the library root has no album subfolder → full scan.
    monkeypatch.setattr(settings, "MUSIC_LIBRARY_PATH", str(tmp_path), raising=False)
    fp = str(tmp_path / "loose.flac")
    assert dw._album_dir_for_scan(fp) is None


# ---------------------------------------------------------------------------
# media_server._plex_partial_path
# ---------------------------------------------------------------------------


def test_plex_partial_maps_single_album(monkeypatch):
    monkeypatch.setattr(settings, "MUSIC_LIBRARY_PATH", "/music", raising=False)
    monkeypatch.setattr(settings, "MEDIA_SERVER_MUSIC_PATH", "/data/media", raising=False)
    assert ms._plex_partial_path({"/music/Artist/Album"}) == "/data/media/Artist/Album"


def test_plex_partial_none_when_no_mapping(monkeypatch):
    # Without MEDIA_SERVER_MUSIC_PATH we can't hand Plex a path it recognises.
    monkeypatch.setattr(settings, "MUSIC_LIBRARY_PATH", "/music", raising=False)
    monkeypatch.setattr(settings, "MEDIA_SERVER_MUSIC_PATH", "", raising=False)
    assert ms._plex_partial_path({"/music/Artist/Album"}) is None


def test_plex_partial_none_when_multiple_albums(monkeypatch):
    # One /refresh?path= call targets one dir; >1 pending → full refresh.
    monkeypatch.setattr(settings, "MUSIC_LIBRARY_PATH", "/music", raising=False)
    monkeypatch.setattr(settings, "MEDIA_SERVER_MUSIC_PATH", "/data/media", raising=False)
    assert ms._plex_partial_path({"/music/A/X", "/music/B/Y"}) is None
    assert ms._plex_partial_path(set()) is None


def test_plex_partial_none_when_outside_root(monkeypatch):
    monkeypatch.setattr(settings, "MUSIC_LIBRARY_PATH", "/music", raising=False)
    monkeypatch.setattr(settings, "MEDIA_SERVER_MUSIC_PATH", "/data/media", raising=False)
    assert ms._plex_partial_path({"/elsewhere/Album"}) is None


# ---------------------------------------------------------------------------
# library_scanner.trigger_scan coalescing
# ---------------------------------------------------------------------------


async def test_trigger_scan_starts_when_idle(monkeypatch):
    started: list = []

    async def _fake_start(scope_paths):
        started.append(scope_paths)
        return 42

    monkeypatch.setattr(ls, "_running_scan_id", None, raising=False)
    monkeypatch.setattr(ls, "_running_scan_scoped", False, raising=False)
    monkeypatch.setattr(ls, "_pending_full", False, raising=False)
    monkeypatch.setattr(ls, "_pending_scopes", set(), raising=False)
    monkeypatch.setattr(ls, "_start_scan", _fake_start)

    assert await ls.trigger_scan("/music/A/Album") == 42
    assert started == [["/music/A/Album"]]


async def test_trigger_scan_queues_scope_while_scoped_scan_runs(monkeypatch):
    async def _fake_start(scope_paths):  # pragma: no cover - must NOT be called
        raise AssertionError("should not start a second scan while one runs")

    monkeypatch.setattr(ls, "_running_scan_id", 7, raising=False)
    monkeypatch.setattr(ls, "_running_scan_scoped", True, raising=False)
    monkeypatch.setattr(ls, "_pending_full", False, raising=False)
    monkeypatch.setattr(ls, "_pending_scopes", set(), raising=False)
    monkeypatch.setattr(ls, "_start_scan", _fake_start)

    # A different album arriving mid-scan is queued, not dropped, and returns the
    # running scan id.
    assert await ls.trigger_scan("/music/B/Other") == 7
    assert ls._pending_scopes == {"/music/B/Other"}
    assert ls._pending_full is False


async def test_trigger_scan_scope_absorbed_by_running_full_scan(monkeypatch):
    monkeypatch.setattr(ls, "_running_scan_id", 7, raising=False)
    monkeypatch.setattr(ls, "_running_scan_scoped", False, raising=False)  # full scan running
    monkeypatch.setattr(ls, "_pending_full", False, raising=False)
    monkeypatch.setattr(ls, "_pending_scopes", set(), raising=False)

    # A running full scan already covers this album — nothing queued.
    assert await ls.trigger_scan("/music/B/Other") == 7
    assert ls._pending_scopes == set()
    assert ls._pending_full is False


async def test_trigger_full_while_running_marks_pending_full(monkeypatch):
    monkeypatch.setattr(ls, "_running_scan_id", 7, raising=False)
    monkeypatch.setattr(ls, "_running_scan_scoped", True, raising=False)
    monkeypatch.setattr(ls, "_pending_full", False, raising=False)
    monkeypatch.setattr(ls, "_pending_scopes", {"/music/B/Other"}, raising=False)

    assert await ls.trigger_scan(None) == 7
    assert ls._pending_full is True


async def test_drain_pending_full_subsumes_scopes(monkeypatch):
    started: list = []

    async def _fake_start(scope_paths):
        started.append(scope_paths)
        return 99

    monkeypatch.setattr(ls, "_pending_full", True, raising=False)
    monkeypatch.setattr(ls, "_pending_scopes", {"/music/A/Album"}, raising=False)
    monkeypatch.setattr(ls, "_start_scan", _fake_start)

    await ls._drain_pending_scans()
    assert started == [None]  # a full scan, scopes discarded
    assert ls._pending_full is False
    assert ls._pending_scopes == set()


async def test_drain_pending_scopes_coalesce(monkeypatch):
    started: list = []

    async def _fake_start(scope_paths):
        started.append(scope_paths)
        return 99

    monkeypatch.setattr(ls, "_pending_full", False, raising=False)
    monkeypatch.setattr(ls, "_pending_scopes", {"/music/B/Y", "/music/A/X"}, raising=False)
    monkeypatch.setattr(ls, "_start_scan", _fake_start)

    await ls._drain_pending_scans()
    assert started == [["/music/A/X", "/music/B/Y"]]  # sorted, coalesced into one scan
    assert ls._pending_scopes == set()


async def test_drain_noop_when_nothing_pending(monkeypatch):
    async def _fake_start(scope_paths):  # pragma: no cover - must NOT be called
        raise AssertionError("nothing pending; should not start a scan")

    monkeypatch.setattr(ls, "_pending_full", False, raising=False)
    monkeypatch.setattr(ls, "_pending_scopes", set(), raising=False)
    monkeypatch.setattr(ls, "_start_scan", _fake_start)

    await ls._drain_pending_scans()  # no exception = pass
