"""
GrooveIQ — Tests for taste-personalized playlists (POST /v1/playlists `user_id`).

The optional `user_id` modifier biases each strategy's per-candidate score toward
the user's affinity — sourced from the recommend ranker (here exercising its
satisfaction-score fallback, with no trained model) — while:

  * preserving each sequencing strategy's sonic objective (flow BPM tolerance), and
  * staying byte-identical to the user-agnostic path when `user_id` is absent.

A track with a `TrackInteraction` gets that interaction's `satisfaction_score`;
an un-interacted track scores 0.0 (feature_eng `build_features`). With the model
pinned to None, `score_candidates` ranks purely by that, so seeding interactions
deterministically controls which tracks are "loved".
"""

from __future__ import annotations

import base64
import time
from itertools import pairwise

import numpy as np
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, PlaylistTrack, TrackFeatures, TrackInteraction, User
from app.services import ranker
from app.services.playlist_service import (
    _generate_flow,
    _load_tracks,
    compute_cache_key,
    generate_playlist,
)

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)


def _now() -> int:
    return int(time.time())


def _make_embedding(seed: int) -> str:
    rng = np.random.RandomState(seed)
    vec = rng.randn(64).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return base64.b64encode(vec.tobytes()).decode()


def _clap_b64(vec: np.ndarray) -> str:
    return base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode()


async def _add_track(s, tid, *, energy=0.5, bpm=120.0, mood_conf=0.8, emb_seed=0, clap=None):
    kw = dict(
        track_id=tid,
        file_path=f"/music/{tid}.mp3",
        title=tid,
        artist="Artist",
        duration=200.0,
        bpm=bpm,
        energy=energy,
        valence=0.5,
        danceability=0.5,
        embedding=_make_embedding(emb_seed),
        mood_tags=[{"label": "happy", "confidence": mood_conf}],
        analyzed_at=_now(),
        analysis_version="1",
    )
    if clap is not None:
        kw["clap_embedding"] = clap
    s.add(TrackFeatures(**kw))


async def _love(s, user_id, tid, *, satisfaction=0.95):
    s.add(
        TrackInteraction(
            user_id=user_id,
            track_id=tid,
            play_count=10,
            satisfaction_score=satisfaction,
            last_played_at=_now(),
            updated_at=_now(),
        )
    )


async def _track_ids(s, playlist_id) -> list[str]:
    rows = await s.execute(
        select(PlaylistTrack.track_id).where(PlaylistTrack.playlist_id == playlist_id).order_by(PlaylistTrack.position)
    )
    return [r[0] for r in rows.all()]


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _force_ranker_fallback(monkeypatch):
    """Pin the ranking model to None so scoring is the deterministic
    satisfaction-score fallback regardless of test order (a prior test that
    trained the ranker would otherwise leak its model via the module global)."""
    monkeypatch.setattr(ranker, "_model", None, raising=False)


# ---------------------------------------------------------------------------
# Cache key — backward-compat hash + per-user segmentation (no DB)
# ---------------------------------------------------------------------------


def test_cache_key_backward_compat_and_user_scoping():
    base = dict(
        created_by="api-key-hash",
        strategy="text",
        seed_track_id=None,
        params={"prompt": "late night drive"},
        max_tracks=25,
        bucket_date="2026-06-24",
    )
    legacy = compute_cache_key(**base)  # pre-change callers pass no user_id
    none_user = compute_cache_key(**base, user_id=None)
    alice = compute_cache_key(**base, user_id="alice")
    bob = compute_cache_key(**base, user_id="bob")

    assert legacy == none_user, "user_id=None must reproduce the exact legacy hash"
    assert alice != legacy, "a personalized request must segment the cache"
    assert alice != bob, "two users must not collide on one cached playlist"


# ---------------------------------------------------------------------------
# Backward-compat contract — the user-agnostic path is unchanged
# ---------------------------------------------------------------------------


async def test_user_id_none_is_noop_and_deterministic():
    async with _Session() as s:
        for i in range(8):
            await _add_track(s, f"t{i:03d}", bpm=118.0 + i, emb_seed=0)
        await s.commit()

    async with _Session() as s:
        tracks = await _load_tracks(s)

    # The taste kwarg's default collapses to the pre-change computation:
    # no kwarg == empty == None == all-zero weights all yield the same walk.
    legacy = _generate_flow(tracks, "t000", 8)
    assert legacy == _generate_flow(tracks, "t000", 8, taste={})
    assert legacy == _generate_flow(tracks, "t000", 8, taste=None)
    assert legacy == _generate_flow(tracks, "t000", 8, taste={t.track_id: 0.0 for t in tracks})

    # The full generate path with user_id=None is stable across runs.
    async with _Session() as s:
        p1 = await generate_playlist(s, "Mix", "flow", "t000", None, 8, user_id=None)
        ids1 = await _track_ids(s, p1.id)
    async with _Session() as s:
        p2 = await generate_playlist(s, "Mix", "flow", "t000", None, 8, user_id=None)
        ids2 = await _track_ids(s, p2.id)
    assert ids1 == ids2
    assert len(ids1) >= 5


# ---------------------------------------------------------------------------
# Selection strategies — taste changes which tracks survive selection
# ---------------------------------------------------------------------------


async def test_mood_selection_pulls_in_loved_tracks():
    async with _Session() as s:
        for i in range(12):
            await _add_track(s, f"t{i:03d}", energy=0.3 + (i % 5) * 0.1, mood_conf=0.8, emb_seed=i)
        s.add(User(user_id="alice", taste_profile={}))
        await _love(s, "alice", "t010")
        await _love(s, "alice", "t011")
        await s.commit()

    async with _Session() as s:
        p = await generate_playlist(s, "Happy", "mood", None, {"mood": "happy"}, 5, user_id="alice")
        taste_ids = set(await _track_ids(s, p.id))
    async with _Session() as s:
        p = await generate_playlist(s, "Happy", "mood", None, {"mood": "happy"}, 5, user_id=None)
        control_ids = set(await _track_ids(s, p.id))

    # All 12 tracks share confidence 0.8, so the control keeps the first 5 by
    # stable order (loved tracks t010/t011 fall outside). Taste boosts them in.
    assert {"t010", "t011"} <= taste_ids
    assert {"t010", "t011"}.isdisjoint(control_ids)


async def test_text_selection_leads_with_loved_tracks(monkeypatch):
    # text needs CLAP; fake a uniform query so base relevance is equal across
    # tracks and taste is the only differentiator.
    from app.core.config import settings as cfg
    from app.services import clap_text

    monkeypatch.setattr(cfg, "CLAP_ENABLED", True, raising=False)
    query = np.ones(16, dtype=np.float32)
    query /= np.linalg.norm(query)
    monkeypatch.setattr(clap_text, "encode_text", lambda prompt: query)

    async with _Session() as s:
        for i in range(6):
            await _add_track(s, f"t{i:03d}", emb_seed=i, clap=_clap_b64(np.ones(16)))
        s.add(User(user_id="alice", taste_profile={}))
        await _love(s, "alice", "t004")
        await _love(s, "alice", "t005")
        await s.commit()

    async with _Session() as s:
        p = await generate_playlist(s, "P", "text", None, {"prompt": "anything"}, 4, user_id="alice")
        taste_ids = await _track_ids(s, p.id)
    async with _Session() as s:
        p = await generate_playlist(s, "P", "text", None, {"prompt": "anything"}, 4, user_id=None)
        control_ids = await _track_ids(s, p.id)

    # text ranks by relevance with no post-sort reorder, so loved tracks lead.
    assert set(taste_ids[:2]) == {"t004", "t005"}
    assert {"t004", "t005"}.isdisjoint(set(control_ids))


# ---------------------------------------------------------------------------
# Sequencing strategy — taste biases the walk WITHOUT breaking the chain
# ---------------------------------------------------------------------------


async def test_flow_biases_loved_and_preserves_sequencing():
    bpms = {f"t{i:03d}": 118.0 + i for i in range(8)}  # 118..125: every pair within ±15
    async with _Session() as s:
        for i in range(8):
            # Identical embeddings + constant energy → cosine/energy bonuses are
            # uniform, so the BPM filter and taste are what actually drive the walk.
            await _add_track(s, f"t{i:03d}", energy=0.5, bpm=118.0 + i, emb_seed=0)
        s.add(User(user_id="alice", taste_profile={}))
        await _love(s, "alice", "t006")
        await _love(s, "alice", "t007")
        await s.commit()

    async with _Session() as s:
        p = await generate_playlist(s, "Flow", "flow", "t000", None, 8, user_id="alice")
        taste_ids = await _track_ids(s, p.id)
    async with _Session() as s:
        p = await generate_playlist(s, "Flow", "flow", "t000", None, 8, user_id=None)
        control_ids = await _track_ids(s, p.id)

    assert taste_ids[0] == "t000", "seed must stay first"
    # The artifact (smooth BPM chain) is preserved: the blend feeds the greedy
    # score, it does not bypass the ±15 BPM tolerance.
    for a, b in pairwise(taste_ids):
        assert abs(bpms[a] - bpms[b]) <= 15

    def mean_pos(ids):
        return sum(ids.index(t) for t in ("t006", "t007")) / 2

    # Loved tracks are reached earlier than in the unpersonalized control.
    assert mean_pos(taste_ids) < mean_pos(control_ids)


# ---------------------------------------------------------------------------
# Cold start — an unknown user degrades to a full, non-erroring playlist
# ---------------------------------------------------------------------------


async def test_cold_user_degrades_to_a_sane_playlist():
    """An unknown user (no User row, no interactions) must still get a full,
    non-erroring playlist — `_taste_weights` returns {} and the blend is a no-op."""
    async with _Session() as s:
        for i in range(8):
            await _add_track(s, f"t{i:03d}", bpm=118.0 + i, emb_seed=0)
        await s.commit()

    async with _Session() as s:
        p = await generate_playlist(s, "Cold", "flow", "t000", None, 8, user_id="nobody")
        ids = await _track_ids(s, p.id)

    assert ids[0] == "t000"
    assert len(ids) >= 5


# ---------------------------------------------------------------------------
# HTTP — two users get distinct playlists; same user/day is idempotent
# ---------------------------------------------------------------------------


async def _override_get_session():
    async with _Session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture
async def client():
    app.dependency_overrides[get_session] = _override_get_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {},
    ) as c:
        yield c
    app.dependency_overrides.clear()


class TestHttpPersonalization:
    async def test_two_users_distinct_and_same_user_idempotent(self, client: AsyncClient):
        async with _Session() as s:
            for i in range(12):
                await _add_track(s, f"t{i:03d}", energy=0.3 + (i % 5) * 0.1, mood_conf=0.8, emb_seed=i)
            s.add(User(user_id="alice", taste_profile={}))
            s.add(User(user_id="bob", taste_profile={}))
            await _love(s, "alice", "t010")
            await _love(s, "alice", "t011")
            await _love(s, "bob", "t000")
            await _love(s, "bob", "t001")
            await s.commit()

        def body(uid):
            return {"name": "Daily", "strategy": "mood", "params": {"mood": "happy"}, "max_tracks": 5, "user_id": uid}

        a1 = await client.post("/v1/playlists", json=body("alice"))
        b1 = await client.post("/v1/playlists", json=body("bob"))
        assert a1.status_code == 201, a1.text
        assert b1.status_code == 201, b1.text

        # Per-user cache segmentation → two distinct persisted playlists.
        assert a1.json()["id"] != b1.json()["id"]

        a_ids = {t["track_id"] for t in a1.json()["tracks"]}
        b_ids = {t["track_id"] for t in b1.json()["tracks"]}
        assert "t010" in a_ids and "t010" not in b_ids, "content must lean to each user's taste"

        # Same user + same body + same UTC day → idempotent cache hit.
        a2 = await client.post("/v1/playlists", json=body("alice"))
        assert a2.status_code == 200
        assert a2.json()["id"] == a1.json()["id"]
