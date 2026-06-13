"""Tests for the lyrics acquisition drain (queue mechanics, pacing, retry)."""

from __future__ import annotations

import sys
import types
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.lyrics_drain as drain
from app.core.config import settings
from app.models.db import Base, LyricsRequest, TrackFeatures
from app.services.lyrics import (
    OUTCOME_ASR_DEFERRED,
    OUTCOME_FOUND,
    OUTCOME_INSTRUMENTAL,
    OUTCOME_NO_LYRICS,
    OUTCOME_SEARCH_ERROR,
    LyricsResolution,
)

_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(_TEST_DB_URL, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        for i in range(6):
            session.add(TrackFeatures(
                track_id=f"t{i}", file_path=f"/m/{i}.flac", artist="A", title=f"T{i}",
                duration=180.0, instrumentalness=0.1,
            ))
        await session.commit()
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(settings, "LYRICS_ENABLED", True)
    monkeypatch.setattr(settings, "LYRICS_LRCLIB_ENABLED", False)
    monkeypatch.setattr(settings, "LYRICS_ASR_ENABLED", False)
    monkeypatch.setattr(settings, "LYRICS_API_URL", "")
    monkeypatch.setattr(settings, "LYRICS_DRAIN_BATCH_SIZE", 100)
    monkeypatch.setattr(settings, "LYRICS_DRAIN_MAX_PER_HOUR", 0)
    monkeypatch.setattr(settings, "LYRICS_DRAIN_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(settings, "LYRICS_DRAIN_POLL_MINUTES", 5)
    yield


def _scripted_resolve(script):
    async def fake(track, **kw):
        return script[track.track_id]
    return fake


@pytest.mark.asyncio
async def test_disabled_skips(db_session, monkeypatch):
    monkeypatch.setattr(settings, "LYRICS_ENABLED", False)
    out = await drain.run_lyrics_tick(db_session)
    assert out == {"skipped": "disabled"}


@pytest.mark.asyncio
async def test_tick_resolves_and_persists(db_session, monkeypatch):
    script = {
        "t0": LyricsResolution(outcome=OUTCOME_FOUND, source="lrclib", quality=3, synced="[00:01]x", plain="x"),
        "t1": LyricsResolution(outcome=OUTCOME_NO_LYRICS, source="none", cheap_exhausted=True),
        "t2": LyricsResolution(outcome=OUTCOME_SEARCH_ERROR, detail="lrclib down"),
        "t3": LyricsResolution(outcome=OUTCOME_INSTRUMENTAL, source="instrumental", cheap_exhausted=True),
        "t4": LyricsResolution(outcome=OUTCOME_NO_LYRICS, source="none", cheap_exhausted=True),
        "t5": LyricsResolution(outcome=OUTCOME_FOUND, source="embedded", quality=2, plain="p"),
    }
    monkeypatch.setattr(drain, "resolve_lyrics", _scripted_resolve(script))
    summary = await drain.run_lyrics_tick(db_session)
    assert summary["processed"] == 6

    rows = {r.track_id: r for r in (await db_session.execute(select(LyricsRequest))).scalars().all()}
    tfs = {t.track_id: t for t in (await db_session.execute(select(TrackFeatures))).scalars().all()}

    assert rows["t0"].status == "complete"
    assert tfs["t0"].lyrics_source == "lrclib" and tfs["t0"].lyrics_version == drain.LYRICS_VERSION
    assert rows["t1"].status == "no_lyrics" and rows["t1"].attempt_count == 1
    assert rows["t1"].next_retry_at is not None and rows["t1"].cheap_exhausted
    assert rows["t2"].status == "search_error" and rows["t2"].attempt_count == 0  # no bump
    assert rows["t3"].status == "instrumental" and tfs["t3"].lyrics_source == "instrumental"
    assert rows["t5"].status == "complete" and tfs["t5"].lyrics_quality == 2


@pytest.mark.asyncio
async def test_no_lyrics_exhausts_to_permanently_skipped(db_session, monkeypatch):
    script = {f"t{i}": LyricsResolution(outcome=OUTCOME_NO_LYRICS, source="none", cheap_exhausted=True) for i in range(6)}
    monkeypatch.setattr(drain, "resolve_lyrics", _scripted_resolve(script))

    await drain.run_lyrics_tick(db_session)  # attempt 1 -> no_lyrics
    # make them retry-due
    await db_session.execute(update(LyricsRequest).values(next_retry_at=0))
    await db_session.commit()
    await drain.run_lyrics_tick(db_session)  # attempt 2 -> permanently_skipped (max_attempts=2)

    rows = (await db_session.execute(select(LyricsRequest))).scalars().all()
    assert all(r.status == "permanently_skipped" and r.attempt_count == 2 for r in rows)


@pytest.mark.asyncio
async def test_search_error_does_not_bump_attempts_or_permanently_skip(db_session, monkeypatch):
    script = {f"t{i}": LyricsResolution(outcome=OUTCOME_SEARCH_ERROR, detail="down") for i in range(6)}
    monkeypatch.setattr(drain, "resolve_lyrics", _scripted_resolve(script))
    for _ in range(3):
        await drain.run_lyrics_tick(db_session)
        await db_session.execute(update(LyricsRequest).values(next_retry_at=0))
        await db_session.commit()
    rows = (await db_session.execute(select(LyricsRequest))).scalars().all()
    assert all(r.status == "search_error" and r.attempt_count == 0 for r in rows)


@pytest.mark.asyncio
async def test_asr_budget_caps_and_defers(db_session, monkeypatch):
    # ASR enabled, max_per_hour=12, poll=5 -> asr_per_tick = ceil(12*5/60) = 1
    monkeypatch.setattr(settings, "LYRICS_ASR_ENABLED", True)
    monkeypatch.setattr(settings, "LYRICS_API_URL", "http://gpu:8300")
    monkeypatch.setattr(settings, "LYRICS_DRAIN_MAX_PER_HOUR", 12)

    # Provide a fake lyrics_asr module so the drain gets a non-None client.
    mod = types.ModuleType("app.services.lyrics_asr")
    mod.get_lyrics_asr_client = lambda: object()
    monkeypatch.setitem(sys.modules, "app.services.lyrics_asr", mod)

    async def fake(track, *, lrclib_client=None, asr_client=None, allow_asr=True, skip_cheap_tiers=False):
        if allow_asr:
            return LyricsResolution(outcome=OUTCOME_FOUND, source="asr", quality=0, synced="[00:01]x",
                                    asr_used=True, cheap_exhausted=True)
        return LyricsResolution(outcome=OUTCOME_ASR_DEFERRED, cheap_exhausted=True)

    monkeypatch.setattr(drain, "resolve_lyrics", fake)
    summary = await drain.run_lyrics_tick(db_session)
    assert summary["asr_used"] == 1

    rows = (await db_session.execute(select(LyricsRequest))).scalars().all()
    complete = [r for r in rows if r.status == "complete"]
    deferred = [r for r in rows if r.status == "queued"]
    assert len(complete) == 1 and len(deferred) == 5
    assert all(r.attempt_count == 0 and r.next_retry_at is not None for r in deferred)


@pytest.mark.asyncio
async def test_reset_state_requeues_and_clears_cheap_exhausted(db_session, monkeypatch):
    script = {f"t{i}": LyricsResolution(outcome=OUTCOME_NO_LYRICS, source="none", cheap_exhausted=True) for i in range(6)}
    monkeypatch.setattr(drain, "resolve_lyrics", _scripted_resolve(script))
    await drain.run_lyrics_tick(db_session)
    n = await drain.reset_state(db_session, "no_lyrics")
    assert n == 6
    rows = (await db_session.execute(select(LyricsRequest))).scalars().all()
    assert all(r.status == "queued" and r.attempt_count == 0 and not r.cheap_exhausted for r in rows)


@pytest.mark.asyncio
async def test_reset_unknown_scope_raises(db_session):
    with pytest.raises(ValueError):
        await drain.reset_state(db_session, "bogus")


@pytest.mark.asyncio
async def test_reaper_resets_stale_searching(db_session, monkeypatch):
    # Insert a row stuck in `searching` long ago.
    async with async_sessionmaker(db_session.bind, expire_on_commit=False)() as s:
        s.add(LyricsRequest(track_id="t0", status="searching", created_at=0, updated_at=0))
        await s.commit()
    reaped = await drain._reap_stale_searching(db_session)
    assert reaped == 1
    row = (await db_session.execute(select(LyricsRequest).where(LyricsRequest.track_id == "t0"))).scalar_one()
    assert row.status == "queued"


@pytest.mark.asyncio
async def test_stats(db_session, monkeypatch):
    script = {f"t{i}": LyricsResolution(outcome=OUTCOME_FOUND, source="lrclib", quality=3, plain="x") for i in range(6)}
    monkeypatch.setattr(drain, "resolve_lyrics", _scripted_resolve(script))
    await drain.run_lyrics_tick(db_session)
    stats = await drain.get_stats(db_session)
    assert stats["total_tracks"] == 6 and stats["resolved"] == 6
    assert stats["by_status"]["complete"] == 6
    assert stats["by_source"]["lrclib"] == 6
