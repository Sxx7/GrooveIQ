"""
Tests for the download backend cascade.

Covers:
  * Chain walks in priority order, first success wins.
  * Disabled entries are skipped.
  * Quality threshold filters out underqualified backends pre-flight.
  * Failures are recorded in the attempts log.
  * AttemptResult.to_dict round-trips cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from app.models.download_routing_schema import (
    BackendChainEntry,
    BackendName,
    DownloadRoutingConfigData,
    QualityTier,
    quality_meets,
)
from app.services.download_chain import (
    AttemptResult,
    CascadeResult,
    NormalizedSearchResult,
    TrackRef,
    try_download_chain,
)

# ---------------------------------------------------------------------------
# Fake adapter — drop-in replacement for any real backend
# ---------------------------------------------------------------------------


@dataclass
class _FakeAdapter:
    """Configurable test double matching the BackendAdapter protocol."""

    name: BackendName
    expected_quality: QualityTier
    configured: bool = True
    raise_on_download: bool = False
    succeed: bool = True
    task_id: str = "fake-task-1"
    closed: bool = False
    extra: dict = field(default_factory=dict)

    async def is_configured(self) -> bool:
        return self.configured

    async def search(self, query: str, limit: int = 10) -> list:
        return []

    async def try_download(self, track_ref: TrackRef) -> AttemptResult:
        if self.raise_on_download:
            raise RuntimeError("boom")
        if not self.succeed:
            return AttemptResult(
                backend=self.name.value,
                success=False,
                status="error",
                error="fake failure",
            )
        return AttemptResult(
            backend=self.name.value,
            success=True,
            status="downloading",
            task_id=self.task_id,
            quality=self.expected_quality,
            extra=self.extra,
        )

    async def from_handle(self, handle: dict[str, Any]) -> AttemptResult:
        return await self.try_download(TrackRef())

    async def close(self) -> None:
        self.closed = True


def _routing_with(individual: list[BackendChainEntry]) -> DownloadRoutingConfigData:
    """Build a routing config with a custom individual chain (other chains default)."""
    cfg = DownloadRoutingConfigData()
    cfg.individual = individual
    return cfg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cascade_first_success_wins():
    """First enabled backend that succeeds returns immediately; later backends untouched."""
    spotdl = _FakeAdapter(name=BackendName.SPOTDL, expected_quality=QualityTier.LOSSY_HIGH, succeed=True, task_id="t-spotdl")
    streamrip = _FakeAdapter(name=BackendName.STREAMRIP, expected_quality=QualityTier.HIRES, succeed=True, task_id="t-stream")

    def fake_make(b):
        return {BackendName.SPOTDL: spotdl, BackendName.STREAMRIP: streamrip}.get(b)

    routing = _routing_with(
        [
            BackendChainEntry(backend=BackendName.SPOTDL, enabled=True),
            BackendChainEntry(backend=BackendName.STREAMRIP, enabled=True),
        ]
    )

    with patch("app.services.download_chain.get_routing", return_value=routing), patch(
        "app.services.download_chain.make_adapter", side_effect=fake_make
    ):
        result = await try_download_chain(TrackRef(spotify_id="abc"), purpose="individual")

    assert result.success is True
    assert result.final_backend == "spotdl"
    assert result.final_task_id == "t-spotdl"
    assert len(result.attempts) == 1
    assert result.attempts[0].backend == "spotdl"
    # Streamrip should not have been touched.
    assert streamrip.closed is False
    assert spotdl.closed is True


@pytest.mark.asyncio
async def test_cascade_falls_through_on_failure():
    """When the first backend fails, the cascade tries the next."""
    spotdl = _FakeAdapter(name=BackendName.SPOTDL, expected_quality=QualityTier.LOSSY_HIGH, succeed=False)
    streamrip = _FakeAdapter(name=BackendName.STREAMRIP, expected_quality=QualityTier.HIRES, succeed=True, task_id="t-s")

    def fake_make(b):
        return {BackendName.SPOTDL: spotdl, BackendName.STREAMRIP: streamrip}.get(b)

    routing = _routing_with(
        [
            BackendChainEntry(backend=BackendName.SPOTDL, enabled=True),
            BackendChainEntry(backend=BackendName.STREAMRIP, enabled=True),
        ]
    )

    with patch("app.services.download_chain.get_routing", return_value=routing), patch(
        "app.services.download_chain.make_adapter", side_effect=fake_make
    ):
        result = await try_download_chain(TrackRef(spotify_id="abc"), purpose="individual")

    assert result.success is True
    assert result.final_backend == "streamrip"
    assert len(result.attempts) == 2
    assert [a.backend for a in result.attempts] == ["spotdl", "streamrip"]
    assert result.attempts[0].success is False
    assert result.attempts[1].success is True


@pytest.mark.asyncio
async def test_cascade_skips_disabled_entries():
    """Disabled chain entries are silently skipped and never construct an adapter."""
    streamrip = _FakeAdapter(name=BackendName.STREAMRIP, expected_quality=QualityTier.HIRES, succeed=True, task_id="t")

    constructed: list[BackendName] = []

    def fake_make(b):
        constructed.append(b)
        return streamrip if b == BackendName.STREAMRIP else None

    routing = _routing_with(
        [
            BackendChainEntry(backend=BackendName.SPOTDL, enabled=False),  # disabled
            BackendChainEntry(backend=BackendName.STREAMRIP, enabled=True),
        ]
    )

    with patch("app.services.download_chain.get_routing", return_value=routing), patch(
        "app.services.download_chain.make_adapter", side_effect=fake_make
    ):
        result = await try_download_chain(TrackRef(spotify_id="abc"), purpose="individual")

    assert result.success is True
    assert constructed == [BackendName.STREAMRIP]  # spotdl was never even constructed
    assert len(result.attempts) == 1


@pytest.mark.asyncio
async def test_cascade_quality_gate_pre_flight():
    """A min_quality higher than the backend's expected quality short-circuits it."""
    # spotdl declares LOSSY_HIGH; entry requires LOSSLESS → should be skipped.
    spotdl = _FakeAdapter(name=BackendName.SPOTDL, expected_quality=QualityTier.LOSSY_HIGH, succeed=True)
    streamrip = _FakeAdapter(name=BackendName.STREAMRIP, expected_quality=QualityTier.HIRES, succeed=True, task_id="t-s")

    def fake_make(b):
        return {BackendName.SPOTDL: spotdl, BackendName.STREAMRIP: streamrip}.get(b)

    routing = _routing_with(
        [
            BackendChainEntry(backend=BackendName.SPOTDL, enabled=True, min_quality=QualityTier.LOSSLESS),
            BackendChainEntry(backend=BackendName.STREAMRIP, enabled=True),
        ]
    )

    with patch("app.services.download_chain.get_routing", return_value=routing), patch(
        "app.services.download_chain.make_adapter", side_effect=fake_make
    ):
        result = await try_download_chain(TrackRef(spotify_id="abc"), purpose="individual")

    assert result.success is True
    assert result.final_backend == "streamrip"
    # Spotdl is recorded as a "skipped" attempt with the quality reason.
    spotdl_attempt = result.attempts[0]
    assert spotdl_attempt.backend == "spotdl"
    assert spotdl_attempt.success is False
    assert spotdl_attempt.status == "skipped"
    assert "quality" in (spotdl_attempt.error or "")


@pytest.mark.asyncio
async def test_cascade_all_fail_returns_full_attempt_log():
    """Every backend failing yields a CascadeResult with success=False and full attempt list."""
    a = _FakeAdapter(name=BackendName.SPOTDL, expected_quality=QualityTier.LOSSY_HIGH, succeed=False)
    b = _FakeAdapter(name=BackendName.STREAMRIP, expected_quality=QualityTier.HIRES, succeed=False)

    def fake_make(bn):
        return {BackendName.SPOTDL: a, BackendName.STREAMRIP: b}.get(bn)

    routing = _routing_with(
        [
            BackendChainEntry(backend=BackendName.SPOTDL, enabled=True),
            BackendChainEntry(backend=BackendName.STREAMRIP, enabled=True),
        ]
    )

    with patch("app.services.download_chain.get_routing", return_value=routing), patch(
        "app.services.download_chain.make_adapter", side_effect=fake_make
    ):
        result = await try_download_chain(TrackRef(spotify_id="abc"), purpose="individual")

    assert result.success is False
    assert len(result.attempts) == 2
    assert all(att.success is False for att in result.attempts)
    assert a.closed and b.closed


@pytest.mark.asyncio
async def test_cascade_skips_unconfigured_backend():
    """is_configured() == False marks the attempt as skipped without trying download."""
    spotdl = _FakeAdapter(name=BackendName.SPOTDL, expected_quality=QualityTier.LOSSY_HIGH, configured=False)
    streamrip = _FakeAdapter(name=BackendName.STREAMRIP, expected_quality=QualityTier.HIRES, succeed=True, task_id="t")

    def fake_make(b):
        return {BackendName.SPOTDL: spotdl, BackendName.STREAMRIP: streamrip}.get(b)

    routing = _routing_with(
        [
            BackendChainEntry(backend=BackendName.SPOTDL, enabled=True),
            BackendChainEntry(backend=BackendName.STREAMRIP, enabled=True),
        ]
    )

    with patch("app.services.download_chain.get_routing", return_value=routing), patch(
        "app.services.download_chain.make_adapter", side_effect=fake_make
    ):
        result = await try_download_chain(TrackRef(spotify_id="abc"), purpose="individual")

    assert result.success is True
    assert result.final_backend == "streamrip"
    assert result.attempts[0].status == "skipped"
    assert "not configured" in (result.attempts[0].error or "")


# ---------------------------------------------------------------------------
# Quality tier helper
# ---------------------------------------------------------------------------


def test_quality_meets_ordering():
    assert quality_meets(QualityTier.HIRES, QualityTier.LOSSY_LOW) is True
    assert quality_meets(QualityTier.HIRES, QualityTier.LOSSLESS) is True
    assert quality_meets(QualityTier.LOSSY_LOW, QualityTier.LOSSY_HIGH) is False
    assert quality_meets(QualityTier.LOSSLESS, QualityTier.HIRES) is False
    # None thresholds always pass.
    assert quality_meets(QualityTier.LOSSY_LOW, None) is True
    # Unknown actual quality treated as LOSSY_LOW.
    assert quality_meets(None, QualityTier.LOSSY_HIGH) is False


def test_attempt_result_to_dict():
    a = AttemptResult(
        backend="spotdl",
        success=True,
        status="downloading",
        task_id="abc",
        quality=QualityTier.LOSSY_HIGH,
        duration_ms=42,
    )
    d = a.to_dict()
    assert d["backend"] == "spotdl"
    assert d["quality"] == "lossy_high"
    assert d["task_id"] == "abc"
    assert d["duration_ms"] == 42


def test_cascade_result_default():
    r = CascadeResult(success=False)
    assert r.attempts == []
    assert r.final_backend is None
    assert r.final_status == "error"


def test_normalized_search_result_shape():
    n = NormalizedSearchResult(
        backend="streamrip",
        download_handle={"backend": "streamrip", "service_id": "x"},
        title="Creep",
        artist="Radiohead",
    )
    assert n.backend == "streamrip"
    assert n.download_handle["service_id"] == "x"
