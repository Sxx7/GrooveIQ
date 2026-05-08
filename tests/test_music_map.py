"""
Tests for app.services.music_map.build_map.

Mostly a regression test for the executemany persist path: a previous version
ran 60k single-row UPDATEs in one transaction, which held the SQLite write
lock for minutes and starved every other writer. This test asserts that
build_map writes via a single executemany and updates only the tracks that
had embeddings.
"""

from __future__ import annotations

import base64

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, TrackFeatures

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


def _encode_embedding(vec: np.ndarray) -> str:
    return base64.b64encode(vec.astype(np.float32).tobytes()).decode("ascii")


@pytest_asyncio.fixture
async def db_session(monkeypatch):
    engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr("app.services.music_map.AsyncSessionLocal", Session)
    yield Session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_build_map_persists_via_executemany(db_session, monkeypatch):
    from app.services import music_map

    rng = np.random.default_rng(0)
    n_with_embedding = 80
    rows_with_embedding = []
    async with db_session() as s:
        for i in range(n_with_embedding):
            vec = rng.standard_normal(64).astype(np.float32)
            row = TrackFeatures(
                track_id=f"with-emb-{i}",
                file_path=f"/music/with-emb-{i}.flac",
                embedding=_encode_embedding(vec),
            )
            s.add(row)
            rows_with_embedding.append(row.track_id)
        s.add(TrackFeatures(track_id="no-emb-A", file_path="/music/no-emb-A.flac"))
        s.add(TrackFeatures(track_id="no-emb-B", file_path="/music/no-emb-B.flac"))
        await s.commit()

    def _fake_project(track_ids, matrix):
        n = len(track_ids)
        coords = np.linspace([0.0, 0.0], [1.0, 1.0], n).astype(np.float32)
        return list(track_ids), coords

    monkeypatch.setattr(music_map, "_project_sync", _fake_project)

    result = await music_map.build_map()

    assert result["tracks_mapped"] == n_with_embedding

    async with db_session() as s:
        from sqlalchemy import select

        mapped = (await s.execute(select(TrackFeatures.track_id, TrackFeatures.map_x, TrackFeatures.map_y))).all()
    by_id = {tid: (mx, my) for tid, mx, my in mapped}

    for tid in rows_with_embedding:
        mx, my = by_id[tid]
        assert mx is not None
        assert my is not None
        assert 0.0 <= mx <= 1.0
        assert 0.0 <= my <= 1.0

    for tid in ("no-emb-A", "no-emb-B"):
        assert by_id[tid] == (None, None)


@pytest.mark.asyncio
async def test_build_map_skips_when_too_few_tracks(db_session):
    from app.services import music_map

    rng = np.random.default_rng(1)
    async with db_session() as s:
        for i in range(music_map.MIN_TRACKS - 1):
            vec = rng.standard_normal(64).astype(np.float32)
            s.add(
                TrackFeatures(
                    track_id=f"t{i}",
                    file_path=f"/music/t{i}.flac",
                    embedding=_encode_embedding(vec),
                )
            )
        await s.commit()

    result = await music_map.build_map()
    assert result == {"tracks_mapped": 0, "skipped": "insufficient_tracks"}
