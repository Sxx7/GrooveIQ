"""GrooveIQ – Tests for the semi-known tier helper.

``candidate_gen.get_semiknown_track_ids`` returns the "appeared before, didn't
skip, not a favourite yet" tier that the Discover posture mixes in (~30%)
alongside proven favourites.
"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, TrackInteraction
from app.services.candidate_gen import get_semiknown_track_ids

_engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)

_UID = "alice"


def _now() -> int:
    return int(time.time())


@pytest_asyncio.fixture(autouse=True)
async def _db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _interaction(tid: str, *, user: str = _UID, **kw) -> TrackInteraction:
    """A TrackInteraction with all NOT-NULL columns satisfied (updated_at has no default)."""
    fields = dict(
        user_id=user,
        track_id=tid,
        play_count=0,
        skip_count=0,
        like_count=0,
        dislike_count=0,
        repeat_count=0,
        last_played_at=_now(),
        updated_at=_now(),
    )
    fields.update(kw)
    return TrackInteraction(**fields)


@pytest.mark.asyncio
async def test_semiknown_selects_only_sampled_unrejected_below_proven():
    async with _Session() as s:
        s.add_all(
            [
                # --- semi-known: the only two that should come back ---
                _interaction("semi_old", play_count=1, last_played_at=100),
                _interaction("semi_new", play_count=2, satisfaction_score=0.4, last_played_at=200),
                # --- excluded for each distinct reason ---
                _interaction("proven_by_plays", play_count=5, last_played_at=300),
                _interaction("proven_by_satisfaction", play_count=2, satisfaction_score=0.85),
                _interaction("liked", play_count=1, like_count=1),
                _interaction("skipped", play_count=2, skip_count=1),
                _interaction("disliked", play_count=1, dislike_count=1),
                _interaction("never_played", play_count=0),
            ]
        )
        await s.commit()

    async with _Session() as s:
        ids = await get_semiknown_track_ids(_UID, s)

    # exactly the sampled-but-uncommitted tracks, most-recently-sampled first
    assert ids == ["semi_new", "semi_old"]


@pytest.mark.asyncio
async def test_semiknown_respects_user_scope_and_limit():
    async with _Session() as s:
        s.add_all(
            [
                _interaction("a", play_count=1, last_played_at=300),
                _interaction("b", play_count=1, last_played_at=200),
                _interaction("c", play_count=2, last_played_at=100),
                _interaction("other_user", user="bob", play_count=1, last_played_at=999),
            ]
        )
        await s.commit()

    async with _Session() as s:
        ids = await get_semiknown_track_ids(_UID, s, limit=2)

    assert ids == ["a", "b"]  # bob's track excluded; limit honoured; recency order
