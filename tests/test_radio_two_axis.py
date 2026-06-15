"""GrooveIQ – Tests for the radio two-axis dial helpers.

Covers the Anchoring axis (``_blend_anchor`` — seed ↔ taste centroid), the Discover
semi-known quota (``_apply_semiknown_quota``), and the Discover brand-new floor
(``_filter_require_interaction``).
"""

from __future__ import annotations

import time

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, TrackInteraction
from app.services.radio import (
    RadioSession,
    _apply_semiknown_quota,
    _blend_anchor,
    _filter_require_interaction,
)

_engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)


def _session(seed, profile, anchor: float) -> RadioSession:
    s = RadioSession(session_id="x", user_id="u", seed_type="track", seed_value="t")
    s.seed_embedding = None if seed is None else np.array(seed, dtype=np.float32)
    s.profile_embedding = None if profile is None else np.array(profile, dtype=np.float32)
    s.seed_anchor_weight = anchor
    return s


# ---------------------------------------------------------------------------
# Anchoring axis — _blend_anchor
# ---------------------------------------------------------------------------


def test_blend_anchor_no_profile_returns_seed():
    s = _session([1.0, 0.0], None, anchor=0.25)
    out = _blend_anchor(s)
    assert np.allclose(out, [1.0, 0.0])  # cold start -> pure seed regardless of anchor


def test_blend_anchor_full_anchor_hugs_seed():
    s = _session([1.0, 0.0], [0.0, 1.0], anchor=1.0)
    assert np.allclose(_blend_anchor(s), [1.0, 0.0], atol=1e-6)  # familiar: all seed


def test_blend_anchor_zero_anchor_is_taste_centroid():
    s = _session([1.0, 0.0], [0.0, 1.0], anchor=0.0)
    assert np.allclose(_blend_anchor(s), [0.0, 1.0], atol=1e-6)  # fully roam the taste centroid


def test_blend_anchor_midpoint_is_between_and_unit_norm():
    s = _session([1.0, 0.0], [0.0, 1.0], anchor=0.5)
    out = _blend_anchor(s)
    assert np.allclose(out, [0.70710677, 0.70710677], atol=1e-5)  # halfway, L2-normalised
    assert np.isclose(np.linalg.norm(out), 1.0, atol=1e-6)


def test_blend_anchor_none_seed_returns_none():
    assert _blend_anchor(_session(None, [0.0, 1.0], anchor=0.5)) is None


# ---------------------------------------------------------------------------
# Discover quota — _apply_semiknown_quota
# ---------------------------------------------------------------------------


def test_semiknown_quota_hits_target_fraction():
    # 20 candidates, top-10 are proven/other, bottom-10 are semi-known.
    reranked = [(f"t{i}", 20.0 - i) for i in range(20)]
    semiknown = {f"t{i}" for i in range(10, 20)}
    out = _apply_semiknown_quota(reranked, semiknown, count=10, frac=0.30)
    assert len(out) == 10
    n_semi = sum(1 for t, _ in out if t in semiknown)
    assert n_semi == 3  # round(10 * 0.30)
    # result is score-sorted descending
    assert [s for _, s in out] == sorted((s for _, s in out), reverse=True)


def test_semiknown_quota_backfills_when_a_tier_is_short():
    # Only two semi-known available but the quota wants three -> backfill from 'other'.
    reranked = [(f"t{i}", 20.0 - i) for i in range(20)]
    semiknown = {"t18", "t19"}
    out = _apply_semiknown_quota(reranked, semiknown, count=10, frac=0.30)
    assert len(out) == 10  # still returns a full batch
    assert sum(1 for t, _ in out if t in semiknown) == 2  # all available semi-known used


def test_semiknown_quota_noop_when_frac_zero():
    reranked = [(f"t{i}", 20.0 - i) for i in range(20)]
    out = _apply_semiknown_quota(reranked, {"t1"}, count=5, frac=0.0)
    assert out == reranked[:5]


# ---------------------------------------------------------------------------
# Discover floor — _filter_require_interaction
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _ti(tid: str, plays: int) -> TrackInteraction:
    now = int(time.time())
    return TrackInteraction(
        user_id="u",
        track_id=tid,
        play_count=plays,
        skip_count=0,
        like_count=0,
        dislike_count=0,
        repeat_count=0,
        last_played_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_require_interaction_drops_brand_new():
    async with _Session() as s:
        s.add_all([_ti("t0", 2), _ti("t1", 1), _ti("t2", 0)])  # t3/t4 have no row at all
        await s.commit()

    cands = [{"track_id": t, "score": 1.0, "source": "x"} for t in ("t0", "t1", "t2", "t3", "t4")]
    async with _Session() as s:
        kept = await _filter_require_interaction(cands, "u", s, min_keep=2)

    assert {c["track_id"] for c in kept} == {"t0", "t1"}  # zero-play + no-row dropped


@pytest.mark.asyncio
async def test_require_interaction_no_starve_keeps_original():
    async with _Session() as s:
        s.add_all([_ti("t0", 2), _ti("t1", 1)])
        await s.commit()

    cands = [{"track_id": t, "score": 1.0, "source": "x"} for t in ("t0", "t1", "t2", "t3", "t4")]
    async with _Session() as s:
        kept = await _filter_require_interaction(cands, "u", s, min_keep=4)  # can't reach 4 -> keep all

    assert len(kept) == 5  # no-starve guard returns the original pool
