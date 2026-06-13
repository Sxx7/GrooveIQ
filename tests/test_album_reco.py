"""
GrooveIQ — Tests for the album recommendation service (app/services/album_reco.py).

Covers the Phase B acceptance criteria from docs/HANDOFF_artist_album_reco.md:

  * An album with several high-scoring tracks ranks above one with a single hit
    plus filler (top-k roll-up, not a plain mean).
  * `coverage` is derived from max(track_number).
  * Null-album rows are ignored.
  * Freshness boosts a long-unplayed favourite.
"""

from __future__ import annotations

import time

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, TrackFeatures, TrackInteraction, User
from app.services import album_reco

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)


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
    # Albums are scored content-free here (no FAISS index built) so the blend
    # isolates the dimension each test exercises.
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


async def _add_track(session, tid, *, artist, album, album_artist=None, track_number=None, title=None):
    session.add(
        TrackFeatures(
            track_id=tid,
            file_path=f"/music/{tid}.mp3",
            artist=artist,
            album=album,
            album_artist=album_artist,
            track_number=track_number,
            title=title or tid,
            energy=0.5,
            valence=0.5,
            analyzed_at=_now(),
            analysis_version="1",
        )
    )


async def _add_interaction(session, user_id, tid, *, play_count=0, satisfaction=0.0, last_played=None):
    session.add(
        TrackInteraction(
            user_id=user_id,
            track_id=tid,
            play_count=play_count,
            satisfaction_score=satisfaction,
            last_played_at=last_played,
            updated_at=_now(),
        )
    )


async def test_multi_high_beats_one_hit_plus_filler(monkeypatch):
    """Album of consistently strong tracks ranks above one hit + filler (top-k roll-up)."""
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={"top_tracks": []}))
        for i in range(4):
            await _add_track(s, f"g{i}", artist="AAA", album="Great", album_artist="AAA")
            await _add_track(s, f"m{i}", artist="BBB", album="Meh", album_artist="BBB")
        await s.commit()

    scores = {f"g{i}": 0.9 for i in range(4)}
    scores.update({"m0": 0.9, "m1": 0.1, "m2": 0.1, "m3": 0.1})
    monkeypatch.setattr("app.services.ranker.score_candidates", _ranker_stub(scores))

    async with _Session() as s:
        res = await album_reco.recommend_albums(s, "alice", mode="balanced", limit=10)

    names = [a["album"] for a in res["albums"]]
    assert "Great" in names and "Meh" in names
    assert names.index("Great") < names.index("Meh")


async def test_coverage_from_max_track_number(monkeypatch):
    """coverage = owned / max(track_number)."""
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={"top_tracks": []}))
        # 5 owned tracks, but track numbers imply a 10-track album.
        for i, tn in enumerate([1, 2, 3, 4, 10]):
            await _add_track(s, f"p{i}", artist="AAA", album="Partial", album_artist="AAA", track_number=tn)
        await s.commit()

    monkeypatch.setattr("app.services.ranker.score_candidates", _ranker_stub({}))

    async with _Session() as s:
        res = await album_reco.recommend_albums(s, "alice", limit=10)

    partial = next(a for a in res["albums"] if a["album"] == "Partial")
    assert partial["signals"]["coverage"] == 0.5
    assert partial["completeness"] == 0.5
    assert partial["library_track_count"] == 5


async def test_null_album_rows_ignored(monkeypatch):
    """Tracks without an album never form an album entry."""
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={"top_tracks": []}))
        for i in range(3):
            await _add_track(s, f"r{i}", artist="AAA", album="Real", album_artist="AAA")
        await _add_track(s, "loose1", artist="AAA", album=None, album_artist="AAA")
        await _add_track(s, "loose2", artist="AAA", album="", album_artist="AAA")
        await s.commit()

    monkeypatch.setattr("app.services.ranker.score_candidates", _ranker_stub({}))

    async with _Session() as s:
        res = await album_reco.recommend_albums(s, "alice", limit=10)

    albums = [a["album"] for a in res["albums"]]
    assert albums == ["Real"]
    assert all(a["album"] for a in res["albums"])  # no null/empty album surfaced


async def test_freshness_boosts_long_unplayed_favourite(monkeypatch):
    """A long-unplayed favourite outranks an otherwise-identical recently-played album."""
    now = _now()
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={"top_tracks": []}))
        for i in range(3):
            await _add_track(s, f"old{i}", artist="OLD", album="OldFave", album_artist="OLD")
            await _add_track(s, f"new{i}", artist="NEW", album="FreshPlay", album_artist="NEW")
            # Same satisfaction/plays; only last_played differs.
            await _add_interaction(s, "alice", f"old{i}", play_count=1, satisfaction=0.6, last_played=now - 200 * 86400)
            await _add_interaction(s, "alice", f"new{i}", play_count=1, satisfaction=0.6, last_played=now)
        await s.commit()

    # Equal ranker scores → rollup identical; only freshness can separate them.
    monkeypatch.setattr("app.services.ranker.score_candidates", _ranker_stub({}))

    async with _Session() as s:
        res = await album_reco.recommend_albums(s, "alice", mode="discover", limit=10)

    names = [a["album"] for a in res["albums"]]
    assert names.index("OldFave") < names.index("FreshPlay")
    old = next(a for a in res["albums"] if a["album"] == "OldFave")
    fresh = next(a for a in res["albums"] if a["album"] == "FreshPlay")
    assert old["signals"]["days_since_last_play"] >= 180
    assert (fresh["signals"]["days_since_last_play"] or 0) <= 1


async def test_empty_library_returns_well_formed(monkeypatch):
    """A fresh/un-analysed library still returns a well-formed (empty) response."""
    async with _Session() as s:
        s.add(User(user_id="alice", taste_profile={"top_tracks": []}))
        await s.commit()
    monkeypatch.setattr("app.services.ranker.score_candidates", _ranker_stub({}))
    async with _Session() as s:
        res = await album_reco.recommend_albums(s, "alice", mode="discover", limit=10)
    assert res["albums"] == []
    assert res["mode"] == "discover"
    assert "generated_at" in res
