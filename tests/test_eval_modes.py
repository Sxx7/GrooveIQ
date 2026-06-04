"""
GrooveIQ – Chunk 10: per-dial-bucket evaluation metrics.

Two layers (same split as ``test_modes_endpoint.py``):

* **Pure-function unit tests** for the list-quality metrics (novelty, % never
  played, intra-list diversity, catalog coverage, skip-rate-on-set) — fully
  deterministic, fixture-driven, no DB.
* **App-guarded integration tests** of the ``evaluate_dial_modes`` orchestrator
  and the ``get_model_report`` surfacing, on a seeded in-memory DB with an
  injected generator (so the metrics are exercised without standing up FAISS /
  the ranker) and a fake embedding index.

The metrics live in ``app.services.evaluation``, whose module imports pull the
ranker / feature stack, so the whole file self-skips on the legacy 3.9 dev env.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

try:
    import pytest_asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.algorithm_config_schema import PRESET_NAMES
    from app.models.db import Base, ListenEvent, TrackFeatures, TrackInteraction, User
    from app.services import evaluation

    _APP_OK = True
except Exception:  # pragma: no cover - the 3.9 dev env can't import the full stack
    _APP_OK = False

_requires_app = pytest.mark.skipif(not _APP_OK, reason="full app import requires Python 3.11+")


# ===========================================================================
# Pure metric functions — deterministic, fixture-driven
# ===========================================================================


@_requires_app
def test_mean_inverse_popularity():
    # 1/(1+pop): t0→1.0, t1→0.5, t3→0.25; an unknown track counts as 0 plays → 1.0.
    pop = {"t0": 0, "t1": 1, "t3": 3}
    assert evaluation.mean_inverse_popularity(["t0", "t1", "t3"], pop) == round((1.0 + 0.5 + 0.25) / 3, 4)
    assert evaluation.mean_inverse_popularity(["t9"], pop) == 1.0  # unknown → never-played
    assert evaluation.mean_inverse_popularity([], pop) is None


@_requires_app
def test_pct_never_played():
    pop = {"a": 5, "b": 0, "c": 2}
    # b has 0 plays and d is unknown (→0) → 2 of 4.
    assert evaluation.pct_never_played(["a", "b", "c", "d"], pop) == 0.5
    assert evaluation.pct_never_played([], pop) is None


@_requires_app
def test_intra_list_diversity_orthogonal_vs_identical():
    e0 = np.array([1.0, 0.0, 0.0], dtype=np.float64)
    e1 = np.array([0.0, 1.0, 0.0], dtype=np.float64)
    # Orthogonal → cosine distance 1.0.
    assert evaluation.intra_list_diversity(["a", "b"], {"a": e0, "b": e1}) == 1.0
    # Identical direction (any scale) → distance 0.0.
    assert evaluation.intra_list_diversity(["a", "b"], {"a": e0, "b": e0 * 3.0}) == 0.0
    # Fewer than two usable embeddings → None.
    assert evaluation.intra_list_diversity(["a", "b"], {"a": e0, "b": None}) is None
    assert evaluation.intra_list_diversity(["a"], {"a": e0}) is None


@_requires_app
def test_catalog_coverage():
    assert evaluation.catalog_coverage({"a", "b", "c"}, 10) == 0.3
    assert evaluation.catalog_coverage(set(), 10) == 0.0
    assert evaluation.catalog_coverage({"a"}, 0) is None  # unknown catalog
    assert evaluation.catalog_coverage({"a", "b", "c"}, 2) == 1.0  # never exceeds 1.0


@_requires_app
def test_skip_rate_on_set():
    skips = {"p0": 2, "p1": 1, "other": 9}
    plays = {"p0": 6, "p1": 3, "other": 9}
    # (2+1) / ((2+1) + (6+3)) = 3/12 = 0.25 — "other" is outside the target set.
    assert evaluation.skip_rate_on_set(skips, plays, {"p0", "p1"}) == 0.25
    assert evaluation.skip_rate_on_set(skips, plays, {"missing"}) is None  # no activity


# ===========================================================================
# Orchestrator + surfacing — full app (Python 3.11+); self-skip on legacy 3.9.
# ===========================================================================

if _APP_OK:
    _engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False})
    _Session = async_sessionmaker(_engine, expire_on_commit=False)

    _NOW = int(time.time())
    _TEN_DAYS_AGO = _NOW - 10 * 86_400
    _PROVEN_IDS = [f"p{i}" for i in range(6)]
    _UNHEARD_IDS = [f"u{i}" for i in range(10)]

    class _FakeIndex:
        """Minimal ``EmbeddingIndex`` backed by an in-memory mapping."""

        def __init__(self, mapping: dict[str, np.ndarray]):
            self._m = mapping

        def get_embedding(self, track_id: str):
            emb = self._m.get(track_id)
            return None if emb is None else emb.copy()

    def _fake_index() -> _FakeIndex:
        rng = np.random.RandomState(0)
        mapping: dict[str, np.ndarray] = {}
        for tid in _PROVEN_IDS + _UNHEARD_IDS:
            v = rng.randn(64).astype(np.float32)
            v /= np.linalg.norm(v)
            mapping[tid] = v
        return _FakeIndex(mapping)

    def _fake_generate():
        """Familiar/balanced → proven tracks; discovery/deep → unheard tracks."""

        async def gen(user_id: str, dial, limit: int, session) -> list[str]:
            if dial.preset in ("familiar", "balanced"):
                return _PROVEN_IDS[:limit]
            return _UNHEARD_IDS[:limit]

        return gen

    @pytest_asyncio.fixture(autouse=True)
    async def _setup(monkeypatch):
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # The orchestrator opens its own session via the module-level factory.
        monkeypatch.setattr(evaluation, "AsyncSessionLocal", _Session)
        yield
        evaluation._last_dial_eval = {}
        evaluation._last_eval = {}
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def _seed():
        async with _Session() as session:
            for tid in _PROVEN_IDS + _UNHEARD_IDS:
                session.add(
                    TrackFeatures(
                        track_id=tid,
                        file_path=f"/music/{tid}.mp3",
                        duration=210.0,
                        analyzed_at=_NOW,
                        analysis_version="1",
                    )
                )
            session.add(User(user_id="u", last_seen=_NOW))
            # Proven favourites: played a lot, fully completed, high satisfaction.
            for tid in _PROVEN_IDS:
                session.add(
                    TrackInteraction(
                        user_id="u",
                        track_id=tid,
                        play_count=30,
                        full_listen_count=30,
                        early_skip_count=0,
                        avg_completion=0.95,
                        satisfaction_score=0.95,
                        last_played_at=_TEN_DAYS_AGO,
                        updated_at=_NOW,
                    )
                )
            # Synthetic events on 3 proven tracks: 3 skips / 9 plays → skip rate 0.25.
            for tid in _PROVEN_IDS[:3]:
                session.add(ListenEvent(user_id="u", track_id=tid, event_type="skip", timestamp=_NOW))
                for _ in range(3):
                    session.add(ListenEvent(user_id="u", track_id=tid, event_type="play_start", timestamp=_NOW))
            await session.commit()

    @_requires_app
    async def test_evaluate_dial_modes_buckets():
        await _seed()
        result = await evaluation.evaluate_dial_modes(
            generate=_fake_generate(),
            faiss_index=_fake_index(),
            user_ids=["u"],
            limit=25,
            now=_NOW,
        )

        buckets = result["buckets"]
        assert set(buckets) == set(PRESET_NAMES)
        fam, deep = buckets["familiar"], buckets["deep_discovery"]

        # Novelty + % never-played rise from the familiar end to the discovery end.
        assert deep["novelty"] > fam["novelty"]
        assert fam["pct_never_played"] == 0.0
        assert deep["pct_never_played"] == 1.0

        # Coverage is a valid fraction for every bucket.
        for b in buckets.values():
            assert 0.0 < b["catalog_coverage"] <= 1.0
        assert result["catalog_size"] == len(_PROVEN_IDS) + len(_UNHEARD_IDS)

        # Intra-list diversity is computed (the fake index supplies embeddings).
        assert fam["intra_list_diversity"] is not None
        assert deep["intra_list_diversity"] is not None

        # The proven-set skip-rate diagnostic rides on the familiar bucket only.
        assert fam["proven_skip_rate"] == 0.25
        assert fam["proven_set_size"] == 6
        assert "proven_skip_rate" not in deep

    @_requires_app
    async def test_insufficient_data_returns_marker():
        # Tables exist (fixture) but nothing is seeded — no users, empty catalog.
        result = await evaluation.evaluate_dial_modes(generate=_fake_generate(), faiss_index=_fake_index(), now=_NOW)
        assert result["error"] == "insufficient_data"
        assert result["buckets"] == {}

    @_requires_app
    async def test_get_model_report_surfaces_dial_modes():
        await _seed()
        # Populate the cache via the injected orchestrator (cheap + deterministic),
        # then confirm get_model_report serves it under `dial_modes` within the TTL.
        await evaluation.evaluate_dial_modes(
            generate=_fake_generate(),
            faiss_index=_fake_index(),
            user_ids=["u"],
            limit=25,
            now=int(time.time()),
        )
        report = await evaluation.get_model_report()
        assert "dial_modes" in report
        dm = report["dial_modes"]
        assert dm is not None and "buckets" in dm
        assert dm["buckets"]["familiar"]["proven_skip_rate"] == 0.25
