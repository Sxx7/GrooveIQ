"""
GrooveIQ — Tests for the artist recommendation service (app/services/artist_reco.py).

Covers the Phase A acceptance criteria from docs/HANDOFF_artist_album_reco.md:

  1. An under-played but well-liked in-library artist ranks ABOVE a
     heavily-played but low-satisfaction one (proves we beat the play-count sort).
  2. mode=discover surfaces at least one play_count==0 artist via the FAISS
     content path.
  3. Graceful fallback when FAISS isn't ready and the ranker is unavailable.
  4. Every artist in the response carries `sources` and `reasons`.
"""

from __future__ import annotations

import base64
import time

import numpy as np
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, TrackFeatures, TrackInteraction, User
from app.services import artist_reco

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)


def _emb(seed: int) -> str:
    rng = np.random.RandomState(seed)
    v = rng.randn(64).astype(np.float32)
    v /= np.linalg.norm(v)
    return base64.b64encode(v.tobytes()).decode()


def _now() -> int:
    return int(time.time())


def _reset_faiss() -> None:
    from app.services.faiss_index import effnet_index

    effnet_index._index = None
    effnet_index._id_to_track = []
    effnet_index._track_to_id = {}
    effnet_index._embeddings = None


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr("app.services.faiss_index.AsyncSessionLocal", _Session)
    monkeypatch.setattr("app.services.candidate_gen.AsyncSessionLocal", _Session)
    _reset_faiss()
    yield
    _reset_faiss()
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _ranker_stub(scores: dict[str, float]):
    async def stub(user_id, track_ids, session, **kw):
        out = [(t, float(scores.get(t, 0.0))) for t in track_ids]
        out.sort(key=lambda x: x[1], reverse=True)
        return out

    return stub


async def _add_track(session, tid, artist, *, emb_seed=None, energy=0.5, valence=0.5, title=None):
    session.add(
        TrackFeatures(
            track_id=tid,
            file_path=f"/music/{tid}.mp3",
            artist=artist,
            title=title or tid,
            energy=energy,
            valence=valence,
            embedding=_emb(emb_seed) if emb_seed is not None else None,
            analyzed_at=_now(),
            analysis_version="1",
        )
    )


async def _add_interaction(session, user_id, tid, *, play_count=0, like_count=0, satisfaction=0.0, last_played=None):
    session.add(
        TrackInteraction(
            user_id=user_id,
            track_id=tid,
            play_count=play_count,
            like_count=like_count,
            satisfaction_score=satisfaction,
            last_played_at=last_played if last_played is not None else _now(),
            updated_at=_now(),
        )
    )


async def test_liked_underplayed_beats_heavy_lowsat(monkeypatch):
    """Under-played but well-liked artist outranks a heavily-played, low-satisfaction one."""
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={"top_tracks": []}))
        await _add_track(s, "ti", "Indie Darling")
        await _add_track(s, "tp", "Pop Machine")
        await _add_interaction(s, "alice", "ti", play_count=2, like_count=3, satisfaction=0.9)
        await _add_interaction(s, "alice", "tp", play_count=100, like_count=0, satisfaction=0.1)
        await s.commit()

    # Ranker mirrors satisfaction (a model trained on satisfaction labels).
    monkeypatch.setattr("app.services.ranker.score_candidates", _ranker_stub({"ti": 0.9, "tp": 0.1}))

    async with _Session() as s:
        res = await artist_reco.recommend_artists(s, "alice", mode="balanced", limit=10)

    names = [a["name"] for a in res["artists"]]
    assert "Indie Darling" in names and "Pop Machine" in names
    assert names.index("Indie Darling") < names.index("Pop Machine")


async def test_discover_surfaces_unplayed_artist(monkeypatch):
    """mode=discover surfaces a never-played in-library artist that sounds like the user's taste."""
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={"top_tracks": [{"track_id": "a1", "score": 1.0}]}))
        await _add_track(s, "a1", "Played Artist", emb_seed=1)
        # Never-played track sharing the seed's embedding → nearest neighbour to the centroid.
        await _add_track(s, "d1", "Hidden Gem", emb_seed=1)
        await _add_track(s, "f1", "Filler One", emb_seed=50)
        await _add_track(s, "f2", "Filler Two", emb_seed=99)
        await _add_interaction(s, "alice", "a1", play_count=5, satisfaction=0.8)
        await s.commit()

    from app.services.faiss_index import build_index

    await build_index()

    monkeypatch.setattr("app.services.ranker.score_candidates", _ranker_stub({}))

    async with _Session() as s:
        res = await artist_reco.recommend_artists(s, "alice", mode="discover", include_discovery=True, limit=25)

    discoveries = [a for a in res["artists"] if a["plays"] == 0 and a["in_library"]]
    assert any(a["name"] == "Hidden Gem" for a in discoveries)
    hidden = next(a for a in res["artists"] if a["name"] == "Hidden Gem")
    assert "taste_centroid" in hidden["sources"]
    assert hidden["signals"]["content_score"] > 0.5


async def test_graceful_fallback_no_faiss_no_ranker(monkeypatch):
    """No FAISS index + ranker failure → still returns ranked artists with sources/reasons."""
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={"top_tracks": []}))
        await _add_track(s, "t1", "Some Artist")
        await _add_interaction(s, "alice", "t1", play_count=10, satisfaction=0.7)
        await s.commit()

    async def boom(*a, **k):
        raise RuntimeError("no ranker")

    monkeypatch.setattr("app.services.ranker.score_candidates", boom)

    async with _Session() as s:
        res = await artist_reco.recommend_artists(s, "alice", mode="discover", limit=10)

    assert res["artists"], "should still return artists from the legacy heuristic"
    for a in res["artists"]:
        assert a.get("sources")
        assert "reasons" in a
    assert res["mode"] == "discover"


async def test_response_always_has_sources_and_reasons(monkeypatch):
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={"top_tracks": []}))
        await _add_track(s, "t1", "Artist A")
        await _add_interaction(s, "alice", "t1", play_count=5, satisfaction=0.6)
        await s.commit()

    monkeypatch.setattr("app.services.ranker.score_candidates", _ranker_stub({"t1": 0.6}))

    async with _Session() as s:
        res = await artist_reco.recommend_artists(s, "alice", limit=5)

    assert res["artists"]
    for a in res["artists"]:
        assert isinstance(a["sources"], list) and a["sources"]
        assert isinstance(a["reasons"], list)
        assert "signals" in a

    # Semantic correctness, not just presence: no FAISS → content 0; history &
    # ranker present → their sources/reasons fire on the right thresholds.
    a = res["artists"][0]
    assert a["signals"]["content_score"] == 0.0
    assert "listening" in a["sources"]  # history_score > 0
    assert "ranker_rollup" in a["sources"]  # rollup 0.6 > 0
    assert "taste_centroid" not in a["sources"]  # content 0
    assert "you rate their tracks highly" in a["reasons"]  # rollup 0.6 >= 0.6
    assert "sounds like your taste" not in a["reasons"]  # content below threshold


# ---------------------------------------------------------------------------
# Degradation, mode-shifting, config sensitivity, empty library
# ---------------------------------------------------------------------------


def _emb_vec(vec) -> str:
    v = vec.astype(np.float32)
    v /= np.linalg.norm(v)
    return base64.b64encode(v.tobytes()).decode()


async def _seed_content_vs_history(s) -> None:
    """NearTaste: high content (its track IS the taste centroid), never played.
    FarPlayed: opposite embedding (content ~0), played heavily with high satisfaction."""
    rng = np.random.RandomState(7)
    base = rng.randn(64).astype(np.float32)
    s.add(User(user_id="alice", taste_profile={"top_tracks": [{"track_id": "tb", "score": 1.0}]}))
    s.add(
        TrackFeatures(
            track_id="tb",
            file_path="/m/tb.mp3",
            artist="NearTaste",
            title="tb",
            embedding=_emb_vec(base),
            analyzed_at=_now(),
            analysis_version="1",
        )
    )
    s.add(
        TrackFeatures(
            track_id="ta",
            file_path="/m/ta.mp3",
            artist="FarPlayed",
            title="ta",
            embedding=_emb_vec(-base),
            analyzed_at=_now(),
            analysis_version="1",
        )
    )
    await _add_interaction(s, "alice", "ta", play_count=50, like_count=2, satisfaction=0.9)


async def test_empty_library_returns_well_formed(monkeypatch):
    """A fresh/un-analysed library still returns a well-formed (empty) response."""
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={"top_tracks": []}))
        await s.commit()
    monkeypatch.setattr("app.services.ranker.score_candidates", _ranker_stub({}))
    async with _Session() as s:
        res = await artist_reco.recommend_artists(s, "alice", mode="discover", limit=10)
    assert res["artists"] == []
    assert res["mode"] == "discover"
    assert "generated_at" in res


async def test_ranker_failure_with_faiss(monkeypatch):
    """FAISS available + ranker failing → content term lives, roll-up degrades to 0."""
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={"top_tracks": [{"track_id": "a1", "score": 1.0}]}))
        await _add_track(s, "a1", "Solo", emb_seed=1)
        await _add_interaction(s, "alice", "a1", play_count=5, satisfaction=0.8)
        await s.commit()
    from app.services.faiss_index import build_index

    await build_index()

    async def boom(*a, **k):
        raise RuntimeError("no ranker")

    monkeypatch.setattr("app.services.ranker.score_candidates", boom)
    async with _Session() as s:
        res = await artist_reco.recommend_artists(s, "alice", mode="balanced", limit=10)
    solo = next(a for a in res["artists"] if a["name"] == "Solo")
    assert solo["signals"]["content_score"] > 0
    assert solo["signals"]["ranker_rollup"] == 0
    assert "taste_centroid" in solo["sources"]
    assert "ranker_rollup" not in solo["sources"]


async def test_faiss_off_with_ranker(monkeypatch):
    """FAISS not ready + ranker available → content term skipped, roll-up lives."""
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={"top_tracks": []}))
        await _add_track(s, "t1", "Solo", emb_seed=1)
        await _add_interaction(s, "alice", "t1", play_count=5, satisfaction=0.8)
        await s.commit()
    # No build_index() → FAISS not ready.
    monkeypatch.setattr("app.services.ranker.score_candidates", _ranker_stub({"t1": 0.8}))
    async with _Session() as s:
        res = await artist_reco.recommend_artists(s, "alice", mode="balanced", limit=10)
    solo = next(a for a in res["artists"] if a["name"] == "Solo")
    assert solo["signals"]["content_score"] == 0
    assert solo["signals"]["ranker_rollup"] > 0
    assert "ranker_rollup" in solo["sources"]
    assert "taste_centroid" not in solo["sources"]


async def test_mode_shifting_flips_ordering(monkeypatch):
    """discover up-weights content (NearTaste wins); familiar up-weights ranker/history (FarPlayed wins)."""
    async with _Session() as s:
        await _seed_content_vs_history(s)
        await s.commit()
    from app.services.faiss_index import build_index

    await build_index()
    monkeypatch.setattr("app.services.ranker.score_candidates", _ranker_stub({"ta": 0.9, "tb": 0.0}))

    async with _Session() as s:
        disc = await artist_reco.recommend_artists(s, "alice", mode="discover", limit=10)
    async with _Session() as s:
        fam = await artist_reco.recommend_artists(s, "alice", mode="familiar", limit=10)

    dn = [a["name"] for a in disc["artists"]]
    fn = [a["name"] for a in fam["artists"]]
    assert dn.index("NearTaste") < dn.index("FarPlayed")
    assert fn.index("FarPlayed") < fn.index("NearTaste")


async def test_config_weights_drive_ranking(monkeypatch):
    """get_config() weights are read per-call: content-only vs history-only flips the top artist."""
    from app.models.algorithm_config_schema import AlgorithmConfigData, ArtistRecoConfig

    async with _Session() as s:
        await _seed_content_vs_history(s)
        await s.commit()
    from app.services.faiss_index import build_index

    await build_index()
    monkeypatch.setattr("app.services.ranker.score_candidates", _ranker_stub({"ta": 0.9, "tb": 0.0}))

    content_only = AlgorithmConfigData(
        artist_reco=ArtistRecoConfig(w_content=1.0, w_ranker=0.0, w_lastfm=0.0, w_history=0.0)
    )
    history_only = AlgorithmConfigData(
        artist_reco=ArtistRecoConfig(w_content=0.0, w_ranker=0.0, w_lastfm=0.0, w_history=1.0)
    )

    monkeypatch.setattr("app.services.artist_reco.get_config", lambda: content_only)
    async with _Session() as s:
        c = await artist_reco.recommend_artists(s, "alice", mode="balanced", limit=10)
    monkeypatch.setattr("app.services.artist_reco.get_config", lambda: history_only)
    async with _Session() as s:
        h = await artist_reco.recommend_artists(s, "alice", mode="balanced", limit=10)

    assert c["artists"][0]["name"] == "NearTaste"
    assert h["artists"][0]["name"] == "FarPlayed"
