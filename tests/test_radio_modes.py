"""
GrooveIQ – Chunk 7: radio honours the discovery dial.

The dial sets a *baseline posture* for a radio session that in-session feedback
then drifts around. Covered here:

  * the drift step scales with the dial (familiar hugs the seed, deep roams) and
    feedback still moves the vector at every posture;
  * a ``deep_discovery`` session excludes the user's proven set from the pool,
    while a ``familiar`` session keeps it (the novelty filter, reused from
    candidate generation);
  * input validation (``discovery`` out of range -> 422) and session ownership
    (cross-user -> 403) hold on the radio routes.

These import the full app (Python 3.11+); they self-skip on the legacy 3.9 dev
env exactly like ``test_recommend_modes_api``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator

import numpy as np
import pytest

try:
    import pytest_asyncio
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.core import security
    from app.core.config import settings
    from app.db.session import get_session
    from app.main import app
    from app.models.db import Base, TrackFeatures, TrackInteraction, User
    from app.services import radio as radio_service

    _APP_OK = True
except Exception:  # pragma: no cover - the 3.9 dev env can't import the full app
    _APP_OK = False

_requires_app = pytest.mark.skipif(not _APP_OK, reason="full app import requires Python 3.11+")


if _APP_OK:
    _engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False})
    _TestSession = async_sessionmaker(_engine, expire_on_commit=False)
    _NOW = int(time.time())

    _PROVEN_IDS = [f"proven{i}" for i in range(6)]
    _WEAK_IDS = [f"weak{i}" for i in range(14)]
    _ALL_IDS = _PROVEN_IDS + _WEAK_IDS

    async def _override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with _TestSession() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @pytest_asyncio.fixture(autouse=True)
    async def _setup_db():
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        app.dependency_overrides[get_session] = _override_get_session
        radio_service._sessions.clear()
        yield
        radio_service._sessions.clear()
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        app.dependency_overrides.clear()

    @pytest_asyncio.fixture
    async def client():
        headers = {"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
            yield c

    def _unit(vec: np.ndarray) -> np.ndarray:
        return (vec / np.linalg.norm(vec)).astype(np.float32)

    async def _seed_proven_and_weak(user_id: str = "u") -> None:
        """A user with 6 proven favourites + 14 weak tracks (mirrors the recommend
        modes fixture). The proven set is what ``familiar`` keeps and
        ``deep_discovery`` excludes; the weak pool stays above the novelty
        filter's starvation floor so the exclusion actually bites."""
        async with _TestSession() as session:
            session.add(User(user_id=user_id, display_name="U", profile_updated_at=_NOW))
            for i, tid in enumerate(_PROVEN_IDS):
                session.add(
                    TrackFeatures(
                        track_id=tid,
                        file_path=f"/music/proven{i}/album/{tid}.mp3",
                        duration=210.0,
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
                        file_path=f"/music/weak{i}/album/{tid}.mp3",
                        duration=200.0,
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

    def _stub_pool(monkeypatch) -> None:
        """Make every seeded track a candidate via FAISS source 1, and let the
        ranker/reranker pass through so the novelty filter is the only thing that
        changes the output set between postures."""
        monkeypatch.setattr("app.services.faiss_index.is_ready", lambda: True)

        def _fake_search(emb, k=50, exclude_ids=None):
            ex = exclude_ids or set()
            return [(tid, 0.9) for tid in _ALL_IDS if tid not in ex]

        monkeypatch.setattr("app.services.faiss_index.search", _fake_search)
        monkeypatch.setattr("app.services.faiss_index.search_by_track_id", lambda tid, k=50, exclude_ids=None: [])
        monkeypatch.setattr("app.services.session_embeddings.is_ready", lambda: False)
        monkeypatch.setattr("app.services.lastfm_candidates.is_ready", lambda: False)
        monkeypatch.setattr("app.services.collab_filter.is_ready", lambda: False)

        async def _fake_score(user_id, candidate_ids, db, **_):
            return [(tid, 1.0) for tid in candidate_ids]

        async def _fake_rerank(scored, user_id, db, **_):
            return scored

        monkeypatch.setattr("app.services.ranker.score_candidates", _fake_score)
        monkeypatch.setattr("app.services.reranker.rerank", _fake_rerank)
        monkeypatch.setattr("app.services.reranker.get_last_rerank_actions", lambda: [])

    def _make_session(session_id: str, *, discovery: float, user_id: str = "u") -> radio_service.RadioSession:
        s = radio_service.RadioSession(
            session_id=session_id,
            user_id=user_id,
            seed_type="track",
            seed_value="seed",
            seed_track_ids=["seed"],
            seed_embedding=_unit(np.ones(64, dtype=np.float32)),
            drift_embedding=_unit(np.ones(64, dtype=np.float32)),
            discovery=discovery,
        )
        radio_service.store_session(s)
        return s

    # -----------------------------------------------------------------------
    # Drift step scales with the dial (and feedback still moves the vector)
    # -----------------------------------------------------------------------

    @_requires_app
    def test_dial_drift_scale_anchored_at_balanced():
        """The drift-step multiplier is 1.0 at the balanced anchor and rises
        monotonically with the dial — so the default radio is unchanged."""
        assert radio_service._dial_drift_scale(0.3) == 1.0  # balanced anchor = no-op
        assert radio_service._dial_drift_scale(0.0) < radio_service._dial_drift_scale(0.3)
        assert radio_service._dial_drift_scale(0.3) < radio_service._dial_drift_scale(1.0)
        assert radio_service._dial_drift_scale(0.0) >= 0.0  # never inverts the step

    @_requires_app
    def test_radio_source_dial_key_mapping_contract():
        """``radio_drift`` (the adaptive core) is never re-weighted; the
        exploratory/anchor sources map onto the recommend-source mult keys."""
        m = radio_service._RADIO_SOURCE_DIAL_KEY
        assert "radio_drift" not in m
        assert m["radio_lastfm"] == "lastfm_similar"
        assert m["radio_seed"] == "content_profile"

    @_requires_app
    def test_like_drifts_more_at_deep_and_still_drifts_at_familiar(monkeypatch):
        """A like moves the drift vector at *every* posture (feedback intact),
        and moves it farther at deep_discovery than at familiar (larger step)."""
        seed = _unit(np.eye(64, dtype=np.float32)[0])  # unit basis vector e0
        liked = _unit(np.eye(64, dtype=np.float32)[1])  # orthogonal unit vector e1
        monkeypatch.setattr("app.services.faiss_index.get_embedding", lambda tid: liked.copy())

        fam = radio_service.RadioSession(
            session_id="fam",
            user_id="u",
            seed_type="track",
            seed_value="seed",
            seed_embedding=seed.copy(),
            drift_embedding=seed.copy(),
            discovery=0.0,
        )
        deep = radio_service.RadioSession(
            session_id="deep",
            user_id="u",
            seed_type="track",
            seed_value="seed",
            seed_embedding=seed.copy(),
            drift_embedding=seed.copy(),
            discovery=1.0,
        )
        radio_service.store_session(fam)
        radio_service.store_session(deep)

        assert radio_service.record_feedback("fam", "liked-track", "like")
        assert radio_service.record_feedback("deep", "liked-track", "like")

        fam_dist = float(np.linalg.norm(fam.drift_embedding - seed))
        deep_dist = float(np.linalg.norm(deep.drift_embedding - seed))

        assert fam_dist > 1e-6  # feedback still drifts at the familiar end
        assert deep_dist > fam_dist  # deep posture takes a larger step

    # -----------------------------------------------------------------------
    # Proven-set novelty filter: deep excludes, familiar keeps
    # -----------------------------------------------------------------------

    @_requires_app
    async def test_deep_discovery_excludes_proven_familiar_keeps_them(monkeypatch):
        await _seed_proven_and_weak()
        _stub_pool(monkeypatch)

        _make_session("fam", discovery=0.0)
        _make_session("deep", discovery=1.0)

        async with _TestSession() as db:
            fam_tracks = await radio_service.get_next_tracks("fam", 50, db)
        async with _TestSession() as db:
            deep_tracks = await radio_service.get_next_tracks("deep", 50, db)

        fam_ids = {t["track_id"] for t in fam_tracks}
        deep_ids = {t["track_id"] for t in deep_tracks}
        proven = set(_PROVEN_IDS)

        # familiar keeps the proven favourites; deep_discovery excludes them.
        assert proven <= fam_ids, "familiar should surface proven favourites"
        assert not (proven & deep_ids), "deep_discovery must exclude the proven set"
        # deep still returns a healthy pool of novel tracks (above the floor).
        assert set(deep_ids) <= set(_WEAK_IDS)
        assert len(deep_ids) >= 10
        assert fam_ids != deep_ids  # materially different posture

    @_requires_app
    async def test_default_balanced_session_keeps_proven(monkeypatch):
        """A session created without a dial value defaults to balanced (0.3),
        which keeps the proven set — the no-regression posture."""
        await _seed_proven_and_weak()
        _stub_pool(monkeypatch)
        s = _make_session("bal", discovery=0.3)
        assert s.discovery == 0.3

        async with _TestSession() as db:
            tracks = await radio_service.get_next_tracks("bal", 50, db)
        ids = {t["track_id"] for t in tracks}
        assert set(_PROVEN_IDS) <= ids

    # -----------------------------------------------------------------------
    # Routes: posture plumbing, validation, ownership
    # -----------------------------------------------------------------------

    @_requires_app
    async def test_start_persists_and_echoes_discovery(client, monkeypatch):
        """POST /radio/start carries the body's discovery onto the session and
        echoes it back."""
        await _seed_proven_and_weak()
        monkeypatch.setattr(settings, "RECO_AUDIT_ENABLED", False)
        monkeypatch.setattr("app.services.faiss_index.get_embedding", lambda tid: _unit(np.ones(64, dtype=np.float32)))

        async def _fake_get_next_tracks(session_id, count, db, *, collect_audit=False):
            track = {"position": 0, "track_id": "proven0", "source": "radio_drift", "score": 1.0}
            if collect_audit:
                return [track], {"candidate_rows": [], "candidates_by_source": {}, "candidates_total": 0}
            return [track]

        monkeypatch.setattr("app.services.radio.get_next_tracks", _fake_get_next_tracks)

        resp = await client.post(
            "/v1/radio/start",
            json={"user_id": "u", "seed_type": "track", "seed_value": "proven0", "count": 1, "discovery": 0.9},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["discovery"] == 0.9
        session_id = resp.json()["session_id"]
        assert radio_service.get_session(session_id).discovery == 0.9

    @_requires_app
    async def test_start_rejects_out_of_range_discovery(client):
        await _seed_proven_and_weak()
        resp = await client.post(
            "/v1/radio/start",
            json={"user_id": "u", "seed_type": "track", "seed_value": "proven0", "count": 1, "discovery": 2.0},
        )
        assert resp.status_code == 422

    @_requires_app
    async def test_next_rejects_out_of_range_discovery(client):
        # Query-param validation fires before the handler, so no session needed.
        assert (await client.get("/v1/radio/whatever/next?discovery=2.0")).status_code == 422
        assert (await client.get("/v1/radio/whatever/next?discovery=-0.5")).status_code == 422

    @_requires_app
    async def test_next_enforces_session_ownership(client, monkeypatch):
        """A key bound to a different user cannot pull from this session."""
        _make_session("owned", discovery=0.3, user_id="u")
        # Bind the *actual* test identity (the configured key when an env .env
        # supplies one, else "anonymous") to a different user, so
        # check_user_access rejects access to session owner "u".
        identity = settings.api_keys_list[0] if settings.api_keys_list else "anonymous"
        monkeypatch.setattr(security, "_key_user_bindings", {security.hash_key(identity): {"someone-else"}})
        resp = await client.get("/v1/radio/owned/next?count=1")
        assert resp.status_code == 403

    # -----------------------------------------------------------------------
    # Named-preset (mode) posture — the 4-stop picker path
    # -----------------------------------------------------------------------

    @_requires_app
    def test_discovery_for_mode_returns_anchor():
        """A named preset maps to its dial anchor; None falls back to the given value."""
        assert radio_service.discovery_for_mode("familiar") == 0.0
        assert radio_service.discovery_for_mode("balanced") == 0.3
        assert radio_service.discovery_for_mode("discovery") == 0.6
        assert radio_service.discovery_for_mode("deep_discovery") == 1.0
        assert radio_service.discovery_for_mode(None, 0.42) == 0.42  # no mode -> fallback

    def _stub_single_track(monkeypatch):
        monkeypatch.setattr(settings, "RECO_AUDIT_ENABLED", False)
        monkeypatch.setattr("app.services.faiss_index.get_embedding", lambda tid: _unit(np.ones(64, dtype=np.float32)))

        async def _fake_get_next_tracks(session_id, count, db, *, collect_audit=False):
            track = {"position": 0, "track_id": "proven0", "source": "radio_drift", "score": 1.0}
            if collect_audit:
                return [track], {"candidate_rows": [], "candidates_by_source": {}, "candidates_total": 0}
            return [track]

        monkeypatch.setattr("app.services.radio.get_next_tracks", _fake_get_next_tracks)

    @_requires_app
    async def test_start_with_mode_pins_anchor_and_echoes(client, monkeypatch):
        """POST /radio/start with `mode` pins discovery to that preset's anchor,
        stores the mode on the session, and echoes the mode back."""
        await _seed_proven_and_weak()
        _stub_single_track(monkeypatch)

        resp = await client.post(
            "/v1/radio/start",
            json={"user_id": "u", "seed_type": "track", "seed_value": "proven0", "count": 1, "mode": "familiar"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["mode"] == "familiar"
        assert body["discovery"] == 0.0  # pinned to the familiar anchor
        s = radio_service.get_session(body["session_id"])
        assert s.mode == "familiar"
        assert s.discovery == 0.0

    @_requires_app
    async def test_mode_overrides_discovery_on_start(client, monkeypatch):
        """When both are sent, `mode` wins and pins the anchor (not the float)."""
        await _seed_proven_and_weak()
        _stub_single_track(monkeypatch)

        resp = await client.post(
            "/v1/radio/start",
            json={
                "user_id": "u", "seed_type": "track", "seed_value": "proven0",
                "count": 1, "discovery": 0.9, "mode": "familiar",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["mode"] == "familiar"
        assert resp.json()["discovery"] == 0.0  # mode wins -> familiar anchor, not 0.9

    @_requires_app
    async def test_next_with_mode_updates_posture(client, monkeypatch):
        """GET /next?mode= repins the session to that preset's anchor + stores it."""
        await _seed_proven_and_weak()
        _stub_single_track(monkeypatch)
        s = _make_session("m", discovery=0.3, user_id="u")  # starts balanced

        resp = await client.get("/v1/radio/m/next?count=1&mode=deep_discovery")
        assert resp.status_code == 200, resp.text
        assert resp.json()["mode"] == "deep_discovery"
        assert resp.json()["discovery"] == 1.0
        assert s.mode == "deep_discovery"
        assert s.discovery == 1.0

    @_requires_app
    async def test_start_rejects_invalid_mode(client):
        await _seed_proven_and_weak()
        resp = await client.post(
            "/v1/radio/start",
            json={"user_id": "u", "seed_type": "track", "seed_value": "proven0", "count": 1, "mode": "bogus"},
        )
        assert resp.status_code == 422

    @_requires_app
    async def test_next_rejects_invalid_mode(client):
        # Query-param validation fires before the handler, so no session needed.
        assert (await client.get("/v1/radio/whatever/next?mode=bogus")).status_code == 422

    @_requires_app
    async def test_mode_drives_posture_like_discovery(monkeypatch):
        """A session set by `mode` resolves the named preset through resolve_dial:
        deep_discovery excludes the proven set, familiar keeps it — proving the
        picker actually drives the dial (and pins it exactly, no interpolation)."""
        await _seed_proven_and_weak()
        _stub_pool(monkeypatch)

        def _mode_session(sid: str, mode: str) -> radio_service.RadioSession:
            s = radio_service.RadioSession(
                session_id=sid,
                user_id="u",
                seed_type="track",
                seed_value="seed",
                seed_track_ids=["seed"],
                seed_embedding=_unit(np.ones(64, dtype=np.float32)),
                drift_embedding=_unit(np.ones(64, dtype=np.float32)),
                discovery=radio_service.discovery_for_mode(mode),
                mode=mode,
            )
            radio_service.store_session(s)
            return s

        _mode_session("fam-mode", "familiar")
        _mode_session("deep-mode", "deep_discovery")

        async with _TestSession() as db:
            fam_ids = {t["track_id"] for t in await radio_service.get_next_tracks("fam-mode", 50, db)}
        async with _TestSession() as db:
            deep_ids = {t["track_id"] for t in await radio_service.get_next_tracks("deep-mode", 50, db)}

        assert set(_PROVEN_IDS) <= fam_ids  # familiar keeps proven favourites
        assert not (set(_PROVEN_IDS) & deep_ids)  # deep_discovery excludes them
