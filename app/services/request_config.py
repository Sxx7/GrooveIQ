"""
GrooveIQ – Request-scoped algorithm-config overrides.

The active :class:`AlgorithmConfigData` is a process-wide singleton
(``app/services/algorithm_config.py:get_config``).  The recommendation-modes
"discovery dial" needs to vary a handful of those knobs **per request**
without mutating that singleton or racing across concurrent requests.

This module provides the async-safe primitive for that: a
:class:`contextvars.ContextVar` holding an optional override dict for the
*current* request, plus helpers to set / reset / inspect it.  ``get_config``
merges any active override on top of the cached config (see
``algorithm_config.get_config``).

Why ``contextvars`` and not a thread-local or a plain global:

* It propagates across ``await`` boundaries within a single request's task,
  so deep call sites (``reranker``, ``candidate_gen``) read the override via
  ``get_config()`` without any extra plumbing.
* ``asyncio.create_task`` snapshots the current context, so a fire-and-forget
  task spawned inside an override block (e.g. the audit writer) inherits the
  override even after the parent request has reset it.
* Concurrent requests run in distinct tasks with distinct context copies, so
  their overrides never bleed into one another.

Security note: the override dict is trusted, server-constructed data.  The
untrusted-input boundary — mapping a request's ``discovery``/``mode`` to a
*whitelisted* override dict — is introduced later in the build sequence
(Chunk 5).  Nothing in this module accepts raw client input.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar, Token

# The current request's override dict, or ``None`` when no override is active.
# Shape mirrors a partial ``AlgorithmConfigData`` dump, e.g.
#   {"reranker": {"freshness_boost": 0.0, "exploration_fraction": 0.0}}
_overrides: ContextVar[dict | None] = ContextVar("algorithm_config_overrides", default=None)


def current_overrides() -> dict | None:
    """Return the override dict active for the current context, or ``None``."""
    return _overrides.get()


def set_overrides(overrides: dict | None) -> Token:
    """Set the current override dict, returning a reset token.

    Prefer :func:`apply_overrides` (a context manager) which pairs the set with
    a guaranteed reset.  Use this lower-level pair only when the set and reset
    cannot be lexically scoped in a single ``with`` block.
    """
    return _overrides.set(overrides)


def reset_overrides(token: Token) -> None:
    """Restore the override dict to its value before ``token`` was issued."""
    _overrides.reset(token)


@contextlib.contextmanager
def apply_overrides(overrides: dict | None) -> Iterator[None]:
    """Apply ``overrides`` for the duration of the ``with`` block.

    Async-safe: the override is visible to everything awaited inside the block
    (it runs in the same task/context) and is reset on exit even if the block
    raises.  A child task spawned inside the block inherits the override at
    ``create_task`` time and keeps it independently of this reset.
    """
    token = _overrides.set(overrides)
    try:
        yield
    finally:
        _overrides.reset(token)
