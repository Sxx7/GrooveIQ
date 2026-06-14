"""Tests for the lyrics cascade, embedded-tag parsing, and the display endpoint."""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.lyrics as lyr
from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, TrackFeatures
from app.services import metadata_reader as mr
from app.services.lyrics import (
    OUTCOME_ASR_DEFERRED,
    OUTCOME_FOUND,
    OUTCOME_INSTRUMENTAL,
    OUTCOME_NO_LYRICS,
    OUTCOME_SEARCH_ERROR,
    resolve_lyrics,
)

# ---------------------------------------------------------------------------
# LRC / text helpers
# ---------------------------------------------------------------------------


def test_looks_like_lrc():
    assert mr._looks_like_lrc("[00:12.34]hi")
    assert mr._looks_like_lrc("[ar:x]\n[01:02]line")
    assert not mr._looks_like_lrc("just plain lyrics\nno timestamps")
    assert not mr._looks_like_lrc("")


def test_strip_lrc_timestamps():
    lrc = "[ar:Artist]\n[00:12.34]Hello world\n[00:15.00][00:20.00]Chorus"
    assert mr._strip_lrc_timestamps(lrc) == "Hello world\nChorus"


def test_clean_lyric_text_strips_nul_and_crlf_and_caps():
    assert mr._clean_lyric_text("  a\x00b\r\nc  ") == "ab\nc"
    assert mr._clean_lyric_text("   ") is None
    assert mr._clean_lyric_text(["first", "second"]) == "first"
    assert mr._clean_lyric_text(None) is None


def test_sylt_to_lrc():
    out = mr._sylt_to_lrc([("Line one", 12340), ("\nLine two", 15000)])
    assert out == "[00:12.34]Line one\n[00:15.00]Line two"
    assert mr._sylt_to_lrc(None) is None


def test_sylt_to_lrc_normalizes_cr_and_crlf():
    # SYLT segments from Windows / old-Mac taggers may carry \r\n or \r; those
    # must not leak into the LRC line (they would corrupt the timestamped line).
    out = mr._sylt_to_lrc([("\r\nWindows line\r", 1000), ("\rMac line", 2000)])
    assert out == "[00:01.00]Windows line\n[00:02.00]Mac line"
    assert "\r" not in out


# ---------------------------------------------------------------------------
# Embedded-tag parsing (mutagen.File monkeypatched per container)
# ---------------------------------------------------------------------------


class _ID3Tags:
    def __init__(self, uslt=None, sylt=None):
        self._uslt = uslt or []
        self._sylt = sylt or []

    def getall(self, key):
        return {"USLT": self._uslt, "SYLT": self._sylt}.get(key, [])

    def get(self, key, default=None):
        return default  # ID3 has no \xa9lyr


class _MP4Tags(dict):
    pass  # supports .get, no .getall


class _VComment(dict):
    pass  # supports .get, no .getall (keys lower-cased by caller)


class _Audio:
    """Mimics a mutagen FileType: dict-like .get() over tags + a .tags attr."""

    def __init__(self, tags, vorbis=None):
        self.tags = tags
        self._vorbis = {k.lower(): v for k, v in (vorbis or {}).items()}

    def get(self, key, default=None):
        return self._vorbis.get(key.lower(), default)


def _patch_file(monkeypatch, audio):
    monkeypatch.setattr(mr.mutagen, "File", lambda path: audio)


def test_embedded_id3_uslt_plain(monkeypatch):
    audio = _Audio(_ID3Tags(uslt=[SimpleNamespace(text="line a\nline b")]))
    _patch_file(monkeypatch, audio)
    out = mr.read_embedded_lyrics("/m/x.mp3")
    assert out == {"plain": "line a\nline b", "synced": None, "source": "embedded"}


def test_embedded_id3_sylt_synced(monkeypatch):
    audio = _Audio(_ID3Tags(sylt=[SimpleNamespace(text=[("Hi", 1000), ("Bye", 2500)])]))
    _patch_file(monkeypatch, audio)
    out = mr.read_embedded_lyrics("/m/x.mp3")
    assert out["source"] == "embedded"
    assert out["synced"] == "[00:01.00]Hi\n[00:02.50]Bye"


def test_embedded_mp4_lyr(monkeypatch):
    audio = _Audio(_MP4Tags({"\xa9lyr": ["mp4 lyrics here"]}))
    _patch_file(monkeypatch, audio)
    out = mr.read_embedded_lyrics("/m/x.m4a")
    assert out["plain"] == "mp4 lyrics here" and out["synced"] is None


def test_embedded_vorbis_plain_lyrics(monkeypatch):
    audio = _Audio(_VComment(), vorbis={"LYRICS": ["plain flac lyrics"]})
    _patch_file(monkeypatch, audio)
    out = mr.read_embedded_lyrics("/m/x.flac")
    assert out["plain"] == "plain flac lyrics" and out["synced"] is None


def test_embedded_vorbis_lyrics_holding_lrc_is_synced(monkeypatch):
    audio = _Audio(_VComment(), vorbis={"LYRICS": ["[00:01.00]a\n[00:02.00]b"]})
    _patch_file(monkeypatch, audio)
    out = mr.read_embedded_lyrics("/m/x.flac")
    assert out["synced"] == "[00:01.00]a\n[00:02.00]b"
    assert out["plain"] == "a\nb"  # derived by stripping timestamps


def test_embedded_none(monkeypatch):
    _patch_file(monkeypatch, _Audio(_VComment(), vorbis={}))
    assert mr.read_embedded_lyrics("/m/x.flac") == {"plain": None, "synced": None, "source": None}


def test_embedded_missing_file_returns_empty():
    assert mr.read_embedded_lyrics("/nonexistent/file.mp3") == {"plain": None, "synced": None, "source": None}


# ---------------------------------------------------------------------------
# Cascade precedence (resolve_lyrics)
# ---------------------------------------------------------------------------


def _track(**kw):
    base = dict(artist="A", title="T", album="Al", duration=180.0, file_path="/m/x.flac", instrumentalness=0.1)
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeLrclib:
    def __init__(self, outcome):
        self._outcome = outcome

    async def lookup(self, artist, title, album=None, duration=None):
        return self._outcome


def _lr(ok=True, found=False, instrumental=False, plain=None, synced=None, error=None):
    from app.services.lrclib import LrclibOutcome, LrclibResult

    return LrclibOutcome(
        ok=ok, error=error, result=LrclibResult(found=found, instrumental=instrumental, plain=plain, synced=synced)
    )


class _FakeAsr:
    def __init__(self, **kw):
        self._kw = kw

    async def transcribe(self, path):
        return SimpleNamespace(**self._kw)


@pytest_asyncio.fixture(autouse=True)
def _enable_lyrics(monkeypatch):
    monkeypatch.setattr(settings, "LYRICS_ENABLED", True)
    monkeypatch.setattr(settings, "LYRICS_LRCLIB_ENABLED", True)
    monkeypatch.setattr(settings, "LYRICS_ASR_ENABLED", False)
    monkeypatch.setattr(settings, "LYRICS_API_URL", "")
    yield


def _set_embedded(monkeypatch, plain=None, synced=None):
    monkeypatch.setattr(
        lyr,
        "read_embedded_lyrics",
        lambda fp: {"plain": plain, "synced": synced, "source": "embedded" if (plain or synced) else None},
    )


@pytest.mark.asyncio
async def test_cascade_embedded_synced_wins(monkeypatch):
    _set_embedded(monkeypatch, synced="[00:01]x", plain="p")
    res = await resolve_lyrics(_track(), lrclib_client=_FakeLrclib(_lr(found=True, synced="[00:02]y")))
    assert res.outcome == OUTCOME_FOUND and res.source == "embedded" and res.quality == 4


@pytest.mark.asyncio
async def test_cascade_lrclib_synced_beats_embedded_plain(monkeypatch):
    _set_embedded(monkeypatch, plain="embed plain")
    res = await resolve_lyrics(_track(), lrclib_client=_FakeLrclib(_lr(found=True, synced="[00:02]y", plain="lp")))
    assert res.source == "lrclib" and res.quality == 3


@pytest.mark.asyncio
async def test_cascade_embedded_plain_beats_lrclib_plain(monkeypatch):
    _set_embedded(monkeypatch, plain="embed plain")
    res = await resolve_lyrics(_track(), lrclib_client=_FakeLrclib(_lr(found=True, plain="lp")))
    assert res.source == "embedded" and res.quality == 2


@pytest.mark.asyncio
async def test_cascade_lrclib_instrumental(monkeypatch):
    _set_embedded(monkeypatch)
    res = await resolve_lyrics(_track(), lrclib_client=_FakeLrclib(_lr(found=True, instrumental=True)))
    assert res.outcome == OUTCOME_INSTRUMENTAL and res.source == "instrumental"


@pytest.mark.asyncio
async def test_cascade_lrclib_error_is_search_error(monkeypatch):
    _set_embedded(monkeypatch)
    res = await resolve_lyrics(_track(), lrclib_client=_FakeLrclib(_lr(ok=False, error="boom")))
    assert res.outcome == OUTCOME_SEARCH_ERROR and not res.cheap_exhausted


@pytest.mark.asyncio
async def test_cascade_instrumental_gate_blocks_asr(monkeypatch):
    _set_embedded(monkeypatch)
    monkeypatch.setattr(settings, "LYRICS_ASR_INSTRUMENTAL_MAX", 0.5)
    res = await resolve_lyrics(_track(instrumentalness=0.9), lrclib_client=_FakeLrclib(_lr(ok=True, found=False)))
    assert res.outcome == OUTCOME_INSTRUMENTAL and "gate" in res.detail and res.cheap_exhausted


@pytest.mark.asyncio
async def test_cascade_asr_when_voiced(monkeypatch):
    _set_embedded(monkeypatch)
    monkeypatch.setattr(settings, "LYRICS_ASR_ENABLED", True)
    monkeypatch.setattr(settings, "LYRICS_API_URL", "http://gpu:8300")
    asr = _FakeAsr(ok=True, text="sung", synced="[00:01]sung", language="en", error=None)
    res = await resolve_lyrics(_track(), lrclib_client=_FakeLrclib(_lr(ok=True, found=False)), asr_client=asr)
    assert res.source == "asr" and res.quality == 0 and res.language == "en" and res.asr_used


@pytest.mark.asyncio
async def test_cascade_asr_deferred_when_budget_withheld(monkeypatch):
    _set_embedded(monkeypatch)
    monkeypatch.setattr(settings, "LYRICS_ASR_ENABLED", True)
    monkeypatch.setattr(settings, "LYRICS_API_URL", "http://gpu:8300")
    asr = _FakeAsr(ok=True, text="x", synced=None, language="en", error=None)
    res = await resolve_lyrics(
        _track(), lrclib_client=_FakeLrclib(_lr(ok=True, found=False)), asr_client=asr, allow_asr=False
    )
    assert res.outcome == OUTCOME_ASR_DEFERRED and not res.asr_used


@pytest.mark.asyncio
async def test_cascade_no_lyrics_when_asr_disabled_and_voiced(monkeypatch):
    _set_embedded(monkeypatch)
    res = await resolve_lyrics(_track(), lrclib_client=_FakeLrclib(_lr(ok=True, found=False)))
    assert res.outcome == OUTCOME_NO_LYRICS and res.cheap_exhausted


@pytest.mark.asyncio
async def test_cascade_skip_cheap_tiers_goes_straight_to_asr(monkeypatch):
    # If skip_cheap_tiers=True, embedded must not even be read.
    def _boom(fp):
        raise AssertionError("embedded should be skipped")

    monkeypatch.setattr(lyr, "read_embedded_lyrics", _boom)
    monkeypatch.setattr(settings, "LYRICS_ASR_ENABLED", True)
    monkeypatch.setattr(settings, "LYRICS_API_URL", "http://gpu:8300")
    asr = _FakeAsr(ok=True, text="sung", synced="[00:01]s", language="en", error=None)
    res = await resolve_lyrics(_track(), asr_client=asr, skip_cheap_tiers=True)
    assert res.source == "asr"


# ---------------------------------------------------------------------------
# Display endpoint (GET /v1/tracks/{id}/lyrics)
# ---------------------------------------------------------------------------

_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(_TEST_DB_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)


async def _override_session() -> AsyncGenerator[AsyncSession, None]:
    async with _Session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture
async def lyrics_client():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.dependency_overrides[get_session] = _override_session
    async with _Session() as s:
        s.add(
            TrackFeatures(
                track_id="lyr_real",
                file_path="/m/a.flac",
                title="Real",
                artist="A",
                lyrics_source="lrclib",
                lyrics_quality=3,
                lyrics_plain="hi",
                lyrics_synced="[00:01]hi",
                lyrics_language="en",
                lyrics_fetched_at=int(time.time()),
            )
        )
        s.add(
            TrackFeatures(
                track_id="lyr_instr",
                file_path="/m/b.flac",
                title="Instr",
                artist="A",
                lyrics_source="instrumental",
            )
        )
        s.add(TrackFeatures(track_id="lyr_none", file_path="/m/c.flac", title="None", artist="A"))
        await s.commit()
    headers = {"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
        yield c
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_endpoint_real_lyrics(lyrics_client):
    r = await lyrics_client.get("/v1/tracks/lyr_real/lyrics")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "lrclib" and body["is_synced"] is True and body["language"] == "en"
    assert body["synced"] == "[00:01]hi"


@pytest.mark.asyncio
async def test_endpoint_instrumental_is_200(lyrics_client):
    r = await lyrics_client.get("/v1/tracks/lyr_instr/lyrics")
    assert r.status_code == 200
    assert r.json()["source"] == "instrumental" and r.json()["is_synced"] is False


@pytest.mark.asyncio
async def test_endpoint_no_lyrics_is_404(lyrics_client):
    assert (await lyrics_client.get("/v1/tracks/lyr_none/lyrics")).status_code == 404


@pytest.mark.asyncio
async def test_endpoint_unknown_track_is_404(lyrics_client):
    assert (await lyrics_client.get("/v1/tracks/nope/lyrics")).status_code == 404
