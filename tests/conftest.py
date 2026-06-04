"""
Shared pytest configuration for the GrooveIQ test suite.

Sets environment variables BEFORE any ``app.*`` module is imported so that
``app.core.config.Settings`` — instantiated at import time — picks up a
deterministic test configuration instead of the developer's local ``.env``.

Two things matter here:

1. ``ALLOWED_HOSTS`` must accept the httpx test client's ``Host: test`` header,
   otherwise ``TrustedHostMiddleware`` rejects every request with 400.
2. All integration-style env vars (media server, Last.fm, Lidarr, Spotizerr)
   must be cleared so tests exercise the "not configured" code paths and do
   not hit real services.
"""

from __future__ import annotations

import os

import pytest

# Must run before any `from app...` import, so set env at module top-level.
_TEST_ENV = {
    "ALLOWED_HOSTS": "*",
    "APP_ENV": "development",
    # Clear integrations so tests see a pristine "not configured" state
    # regardless of what's in the developer's .env file.
    "MEDIA_SERVER_TYPE": "",
    "MEDIA_SERVER_URL": "",
    "MEDIA_SERVER_USER": "",
    "MEDIA_SERVER_PASSWORD": "",
    "MEDIA_SERVER_TOKEN": "",
    "LIDARR_URL": "",
    "LIDARR_API_KEY": "",
    "SPOTIZERR_URL": "",
    "SPOTIZERR_USERNAME": "",
    "SPOTIZERR_PASSWORD": "",
    "LASTFM_API_KEY": "",
    "LASTFM_API_SECRET": "",
    # Issue #86: relax user_id format enforcement for the existing test
    # suite which seeds users with names like "alice" / "testuser".  The
    # dedicated test_user_id.py module monkeypatches USER_ID_PATTERN to
    # the production default to verify enforcement under real config.
    "USER_ID_PATTERN": r"^[A-Za-z0-9_]+$",
    # No API keys / auth in unit tests so importing settings doesn't
    # hit the SystemExit guard in app/core/config.py.
    "DISABLE_AUTH": "true",
}

for _k, _v in _TEST_ENV.items():
    os.environ[_k] = _v


@pytest.fixture(autouse=True)
def _isolate_mix_cache():
    """Clear the in-memory SWR mix cache around every test.

    The recommend handler caches "plain" mode/dial requests in a module-level
    singleton (``app.services.mix_cache``). Without per-test isolation, a cached
    mix from one test could be served to another that rebuilt the same user with
    different DB state. Clearing before *and* after guarantees each test starts
    cold (its first request always regenerates).

    Lazy-imported and guarded so the legacy Python 3.9 dev env — which can't
    import the full app — silently skips it instead of erroring at collection.
    """
    try:
        from app.services import mix_cache
    except Exception:
        yield
        return
    mix_cache.clear()
    yield
    mix_cache.clear()
