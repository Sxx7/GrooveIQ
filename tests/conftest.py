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
}

for _k, _v in _TEST_ENV.items():
    os.environ[_k] = _v
