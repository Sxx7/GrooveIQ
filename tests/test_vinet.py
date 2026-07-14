"""
GrooveIQ – Tests for the Discogs-VINet version/remix embedding integration.

The heavy audio path (librosa CQT + onnxruntime CQTNet) is exercised only when
those libs *and* an exported model are present; those tests skip otherwise
(mirrors the handoff's "parity test marked slow/skippable in CI"). The rest —
disabled-path no-ops, the version FAISS index (incl. the media_server_id gate),
compute-only backfill idempotency, the migration, the title normalisers, and the
/versions endpoint — run without any audio deps.
"""

from __future__ import annotations

import base64
import importlib.util
import time
from collections.abc import AsyncGenerator

import numpy as np
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import Base, TrackFeatures

_HAS_LIBROSA = importlib.util.find_spec("librosa") is not None
_HAS_ORT = importlib.util.find_spec("onnxruntime") is not None

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


def _make_vec_b64(seed: int = 0, dim: int = 512) -> str:
    """Deterministic L2-normalised dim-vector, base64 float32 (as stored)."""
    rng = np.random.RandomState(seed)
    vec = rng.randn(dim).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return base64.b64encode(vec.tobytes()).decode()


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
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Point the index + backfill services and the request DB at the test engine.
    monkeypatch.setattr("app.services.faiss_index.AsyncSessionLocal", _TestSession)
    monkeypatch.setattr("app.services.vinet_backfill.AsyncSessionLocal", _TestSession)
    app.dependency_overrides[get_session] = override_get_session

    yield

    app.dependency_overrides.clear()
    import app.services.faiss_index as fi

    idx = fi.version_index
    idx._index = None
    idx._id_to_track = []
    idx._track_to_id = {}
    idx._embeddings = None

    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {},
    ) as c:
        yield c


async def _insert_track(
    track_id: str,
    *,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    media_server_id: str | None = "ms",
    version_embedding: str | None = None,
    file_path: str | None = None,
):
    async with _TestSession() as session:
        session.add(
            TrackFeatures(
                track_id=track_id,
                title=title,
                artist=artist,
                album=album,
                media_server_id=(media_server_id if media_server_id != "ms" else f"ms_{track_id}"),
                file_path=file_path or f"/music/{track_id}.flac",
                version_embedding=version_embedding,
                analyzed_at=int(time.time()),
                analysis_version="1",
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Pure helpers (no audio deps)
# ---------------------------------------------------------------------------


def test_mean_downsample_cqt_verbatim():
    """Averages non-overlapping windows and discards the trailing partial frame."""
    from app.services.analysis_worker import _mean_downsample_cqt

    cqt = np.arange(7 * 2, dtype=np.float32).reshape(7, 2)  # T=7, F=2
    out = _mean_downsample_cqt(cqt, 3)
    # 7 // 3 = 2 windows; last frame discarded.
    assert out.shape == (2, 2)
    assert np.allclose(out[0], cqt[0:3].mean(axis=0))
    assert np.allclose(out[1], cqt[3:6].mean(axis=0))
    # Input shorter than one window → (0, F).
    assert _mean_downsample_cqt(cqt[:2], 5).shape == (0, 2)


def test_title_normaliser_groups_versions():
    from app.api.routes.tracks import _base_title, _primary_artist

    base = _base_title("Song Title")
    assert _base_title("Song Title (Live)") == base
    assert _base_title("Song Title - Radio Edit") == base
    assert _base_title("Song Title (2011 Remaster)") == base
    assert _base_title("Song Title (feat. Someone)") == base
    # Different song does not collapse to the same base.
    assert _base_title("Another Song") != base

    assert _primary_artist("The Beatles") == _primary_artist("Beatles")
    assert _primary_artist("Artist feat. Guest") == _primary_artist("Artist")
    assert _primary_artist("A & B") == _primary_artist("A")


# ---------------------------------------------------------------------------
# Disabled-path no-ops
# ---------------------------------------------------------------------------


def test_init_vinet_session_disabled(monkeypatch):
    from app.services.analysis_worker import _init_vinet_session

    monkeypatch.setattr(settings, "VINET_ENABLED", False)
    assert _init_vinet_session() is None


def test_init_vinet_session_enabled_but_model_missing(monkeypatch, tmp_path):
    """Enabled but no model file (or no onnxruntime) → None, fails soft."""
    from app.services.analysis_worker import _init_vinet_session

    monkeypatch.setattr(settings, "VINET_ENABLED", True)
    monkeypatch.setattr(settings, "VINET_MODEL_DIR", str(tmp_path))  # empty dir
    assert _init_vinet_session() is None


async def test_backfill_disabled(monkeypatch):
    from app.services.vinet_backfill import backfill_vinet_embeddings

    monkeypatch.setattr(settings, "VINET_ENABLED", False)
    result = await backfill_vinet_embeddings()
    assert result == {"processed": 0, "updated": 0, "skipped": "vinet_disabled"}


async def test_build_index_skips_when_disabled(monkeypatch):
    """build_index() must not touch the version index when VINET_ENABLED=false."""
    import app.services.faiss_index as fi

    monkeypatch.setattr(settings, "VINET_ENABLED", False)
    await _insert_track("t1", version_embedding=_make_vec_b64(1))
    await fi.build_index()
    assert not fi.version_index.is_ready()


# ---------------------------------------------------------------------------
# Version FAISS index
# ---------------------------------------------------------------------------


async def test_version_index_build_and_search():
    import app.services.faiss_index as fi

    for i in range(6):
        await _insert_track(f"v{i}", version_embedding=_make_vec_b64(i))

    n = await fi.version_index.rebuild(column="version_embedding")
    assert n == 6
    assert fi.version_index.is_ready()

    hits = fi.version_index.search_by_track_id("v0", k=3)
    assert len(hits) == 3
    for tid, score in hits:
        assert isinstance(tid, str) and tid != "v0"
        assert isinstance(score, float)


async def test_version_index_media_server_id_gate():
    """Tracks with a version_embedding but no media_server_id are NOT indexed."""
    import app.services.faiss_index as fi

    for i in range(3):
        await _insert_track(f"v{i}", version_embedding=_make_vec_b64(i))
    await _insert_track("no_msid", media_server_id=None, version_embedding=_make_vec_b64(99))

    n = await fi.version_index.rebuild(column="version_embedding")
    assert n == 3
    assert fi.version_index.get_embedding("no_msid") is None


# ---------------------------------------------------------------------------
# Compute-only backfill
# ---------------------------------------------------------------------------


class _FakePool:
    def __init__(self):
        self.calls = 0

    async def compute_vinet_only(self, file_path: str) -> str:
        self.calls += 1
        return _make_vec_b64(abs(hash(file_path)) % 1000)


async def test_backfill_idempotency(monkeypatch, tmp_path):
    from app.services import vinet_backfill

    monkeypatch.setattr(settings, "VINET_ENABLED", True)

    fake = _FakePool()

    async def _fake_get_pool():
        return fake

    monkeypatch.setattr("app.services.analysis_worker.get_worker_pool", _fake_get_pool)

    # Real files so the os.path.exists guard passes.
    paths = []
    for i in range(3):
        p = tmp_path / f"track_{i}.flac"
        p.write_bytes(b"x")
        paths.append(str(p))
        await _insert_track(f"b{i}", file_path=str(p))

    first = await vinet_backfill.backfill_vinet_embeddings()
    assert first["processed"] == 3
    assert first["updated"] == 3
    assert fake.calls == 3

    # All rows now populated → second run finds nothing pending, computes nothing.
    second = await vinet_backfill.backfill_vinet_embeddings()
    assert second == {"processed": 0, "updated": 0, "skipped": "none_pending"}
    assert fake.calls == 3  # no extra compute


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


async def test_migration_adds_version_embedding_column():
    from app.db.session import _apply_column_migrations

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Bare table missing the new column.
        await conn.exec_driver_sql("CREATE TABLE track_features (track_id TEXT PRIMARY KEY)")
        await _apply_column_migrations(conn)
        res = await conn.exec_driver_sql("PRAGMA table_info(track_features)")
        cols = {row[1] for row in res.fetchall()}
    await engine.dispose()
    assert "version_embedding" in cols


# ---------------------------------------------------------------------------
# /versions endpoint
# ---------------------------------------------------------------------------


async def test_versions_endpoint_404(client):
    resp = await client.get("/v1/tracks/does_not_exist/versions")
    assert resp.status_code == 404


async def test_versions_endpoint_tier1_title(client):
    """Same-artist versions surface via the title tier (no VINet needed)."""
    await _insert_track("orig", title="Bohemian Rhapsody", artist="Queen")
    await _insert_track("live", title="Bohemian Rhapsody (Live at Wembley)", artist="Queen")
    await _insert_track("remaster", title="Bohemian Rhapsody - 2011 Remaster", artist="Queen")
    await _insert_track("other", title="We Will Rock You", artist="Queen")
    await _insert_track("cover_diff_artist", title="Bohemian Rhapsody", artist="Panic! at the Disco")

    resp = await client.get("/v1/tracks/orig/versions")
    assert resp.status_code == 200
    body = resp.json()
    ids = {v["track_id"] for v in body["versions"]}
    assert "live" in ids
    assert "remaster" in ids
    assert "other" not in ids  # different song
    # Cross-artist cover is NOT caught by title alone (needs Tier 2/3).
    assert "cover_diff_artist" not in ids
    for v in body["versions"]:
        assert "title" in v["evidence"]
        assert v["confidence"] == "high"
    assert body["tiers"]["title"] is True
    assert body["tiers"]["audio"] is False


async def test_versions_endpoint_tier1_punctuated_artist(client):
    """Punctuated artist names must still match same-artist versions.

    Regression: the SQL block must anchor on a token, not the punctuation-
    stripped whole primary ("Panic! at the Disco" -> "panic at the disco" is
    not a substring of the raw "Panic! at the Disco"), else recall collapses
    for every punctuated name.
    """
    await _insert_track("p_orig", title="Nine in the Afternoon", artist="Panic! at the Disco")
    await _insert_track("p_live", title="Nine in the Afternoon (Live)", artist="Panic! at the Disco")
    await _insert_track("b_orig", title="All the Small Things", artist="Blink-182")
    await _insert_track("b_remix", title="All the Small Things - Remix", artist="Blink-182")

    r1 = await client.get("/v1/tracks/p_orig/versions")
    assert r1.status_code == 200
    assert "p_live" in {v["track_id"] for v in r1.json()["versions"]}

    r2 = await client.get("/v1/tracks/b_orig/versions")
    assert r2.status_code == 200
    assert "b_remix" in {v["track_id"] for v in r2.json()["versions"]}


async def test_versions_endpoint_tier3_audio(client, monkeypatch):
    """VINet neighbours above threshold surface as medium-confidence audio evidence."""
    import app.services.faiss_index as fi

    monkeypatch.setattr(settings, "VINET_ENABLED", True)
    monkeypatch.setattr(settings, "VINET_MATCH_THRESHOLD", -1.0)  # accept all neighbours

    # Seed + a couple of cross-artist tracks with version embeddings, distinct titles.
    await _insert_track("seed", title="Song A", artist="Artist X", version_embedding=_make_vec_b64(1))
    await _insert_track("nbr1", title="Totally Different", artist="Artist Y", version_embedding=_make_vec_b64(2))
    await _insert_track("nbr2", title="Another Name", artist="Artist Z", version_embedding=_make_vec_b64(3))
    await fi.version_index.rebuild(column="version_embedding")

    resp = await client.get("/v1/tracks/seed/versions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tiers"]["audio"] is True
    ids = {v["track_id"] for v in body["versions"]}
    assert ids == {"nbr1", "nbr2"}
    for v in body["versions"]:
        assert v["evidence"] == ["audio"]
        assert v["confidence"] == "medium"
        assert v["similarity"] is not None


# ---------------------------------------------------------------------------
# Heavy audio path (skips unless librosa + onnxruntime present)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not (_HAS_LIBROSA and _HAS_ORT), reason="librosa/onnxruntime not installed")
def test_compute_vinet_embedding_shape_dtype():
    """With a stub CNN session, the CQT path yields a 512-dim L2-normed float32 vec."""
    from app.services import analysis_worker

    class _StubSession:
        def get_inputs(self):
            class _I:
                name = "cqt"

            return [_I()]

        def run(self, _outs, feed):
            x = feed["cqt"]
            assert x.ndim == 4 and x.shape[0] == 1 and x.shape[1] == 1 and x.shape[2] == 84
            assert x.dtype == np.float32
            return [np.random.RandomState(0).randn(1, 512).astype(np.float32)]

    analysis_worker._VINET_INPUT_NAME = None  # reset cached input name
    sr = settings.VINET_AUDIO_SR
    rng = np.random.RandomState(0)
    audio = rng.randn(sr * 12).astype(np.float32) * 0.1  # 12 s of noise
    vec = analysis_worker._compute_vinet_embedding(audio, sr, _StubSession())
    assert vec is not None
    assert vec.shape == (512,)
    assert vec.dtype == np.float32
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-5
