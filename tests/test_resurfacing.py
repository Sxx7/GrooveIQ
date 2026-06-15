"""GrooveIQ – Tests for the recently-engaged resurfacing heat."""

from __future__ import annotations

import random
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, ListenEvent, TrackInteraction
from app.services.resurfacing import engagement_heat, engagement_intensity, get_resurfacing_tracks

_engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)

_DAY = 86_400


def _inter(**kw) -> SimpleNamespace:
    """A lightweight stand-in for a TrackInteraction row (ORM Column defaults only apply on flush)."""
    base = dict(
        like_count=0,
        repeat_count=0,
        full_listen_count=0,
        total_seekbk=0,
        skip_count=0,
        early_skip_count=0,
        last_played_at=1000,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Pure heat math
# ---------------------------------------------------------------------------


def test_intensity_weights_and_caps():
    assert engagement_intensity(_inter(like_count=1)) == pytest.approx(0.5)  # 1.0 / 2
    assert engagement_intensity(_inter(repeat_count=2)) == pytest.approx(0.8)  # 2 * 0.8 / 2
    # Per-signal cap: 10 replays count as 3.
    assert engagement_intensity(_inter(repeat_count=10)) == pytest.approx(3 * 0.8 / 2)


def test_intensity_skip_penalty_can_zero_it_out():
    assert engagement_intensity(_inter(repeat_count=1, skip_count=2)) == 0.0  # 0.8 - 1.2 < 0 -> 0


def test_heat_decays_with_recency_and_respects_window():
    now = 1_000_000
    assert engagement_heat(_inter(repeat_count=1, last_played_at=now), now) == pytest.approx(0.4)  # recency 1
    assert engagement_heat(_inter(repeat_count=1, last_played_at=now - 5 * _DAY), now) == pytest.approx(0.2)  # half
    assert engagement_heat(_inter(repeat_count=3, last_played_at=now - 25 * _DAY), now) == 0.0  # outside window
    assert engagement_heat(_inter(repeat_count=1, last_played_at=None), now) == 0.0  # never played
    assert engagement_heat(_inter(repeat_count=1, skip_count=3, last_played_at=now), now) == 0.0  # net-negative


# ---------------------------------------------------------------------------
# get_resurfacing_tracks (DB-backed)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _row(tid: str, *, played_ago_days: float, **kw) -> TrackInteraction:
    now = int(time.time())
    base = dict(
        user_id="u",
        track_id=tid,
        play_count=1,
        skip_count=0,
        like_count=0,
        dislike_count=0,
        repeat_count=0,
        full_listen_count=0,
        total_seekbk=0,
        early_skip_count=0,
        last_played_at=now - int(played_ago_days * _DAY),
        updated_at=now,
    )
    base.update(kw)
    return TrackInteraction(**base)


@pytest.mark.asyncio
async def test_get_resurfacing_ranks_hot_excludes_cold_and_negative():
    now = int(time.time())
    async with _Session() as s:
        s.add_all(
            [
                _row("hot", played_ago_days=1, repeat_count=2),  # high heat
                _row("warm", played_ago_days=3, full_listen_count=1),  # moderate
                _row("cold", played_ago_days=20, total_seekbk=1),  # decayed below min_heat
                _row("stale", played_ago_days=25, repeat_count=3),  # outside the window
                _row("negative", played_ago_days=1, repeat_count=1, skip_count=3),  # net-negative
            ]
        )
        await s.commit()

    async with _Session() as s:
        out = await get_resurfacing_tracks("u", s)

    assert [t for t, _ in out] == ["hot", "warm"]  # sorted by heat; cold/stale/negative excluded
    assert out[0][1] > out[1][1]


@pytest.mark.asyncio
async def test_get_resurfacing_honours_suppress_until_reengagement():
    now = int(time.time())
    async with _Session() as s:
        s.add_all(
            [
                _row("suppressed", played_ago_days=1, repeat_count=2),
                _row("resurfaced", played_ago_days=1, repeat_count=1),
            ]
        )
        # suppress AFTER the last engagement of "suppressed" -> stays hidden
        s.add(ListenEvent(user_id="u", track_id="suppressed", event_type="suppress", timestamp=now))
        # suppress BEFORE the last engagement of "resurfaced" -> re-engaging brings it back
        s.add(ListenEvent(user_id="u", track_id="resurfaced", event_type="suppress", timestamp=now - 2 * _DAY))
        await s.commit()

    async with _Session() as s:
        out = await get_resurfacing_tracks("u", s)

    ids = [t for t, _ in out]
    assert "suppressed" not in ids
    assert "resurfaced" in ids


@pytest.mark.asyncio
async def test_reranker_boosts_hottest_recently_engaged():
    """The cross-surface boost lifts the single hottest recently-engaged candidate."""
    from app.models.db import TrackFeatures
    from app.services import reranker

    now = int(time.time())
    async with _Session() as s:
        s.add_all(
            [
                TrackFeatures(track_id="hot", file_path="/m/a/hot.mp3", analyzed_at=now, analysis_version="1"),
                TrackFeatures(track_id="cold", file_path="/m/b/cold.mp3", analyzed_at=now, analysis_version="1"),
                # 3h ago (recent heat, but outside the 2h anti-repetition window), heavily replayed.
                _row("hot", played_ago_days=3 / 24, repeat_count=3),
                _row("cold", played_ago_days=3 / 24),  # no engagement signals -> zero heat
            ]
        )
        await s.commit()

    async with _Session() as s:
        out = await reranker.rerank(
            [("cold", 0.60), ("hot", 0.50)], "u", s, collect_actions=True, rng=random.Random(0)
        )

    assert out[0][0] == "hot"  # boost lifted it above the higher-ranked 'cold'
    actions = reranker.get_last_rerank_actions()
    assert any(a["action"] == "recently_engaged_boost" and a["track_id"] == "hot" for a in actions)
