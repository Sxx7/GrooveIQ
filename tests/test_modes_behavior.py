"""
GrooveIQ – Behavioural tests for the discovery dial (Chunk 4).

Exercises the gated, additive UCB acquisition in the reranker and the
source-weight / proven-novelty filter in candidate generation, driven by the
request-scoped override (Chunk 1) carrying a dial-resolved preset (Chunk 2).

Key contracts asserted here:
  * balanced / default is byte-for-byte unchanged (seeded RNG) and never even
    computes confidence (structural gate, not float luck);
  * deep_discovery demotes the proven set and lifts high-sigma (unheard) tracks;
  * source-weight multipliers scale a whole source;
  * the proven-set novelty filter excludes the user's proven tracks;
  * novelty of the output rises monotonically with the dial.
"""

from __future__ import annotations

import random
import time

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.algorithm_config_schema import get_defaults
from app.models.db import Base, TrackFeatures, TrackInteraction
from app.services import candidate_gen, confidence, reranker
from app.services.request_config import apply_overrides

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)

_NOW = int(time.time())
_TEN_DAYS_AGO = _NOW - 10 * 86_400  # within the 30-day "popular" window, outside the 2h repeat window


@pytest_asyncio.fixture(autouse=True)
async def _setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _preset_override(name: str) -> dict:
    """Mini resolve_dial: map a named preset to the request override dict.

    Sets ``modes.active`` (the resolved preset the reranker/candidate_gen read)
    plus the overlapping reranker knobs the preset tunes. Chunk 5 formalises
    this as a whitelisted resolver; here it stands in for testing behaviour.
    """
    p = getattr(get_defaults().modes, name)
    return {
        "modes": {"active": p.model_dump()},
        "reranker": {
            "exploration_fraction": p.exploration_fraction,
            "freshness_boost": p.freshness_boost,
            "repeat_window_hours": p.repeat_window_hours,
        },
    }


async def _add_track(session, tid: str, *, artist: str, played: bool, proven: bool):
    session.add(
        TrackFeatures(
            track_id=tid,
            file_path=f"/music/{artist}/album/{tid}.mp3",
            duration=210.0,
            analyzed_at=_NOW,
            analysis_version="1",
        )
    )
    if played:
        if proven:
            session.add(
                TrackInteraction(
                    user_id="u",
                    track_id=tid,
                    play_count=30,
                    full_listen_count=30,
                    early_skip_count=0,
                    avg_completion=0.95,
                    satisfaction_score=0.95,
                    last_played_at=_TEN_DAYS_AGO,
                    updated_at=_NOW,
                )
            )
        else:
            session.add(
                TrackInteraction(
                    user_id="u",
                    track_id=tid,
                    play_count=1,
                    full_listen_count=0,
                    early_skip_count=0,
                    avg_completion=0.30,
                    satisfaction_score=0.20,
                    last_played_at=_TEN_DAYS_AGO,
                    updated_at=_NOW,
                )
            )


# ---------------------------------------------------------------------------
# Reranker: gated additive acquisition
# ---------------------------------------------------------------------------


async def test_balanced_is_byte_for_byte_and_skips_confidence(monkeypatch):
    """Default == balanced override, and confidence is never computed for balanced."""
    async with _Session() as session:
        for i in range(8):
            await _add_track(session, f"p{i}", artist=f"a{i}", played=True, proven=True)
        await session.commit()

        ranked = [(f"p{i}", 0.9 - 0.01 * i) for i in range(8)]

        # Count confidence computations to prove the structural gate.
        calls = {"n": 0}
        real = confidence.compute_confidence

        def _counting(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(confidence, "compute_confidence", _counting)

        baseline = await reranker.rerank(ranked, "u", session, rng=random.Random(42))
        assert calls["n"] == 0  # no override -> no acquisition

        with apply_overrides(_preset_override("balanced")):
            balanced = await reranker.rerank(ranked, "u", session, rng=random.Random(42))
        assert calls["n"] == 0  # balanced is gated off -> still no confidence

        assert balanced == baseline  # byte-for-byte


async def test_deep_discovery_demotes_proven_and_lifts_unheard():
    async with _Session() as session:
        # Two proven (played a lot) and two unheard (never played) tracks.
        await _add_track(session, "proven1", artist="a1", played=True, proven=True)
        await _add_track(session, "proven2", artist="a2", played=True, proven=True)
        await _add_track(session, "unheard1", artist="a3", played=False, proven=False)
        await _add_track(session, "unheard2", artist="a4", played=False, proven=False)
        await session.commit()

        # n<5 -> exploration slots skipped -> fully deterministic.
        ranked = [("proven1", 0.90), ("proven2", 0.85), ("unheard1", 0.50), ("unheard2", 0.45)]

        with apply_overrides(_preset_override("balanced")):
            bal = [t for t, _ in await reranker.rerank(ranked, "u", session, rng=random.Random(0))]
        with apply_overrides(_preset_override("deep_discovery")):
            deep = [t for t, _ in await reranker.rerank(ranked, "u", session, rng=random.Random(0))]

        # Balanced keeps proven on top; deep_discovery flips unheard above proven.
        assert bal.index("proven1") < bal.index("unheard1")
        assert deep.index("unheard1") < deep.index("proven1")
        assert deep.index("unheard2") < deep.index("proven1")


async def test_output_novelty_rises_monotonically_with_dial():
    async with _Session() as session:
        for i in range(6):
            await _add_track(session, f"proven{i}", artist=f"pa{i}", played=True, proven=True)
        for i in range(10):
            await _add_track(session, f"new{i}", artist=f"na{i}", played=False, proven=False)
        await session.commit()

        ranked = [(f"proven{i}", 0.90 - 0.01 * i) for i in range(6)] + [(f"new{i}", 0.50 - 0.01 * i) for i in range(10)]
        top_k = 8

        def novelty(result):
            top = [t for t, _ in result[:top_k]]
            return sum(1 for t in top if t.startswith("new"))

        novelties = []
        for name in ("balanced", "discovery", "deep_discovery"):
            # Isolate the acquisition term: hold the (stochastic) legacy
            # exploration slots at zero. Otherwise exploration_fraction also
            # ramps with the dial and re-injects the *demoted proven* tracks from
            # the bottom half — a separate, known dial/exploration interaction
            # that would mask the acquisition's monotonic novelty effect.
            override = _preset_override(name)
            override["reranker"]["exploration_fraction"] = 0.0
            with apply_overrides(override):
                res = await reranker.rerank(ranked, "u", session, rng=random.Random(7))
            novelties.append(novelty(res))

        bal_n, disc_n, deep_n = novelties
        assert disc_n >= bal_n
        assert deep_n >= disc_n
        assert deep_n > bal_n  # strictly more novel at the discovery extreme


# ---------------------------------------------------------------------------
# Candidate generation: source multipliers + proven novelty filter
# ---------------------------------------------------------------------------


async def test_source_weight_multiplier_scales_a_source():
    async with _Session() as session:
        # Played tracks surface via the `popular` source.
        for i in range(6):
            await _add_track(session, f"pop{i}", artist=f"a{i}", played=True, proven=False)
        await session.commit()

        base = await candidate_gen.get_candidates("u", session=session)
        base_scores = {c["track_id"]: c["score"] for c in base if c["source"] == "popular"}
        assert base_scores  # popular candidates exist

        with apply_overrides({"modes": {"active": {"source_weight_mult": {"popular": 3.0}}}}):
            boosted = await candidate_gen.get_candidates("u", session=session)
        boosted_scores = {c["track_id"]: c["score"] for c in boosted if c["source"] == "popular"}

        for tid, score in base_scores.items():
            assert boosted_scores[tid] == score * 3.0


async def test_novelty_filter_excludes_proven_tracks():
    async with _Session() as session:
        # 6 proven + 12 non-proven, all surfacing via `popular`.
        proven_ids = [f"proven{i}" for i in range(6)]
        new_ids = [f"weak{i}" for i in range(12)]
        for i, tid in enumerate(proven_ids):
            await _add_track(session, tid, artist=f"pa{i}", played=True, proven=True)
        for i, tid in enumerate(new_ids):
            await _add_track(session, tid, artist=f"wa{i}", played=True, proven=False)
        await session.commit()

        base = {c["track_id"] for c in await candidate_gen.get_candidates("u", session=session)}
        assert set(proven_ids) <= base  # present without the dial

        with apply_overrides(_preset_override("deep_discovery")):
            filtered = {c["track_id"] for c in await candidate_gen.get_candidates("u", session=session)}

        # Proven tracks are excluded; the non-proven pool survives (above the floor).
        assert not (set(proven_ids) & filtered)
        assert set(new_ids) <= filtered
