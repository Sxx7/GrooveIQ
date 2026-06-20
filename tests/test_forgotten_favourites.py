"""
GrooveIQ — Tests for the forgotten-favourites service
(app/services/forgotten_favourites.py).

Covers the core contract:

  * A dormant favourite surfaces; an otherwise-identical recently-played track
    is excluded (the dormancy gate).
  * Low-satisfaction tracks are excluded (the favourite gate).
  * Never-played tracks are excluded (forgotten favourites must have been played).
  * The min-play-count gate filters one-off plays.
  * Ranking follows affinity × dormancy (loved-and-very-dormant beats
    mildly-loved-and-mildly-dormant).
  * A fresh library still returns a well-formed (empty) response.
"""

from __future__ import annotations

import time

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, TrackFeatures, TrackInteraction, User
from app.services import forgotten_favourites

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)

_DAY = 86400


def _now() -> int:
    return int(time.time())


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _add_track(session, tid, *, artist="AAA", album="Album", title=None):
    session.add(
        TrackFeatures(
            track_id=tid,
            file_path=f"/music/{tid}.mp3",
            artist=artist,
            album=album,
            title=title or tid,
            duration=200.0,
            energy=0.5,
            valence=0.5,
            analyzed_at=_now(),
            analysis_version="1",
        )
    )


async def _add_interaction(
    session,
    user_id,
    tid,
    *,
    play_count=0,
    satisfaction=0.0,
    last_played=None,
    like_count=0,
    repeat_count=0,
):
    session.add(
        TrackInteraction(
            user_id=user_id,
            track_id=tid,
            play_count=play_count,
            satisfaction_score=satisfaction,
            last_played_at=last_played,
            like_count=like_count,
            repeat_count=repeat_count,
            updated_at=_now(),
        )
    )


async def test_dormant_favourite_surfaces_recent_excluded():
    """A loved-and-dormant track surfaces; a loved-but-recent one is filtered."""
    now = _now()
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={}))
        await _add_track(s, "dormant", title="Dormant Gem")
        await _add_track(s, "recent", title="Recent Bop")
        await _add_interaction(s, "alice", "dormant", play_count=10, satisfaction=0.9, last_played=now - 200 * _DAY)
        await _add_interaction(s, "alice", "recent", play_count=10, satisfaction=0.9, last_played=now - 1 * _DAY)
        await s.commit()

    async with _Session() as s:
        res = await forgotten_favourites.recommend_forgotten_favourites(s, "alice", limit=10)

    ids = [t["track_id"] for t in res["tracks"]]
    assert ids == ["dormant"]
    assert res["tracks"][0]["signals"]["days_since_last_play"] >= 180


async def test_low_satisfaction_excluded():
    """A dormant track below min_satisfaction does not qualify as a favourite."""
    now = _now()
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={}))
        await _add_track(s, "loved")
        await _add_track(s, "meh")
        await _add_interaction(s, "alice", "loved", play_count=5, satisfaction=0.8, last_played=now - 120 * _DAY)
        await _add_interaction(s, "alice", "meh", play_count=5, satisfaction=0.1, last_played=now - 120 * _DAY)
        await s.commit()

    async with _Session() as s:
        res = await forgotten_favourites.recommend_forgotten_favourites(s, "alice", limit=10)

    assert [t["track_id"] for t in res["tracks"]] == ["loved"]


async def test_never_played_excluded():
    """A track with no last_played_at is excluded (not a *forgotten* favourite)."""
    now = _now()
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={}))
        await _add_track(s, "played")
        await _add_track(s, "never")
        await _add_interaction(s, "alice", "played", play_count=5, satisfaction=0.9, last_played=now - 120 * _DAY)
        # never: high satisfaction/plays but last_played_at is None
        await _add_interaction(s, "alice", "never", play_count=5, satisfaction=0.9, last_played=None)
        await s.commit()

    async with _Session() as s:
        res = await forgotten_favourites.recommend_forgotten_favourites(s, "alice", limit=10)

    assert [t["track_id"] for t in res["tracks"]] == ["played"]


async def test_min_play_count_gate():
    """A single-play track is filtered by the default min_play_count=2 gate."""
    now = _now()
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={}))
        await _add_track(s, "oneplay")
        await _add_track(s, "manyplays")
        await _add_interaction(s, "alice", "oneplay", play_count=1, satisfaction=0.9, last_played=now - 120 * _DAY)
        await _add_interaction(s, "alice", "manyplays", play_count=8, satisfaction=0.9, last_played=now - 120 * _DAY)
        await s.commit()

    async with _Session() as s:
        res = await forgotten_favourites.recommend_forgotten_favourites(s, "alice", limit=10)

    assert [t["track_id"] for t in res["tracks"]] == ["manyplays"]


async def test_ranking_affinity_times_dormancy():
    """Loved-and-very-dormant outranks mildly-loved-and-mildly-dormant."""
    now = _now()
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={}))
        await _add_track(s, "strong")
        await _add_track(s, "weak")
        # strong: max affinity + very dormant
        await _add_interaction(
            s, "alice", "strong", play_count=40, satisfaction=1.0, like_count=3, last_played=now - 365 * _DAY
        )
        # weak: just over the gates + only mildly dormant
        await _add_interaction(s, "alice", "weak", play_count=2, satisfaction=0.5, last_played=now - 35 * _DAY)
        await s.commit()

    async with _Session() as s:
        res = await forgotten_favourites.recommend_forgotten_favourites(s, "alice", limit=10)

    ids = [t["track_id"] for t in res["tracks"]]
    assert ids == ["strong", "weak"]
    assert res["tracks"][0]["score"] > res["tracks"][1]["score"]


async def test_signals_and_reasons_present():
    """Each item carries enrichment metadata and human-readable reasons."""
    now = _now()
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={}))
        await _add_track(s, "gem", artist="Artist X", album="Album Y", title="The Gem")
        await _add_interaction(
            s, "alice", "gem", play_count=12, satisfaction=0.95, like_count=2, last_played=now - 210 * _DAY
        )
        await s.commit()

    async with _Session() as s:
        res = await forgotten_favourites.recommend_forgotten_favourites(s, "alice", limit=10)

    t = res["tracks"][0]
    assert t["title"] == "The Gem"
    assert t["artist"] == "Artist X"
    assert "satisfaction" in t["sources"] and "likes" in t["sources"]
    assert 0.0 <= t["signals"]["affinity"] <= 1.0
    assert 0.0 <= t["signals"]["dormancy"] <= 1.0
    assert any("not played in" in r for r in t["reasons"])
    assert any("loved" in r or "liked" in r for r in t["reasons"])


async def test_empty_library_well_formed():
    """A fresh library returns a well-formed empty response."""
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={}))
        await s.commit()
    async with _Session() as s:
        res = await forgotten_favourites.recommend_forgotten_favourites(s, "alice", limit=10)
    assert res["tracks"] == []
    assert "generated_at" in res
