"""
GrooveIQ — Tests for the session-mixes engine (app/services/user_mixes.py).

The session-embedding model is faked: ``_vec`` maps a track id's family prefix
(A/B/C) to a distinct region of vector space, so clustering is deterministic and
no gensim model is needed. ``F_*`` tracks have no session vector (they exercise
the acoustic-embedding fallback). Config is injected per-test.

Covers:
  * rotation keeps a stable core (churn cap) — pure-function level
  * cold start (too little co-listening signal) returns empty + archives
  * a healthy user gets balanced, mostly-disjoint, ordinal-numbered mixes
  * a rebuild within the refresh window is a no-op (stable ids + membership)
  * past the refresh window, rotation swaps at most max_churn
  * a mix whose cluster no longer forms is archived
  * archived mixes resurface as 'nostalgic' only after the dormancy gate
  * acoustic-fallback tracks are placed and flagged provisional
"""

from __future__ import annotations

import base64
import time

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.algorithm_config_schema import AlgorithmConfigData, MixesConfig
from app.models.db import Base, Mix, MixTrack, TrackFeatures, TrackInteraction, User
from app.services import session_embeddings as se
from app.services import user_mixes

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)
_DAY = 86400


def _now() -> int:
    return int(time.time())


# --- fake embedding space -------------------------------------------------


def _vec(tid: str) -> np.ndarray | None:
    fam = tid[0]
    idx = int(tid.split("_")[1])
    if fam not in ("A", "B", "C"):
        return None  # F_* has no session vector -> acoustic fallback
    v = np.zeros(8, dtype=np.float32)
    v[{"A": 0, "B": 1, "C": 2}[fam]] = 10.0
    v[4] = (idx % 5) * 0.05  # tiny intra-cluster spread
    return v


def _acoustic(tid: str) -> np.ndarray:
    fam = tid[0]
    v = np.zeros(8, dtype=np.float32)
    v[{"A": 0, "F": 0, "B": 1, "C": 2}.get(fam, 3)] = 10.0  # F sits acoustically near A
    return v


def _fake_get_vectors(track_ids):
    out = {}
    for t in track_ids:
        v = _vec(t)
        if v is not None:
            out[t] = v
    return out


def _enc(v: np.ndarray) -> str:
    return base64.b64encode(np.asarray(v, dtype=np.float32).tobytes()).decode()


def _cfg(**kw) -> AlgorithmConfigData:
    base = dict(
        enabled=True,
        window_days=30,
        target_size=10,
        min_size=4,
        max_size=30,
        min_mixes=2,
        max_mixes=4,
        min_session_vectors=6,
        refresh_days=6.0,
        max_churn=0.2,
        stale_days=25.0,
        serve_cooldown_days=14.0,
        min_satisfaction=0.0,
        nostalgia_dormancy_days=45.0,
        nostalgia_max=2,
    )
    base.update(kw)
    return AlgorithmConfigData(mixes=MixesConfig(**base))


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def patch_embeddings(monkeypatch):
    monkeypatch.setattr(se, "is_ready", lambda: True)
    monkeypatch.setattr(se, "get_vectors", _fake_get_vectors)
    monkeypatch.setattr(user_mixes, "get_config", lambda: _cfg())


async def _add_track(s, tid):
    s.add(
        TrackFeatures(
            track_id=tid,
            file_path=f"/music/{tid}.mp3",
            title=tid,
            artist=tid[0],
            album="Album",
            duration=200.0,
            energy=0.8,
            valence=0.5,
            media_server_id=f"ms_{tid}",
            embedding=_enc(_acoustic(tid)),
            analyzed_at=_now(),
            analysis_version="1",
        )
    )


async def _add_inter(
    s, user, tid, *, play_count=5, last_played=None, satisfaction=0.5, like_count=0, repeat_count=0, dislike_count=0
):
    s.add(
        TrackInteraction(
            user_id=user,
            track_id=tid,
            play_count=play_count,
            satisfaction_score=satisfaction,
            last_played_at=last_played if last_played is not None else _now() - _DAY,
            like_count=like_count,
            repeat_count=repeat_count,
            dislike_count=dislike_count,
            updated_at=_now(),
        )
    )


async def _seed_user(user, families, n_each, now, *, sat_base=0.5):
    async with _Session() as s:
        s.add(User(user_id=user, taste_profile={}))
        for fam in families:
            for i in range(n_each):
                t = f"{fam}_{i}"
                await _add_track(s, t)
                await _add_inter(s, user, t, last_played=now - _DAY, satisfaction=sat_base + i * 0.001)
        await s.commit()


# --------------------------------------------------------------------------
# Pure rotation
# --------------------------------------------------------------------------


def test_rotate_keeps_stable_core():
    cfg = _cfg(target_size=10, max_churn=0.2).mixes
    old = [f"A_{i}" for i in range(10)]
    # New ranking: 5 fresh tracks on top, then A_0..4; A_5..9 fall to ranks 10-14.
    desired = [f"N_{i}" for i in range(5)] + [f"A_{i}" for i in range(5)] + [f"A_{i}" for i in range(5, 10)]
    out = user_mixes._rotate(old, desired, cfg)
    assert len(out) == 10
    assert len(set(out) & set(old)) >= 8  # <= 2 swapped (20% churn cap)


def test_rotate_fresh_mix_is_top_target():
    cfg = _cfg(target_size=10).mixes
    desired = [f"A_{i}" for i in range(20)]
    assert user_mixes._rotate([], desired, cfg) == [f"A_{i}" for i in range(10)]


# --------------------------------------------------------------------------
# Rebuild lifecycle
# --------------------------------------------------------------------------


async def test_cold_start_returns_empty(monkeypatch):
    monkeypatch.setattr(user_mixes, "get_config", lambda: _cfg(min_session_vectors=20))
    now = _now()
    await _seed_user("u1", ("A",), 5, now)  # only 5 in-vocab tracks < 20
    async with _Session() as s:
        res = await user_mixes.rebuild_user_mixes(s, "u1", now=now)
    assert res["built"] == 0 and res["reason"] == "cold_start"
    async with _Session() as s:
        assert await user_mixes.get_session_mixes(s, "u1") == []


async def test_rebuild_creates_balanced_disjoint_mixes(monkeypatch):
    monkeypatch.setattr(user_mixes, "get_config", lambda: _cfg(target_size=14, min_size=5, min_mixes=2, max_mixes=4))
    now = _now()
    await _seed_user("u1", ("A", "B"), 14, now)
    async with _Session() as s:
        res = await user_mixes.rebuild_user_mixes(s, "u1", now=now)
    assert res["built"] >= 2
    async with _Session() as s:
        mixes = await user_mixes.get_session_mixes(s, "u1")
    assert len(mixes) >= 2
    for m in mixes:
        assert 5 <= m["track_count"] <= 14
    flat = [t["track_id"] for m in mixes for t in m["tracks"]]
    assert len(flat) == len(set(flat))  # mostly-disjoint: no track in two mixes
    assert sorted(m["ordinal"] for m in mixes) == list(range(1, len(mixes) + 1))
    # tracks hydrate with display fields
    assert mixes[0]["tracks"][0]["media_server_id"].startswith("ms_")


async def test_rebuild_idempotent_within_refresh(monkeypatch):
    monkeypatch.setattr(user_mixes, "get_config", lambda: _cfg(target_size=14, min_size=5, refresh_days=6))
    now = _now()
    await _seed_user("u1", ("A", "B"), 14, now)
    async with _Session() as s:
        await user_mixes.rebuild_user_mixes(s, "u1", now=now)
    async with _Session() as s:
        first = await user_mixes.get_session_mixes(s, "u1")
    async with _Session() as s:  # 1 day later — still within the 6-day window -> no-op
        await user_mixes.rebuild_user_mixes(s, "u1", now=now + _DAY)
    async with _Session() as s:
        second = await user_mixes.get_session_mixes(s, "u1")
    assert [m["mix_id"] for m in first] == [m["mix_id"] for m in second]
    fm = {m["mix_id"]: {t["track_id"] for t in m["tracks"]} for m in first}
    sm = {m["mix_id"]: {t["track_id"] for t in m["tracks"]} for m in second}
    assert fm == sm


async def test_rotation_caps_churn_past_refresh(monkeypatch):
    monkeypatch.setattr(
        user_mixes,
        "get_config",
        lambda: _cfg(target_size=10, min_size=4, min_mixes=1, max_mixes=1, refresh_days=6, max_churn=0.2),
    )
    now = _now()
    async with _Session() as s:
        s.add(User(user_id="u1", taste_profile={}))
        for i in range(20):  # one cluster, over-full vs target 10; A_0 best
            await _add_track(s, f"A_{i}")
            await _add_inter(s, "u1", f"A_{i}", last_played=now - _DAY, satisfaction=0.99 - i * 0.01)
        await s.commit()
    async with _Session() as s:
        await user_mixes.rebuild_user_mixes(s, "u1", now=now)
    async with _Session() as s:
        first = await user_mixes.get_session_mixes(s, "u1")
    assert len(first) == 1
    mix_id = first[0]["mix_id"]
    first_set = {t["track_id"] for t in first[0]["tracks"]}
    assert first_set == {f"A_{i}" for i in range(10)}  # top-10 by satisfaction
    # Promote A_10..A_15 to the top.
    async with _Session() as s:
        for i in range(10, 16):
            it = (
                await s.execute(
                    select(TrackInteraction).where(
                        TrackInteraction.user_id == "u1", TrackInteraction.track_id == f"A_{i}"
                    )
                )
            ).scalar_one()
            it.satisfaction_score = 1.0
        await s.commit()
    async with _Session() as s:  # 7 days later -> past the refresh window
        await user_mixes.rebuild_user_mixes(s, "u1", now=now + 7 * _DAY)
    async with _Session() as s:
        second = await user_mixes.get_session_mixes(s, "u1")
    assert second[0]["mix_id"] == mix_id  # same mix, rotated in place
    second_set = {t["track_id"] for t in second[0]["tracks"]}
    swapped = len(first_set ^ second_set) // 2
    assert 1 <= swapped <= 2  # rotated, but capped at 20% of 10


async def test_cluster_gone_archives_mix(monkeypatch):
    monkeypatch.setattr(
        user_mixes,
        "get_config",
        lambda: _cfg(target_size=14, min_size=5, min_mixes=1, max_mixes=4, refresh_days=6),
    )
    now = _now()
    await _seed_user("u1", ("A", "B"), 14, now)
    async with _Session() as s:
        await user_mixes.rebuild_user_mixes(s, "u1", now=now)
    async with _Session() as s:
        assert len(await user_mixes.get_session_mixes(s, "u1")) >= 2
    # B falls out of the window entirely (no longer engaged).
    async with _Session() as s:
        for i in range(14):
            it = (
                await s.execute(
                    select(TrackInteraction).where(
                        TrackInteraction.user_id == "u1", TrackInteraction.track_id == f"B_{i}"
                    )
                )
            ).scalar_one()
            it.last_played_at = now - 100 * _DAY
        await s.commit()
    async with _Session() as s:
        await user_mixes.rebuild_user_mixes(s, "u1", now=now + 7 * _DAY)
    async with _Session() as s:
        active = await user_mixes.get_session_mixes(s, "u1")
        archived = (await s.execute(select(Mix).where(Mix.user_id == "u1", Mix.state == "archived"))).scalars().all()
    assert len(active) == 1  # only the A cluster still forms
    assert len(archived) >= 1


async def test_nostalgic_only_after_dormancy(monkeypatch):
    monkeypatch.setattr(user_mixes, "get_config", lambda: _cfg(nostalgia_dormancy_days=45, nostalgia_max=2))
    now = _now()
    async with _Session() as s:
        s.add(User(user_id="u1", taste_profile={}))
        await _add_track(s, "A_0")
        old = Mix(user_id="u1", kind="session", state="archived", archived_at=now - 60 * _DAY, track_count=1)
        recent = Mix(user_id="u1", kind="session", state="archived", archived_at=now - 10 * _DAY, track_count=1)
        s.add_all([old, recent])
        await s.flush()
        s.add(MixTrack(mix_id=old.id, track_id="A_0", position=0))
        s.add(MixTrack(mix_id=recent.id, track_id="A_0", position=0))
        await s.commit()
    async with _Session() as s:
        nost = await user_mixes.get_nostalgic_mixes(s, "u1", now=now)
    assert len(nost) == 1  # only the 60-day-dormant one resurfaces
    assert nost[0]["kind"] == "nostalgic"
    assert nost[0]["tracks"][0]["track_id"] == "A_0"


async def test_acoustic_fallback_placed_provisional(monkeypatch):
    monkeypatch.setattr(
        user_mixes,
        "get_config",
        lambda: _cfg(target_size=20, min_size=5, min_mixes=1, max_mixes=2, min_session_vectors=6),
    )
    now = _now()
    async with _Session() as s:
        s.add(User(user_id="u1", taste_profile={}))
        for i in range(14):  # session-vector backbone
            await _add_track(s, f"A_{i}")
            await _add_inter(s, "u1", f"A_{i}", last_played=now - _DAY, satisfaction=0.6)
        for i in range(4):  # no session vector; acoustically near A
            await _add_track(s, f"F_{i}")
            await _add_inter(s, "u1", f"F_{i}", last_played=now - _DAY, satisfaction=0.6)
        await s.commit()
    async with _Session() as s:
        res = await user_mixes.rebuild_user_mixes(s, "u1", now=now)
        provisional = (await s.execute(select(MixTrack).where(MixTrack.provisional.is_(True)))).scalars().all()
    assert res["fallback"] >= 1
    assert any(r.track_id.startswith("F") for r in provisional)


# --------------------------------------------------------------------------
# Proven-ness pool gate + orphan exclusion + engagement ordering (2026-07)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_rejects_single_full_listen_but_keeps_proven():
    """A single full listen no longer admits a track; >=2 plays or a like/repeat does.
    This is the gate that stops mixes filling with played-once 'unproven' tracks."""
    user = "u_pool"
    now = _now()
    async with _Session() as s:
        s.add(User(user_id=user, taste_profile={}))
        for t in ("A_0", "A_1", "A_2", "A_3"):
            await _add_track(s, t)
        # played once, fully listened, not liked -> EXCLUDED under the new gate
        s.add(TrackInteraction(user_id=user, track_id="A_0", play_count=1, full_listen_count=1,
                               satisfaction_score=0.5, last_played_at=now - _DAY, updated_at=now))
        s.add(TrackInteraction(user_id=user, track_id="A_1", play_count=2,  # >=2 plays -> in
                               satisfaction_score=0.5, last_played_at=now - _DAY, updated_at=now))
        s.add(TrackInteraction(user_id=user, track_id="A_2", play_count=1, like_count=1,  # liked -> in
                               satisfaction_score=0.5, last_played_at=now - _DAY, updated_at=now))
        s.add(TrackInteraction(user_id=user, track_id="A_3", play_count=1, repeat_count=1,  # repeated -> in
                               satisfaction_score=0.5, last_played_at=now - _DAY, updated_at=now))
        await s.commit()
    async with _Session() as s:
        pool = await user_mixes._engaged_pool(s, user, _cfg().mixes, now)
    assert "A_0" not in pool  # a single full listen is not "proven"
    assert set(pool) == {"A_1", "A_2", "A_3"}


@pytest.mark.asyncio
async def test_min_plays_for_pool_is_tunable():
    """Raising min_plays_for_pool tightens the proven gate."""
    user = "u_tune"
    now = _now()
    async with _Session() as s:
        s.add(User(user_id=user, taste_profile={}))
        for t in ("A_0", "A_1"):
            await _add_track(s, t)
        s.add(TrackInteraction(user_id=user, track_id="A_0", play_count=2,
                               satisfaction_score=0.5, last_played_at=now - _DAY, updated_at=now))
        s.add(TrackInteraction(user_id=user, track_id="A_1", play_count=3,
                               satisfaction_score=0.5, last_played_at=now - _DAY, updated_at=now))
        await s.commit()
    async with _Session() as s:
        pool = await user_mixes._engaged_pool(s, user, _cfg(min_plays_for_pool=3).mixes, now)
    assert "A_0" not in pool  # play_count 2 < 3
    assert "A_1" in pool


@pytest.mark.asyncio
async def test_orphan_without_track_features_never_reaches_a_mix():
    """A well-engaged track with NO track_features row must not surface in a mix —
    it would render as a NULL title/artist/media_server_id slot on the client."""
    user = "u_orphan"
    now = _now()
    async with _Session() as s:
        s.add(User(user_id=user, taste_profile={}))
        for i in range(10):  # real, analysed backbone
            await _add_track(s, f"A_{i}")
            await _add_inter(s, user, f"A_{i}", play_count=5, last_played=now - _DAY, satisfaction=0.6)
        # orphan: strongly engaged, has an A-family session vector, but NO track_features row
        s.add(TrackInteraction(user_id=user, track_id="A_99", play_count=20, full_listen_count=15,
                               satisfaction_score=0.99, last_played_at=now - _DAY, updated_at=now))
        await s.commit()
    async with _Session() as s:
        # it WOULD qualify on engagement...
        pool = await user_mixes._engaged_pool(s, user, _cfg().mixes, now)
        assert "A_99" in pool
        # ...but rebuild drops it, so no served track is the orphan and none hydrate to null
        await user_mixes.rebuild_user_mixes(s, user, now=now)
    async with _Session() as s:
        served = await user_mixes.get_session_mixes(s, user)
    all_tracks = [t for mix in served for t in mix["tracks"]]
    assert all_tracks, "expected at least one mix to be built"
    assert all(t["track_id"] != "A_99" for t in all_tracks)
    assert all(t["title"] is not None for t in all_tracks)  # no NULL-metadata slots


@pytest.mark.asyncio
async def test_write_members_serves_proven_first():
    """rank_by_engagement (default) stores mix positions in engagement-desc order."""
    user = "u_order"
    now = _now()
    async with _Session() as s:
        s.add(User(user_id=user, taste_profile={}))
        m = Mix(user_id=user, kind="session", state="active", created_at=now)
        s.add(m)
        await s.flush()
        pool = {"A_0": 0.1, "A_1": 0.9, "A_2": 0.5}
        # members handed over in NON-engagement order
        await user_mixes._write_members(s, m, ["A_0", "A_1", "A_2"], pool, set(), now)
        await s.commit()
        mid = m.id
    async with _Session() as s:
        rows = (
            await s.execute(select(MixTrack.track_id).where(MixTrack.mix_id == mid).order_by(MixTrack.position))
        ).all()
    assert [r[0] for r in rows] == ["A_1", "A_2", "A_0"]  # 0.9, 0.5, 0.1 -> strongest leads
