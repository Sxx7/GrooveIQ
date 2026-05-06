"""
GrooveIQ – user_id format enforcement (issue #86).

The default ``USER_ID_PATTERN`` matches Navidrome's identifier output
(20–22 alphanumeric chars). Tests in this file pin the pattern to its
production default explicitly, so they remain valid even if the global
test override in ``conftest.py`` is relaxed for legacy fixtures.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.user_id import is_valid_user_id, validate_user_id
from app.db.session import get_session
from app.main import app
from app.models.db import Base, User

_PROD_PATTERN = r"^[A-Za-z0-9]{20,22}$"

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _TestSession() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    # Pin the production-default pattern explicitly for the whole module.
    monkeypatch.setattr(settings, "USER_ID_PATTERN", _PROD_PATTERN)
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.dependency_overrides[get_session] = override_get_session
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {},
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Pure validator
# ---------------------------------------------------------------------------


class TestIsValidUserId:
    def test_navidrome_22char_accepted(self):
        assert is_valid_user_id("RthAVUuiCHM0MVjwu0QFTb")  # 22 chars, real prod ID

    def test_navidrome_20char_accepted(self):
        assert is_valid_user_id("a1b2c3d4e5f6g7h8i9j0")  # legacy xid format

    def test_navidrome_21char_accepted(self):
        assert is_valid_user_id("a1b2c3d4e5f6g7h8i9j01")

    def test_short_test_name_rejected(self):
        assert not is_valid_user_id("Simon")
        assert not is_valid_user_id("alice")
        assert not is_valid_user_id("testuser")

    def test_email_rejected(self):
        assert not is_valid_user_id("39w3z2frwz@privaterelay.appleid.com")

    def test_typos_rejected(self):
        # Real strings that hit prod's api_call_logs as paths.
        assert not is_valid_user_id("Simon27)7")
        assert not is_valid_user_id("Simon2")
        assert not is_valid_user_id("simon")  # lowercase, 5 chars

    def test_empty_or_none_rejected(self):
        assert not is_valid_user_id("")
        assert not is_valid_user_id(None)
        assert not is_valid_user_id("   ")

    def test_too_long_rejected(self):
        assert not is_valid_user_id("a" * 23)

    def test_special_chars_rejected(self):
        # 22 chars but contains a hyphen — Navidrome only emits [A-Za-z0-9].
        assert not is_valid_user_id("a-b2c3d4e5f6g7h8i9j0kl")


class TestValidateUserId:
    def test_returns_input_on_valid(self):
        assert validate_user_id("RthAVUuiCHM0MVjwu0QFTb") == "RthAVUuiCHM0MVjwu0QFTb"

    def test_raises_400_on_invalid(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            validate_user_id("Simon")
        assert exc.value.status_code == 400
        assert "Simon" in exc.value.detail
        assert _PROD_PATTERN in exc.value.detail


# ---------------------------------------------------------------------------
# Endpoint integration
# ---------------------------------------------------------------------------


_GOOD = "RthAVUuiCHM0MVjwu0QFTb"  # 22-char Navidrome-shaped
_BAD = "Simon"


async def _seed(user_id: str = _GOOD) -> None:
    async with _TestSession() as s:
        s.add(User(user_id=user_id, display_name=user_id))
        await s.commit()


class TestEndpointRejectsBadUserId:
    async def test_event_post_rejects_bad_user_id(self, client):
        # POST /v1/events with malformed user_id should 400 BEFORE creating a user row.
        resp = await client.post(
            "/v1/events",
            json={"user_id": _BAD, "track_id": "t1", "event_type": "play_start"},
        )
        assert resp.status_code == 400
        assert "Simon" in resp.json()["detail"]
        # And no User row should have been auto-created.
        async with _TestSession() as s:
            from sqlalchemy import select

            count = (await s.execute(select(User).where(User.user_id == _BAD))).scalars().all()
        assert count == []

    async def test_event_post_accepts_good_user_id(self, client):
        await _seed(_GOOD)
        resp = await client.post(
            "/v1/events",
            json={"user_id": _GOOD, "track_id": "t1", "event_type": "play_start"},
        )
        # Either 202 (accepted) or 422 (track not found) — point is it's not a 400 from user_id.
        assert resp.status_code != 400, f"Got 400 with body: {resp.text}"

    async def test_get_user_profile_rejects_bad_user_id(self, client):
        resp = await client.get(f"/v1/users/{_BAD}/profile")
        assert resp.status_code == 400
        assert "Simon" in resp.json()["detail"]

    async def test_get_user_profile_returns_404_for_unknown_good_user_id(self, client):
        # 22-char string that isn't in the DB — should 404, not 400.
        resp = await client.get(f"/v1/users/{_GOOD}/profile")
        assert resp.status_code == 404

    async def test_post_users_rejects_bad_user_id(self, client):
        resp = await client.post(
            "/v1/users",
            json={"user_id": _BAD, "display_name": "Simon"},
        )
        assert resp.status_code == 400

    async def test_post_users_accepts_good_user_id(self, client):
        resp = await client.post(
            "/v1/users",
            json={"user_id": _GOOD, "display_name": "Test"},
        )
        assert resp.status_code == 201
        assert resp.json()["user_id"] == _GOOD
