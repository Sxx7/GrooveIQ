"""
GrooveIQ – Chunk 6: stale-while-revalidate mix cache + prewarm/mixes endpoints.

Two layers:

* **Cache mechanics** for ``app.services.mix_cache`` — fresh hit calls the builder
  once, a stale entry serves stale immediately and schedules exactly one
  background rebuild, the version-bearing key misses after a config bump, and
  concurrent misses never exceed the rebuild concurrency cap. These use a mock
  builder (no DB).
* **API tests** for ``POST /v1/users/{id}/mixes/prewarm`` and
  ``GET /v1/users/{id}/mixes`` — prewarm returns 202 and populates the cache,
  the mixes menu lists shelf specs, and both reject missing auth / cross-user
  access. These import the full app (Python 3.11+) and self-skip on 3.9.

NOTE: ``tests/conftest.py`` installs an autouse fixture that clears the mix
cache around every test, so each test below starts from a clean cache.
"""

from __future__ import annotations

import asyncio
import time

import pytest

try:
    from app.core.config import settings
    from app.services import mix_cache

    _CACHE_OK = True
except Exception:  # pragma: no cover - the 3.9 dev env can't import the app config stack
    _CACHE_OK = False

_requires_cache = pytest.mark.skipif(not _CACHE_OK, reason="requires app.services.mix_cache (Python 3.11+)")


# ===========================================================================
# Cache mechanics — mock builder, no DB
# ===========================================================================


@_requires_cache
async def test_fresh_hit_calls_builder_once():
    """Two reads inside the fresh window run the builder exactly once."""
    mix_cache.clear()
    calls = {"n": 0}

    async def builder():
        calls["n"] += 1
        return {"v": calls["n"]}

    a = await mix_cache.get_or_build("k", builder)
    b = await mix_cache.get_or_build("k", builder)
    assert a == b == {"v": 1}
    assert calls["n"] == 1


@_requires_cache
async def test_stale_serves_stale_and_schedules_single_rebuild():
    """A stale entry is served immediately; N concurrent reads trigger one rebuild."""
    mix_cache.clear()
    key = "stale"
    # Age the entry past the fresh window but inside the stale grace.
    mix_cache.put(key, {"v": "old"}, built_at=time.monotonic() - 10_000)

    rebuilds = {"n": 0}

    async def builder():
        rebuilds["n"] += 1
        await asyncio.sleep(0.02)
        return {"v": "new"}

    results = await asyncio.gather(
        *[mix_cache.get_or_build(key, builder, fresh_seconds=120, stale_seconds=86_400) for _ in range(5)]
    )
    # Every concurrent reader got the stale payload back immediately.
    assert all(r == {"v": "old"} for r in results)

    # Let the single background rebuild finish and swap the entry.
    await asyncio.sleep(0.1)
    assert rebuilds["n"] == 1, "exactly one background rebuild should run, not N"

    payload, state = mix_cache.peek(key)
    assert state == "fresh"
    assert payload == {"v": "new"}


@_requires_cache
def test_config_version_in_key_misses_after_bump():
    """The version-bearing key means a config bump misses (no stale serve)."""
    mix_cache.clear()
    common = dict(user_id="u", dial_bucket="balanced", context_bucket="", limit=25, model_version="m1")
    k1 = mix_cache.build_key(config_version=1, **common)
    k2 = mix_cache.build_key(config_version=2, **common)
    assert k1 != k2

    mix_cache.put(k1, {"v": 1})
    payload, state = mix_cache.peek(k2)
    assert state == "miss" and payload is None
    # The original version still hits.
    payload1, state1 = mix_cache.peek(k1)
    assert state1 == "fresh" and payload1 == {"v": 1}


@_requires_cache
def test_model_version_in_key_misses_after_retrain():
    """A retrain (new model_version) also lands on a fresh key."""
    mix_cache.clear()
    common = dict(user_id="u", dial_bucket="balanced", context_bucket="", limit=25, config_version=1)
    k1 = mix_cache.build_key(model_version="m1", **common)
    k2 = mix_cache.build_key(model_version="m2", **common)
    assert k1 != k2
    mix_cache.put(k1, {"v": 1})
    assert mix_cache.peek(k2)[1] == "miss"


@_requires_cache
async def test_concurrency_cap_respected(monkeypatch):
    """N > cap simultaneous misses run at most `cap` builders concurrently."""
    mix_cache.clear()
    monkeypatch.setattr(settings, "MIX_CACHE_MAX_CONCURRENT_REBUILDS", 2)
    # clear() reset the semaphore; the first build recreates it at the new cap.

    state = {"cur": 0, "peak": 0}

    async def builder():
        state["cur"] += 1
        state["peak"] = max(state["peak"], state["cur"])
        await asyncio.sleep(0.05)
        state["cur"] -= 1
        return {"ok": True}

    keys = [f"cap{i}" for i in range(6)]
    await asyncio.gather(*[mix_cache.get_or_build(k, builder) for k in keys])

    assert 1 <= state["peak"] <= 2, state["peak"]
    assert mix_cache.size() == 6  # all six distinct keys ultimately built + cached


@_requires_cache
def test_eviction_bounds_entry_count(monkeypatch):
    """The store never exceeds MIX_CACHE_MAX_ENTRIES (oldest evicted first)."""
    mix_cache.clear()
    monkeypatch.setattr(settings, "MIX_CACHE_MAX_ENTRIES", 3)
    base = time.monotonic()
    for i in range(5):
        # Strictly increasing build times so eviction order is deterministic.
        mix_cache.put(f"e{i}", {"v": i}, built_at=base + i)
    assert mix_cache.size() == 3
    # The two oldest (e0, e1) were evicted; the three newest survive.
    assert mix_cache.peek("e0")[1] == "miss"
    assert mix_cache.peek("e1")[1] == "miss"
    assert mix_cache.peek("e4")[1] == "fresh"


@_requires_cache
def test_clear_resets_everything():
    mix_cache.clear()
    mix_cache.put("a", {"v": 1})
    assert mix_cache.size() == 1
    mix_cache.clear()
    assert mix_cache.size() == 0
    assert mix_cache.peek("a")[1] == "miss"


# ===========================================================================
# API tests — full app (Python 3.11+); self-skip on the legacy 3.9 dev env.
# ===========================================================================

try:
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core import security
    from app.db.session import get_session
    from app.main import app
    from app.models.db import Base, TrackFeatures, TrackInteraction, User

    _APP_OK = True
except Exception:  # pragma: no cover - full app import requires Python 3.11+
    _APP_OK = False

_requires_app = pytest.mark.skipif(not _APP_OK, reason="full app import requires Python 3.11+")


if _APP_OK:
    _engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False})
    _Session = async_sessionmaker(_engine, expire_on_commit=False)

    async def _override_get_session():
        async with _Session() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @pytest.fixture(autouse=True)
    async def _setup_db():
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        app.dependency_overrides[get_session] = _override_get_session
        yield
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        app.dependency_overrides.clear()

    @pytest.fixture
    async def client():
        headers = {"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
            yield c

    def _test_identity() -> str:
        """The API-key identity the test client authenticates as.

        Mirrors the ``client`` fixture: the first configured key when API keys
        exist (e.g. a dev ``.env``), else the ``DISABLE_AUTH`` anonymous identity.
        """
        return settings.api_keys_list[0] if settings.api_keys_list else "anonymous"

    async def _drain_cache(minimum: int = 1, tries: int = 100) -> None:
        """Wait for background warm-up tasks to land entries (and finish under patch)."""
        for _ in range(tries):
            if mix_cache.size() >= minimum:
                return
            await asyncio.sleep(0.02)

    _NOW = int(time.time())

    async def _seed_user(user_id: str = "mixuser"):
        """A user with a handful of played tracks so generation yields candidates."""
        async with _Session() as session:
            session.add(
                User(
                    user_id=user_id,
                    display_name="Mix User",
                    taste_profile={
                        "audio_preferences": {"energy_mean": 0.6, "valence_mean": 0.5, "danceability_mean": 0.5},
                        "behaviour": {"total_plays": 120, "skip_rate": 0.1, "avg_completion": 0.85},
                    },
                    profile_updated_at=_NOW,
                )
            )
            for i in range(12):
                tid = f"mt{i}"
                session.add(
                    TrackFeatures(
                        track_id=tid,
                        file_path=f"/music/artist{i}/album/{tid}.mp3",
                        title=f"Track {i}",
                        artist=f"Artist {i}",
                        duration=200.0,
                        bpm=120.0,
                        energy=0.6,
                        valence=0.5,
                        danceability=0.5,
                        analyzed_at=_NOW,
                        analysis_version="1",
                    )
                )
                session.add(
                    TrackInteraction(
                        user_id=user_id,
                        track_id=tid,
                        play_count=10,
                        full_listen_count=9,
                        early_skip_count=0,
                        avg_completion=0.9,
                        satisfaction_score=0.8,
                        last_played_at=_NOW - 86_400,
                        updated_at=_NOW,
                    )
                )
            await session.commit()

    @_requires_app
    async def test_prewarm_returns_202_and_populates_cache(client, monkeypatch):
        """prewarm returns 202 and the background warm-up populates the cache."""
        await _seed_user()
        # Background builders run detached from the request, so they open their
        # own session via AsyncSessionLocal — point that at the test engine.
        monkeypatch.setattr("app.db.session.AsyncSessionLocal", _Session)
        mix_cache.clear()

        resp = await client.post("/v1/users/mixuser/mixes/prewarm", json={"modes": ["balanced"], "limit": 10})
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "warming"
        assert body["modes"] == ["balanced"]

        # Wait briefly for the bounded background rebuild to land an entry.
        await _drain_cache(minimum=1)
        assert mix_cache.size() >= 1, "prewarm should have populated at least one cache entry"

    @_requires_app
    async def test_prewarm_default_warms_all_presets(client, monkeypatch):
        await _seed_user()
        monkeypatch.setattr("app.db.session.AsyncSessionLocal", _Session)
        mix_cache.clear()
        resp = await client.post("/v1/users/mixuser/mixes/prewarm", json={})
        assert resp.status_code == 202
        # All four presets requested (familiar/balanced/discovery/deep_discovery).
        assert len(resp.json()["modes"]) == 4
        # Drain the background warm-ups so they complete while AsyncSessionLocal
        # is still patched (avoids tasks erroring against the real DB post-teardown).
        await _drain_cache(minimum=4)
        assert mix_cache.size() == 4

    @_requires_app
    async def test_mixes_menu_returns_shelf_specs(client):
        await _seed_user()
        resp = await client.get("/v1/users/mixuser/mixes")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["user_id"] == "mixuser"
        mixes = body["mixes"]
        modes = {m["mode"] for m in mixes}
        assert {"familiar", "balanced", "discovery", "deep_discovery"} <= modes
        for m in mixes:
            assert m["endpoint"] == f"/v1/recommend/mixuser?mode={m['mode']}"
            assert "discovery" in m and "title" in m

    @_requires_app
    async def test_mixes_menu_404_for_unknown_user(client):
        resp = await client.get("/v1/users/ghostuser/mixes")
        assert resp.status_code == 404

    @_requires_app
    async def test_prewarm_rejects_missing_api_key(monkeypatch):
        """With auth enforced, a prewarm with no Authorization header is rejected."""
        monkeypatch.setattr(security.settings, "DISABLE_AUTH", False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as noauth:
            resp = await noauth.post("/v1/users/mixuser/mixes/prewarm", json={})
        assert resp.status_code == 401

    @_requires_app
    async def test_prewarm_rejects_cross_user(client, monkeypatch):
        """A key bound to userA cannot prewarm userB's mixes."""
        # Bind the test identity to userA only — a prewarm for userB must 403.
        monkeypatch.setattr(security, "_key_user_bindings", {security.hash_key(_test_identity()): {"userA"}})
        resp = await client.post("/v1/users/userB/mixes/prewarm", json={})
        assert resp.status_code == 403

    @_requires_app
    async def test_mixes_rejects_cross_user(client, monkeypatch):
        monkeypatch.setattr(security, "_key_user_bindings", {security.hash_key(_test_identity()): {"userA"}})
        resp = await client.get("/v1/users/userB/mixes")
        assert resp.status_code == 403

    @_requires_app
    async def test_recommend_cache_hit_is_consistent(client):
        """Two identical mode requests are served from cache: same tracks, fresh request_id."""
        await _seed_user()
        first = (await client.get("/v1/recommend/mixuser?mode=balanced&limit=10")).json()
        second = (await client.get("/v1/recommend/mixuser?mode=balanced&limit=10")).json()
        assert first["tracks"], "expected candidates for a seeded user"
        first_ids = [t["track_id"] for t in first["tracks"]]
        second_ids = [t["track_id"] for t in second["tracks"]]
        assert first_ids == second_ids, "a cache hit must return the identical track list"
        # Impressions are logged per request, so the request_id is always fresh.
        assert first["request_id"] != second["request_id"]
