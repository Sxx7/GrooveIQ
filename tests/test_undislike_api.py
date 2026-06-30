"""API tests for POST /v1/users/{id}/tracks/{tid}/undislike.

The honest reversal of an explicit hard-dislike: zeroes ``dislike_count`` on the
user's ``TrackInteraction`` so candidate generation / mixes / radio stop excluding
the track. Accepts either the internal track_id or a media_server_id (iOS sends the
latter). Self-skips on the legacy 3.9 dev env that can't import the full app.
"""

from __future__ import annotations

import time

import pytest

try:
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import settings
    from app.db.session import get_session
    from app.main import app
    from app.models.db import Base, TrackFeatures, TrackInteraction, User

    _APP_OK = True
except Exception:  # pragma: no cover - the 3.9 dev env can't import the full app
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

    _NOW = int(time.time())

    async def _seed(user_id="u", track_id="t1", media_server_id="ms1", dislike_count=1):
        async with _Session() as s:
            s.add(User(user_id=user_id, taste_profile={}))
            s.add(
                TrackFeatures(
                    track_id=track_id,
                    file_path=f"/m/{track_id}.mp3",
                    media_server_id=media_server_id,
                    analyzed_at=_NOW,
                    analysis_version="1",
                )
            )
            s.add(
                TrackInteraction(
                    user_id=user_id, track_id=track_id, dislike_count=dislike_count, updated_at=_NOW
                )
            )
            await s.commit()

    async def _dislike_count(user_id, track_id):
        async with _Session() as s:
            return (
                await s.execute(
                    select(TrackInteraction.dislike_count).where(
                        TrackInteraction.user_id == user_id,
                        TrackInteraction.track_id == track_id,
                    )
                )
            ).scalar_one_or_none()

    @_requires_app
    @pytest.mark.asyncio
    async def test_undislike_zeroes_count_by_internal_id(client):
        await _seed()
        assert await _dislike_count("u", "t1") == 1
        resp = await client.post("/v1/users/u/tracks/t1/undislike")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "undisliked"
        assert await _dislike_count("u", "t1") == 0  # re-eligible for recommendation

    @_requires_app
    @pytest.mark.asyncio
    async def test_undislike_resolves_media_server_id(client):
        await _seed()
        resp = await client.post("/v1/users/u/tracks/ms1/undislike")
        assert resp.status_code == 200, resp.text
        # ms1 resolves to the internal track_id t1, and *that* row is zeroed.
        assert resp.json()["track_id"] == "t1"
        assert await _dislike_count("u", "t1") == 0

    @_requires_app
    @pytest.mark.asyncio
    async def test_undislike_is_noop_when_never_disliked(client):
        await _seed(dislike_count=0)
        resp = await client.post("/v1/users/u/tracks/t1/undislike")
        assert resp.status_code == 200, resp.text
        assert await _dislike_count("u", "t1") == 0

    @_requires_app
    @pytest.mark.asyncio
    async def test_undislike_unknown_track_still_ok(client):
        """An unresolvable id (no TrackFeatures, no interaction) is a harmless no-op, not a 500."""
        await _seed()
        resp = await client.post("/v1/users/u/tracks/does-not-exist/undislike")
        assert resp.status_code == 200, resp.text
        assert resp.json()["track_id"] == "does-not-exist"
