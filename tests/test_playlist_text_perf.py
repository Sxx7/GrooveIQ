"""
GrooveIQ — Tests for the vectorized, thread-offloaded ``strategy='text'`` path.

These lock in the perf fix for the playlist-500 / pool-exhaustion bug:

  * ``_generate_text_vectorized`` produces the same ranking as the readable
    reference loop ``_generate_text`` (parity), and
  * ``generate_playlist`` runs that ranking in a worker thread (off the single
    uvicorn worker's event loop), so a burst of "Playlists for You" requests
    can't block the loop or starve the DB pool, and
  * concurrent text requests all succeed (the Semaphore(1) gate serializes the
    heavy work without erroring or racing CLAP init).

The CLAP text encoder is faked (a fixed unit query) so no ONNX model is needed.
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.playlist_service as ps
from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, PlaylistTrack, TrackFeatures

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)


def _clap_b64(vec: np.ndarray) -> str:
    return base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode()


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _enable_clap_with_fixed_query(monkeypatch):
    """Enable CLAP and pin the text encoder to a fixed unit query so ranking is
    determined purely by each track's stored CLAP vector."""
    from app.services import clap_text

    monkeypatch.setattr(settings, "CLAP_ENABLED", True, raising=False)
    rng = np.random.RandomState(1)
    q = rng.randn(_DIM).astype(np.float32)
    q /= np.linalg.norm(q)
    monkeypatch.setattr(clap_text, "encode_text", lambda prompt: q.copy())
    return q


_DIM = 32


# ---------------------------------------------------------------------------
# Parity — vectorized ranking == reference loop
# ---------------------------------------------------------------------------


def _random_candidates(n: int, dim: int, seed: int):
    """Distinct, un-normalised random vectors → no exact score ties, so the
    deterministic (-cosine, track_id) tiebreak never diverges from the
    reference's stable order."""
    rng = np.random.RandomState(seed)
    track_ids, b64s, fake_tracks = [], [], []
    for i in range(n):
        v = rng.randn(dim).astype(np.float32)
        tid = f"trk{i:04d}"
        b64 = _clap_b64(v)
        track_ids.append(tid)
        b64s.append(b64)
        fake_tracks.append(SimpleNamespace(track_id=tid, clap_embedding=b64))
    return track_ids, b64s, fake_tracks


@pytest.mark.parametrize("taste", [{}, {"trk0003": 0.9, "trk0017": 0.7, "trk0042": 0.95}])
def test_text_vectorized_matches_reference_full_ranking(_enable_clap_with_fixed_query, taste):
    """With max_tracks >= N every candidate is returned, so this compares the
    full ranking (no truncation boundary) — the strongest parity check."""
    query = _enable_clap_with_fixed_query
    n = 60
    track_ids, b64s, fake_tracks = _random_candidates(n, _DIM, seed=7)

    reference = ps._generate_text(fake_tracks, "anything", n, taste=taste)
    vectorized = ps._generate_text_vectorized(track_ids, b64s, query, taste, n)

    assert vectorized == reference


@pytest.mark.parametrize("taste", [{}, {"trk0005": 0.8}])
def test_text_vectorized_matches_reference_truncated(_enable_clap_with_fixed_query, taste):
    """Truncation boundary (top-k of N) also matches; fixed seed keeps it
    deterministic."""
    query = _enable_clap_with_fixed_query
    track_ids, b64s, fake_tracks = _random_candidates(60, _DIM, seed=7)

    reference = ps._generate_text(fake_tracks, "anything", 10, taste=taste)
    vectorized = ps._generate_text_vectorized(track_ids, b64s, query, taste, 10)

    assert len(vectorized) == 10
    assert vectorized == reference


def test_text_vectorized_skips_malformed_and_zero_norm(_enable_clap_with_fixed_query):
    """Undecodable, wrong-sized, and zero-norm payloads are excluded — matching
    the reference loop's per-row guards — and never crash the stack/reshape."""
    query = _enable_clap_with_fixed_query
    good_ids, good_b64s, _ = _random_candidates(5, _DIM, seed=3)

    track_ids = [*good_ids, "bad_b64", "wrong_dim", "zero_norm"]
    b64s = [
        *good_b64s,
        "!!!not base64!!!",
        _clap_b64(np.ones(_DIM + 4)),  # wrong length
        _clap_b64(np.zeros(_DIM)),  # zero norm
    ]

    out = ps._generate_text_vectorized(track_ids, b64s, query, {}, 25)

    assert set(out) == set(good_ids)
    assert "bad_b64" not in out and "wrong_dim" not in out and "zero_norm" not in out


def test_text_vectorized_empty_candidates_returns_empty(_enable_clap_with_fixed_query):
    assert ps._generate_text_vectorized([], [], _enable_clap_with_fixed_query, {}, 25) == []


# ---------------------------------------------------------------------------
# Offload — the catalog-wide ranking runs in a worker thread, not on the loop
# ---------------------------------------------------------------------------


async def _seed_clap_tracks(n: int):
    rng = np.random.RandomState(11)
    async with _Session() as s:
        for i in range(n):
            v = rng.randn(_DIM).astype(np.float32)
            s.add(
                TrackFeatures(
                    track_id=f"t{i:04d}",
                    file_path=f"/music/t{i:04d}.mp3",
                    title=f"t{i:04d}",
                    artist="Artist",
                    duration=180.0,
                    clap_embedding=_clap_b64(v),
                    analyzed_at=int(time.time()),
                    analysis_version="1",
                )
            )
        await s.commit()


async def test_ranking_runs_off_the_event_loop(monkeypatch):
    """The heavy ranking must execute on a different thread than the running
    event loop — that is the whole point of the asyncio.to_thread offload."""
    await _seed_clap_tracks(40)

    loop_thread_id = threading.get_ident()
    seen: dict[str, int] = {}
    real = ps._generate_text_vectorized

    def spy(*args, **kwargs):
        seen["thread_id"] = threading.get_ident()
        return real(*args, **kwargs)

    monkeypatch.setattr(ps, "_generate_text_vectorized", spy)

    async with _Session() as s:
        playlist = await ps.generate_playlist(s, "Text", "text", None, {"prompt": "deep house"}, 12, user_id=None)
        ids = [
            r[0]
            for r in (
                await s.execute(
                    select(PlaylistTrack.track_id)
                    .where(PlaylistTrack.playlist_id == playlist.id)
                    .order_by(PlaylistTrack.position)
                )
            ).all()
        ]

    assert len(ids) == 12
    assert seen.get("thread_id") is not None
    assert seen["thread_id"] != loop_thread_id, "ranking ran on the event-loop thread (not offloaded)"


async def test_offloaded_work_does_not_block_the_event_loop(monkeypatch):
    """While a slow ranking is in flight (in its worker thread), the event loop
    stays responsive: an independent coroutine completes well before it."""
    await _seed_clap_tracks(20)

    real = ps._generate_text_vectorized

    def slow(*args, **kwargs):
        time.sleep(0.5)  # blocking — but in a worker thread, so the loop is free
        return real(*args, **kwargs)

    monkeypatch.setattr(ps, "_generate_text_vectorized", slow)

    async def gen():
        async with _Session() as s:
            await ps.generate_playlist(s, "T", "text", None, {"prompt": "x"}, 8, user_id=None)

    async def quick_tick():
        await asyncio.sleep(0.05)
        return time.perf_counter()

    start = time.perf_counter()
    gen_task = asyncio.create_task(gen())
    tick_at = await quick_tick()
    # The 50 ms tick resolves long before the 500 ms blocking ranking — only
    # possible if the loop wasn't blocked by the sleep.
    assert tick_at - start < 0.3
    await gen_task


# ---------------------------------------------------------------------------
# Concurrency — a burst of text requests all succeed (Semaphore(1), no races)
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


async def test_concurrent_text_generations_are_serialized_and_all_succeed(monkeypatch):
    """The real ``_generate_text_offloaded`` gate lets only one ranking run at a
    time (Semaphore(1)) and N concurrent calls all complete without racing.

    DB access is stubbed so this asserts the gate/offload property itself rather
    than fighting SQLite's single-connection test harness (prod is Postgres with
    a real pool, where concurrent requests are independent)."""
    track_ids, b64s, _ = _random_candidates(30, _DIM, seed=5)
    candidates = list(zip(track_ids, b64s, strict=True))

    async def fake_load(_session):
        return candidates

    monkeypatch.setattr(ps, "_load_text_candidates", fake_load)

    active = 0
    max_active = 0
    lock = threading.Lock()
    real = ps._generate_text_vectorized

    def tracked(*args, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)  # widen the window so any overlap would be observed
        with lock:
            active -= 1
        return real(*args, **kwargs)

    monkeypatch.setattr(ps, "_generate_text_vectorized", tracked)

    async def one():
        return await ps._generate_text_offloaded(None, "prompt", 10, {})

    results = await asyncio.gather(*[one() for _ in range(6)])

    assert all(len(r) == 10 for r in results)
    assert max_active == 1, f"Semaphore(1) should serialize ranking; saw {max_active} concurrent"


async def test_http_text_playlist_201_end_to_end(client: AsyncClient):
    """Sequential end-to-end: the /v1/playlists text route wires through the
    offloaded path, persists, and returns 201 with the selected tracks."""
    await _seed_clap_tracks(30)

    resp = await client.post(
        "/v1/playlists",
        json={"name": "Late Set", "strategy": "text", "params": {"prompt": "deep house"}, "max_tracks": 10},
    )

    assert resp.status_code == 201, resp.text
    assert len(resp.json()["tracks"]) == 10
