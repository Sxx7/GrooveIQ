"""GrooveIQ – Tests for the boot full-scan gate (issue #150).

scheduler._completed_scan_within decides whether a container restart kicks off
a fresh full-library walk. A recent completed scan suppresses it; a stale or
absent one lets it run (so fresh installs still index immediately).

Run with:  .venv-test/bin/pytest tests/test_startup_scan_gate.py -v
"""

from __future__ import annotations

import time

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.workers.scheduler as sched
from app.models.db import Base, LibraryScanState

_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def gate_session(monkeypatch):
    engine = create_async_engine(_TEST_DB_URL, connect_args={"check_same_thread": False})
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(sched, "AsyncSessionLocal", session_factory)
    yield session_factory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _add_scan(factory, *, status: str, ended_at: int | None) -> None:
    async with factory() as s:
        s.add(
            LibraryScanState(
                scan_started_at=(ended_at or int(time.time())) - 60,
                status=status,
                scan_ended_at=ended_at,
            )
        )
        await s.commit()


async def test_gate_false_when_no_scans(gate_session):
    assert await sched._completed_scan_within(6) is False


async def test_gate_true_for_recent_completed(gate_session):
    await _add_scan(gate_session, status="completed", ended_at=int(time.time()) - 3600)  # 1h ago
    assert await sched._completed_scan_within(6) is True


async def test_gate_false_for_stale_completed(gate_session):
    await _add_scan(gate_session, status="completed", ended_at=int(time.time()) - 7 * 3600)  # 7h ago
    assert await sched._completed_scan_within(6) is False


async def test_gate_ignores_failed_and_running(gate_session):
    await _add_scan(gate_session, status="failed", ended_at=int(time.time()) - 60)
    await _add_scan(gate_session, status="running", ended_at=None)
    assert await sched._completed_scan_within(6) is False
