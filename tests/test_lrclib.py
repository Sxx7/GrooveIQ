"""Tests for the LRCLIB client (tier 2 of the lyrics cascade)."""

from __future__ import annotations

import httpx
import pytest

from app.services.lrclib import LrclibClient


def _client(handler):
    return LrclibClient(
        base_url="http://lrclib.test",
        user_agent="GrooveIQ-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_get_returns_synced_and_plain():
    def handler(req):
        assert req.url.path == "/api/get"
        # duration is sent as a rounded integer second value
        assert req.url.params.get("duration") == "180"
        return httpx.Response(
            200,
            json={"instrumental": False, "plainLyrics": "hello\nworld", "syncedLyrics": "[00:01.00]hello"},
        )

    out = await _client(handler).lookup("Artist", "Title", "Album", 180.4)
    assert out.ok and out.result.found
    assert out.result.synced == "[00:01.00]hello"
    assert out.result.plain == "hello\nworld"
    assert not out.result.instrumental


@pytest.mark.asyncio
async def test_404_falls_back_to_search_with_duration_disambiguation():
    def handler(req):
        if req.url.path == "/api/get":
            return httpx.Response(404, json={})
        # search: 200 is closest by duration AND synced; should win over 181 plain
        return httpx.Response(
            200,
            json=[
                {"duration": 205, "plainLyrics": "too far", "syncedLyrics": None},
                {"duration": 181, "plainLyrics": "close plain", "syncedLyrics": None},
                {"duration": 179, "plainLyrics": "close synced", "syncedLyrics": "[00:02.00]x"},
            ],
        )

    out = await _client(handler).lookup("Artist", "Title", None, 180.0)
    assert out.ok and out.result.found
    assert out.result.synced == "[00:02.00]x"


@pytest.mark.asyncio
async def test_search_no_candidate_within_tolerance_is_clean_miss():
    def handler(req):
        if req.url.path == "/api/get":
            return httpx.Response(404)
        return httpx.Response(200, json=[{"duration": 300, "plainLyrics": "x"}])

    out = await _client(handler).lookup("Artist", "Title", None, 180.0)
    assert out.ok and not out.result.found


@pytest.mark.asyncio
async def test_instrumental_flag():
    out = await _client(lambda r: httpx.Response(200, json={"instrumental": True})).lookup("A", "T", None, 120.0)
    assert out.ok and out.result.found and out.result.instrumental
    assert out.result.plain is None and out.result.synced is None


@pytest.mark.asyncio
async def test_network_error_is_search_error_not_clean_miss():
    def handler(req):
        raise httpx.ConnectError("refused")

    out = await _client(handler).lookup("A", "T", None, 120.0)
    assert not out.ok
    assert out.error and "network" in out.error


@pytest.mark.asyncio
async def test_rate_limited_is_not_ok():
    out = await _client(lambda r: httpx.Response(429)).lookup("A", "T", None, 120.0)
    assert not out.ok and "429" in out.error


@pytest.mark.asyncio
async def test_missing_artist_or_title_short_circuits_without_http():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={})

    out = await _client(handler).lookup(None, "Title")
    assert out.ok and not out.result.found
    assert calls["n"] == 0  # no HTTP made


@pytest.mark.asyncio
async def test_definitive_answers_are_cached():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"plainLyrics": "x", "syncedLyrics": None})

    c = _client(handler)
    await c.lookup("A", "T", "Al", 100.0)
    await c.lookup("A", "T", "Al", 100.0)
    assert calls["n"] == 1  # second lookup served from cache
