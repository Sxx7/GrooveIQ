"""
GrooveIQ – Chunk 5: discovery-dial endpoint wiring + dial resolver.

Two layers:

* **Pure unit tests** for ``app.services.modes`` — ``resolve_dial`` precedence /
  interpolation, the whitelist boundary (the security-critical part: a crafted
  request can never reach an arbitrary config key), and ``derive_reasons``.
  These need no app/DB and run anywhere.
* **API tests** for ``GET /v1/recommend/{user_id}`` — the dial materially changes
  the track list, input validation returns 422, the response echoes ``discovery``
  + per-track ``reasons``, and auth/admin gating holds. These import the full app
  (Python 3.11+); they self-skip on the legacy 3.9 dev env.
"""

from __future__ import annotations

import time

import pytest

from app.models.algorithm_config_schema import PRESET_NAMES, PresetConfig, get_defaults
from app.services import modes as modes_svc
from app.services.modes import OVERRIDE_WHITELIST, derive_reasons, resolve_dial

# ---------------------------------------------------------------------------
# Pure unit tests — resolver (no app, runs on any Python)
# ---------------------------------------------------------------------------


def _cfg():
    return get_defaults().modes


def test_default_resolves_to_default_preset():
    """No params -> the configured default preset (balanced), echoing its anchor."""
    cfg = _cfg()
    res = resolve_dial(None, None, cfg)
    assert res.preset == cfg.default_preset == "balanced"
    assert res.discovery == cfg.dial_anchors["balanced"]
    # The override is exactly the balanced preset (the no-regression contract).
    assert res.overrides["modes"]["active"] == cfg.balanced.model_dump()


def test_override_shape_matches_chunk4_consumer():
    """resolve_dial emits the exact shape the reranker/candidate_gen read (Chunk 4)."""
    p = _cfg().deep_discovery
    res = resolve_dial(None, "deep_discovery", _cfg())
    assert res.overrides == {
        "modes": {"active": p.model_dump()},
        "reranker": {
            "exploration_fraction": p.exploration_fraction,
            "freshness_boost": p.freshness_boost,
            "repeat_window_hours": p.repeat_window_hours,
        },
    }


def test_mode_takes_precedence_over_discovery():
    """When both are given, the named mode wins and the dial echoes its anchor."""
    cfg = _cfg()
    res = resolve_dial(0.95, "familiar", cfg)  # discovery=0.95 would be deep, but mode wins
    assert res.preset == "familiar"
    assert res.discovery == cfg.dial_anchors["familiar"] == 0.0
    assert res.overrides["modes"]["active"]["kappa"] == cfg.familiar.kappa


def test_discovery_zero_equals_mode_familiar():
    """?discovery=0.0 resolves to exactly the same override as ?mode=familiar."""
    cfg = _cfg()
    by_dial = resolve_dial(0.0, None, cfg)
    by_mode = resolve_dial(None, "familiar", cfg)
    assert by_dial.overrides == by_mode.overrides
    assert by_dial.discovery == by_mode.discovery == 0.0


def test_discovery_one_equals_mode_deep_discovery():
    """?discovery=1.0 resolves to exactly the deep_discovery override."""
    cfg = _cfg()
    assert resolve_dial(1.0, None, cfg).overrides == resolve_dial(None, "deep_discovery", cfg).overrides


def test_interpolation_is_monotonic_in_kappa_and_novelty():
    """Sweeping the dial up never lowers the acquisition coefficient or novelty."""
    cfg = _cfg()
    kappas = []
    strengths = []
    for d in [0.0, 0.1, 0.2, 0.3, 0.45, 0.6, 0.75, 0.9, 1.0]:
        active = resolve_dial(d, None, cfg).overrides["modes"]["active"]
        kappas.append(active["kappa"])
        strengths.append(active["novelty_strength"])
    assert kappas == sorted(kappas), kappas
    assert strengths == sorted(strengths), strengths
    assert kappas[-1] > kappas[0]  # strictly more exploratory at the deep end


def test_interpolated_midpoint_sits_between_anchors():
    """A value between two anchors yields an interpolated (unnamed) preset between them."""
    cfg = _cfg()
    res = resolve_dial(0.45, None, cfg)  # between balanced (0.3) and discovery (0.6)
    assert res.preset is None
    k = res.overrides["modes"]["active"]["kappa"]
    assert cfg.balanced.kappa < k < cfg.discovery.kappa
    # novelty_filter engages smoothly as soon as any proven slice is excluded.
    assert res.overrides["modes"]["active"]["novelty_filter"] is True


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        resolve_dial(None, "bogus", _cfg())


# ---------------------------------------------------------------------------
# Pure unit tests — the whitelist boundary (security-critical)
# ---------------------------------------------------------------------------


def _assert_only_whitelisted(overrides: dict) -> None:
    assert set(overrides) <= set(OVERRIDE_WHITELIST), overrides
    for group, fields in overrides.items():
        assert set(fields) <= OVERRIDE_WHITELIST[group], (group, fields)
    # The nested PresetConfig dump may only contain real preset fields.
    assert set(overrides["modes"]["active"]) <= set(PresetConfig.model_fields)


def test_resolver_only_ever_emits_whitelisted_keys():
    """Table-driven over every preset + a dial sweep: nothing escapes the whitelist."""
    cfg = _cfg()
    for name in PRESET_NAMES:
        _assert_only_whitelisted(resolve_dial(None, name, cfg).overrides)
    for d in [0.0, 0.05, 0.123, 0.3, 0.456, 0.6, 0.789, 0.95, 1.0]:
        _assert_only_whitelisted(resolve_dial(d, None, cfg).overrides)
    _assert_only_whitelisted(resolve_dial(None, None, cfg).overrides)


def test_enforce_whitelist_strips_rogue_keys():
    """A hand-built dict with arbitrary config keys is stripped to the whitelist.

    This is the guarantee that a crafted request cannot set, say, a ranker
    hyperparameter through the per-request override merge.
    """
    dirty = {
        "ranker": {"learning_rate": 9.9},  # not a dial-writable group at all
        "reranker": {"freshness_boost": 0.5, "skip_demote_factor": 0.0},  # 2nd key not whitelisted
        "modes": {"active": {"kappa": 1.0, "evil": 2}, "balanced": {"kappa": 0.0}},  # nested + 2nd key bad
    }
    clean = modes_svc._enforce_whitelist(dirty)
    assert clean == {
        "reranker": {"freshness_boost": 0.5},
        "modes": {"active": {"kappa": 1.0}},
    }
    assert "ranker" not in clean


# ---------------------------------------------------------------------------
# Pure unit tests — derive_reasons
# ---------------------------------------------------------------------------


def test_reasons_from_sources():
    assert derive_reasons(["cf", "popular"], []) == ["fans_like_this", "popular"]


def test_reasons_dedup_collapses_equivalent_sources():
    # content + content_profile both map to "matches_your_taste".
    assert derive_reasons(["content", "content_profile"], []) == ["matches_your_taste"]


def test_reasons_intent_precedes_sources_and_dedups():
    actions = [
        {"action": "acquisition", "is_proven": False, "sigma": 0.7},
        {"action": "freshness_boost"},
    ]
    reasons = derive_reasons(["lastfm_similar"], actions)
    assert reasons == ["exploring", "new_to_you", "similar_listeners"]


def test_reasons_proven_favourite_from_acquisition():
    reasons = derive_reasons(["content"], [{"action": "acquisition", "is_proven": True}])
    assert reasons[0] == "proven_favourite"
    assert "matches_your_taste" in reasons


def test_reasons_empty_when_no_signal():
    assert derive_reasons([], []) == []
    assert derive_reasons(["__unknown_source__"], []) == []


# ===========================================================================
# API tests — full app (Python 3.11+); self-skip on the legacy 3.9 dev env.
# ===========================================================================

try:
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core import security
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
    _PROVEN_IDS = [f"proven{i}" for i in range(6)]
    _WEAK_IDS = [f"weak{i}" for i in range(20)]

    async def _seed_mode_user(user_id: str = "modeuser"):
        """A user with proven favourites + many weak tracks, all surfacing as candidates.

        The proven set is what ``familiar`` leans on and ``deep_discovery`` excludes;
        the 20 weak tracks keep the post-filter pool above the starvation floor so the
        exclusion actually bites.
        """
        async with _Session() as session:
            session.add(
                User(
                    user_id=user_id,
                    display_name="Mode User",
                    taste_profile={
                        "audio_preferences": {"energy_mean": 0.6, "valence_mean": 0.5, "danceability_mean": 0.5},
                        "behaviour": {"total_plays": 200, "skip_rate": 0.1, "avg_completion": 0.85},
                    },
                    profile_updated_at=_NOW,
                )
            )
            for i, tid in enumerate(_PROVEN_IDS):
                session.add(
                    TrackFeatures(
                        track_id=tid,
                        file_path=f"/music/proven_artist{i}/album/{tid}.mp3",
                        duration=210.0,
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
                        play_count=30,
                        full_listen_count=30,
                        early_skip_count=0,
                        avg_completion=0.95,
                        satisfaction_score=0.95,
                        last_played_at=_NOW - 10 * 86_400,
                        updated_at=_NOW,
                    )
                )
            for i, tid in enumerate(_WEAK_IDS):
                session.add(
                    TrackFeatures(
                        track_id=tid,
                        file_path=f"/music/weak_artist{i}/album/{tid}.mp3",
                        duration=200.0,
                        bpm=118.0,
                        energy=0.55,
                        valence=0.45,
                        danceability=0.5,
                        analyzed_at=_NOW,
                        analysis_version="1",
                    )
                )
                session.add(
                    TrackInteraction(
                        user_id=user_id,
                        track_id=tid,
                        play_count=1,
                        full_listen_count=0,
                        early_skip_count=0,
                        avg_completion=0.30,
                        satisfaction_score=0.20,
                        last_played_at=_NOW - 10 * 86_400,
                        updated_at=_NOW,
                    )
                )
            await session.commit()

    def _ids(payload) -> list[str]:
        return [t["track_id"] for t in payload["tracks"]]

    @_requires_app
    async def test_familiar_vs_deep_discovery_differ(client):
        """familiar surfaces proven favourites; deep_discovery excludes the proven set."""
        await _seed_mode_user()
        fam = (await client.get("/v1/recommend/modeuser?mode=familiar&limit=30")).json()
        deep = (await client.get("/v1/recommend/modeuser?mode=deep_discovery&limit=30")).json()

        fam_ids, deep_ids = set(_ids(fam)), set(_ids(deep))
        proven = set(_PROVEN_IDS)
        assert proven & fam_ids, "familiar should surface proven favourites"
        assert not (proven & deep_ids), "deep_discovery must exclude the proven set"
        assert fam_ids != deep_ids  # materially different

    @_requires_app
    async def test_response_echoes_discovery_and_reasons(client):
        await _seed_mode_user()
        resp = await client.get("/v1/recommend/modeuser?mode=discovery&limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["discovery"] == _cfg().dial_anchors["discovery"]
        assert body["tracks"], "expected candidates for a seeded user"
        for t in body["tracks"]:
            assert isinstance(t["reasons"], list)
        # At least one track carries a non-empty reason chip.
        assert any(t["reasons"] for t in body["tracks"])

    @_requires_app
    async def test_default_echoes_balanced_dial_value(client):
        await _seed_mode_user()
        body = (await client.get("/v1/recommend/modeuser?limit=10")).json()
        assert body["discovery"] == _cfg().dial_anchors["balanced"] == 0.3

    @_requires_app
    async def test_explicit_discovery_value_echoed(client):
        await _seed_mode_user()
        body = (await client.get("/v1/recommend/modeuser?discovery=0.45&limit=10")).json()
        assert body["discovery"] == 0.45

    @_requires_app
    async def test_invalid_mode_returns_422(client):
        await _seed_mode_user()
        resp = await client.get("/v1/recommend/modeuser?mode=bogus")
        assert resp.status_code == 422

    @_requires_app
    async def test_out_of_range_discovery_returns_422(client):
        await _seed_mode_user()
        assert (await client.get("/v1/recommend/modeuser?discovery=2.0")).status_code == 422
        assert (await client.get("/v1/recommend/modeuser?discovery=-0.5")).status_code == 422

    @_requires_app
    async def test_missing_api_key_rejected_when_auth_enabled(monkeypatch):
        """With auth enforced, a request with no Authorization header is rejected.

        Uses a header-less client (the shared ``client`` fixture sends a Bearer
        token whenever keys are configured, e.g. from a dev ``.env``) and forces
        ``DISABLE_AUTH=False`` so the no-auth bypass is off regardless of env.
        """
        monkeypatch.setattr(security.settings, "DISABLE_AUTH", False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as noauth:
            resp = await noauth.get("/v1/recommend/modeuser?mode=discovery")
        assert resp.status_code == 401

    @_requires_app
    async def test_debug_mode_requires_admin(client, monkeypatch):
        """debug=true stays admin-gated even with the dial params present."""
        # Configure an admin key the (anonymous) test identity is not part of.
        monkeypatch.setattr(security, "_admin_key_hashes", {security.hash_key("the-only-admin-key")})
        resp = await client.get("/v1/recommend/modeuser?mode=discovery&debug=true")
        assert resp.status_code == 403
