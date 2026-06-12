"""
GrooveIQ – Tests for the single-user radio levers (audit doc §8).

Covers the two new pieces of radio-path logic that have non-trivial behaviour:
  * the graded cross-session repeat cooldown (demote recently/often-served
    tracks, floored, no-op without serve history);
  * the no-consecutive-artist flow guard (spread same-artist runs, drop nothing).
"""

from __future__ import annotations

import time

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, ListenEvent, TrackFeatures
from app.services.radio import (
    _COOLDOWN_FLOOR,
    _apply_repeat_cooldown,
    _enforce_no_consecutive_artist,
)

_engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)


def _now() -> int:
    return int(time.time())


@pytest_asyncio.fixture(autouse=True)
async def _db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def test_repeat_cooldown_demotes_recently_served():
    """A track served often+recently in radio is demoted; an unseen one is untouched."""
    now = _now()
    async with _Session() as session:
        for _ in range(5):  # 'hot' served 5x ~10 min ago
            session.add(
                ListenEvent(
                    user_id="u", track_id="hot", event_type="impression", surface="radio", timestamp=now - 600
                )
            )
        await session.commit()

    candidates = [
        {"track_id": "hot", "score": 1.0, "source": "radio_drift"},
        {"track_id": "cold", "score": 1.0, "source": "radio_drift"},
    ]
    async with _Session() as session:
        await _apply_repeat_cooldown(candidates, "u", session, alpha=0.35)

    by = {c["track_id"]: c["score"] for c in candidates}
    assert by["hot"] < by["cold"]  # recently/often served -> demoted
    assert by["cold"] == 1.0  # never served -> untouched
    assert by["hot"] >= _COOLDOWN_FLOOR  # floored, never buried


async def test_repeat_cooldown_is_noop_without_history():
    """No serve history -> no change (and no error)."""
    candidates = [{"track_id": "x", "score": 1.0, "source": "radio_drift"}]
    async with _Session() as session:
        await _apply_repeat_cooldown(candidates, "u", session, alpha=0.35)
    assert candidates[0]["score"] == 1.0


async def test_repeat_cooldown_ignores_non_radio_serves():
    """Plays from other surfaces don't trigger the radio cooldown."""
    now = _now()
    async with _Session() as session:
        for _ in range(5):
            session.add(
                ListenEvent(
                    user_id="u", track_id="hot", event_type="impression", surface="home", timestamp=now - 600
                )
            )
        await session.commit()
    candidates = [{"track_id": "hot", "score": 1.0, "source": "radio_drift"}]
    async with _Session() as session:
        await _apply_repeat_cooldown(candidates, "u", session, alpha=0.35)
    assert candidates[0]["score"] == 1.0


async def test_no_consecutive_artist_spreads_runs():
    """Four top-ranked tracks by one artist get spread so no 3 play in a row."""
    async with _Session() as session:
        for i in range(4):
            session.add(
                TrackFeatures(
                    track_id=f"a{i}",
                    artist="A",
                    title=f"a{i}",
                    file_path=f"/A/album/{i}.mp3",
                    analyzed_at=_now(),
                    analysis_version="1",
                )
            )
        for i in range(2):
            session.add(
                TrackFeatures(
                    track_id=f"b{i}",
                    artist="B",
                    title=f"b{i}",
                    file_path=f"/B/album/{i}.mp3",
                    analyzed_at=_now(),
                    analysis_version="1",
                )
            )
        await session.commit()

    ranked = [("a0", 0.9), ("a1", 0.8), ("a2", 0.7), ("a3", 0.6), ("b0", 0.5), ("b1", 0.4)]
    async with _Session() as session:
        out = await _enforce_no_consecutive_artist(ranked, session, None)

    assert len(out) == len(ranked)  # nothing dropped
    assert {tid for tid, _ in out} == {tid for tid, _ in ranked}  # same set
    amap = {**{f"a{i}": "A" for i in range(4)}, **{f"b{i}": "B" for i in range(2)}}
    seq = [amap[tid] for tid, _ in out]
    assert not any(seq[i] == seq[i + 1] == seq[i + 2] for i in range(len(seq) - 2))


async def test_energy_continuity_smooths_near_tie_jumps():
    """Near-tie tracks are reordered to reduce large energy jumps; nothing dropped.

    Distinct artists isolate the energy effect from the artist guard. In raw score
    order the energies alternate high/low (max jumps); the continuity tiebreak
    groups same-energy tracks so consecutive transitions are gentler.
    """
    energies = {"hi0": 0.9, "lo0": 0.1, "hi1": 0.9, "lo1": 0.1, "hi2": 0.9, "lo2": 0.1}
    async with _Session() as session:
        for i, (tid, e) in enumerate(energies.items()):
            session.add(
                TrackFeatures(
                    track_id=tid,
                    artist=f"art{i}",  # distinct artists -> artist guard never reorders
                    title=tid,
                    file_path=f"/m/{tid}.mp3",
                    energy=e,
                    analyzed_at=_now(),
                    analysis_version="1",
                )
            )
        await session.commit()

    # Scores in a tight band so each pair is a near-tie the continuity pass may swap.
    ranked = [("hi0", 0.90), ("lo0", 0.89), ("hi1", 0.88), ("lo1", 0.87), ("hi2", 0.86), ("lo2", 0.85)]
    async with _Session() as session:
        out = await _enforce_no_consecutive_artist(ranked, session, None)

    assert {tid for tid, _ in out} == {tid for tid, _ in ranked}  # nothing dropped

    def avg_delta(order: list[str]) -> float:
        es = [energies[tid] for tid in order]
        return sum(abs(es[i] - es[i + 1]) for i in range(len(es) - 1)) / (len(es) - 1)

    raw = avg_delta([tid for tid, _ in ranked])
    smoothed = avg_delta([tid for tid, _ in out])
    assert smoothed < raw  # consecutive energy transitions are gentler than score order
