"""
GrooveIQ – Chunk 9: modes read endpoint + admin-gated modes writes.

Two layers (same split as ``test_recommend_modes_api.py``):

* **Pure unit test** — the ``modes`` group is registered in ``CONFIG_GROUPS`` so
  the Settings → Algorithm accordion renders it. No app/DB; runs anywhere.
* **API tests** for the new ``GET /v1/algorithm/modes`` (read-only, any key) and
  for saving the ``modes`` group through the existing admin-gated
  ``PUT /v1/algorithm/config`` — a new version is created and the change is
  reflected by ``get_config().modes`` immediately (serving-time, no pipeline run).
  These import the full app (Python 3.11+) and self-skip on the legacy 3.9 env.
"""

from __future__ import annotations

import pytest

from app.models.algorithm_config_schema import CONFIG_GROUPS, PRESET_NAMES

# ---------------------------------------------------------------------------
# Pure unit test — schema/group registration (no app, runs on any Python)
# ---------------------------------------------------------------------------


def test_modes_group_registered_in_config_groups():
    """Chunk 9 adds `modes` to CONFIG_GROUPS so the GUI renders it; not a retrain group."""
    by_key = {g["key"]: g for g in CONFIG_GROUPS}
    assert "modes" in by_key, "modes group must be exposed via /algorithm/config/defaults"
    assert by_key["modes"]["retrain_required"] is False
    assert by_key["modes"]["label"]


# ===========================================================================
# API tests — full app (Python 3.11+); self-skip on the legacy 3.9 dev env.
# ===========================================================================

try:
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core import security
    from app.models.algorithm_config_schema import get_defaults
    from app.models.db import Base
    from app.services import algorithm_config as config_service

    _APP_OK = True
except Exception:  # pragma: no cover - the 3.9 dev env can't import the full app
    _APP_OK = False

_requires_app = pytest.mark.skipif(not _APP_OK, reason="full app import requires Python 3.11+")


if _APP_OK:
    from app.core.config import settings
    from app.db.session import get_session
    from app.main import app

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

    @pytest.fixture(autouse=True)
    def _restore_algo_cache():
        """Snapshot + restore the module-level config cache.

        A PUT mutates ``config_service._active_config`` (so the change is visible
        at serving time without a DB round-trip). Restoring afterwards keeps the
        cache from bleeding the tweaked config into other test modules.
        """
        snap = (config_service._active_config, config_service._active_version, config_service._active_id)
        yield
        (
            config_service._active_config,
            config_service._active_version,
            config_service._active_id,
        ) = snap

    @pytest.fixture
    async def client():
        headers = {"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
            yield c

    # ---- GET /v1/algorithm/modes (read-only, any authenticated key) --------

    @_requires_app
    async def test_get_modes_returns_active_preset_defs(client):
        resp = await client.get("/v1/algorithm/modes")
        assert resp.status_code == 200
        modes = resp.json()
        for name in PRESET_NAMES:
            assert name in modes, f"preset {name} missing"
            assert "kappa" in modes[name]
            assert "source_weight_mult" in modes[name]
        assert modes["default_preset"] == "balanced"
        assert set(modes["dial_anchors"]) == set(PRESET_NAMES)
        # balanced reproduces today's defaults (the no-regression contract).
        assert modes["balanced"]["exploration_fraction"] == 0.15
        assert modes["balanced"]["kappa"] == 0.0

    @_requires_app
    async def test_get_modes_is_not_admin_gated(client, monkeypatch):
        """Read endpoint is `require_api_key` only — a non-admin key may read it."""
        # Configure an admin set the test identity is NOT part of; GET must still pass.
        monkeypatch.setattr(security, "_admin_key_hashes", {security.hash_key("some-other-admin-key")})
        resp = await client.get("/v1/algorithm/modes")
        assert resp.status_code == 200

    @_requires_app
    async def test_get_modes_requires_auth(monkeypatch):
        """With auth enforced, a header-less request is rejected (401)."""
        monkeypatch.setattr(security.settings, "DISABLE_AUTH", False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as noauth:
            resp = await noauth.get("/v1/algorithm/modes")
        assert resp.status_code == 401

    # ---- PUT /v1/algorithm/config — modes save is admin-gated --------------

    @_requires_app
    async def test_modes_put_requires_admin(client, monkeypatch):
        """Saving modes goes through the admin-gated config PUT — non-admin → 403."""
        monkeypatch.setattr(security, "_admin_key_hashes", {security.hash_key("the-only-admin-key")})
        cfg = get_defaults().model_dump()
        resp = await client.put("/v1/algorithm/config", json={"name": "x", "config": cfg})
        assert resp.status_code == 403

    @_requires_app
    async def test_modes_save_creates_version_and_reflects_immediately(client):
        """An admin PUT of a modes change creates a new version and is live at once."""
        cfg = get_defaults().model_dump()
        base_kappa = cfg["modes"]["discovery"]["kappa"]
        new_kappa = round(base_kappa + 0.5, 3)
        cfg["modes"]["discovery"]["kappa"] = new_kappa

        r1 = await client.put("/v1/algorithm/config", json={"name": "modes-t1", "config": cfg})
        assert r1.status_code == 200
        v1 = r1.json()["version"]
        assert r1.json()["is_active"] is True

        # Serving-time cache reflects it immediately (no pipeline run needed).
        assert config_service.get_config().modes.discovery.kappa == new_kappa
        # And the read endpoint echoes it.
        modes = (await client.get("/v1/algorithm/modes")).json()
        assert modes["discovery"]["kappa"] == new_kappa

        # A second save bumps the version (append-only history).
        cfg["modes"]["discovery"]["kappa"] = round(new_kappa + 0.1, 3)
        r2 = await client.put("/v1/algorithm/config", json={"name": "modes-t2", "config": cfg})
        assert r2.status_code == 200
        assert r2.json()["version"] == v1 + 1
        assert config_service.get_config().modes.discovery.kappa == round(new_kappa + 0.1, 3)
