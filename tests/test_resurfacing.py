"""GrooveIQ – Tests for the recently-engaged resurfacing heat."""

from __future__ import annotations

import random
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, ListenEvent, TrackInteraction
from app.services.resurfacing import (
    SPECIAL_TRACKS_SURFACE,
    engagement_heat,
    engagement_intensity,
    get_resurfacing_tracks,
)

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
        out = await reranker.rerank([("cold", 0.60), ("hot", 0.50)], "u", s, collect_actions=True, rng=random.Random(0))

    assert out[0][0] == "hot"  # boost lifted it above the higher-ranked 'cold'
    actions = reranker.get_last_rerank_actions()
    assert any(a["action"] == "recently_engaged_boost" and a["track_id"] == "hot" for a in actions)


# ---------------------------------------------------------------------------
# Two-card candidate→confirmed split + ignore-gate + reranker spread-gating
# ---------------------------------------------------------------------------


def _imp(tid: str, rid: str, ts: int) -> ListenEvent:
    """A Special-tracks card impression (track shown to the user)."""
    return ListenEvent(
        user_id="u",
        track_id=tid,
        event_type="reco_impression",
        surface=SPECIAL_TRACKS_SURFACE,
        request_id=rid,
        timestamp=ts,
    )


def _play(tid: str, rid: str, ts: int) -> ListenEvent:
    """A play under a request_id — confirms a candidate when it matches a card impression."""
    return ListenEvent(user_id="u", track_id=tid, event_type="play_start", request_id=rid, timestamp=ts)


@pytest.mark.asyncio
async def test_stage_splits_candidates_from_confirmed():
    """A single seek-back/replay is a candidate; a like or a 2nd repeat/full-listen confirms."""
    async with _Session() as s:
        s.add_all(
            [
                _row("cand_seek", played_ago_days=1, total_seekbk=1),  # candidate (one seek-back)
                _row("cand_replay1", played_ago_days=1, repeat_count=1),  # candidate (one replay)
                _row("conf_like", played_ago_days=1, total_seekbk=1, like_count=1),  # confirmed (liked)
                _row("conf_replay2", played_ago_days=1, repeat_count=2),  # confirmed (≥2 replays)
                _row("conf_full2", played_ago_days=1, full_listen_count=2),  # confirmed (≥2 full listens)
            ]
        )
        await s.commit()

    async with _Session() as s:
        cand = await get_resurfacing_tracks("u", s, stage="candidate")
        conf = await get_resurfacing_tracks("u", s, stage="confirmed")

    assert {t for t, _ in cand} == {"cand_seek", "cand_replay1"}
    assert {t for t, _ in conf} == {"conf_like", "conf_replay2", "conf_full2"}


@pytest.mark.asyncio
async def test_played_from_special_card_confirms_candidate():
    """Playing a candidate from the Special card (a play under the card impression's request_id)
    is the strong, explicit confirm — it leaves Special and appears under Keep listening."""
    now = int(time.time())
    async with _Session() as s:
        s.add(_row("track", played_ago_days=1, total_seekbk=1))  # would be a candidate
        s.add(_imp("track", "R", now))  # shown on the Special card under request R
        s.add(_play("track", "R", now + 1))  # …then played under R → confirm
        await s.commit()

    async with _Session() as s:
        cand = await get_resurfacing_tracks("u", s, stage="candidate")
        conf = await get_resurfacing_tracks("u", s, stage="confirmed")

    assert [t for t, _ in cand] == []  # no longer a candidate
    assert [t for t, _ in conf] == ["track"]  # graduated to Keep listening


@pytest.mark.asyncio
async def test_ignore_gate_drops_candidate_after_three_unplayed_impressions():
    now = int(time.time())
    async with _Session() as s:
        s.add(_row("ignored", played_ago_days=1, total_seekbk=1))
        s.add(_row("shown_twice", played_ago_days=1, total_seekbk=1))
        s.add_all([_imp("ignored", f"i{i}", now) for i in range(3)])  # 3 shows, no plays → drop
        s.add_all([_imp("shown_twice", f"s{i}", now) for i in range(2)])  # only 2 → still a candidate
        await s.commit()

    async with _Session() as s:
        ids = [t for t, _ in await get_resurfacing_tracks("u", s, stage="candidate")]

    assert "ignored" not in ids
    assert "shown_twice" in ids


@pytest.mark.asyncio
async def test_ignore_gate_resets_after_reengagement():
    """Impressions predating the track's last engagement don't count — re-engaging forgives them."""
    now = int(time.time())
    async with _Session() as s:
        s.add(_row("reengaged", played_ago_days=0, total_seekbk=1))  # engaged just now
        s.add_all([_imp("reengaged", f"r{i}", now - 2 * _DAY) for i in range(3)])  # ignored 2 days ago
        await s.commit()

    async with _Session() as s:
        ids = [t for t, _ in await get_resurfacing_tracks("u", s, stage="candidate")]

    assert "reengaged" in ids


@pytest.mark.asyncio
async def test_reranker_boost_skips_suppressed():
    """The cross-surface boost must not keep spreading a track the user dismissed (§D)."""
    from app.models.db import TrackFeatures
    from app.services import reranker

    now = int(time.time())
    async with _Session() as s:
        s.add_all(
            [
                TrackFeatures(track_id="suppressed", file_path="/m/a.mp3", analyzed_at=now, analysis_version="1"),
                TrackFeatures(track_id="ok", file_path="/m/b.mp3", analyzed_at=now, analysis_version="1"),
                _row("suppressed", played_ago_days=3 / 24, repeat_count=3),  # hottest, but dismissed
                _row("ok", played_ago_days=3 / 24, repeat_count=1),  # cooler, boostable
                ListenEvent(user_id="u", track_id="suppressed", event_type="suppress", timestamp=now),
            ]
        )
        await s.commit()

    async with _Session() as s:
        await reranker.rerank([("ok", 0.50), ("suppressed", 0.55)], "u", s, collect_actions=True, rng=random.Random(0))

    boost = [a for a in reranker.get_last_rerank_actions() if a["action"] == "recently_engaged_boost"]
    assert boost and boost[0]["track_id"] == "ok"  # suppressed hottest skipped


@pytest.mark.asyncio
async def test_reranker_boost_skips_ignore_gated_candidate():
    """An ignore-gated candidate stops spreading cross-surface too (immediate-spread, #139)."""
    from app.models.db import TrackFeatures
    from app.services import reranker

    now = int(time.time())
    async with _Session() as s:
        s.add_all(
            [
                TrackFeatures(track_id="dropped", file_path="/m/a.mp3", analyzed_at=now, analysis_version="1"),
                TrackFeatures(track_id="ok", file_path="/m/b.mp3", analyzed_at=now, analysis_version="1"),
                _row("dropped", played_ago_days=3 / 24, total_seekbk=4),  # hottest, but ignore-gated
                _row("ok", played_ago_days=3 / 24, repeat_count=1),
            ]
        )
        s.add_all([_imp("dropped", f"d{i}", now) for i in range(3)])  # 3 shows, no plays → dropped
        await s.commit()

    async with _Session() as s:
        await reranker.rerank([("ok", 0.50), ("dropped", 0.55)], "u", s, collect_actions=True, rng=random.Random(0))

    boost = [a for a in reranker.get_last_rerank_actions() if a["action"] == "recently_engaged_boost"]
    assert boost and boost[0]["track_id"] == "ok"  # ignore-gated candidate skipped


@pytest.mark.asyncio
async def test_apply_ignore_gate_drops_only_unconfirmed_candidate():
    """The radio-injection path (stage=None, apply_ignore_gate) keeps confirmed + un-gated hot
    tracks but drops a still-unconfirmed candidate the user keeps ignoring on the Special card."""
    now = int(time.time())
    async with _Session() as s:
        s.add_all(
            [
                _row("dropped", played_ago_days=1, total_seekbk=1),  # candidate, will be ignore-gated
                _row("confirmed", played_ago_days=1, repeat_count=2),  # confirmed (organic) — kept
                _row("ungated", played_ago_days=1, total_seekbk=1),  # candidate, only shown twice — kept
            ]
        )
        s.add_all([_imp("dropped", f"d{i}", now) for i in range(3)])  # 3 ignores → dropped
        s.add_all([_imp("confirmed", f"c{i}", now) for i in range(3)])  # ignored but confirmed → kept
        s.add_all([_imp("ungated", f"u{i}", now) for i in range(2)])  # under the gate → kept
        await s.commit()

    async with _Session() as s:
        all_hot = {t for t, _ in await get_resurfacing_tracks("u", s)}  # stage=None, no gate
        gated = {t for t, _ in await get_resurfacing_tracks("u", s, apply_ignore_gate=True)}

    assert all_hot == {"dropped", "confirmed", "ungated"}  # without the gate, every hot track
    assert gated == {"confirmed", "ungated"}  # the gate drops only the unconfirmed, over-ignored one
