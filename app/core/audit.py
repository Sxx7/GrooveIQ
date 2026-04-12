"""
GrooveIQ – Audit logging for security-sensitive operations.

Emits structured log entries tagged with ``audit=True`` so they can be
filtered, shipped to a SIEM, or written to a separate file by the
logging configuration.

Every audit entry includes:
- ``action``: what happened (e.g. ``pipeline_reset``, ``user_rename``)
- ``api_key_hash``: SHA-256 prefix of the API key that triggered it
- ``detail``: action-specific context (user IDs, playlist IDs, etc.)
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.security import hash_key

logger = logging.getLogger("grooveiq.audit")


def audit_log(
    action: str,
    *,
    api_key: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Emit a structured audit log entry.

    Parameters
    ----------
    action:
        Short verb describing what happened.
    api_key:
        The raw API key (first 8 hex chars of SHA-256 stored for correlation,
        never the full key).
    detail:
        Arbitrary context dict (user IDs, resource IDs, counts, etc.).
    """
    key_prefix = hash_key(api_key)[:8] if api_key else "anonymous"
    logger.info(
        "AUDIT %s by key=%s",
        action,
        key_prefix,
        extra={
            "audit": True,
            "action": action,
            "api_key_hash": key_prefix,
            **(detail or {}),
        },
    )
