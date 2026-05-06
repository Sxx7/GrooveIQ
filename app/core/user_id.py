"""
GrooveIQ – user_id format validation (issue #86).

Single source of truth for "is this string a plausible user_id?". Default
pattern matches Navidrome's identifier output across versions:

  - xid    (Navidrome <0.50): 20 lowercase alphanumeric chars
  - nanoid (Navidrome 0.50+): 21–22 mixed-case alphanumeric chars

The pattern is configurable via ``USER_ID_PATTERN`` so non-Navidrome
deployments (or future format changes) can override without a code change.
"""

from __future__ import annotations

import re

from fastapi import HTTPException

from app.core.config import settings

_compiled_pattern: tuple[str, re.Pattern[str]] | None = None


def _get_pattern() -> re.Pattern[str]:
    """Return the compiled USER_ID_PATTERN regex, recompiling if config changed.

    Stored as a (source, compiled) tuple so `monkeypatch.setattr(settings,
    "USER_ID_PATTERN", ...)` in tests is picked up without restart.
    """
    global _compiled_pattern
    src = settings.USER_ID_PATTERN
    if _compiled_pattern is None or _compiled_pattern[0] != src:
        _compiled_pattern = (src, re.compile(src))
    return _compiled_pattern[1]


def is_valid_user_id(user_id: str | None) -> bool:
    """Pure check, no exception. Use this in tests / conditionals."""
    if not user_id:
        return False
    return bool(_get_pattern().fullmatch(user_id))


def validate_user_id(user_id: str | None) -> str:
    """Raise HTTPException(400) if `user_id` doesn't match the configured pattern.

    Returns the input unchanged on success so callers can chain it as
    ``user_id = validate_user_id(body.user_id)``.
    """
    if not is_valid_user_id(user_id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid user_id {user_id!r}: must match {settings.USER_ID_PATTERN}. "
                "GrooveIQ requires the Navidrome user identifier."
            ),
        )
    return user_id  # type: ignore[return-value]


__all__ = ["is_valid_user_id", "validate_user_id"]
