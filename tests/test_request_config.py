"""
GrooveIQ – Tests for the request-scoped config override mechanism (Chunk 1).

Verifies that per-request overrides applied via ``apply_overrides`` are
visible through ``get_config()``, are reset on block exit (even on
exception), are isolated across concurrent asyncio tasks, are inherited by
fire-and-forget child tasks, and never mutate the cached config singleton.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models.algorithm_config_schema import get_defaults
from app.services.algorithm_config import get_config
from app.services.request_config import (
    apply_overrides,
    current_overrides,
    reset_overrides,
    set_overrides,
)

# Stable anchors for assertions — read straight from the schema defaults so the
# tests don't hardcode magic numbers that could drift.
_DEFAULT_FRESHNESS = get_defaults().reranker.freshness_boost
_DEFAULT_EXPLORATION = get_defaults().reranker.exploration_fraction


def test_no_override_returns_cached_singleton():
    """With no override active, get_config() returns the same cached object."""
    assert current_overrides() is None
    a = get_config()
    b = get_config()
    assert a is b


def test_override_applies_and_resets():
    assert get_config().reranker.freshness_boost == _DEFAULT_FRESHNESS

    with apply_overrides({"reranker": {"freshness_boost": 0.99}}):
        assert get_config().reranker.freshness_boost == 0.99
        # Untouched fields inside the same group keep their defaults.
        assert get_config().reranker.exploration_fraction == _DEFAULT_EXPLORATION

    # Back to default once the block exits.
    assert get_config().reranker.freshness_boost == _DEFAULT_FRESHNESS
    assert current_overrides() is None


def test_override_reset_on_exception():
    """The override is cleared even when the block raises."""

    class BoomError(Exception):
        pass

    with pytest.raises(BoomError):  # noqa: SIM117 — nested context is the thing under test
        with apply_overrides({"reranker": {"freshness_boost": 0.42}}):
            assert get_config().reranker.freshness_boost == 0.42
            raise BoomError

    assert current_overrides() is None
    assert get_config().reranker.freshness_boost == _DEFAULT_FRESHNESS


def test_set_reset_token_pair():
    """The lower-level set/reset token pair round-trips correctly."""
    token = set_overrides({"reranker": {"freshness_boost": 0.33}})
    try:
        assert get_config().reranker.freshness_boost == 0.33
    finally:
        reset_overrides(token)

    assert current_overrides() is None
    assert get_config().reranker.freshness_boost == _DEFAULT_FRESHNESS


def test_merge_does_not_mutate_singleton():
    """The override path returns a fresh copy; the cached singleton is unchanged."""
    before = get_config()

    with apply_overrides({"reranker": {"freshness_boost": 0.99}}):
        merged = get_config()
        assert merged is not before
        assert merged.reranker.freshness_boost == 0.99

    after = get_config()
    # Same cached object handed back on the fast path, with original value intact.
    assert after is before
    assert before.reranker.freshness_boost == _DEFAULT_FRESHNESS


def test_deep_merge_preserves_sibling_groups_and_fields():
    """Overriding one field leaves every other group/field at its default."""
    with apply_overrides({"reranker": {"freshness_boost": 0.0}}):
        cfg = get_config()
        assert cfg.reranker.freshness_boost == 0.0
        # Sibling field in the same group untouched.
        assert cfg.reranker.exploration_fraction == _DEFAULT_EXPLORATION
        # Entirely different groups fully intact.
        assert cfg.track_scoring.w_like == get_defaults().track_scoring.w_like
        assert cfg.candidate_sources.content == get_defaults().candidate_sources.content


async def test_async_isolation_no_bleed():
    """Two concurrent tasks with different overrides never see each other's."""
    results: dict[str, float] = {}

    async def worker(name: str, value: float) -> None:
        with apply_overrides({"reranker": {"freshness_boost": value}}):
            await asyncio.sleep(0.01)  # interleave with the sibling task
            mid = get_config().reranker.freshness_boost
            await asyncio.sleep(0.01)
            end = get_config().reranker.freshness_boost
            assert mid == value
            assert end == value
            results[name] = end

    await asyncio.gather(worker("a", 0.11), worker("b", 0.22))

    assert results == {"a": 0.11, "b": 0.22}
    assert current_overrides() is None


async def test_create_task_inherits_override_and_survives_parent_reset():
    """A task spawned inside an override block inherits the override at
    create_task time and keeps it even after the parent block exits — the
    fire-and-forget audit-task pattern.
    """
    captured: dict[str, float] = {}
    release = asyncio.Event()

    async def child() -> None:
        # Wait until the parent has exited its block before reading, so we prove
        # the child holds its own context snapshot rather than racing the reset.
        await release.wait()
        captured["value"] = get_config().reranker.freshness_boost

    with apply_overrides({"reranker": {"freshness_boost": 0.77}}):
        task = asyncio.create_task(child())

    # Parent context has been reset...
    assert current_overrides() is None
    assert get_config().reranker.freshness_boost == _DEFAULT_FRESHNESS

    # ...but the child still sees the snapshot taken at create_task time.
    release.set()
    await task
    assert captured["value"] == 0.77
