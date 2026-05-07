"""
GrooveIQ – Tests for the API call log middleware + service (issue #79).

Covers:
- redact() doesn't leave secret-keyed values in JSON
- truncate_body() handles oversized bodies + JSON parsing
- should_log_path() honours the include_events toggle and skip prefixes
- HTTP middleware persists request/response rows for /v1/users/* and skips
  the configured surfaces (/health, /v1/pipeline/stream)
- list_calls() filters by user, method, path, status, include_events
- purge_old() deletes rows older than the retention window
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.db import ApiCallLog, Base, User
from app.services.api_call_log import (
    classify_user_agent,
    list_calls,
    parse_client_ip,
    purge_old,
    redact,
    should_log_path,
    start_log_writer,
    stop_log_writer,
    truncate_body,
    write_log,
)

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)


async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _TestSession() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@pytest_asyncio.fixture(autouse=True)
async def setup_db(monkeypatch):
    # The middleware writes via AsyncSessionLocal (its own session, since the
    # write is fire-and-forget). Point that at the in-memory engine too so the
    # rows show up in our queries.
    monkeypatch.setattr("app.services.api_call_log.AsyncSessionLocal", _TestSession)

    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # The httpx ASGITransport doesn't fire FastAPI's lifespan, so the
    # background batch-writer that production startup launches isn't
    # running by default. Start it here so middleware writes get flushed.
    start_log_writer()

    app.dependency_overrides[get_session] = override_get_session
    yield
    await stop_log_writer()
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.api_keys_list[0]}"} if settings.api_keys_list else {},
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Pure-function tests (no DB)
# ---------------------------------------------------------------------------


class TestRedact:
    def test_redacts_password(self):
        out = redact({"user_id": "alice", "password": "secret123"})
        assert out["user_id"] == "alice"
        assert out["password"] == "***redacted***"

    def test_redacts_nested(self):
        out = redact({"auth": {"api_key": "abc"}, "data": [{"token": "xyz"}]})
        assert out["auth"]["api_key"] == "***redacted***"
        assert out["data"][0]["token"] == "***redacted***"

    def test_passes_through_safe(self):
        body = {"user_id": "alice", "track_id": "t1", "count": 10}
        assert redact(body) == body

    def test_handles_non_dict_input(self):
        assert redact([1, 2, 3]) == [1, 2, 3]
        assert redact("hello") == "hello"
        assert redact(None) is None


class TestTruncateBody:
    def test_returns_none_for_empty(self):
        assert truncate_body(b"", "application/json") is None
        assert truncate_body(None, "application/json") is None

    def test_parses_small_json(self):
        out = truncate_body(b'{"x": 1}', "application/json")
        assert out == {"x": 1}

    def test_caps_oversize_body(self, monkeypatch):
        monkeypatch.setattr(settings, "API_LOG_MAX_BODY_BYTES", 256)
        body = b'"' + b"x" * 5000 + b'"'  # ~5KB JSON string
        out = truncate_body(body, "application/json")
        # JSON-parses up to cap; if parse fails on truncation, falls back to string preview.
        assert out is not None

    def test_string_preview_for_non_json(self):
        out = truncate_body(b"Hello, world!", "text/plain")
        assert "Hello" in out

    def test_redacts_within_parsed_body(self):
        out = truncate_body(b'{"password": "topsecret", "ok": 1}', "application/json")
        assert out["password"] == "***redacted***"
        assert out["ok"] == 1

    def test_truncates_long_lists(self, monkeypatch):
        monkeypatch.setattr(settings, "API_LOG_MAX_LIST_ITEMS", 5)
        # Build a 50-item list response
        big = [{"track_id": f"t{i}"} for i in range(50)]
        import json

        out = truncate_body(json.dumps(big).encode(), "application/json")
        assert isinstance(out, dict)
        assert out.get("__truncated_list__") is True
        assert out.get("items_total") == 50
        assert len(out.get("items", [])) == 5


class TestShouldLogPath:
    def test_v1_paths_logged(self):
        assert should_log_path("/v1/users/alice/profile")
        assert should_log_path("/v1/recommend/alice")
        assert should_log_path("/v1/radio/start")

    def test_health_skipped(self):
        assert not should_log_path("/health")

    def test_streaming_skipped(self):
        assert not should_log_path("/v1/pipeline/stream")

    def test_static_skipped(self):
        assert not should_log_path("/static/css/foo.css")
        assert not should_log_path("/dashboard")

    def test_self_skipped(self):
        assert not should_log_path("/v1/api-calls/123")
        assert not should_log_path("/v1/api-calls")
        # The per-user log-viewer must also skip itself, otherwise the dashboard
        # poll would create a "log → row → log" feedback loop.
        assert not should_log_path("/v1/users/alice/api-calls")
        assert not should_log_path("/v1/users/alice/api-calls/123")

    def test_events_toggle(self, monkeypatch):
        monkeypatch.setattr(settings, "API_LOG_INCLUDE_EVENTS", False)
        assert not should_log_path("/v1/events")
        assert not should_log_path("/v1/events/batch")
        monkeypatch.setattr(settings, "API_LOG_INCLUDE_EVENTS", True)
        assert should_log_path("/v1/events")


class TestClassifyUserAgent:
    def test_chrome_browser(self):
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        assert classify_user_agent(ua) == "browser"

    def test_firefox_browser(self):
        assert classify_user_agent("Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/130.0") == "browser"

    def test_curl_cli(self):
        assert classify_user_agent("curl/8.4.0") == "cli"

    def test_python_requests_cli(self):
        assert classify_user_agent("python-requests/2.31.0") == "cli"

    def test_postman_cli(self):
        assert classify_user_agent("PostmanRuntime/7.32.3") == "cli"

    def test_iphone_mobile(self):
        ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
        # iPhone wins over generic Mozilla — mobile patterns checked first.
        assert classify_user_agent(ua) == "mobile"

    def test_android_mobile(self):
        ua = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Mobile Safari/537.36"
        assert classify_user_agent(ua) == "mobile"

    def test_empty_or_none(self):
        assert classify_user_agent(None) == "other"
        assert classify_user_agent("") == "other"
        assert classify_user_agent("   ") == "other"

    def test_unknown_falls_through(self):
        assert classify_user_agent("CustomScraperBot/0.1") == "other"


class TestParseClientIp:
    def test_uses_xff_when_set(self):
        assert parse_client_ip("203.0.113.5", "172.21.0.1") == "203.0.113.5"

    def test_xff_takes_leftmost(self):
        # Trusted proxy chain: original client comes first per RFC 7239 conventions.
        assert parse_client_ip("203.0.113.5, 198.51.100.1", "172.21.0.1") == "203.0.113.5"

    def test_falls_back_to_peer(self):
        assert parse_client_ip(None, "172.21.0.1") == "172.21.0.1"
        assert parse_client_ip("", "172.21.0.1") == "172.21.0.1"

    def test_returns_none_when_both_missing(self):
        assert parse_client_ip(None, None) is None
        assert parse_client_ip("", "") is None

    def test_caps_length(self):
        # Pathological XFF (huge string) shouldn't blow past the column.
        assert len(parse_client_ip("X" * 200, None)) <= 64


# ---------------------------------------------------------------------------
# Middleware integration tests
# ---------------------------------------------------------------------------


async def _wait_for_log_rows(min_count: int = 1, timeout_s: float = 5.0) -> list[ApiCallLog]:
    """Spin until the background batch flusher has committed enough rows.

    Default timeout is generous (5 s) because the flusher commits on a
    1 s cadence and a slow CI runner can push a single tick past the
    naive `flush_interval + poll_step` floor.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        async with _TestSession() as s:
            rows = (await s.execute(select(ApiCallLog).order_by(ApiCallLog.id))).scalars().all()
            if len(rows) >= min_count:
                return list(rows)
        await asyncio.sleep(0.05)
    return rows  # may be shorter than min_count


async def _seed_user(user_id: str = "alice") -> None:
    async with _TestSession() as session:
        session.add(User(user_id=user_id, display_name=user_id.capitalize()))
        await session.commit()


class TestMiddlewarePersists:
    async def test_get_request_logged(self, client, monkeypatch):
        monkeypatch.setattr(settings, "API_LOG_ENABLED", True)
        await _seed_user()
        resp = await client.get("/v1/users/alice/profile")
        assert resp.status_code == 200

        rows = await _wait_for_log_rows(min_count=1)
        assert len(rows) >= 1
        row = next(r for r in rows if r.path == "/v1/users/alice/profile")
        assert row.method == "GET"
        assert row.user_id == "alice"
        assert row.status_code == 200
        assert row.response_summary is not None
        assert row.response_size_bytes > 0
        assert row.duration_ms >= 0

    async def test_post_body_captured_with_redaction(self, client, monkeypatch):
        monkeypatch.setattr(settings, "API_LOG_ENABLED", True)
        # Hitting an endpoint we know about — POST /v1/users to create a user.
        # The route may 401/403 without admin, but the body should still be logged.
        await client.post(
            "/v1/users",
            json={"user_id": "bob", "display_name": "Bob", "password": "hunter2"},
        )
        # We don't assert status — auth may reject; the point is the log entry.
        rows = await _wait_for_log_rows(min_count=1)
        match = next((r for r in rows if r.method == "POST" and r.path == "/v1/users"), None)
        assert match is not None, f"got {[(r.method, r.path) for r in rows]}"
        # password must be redacted in the captured body.
        body = match.request_body or {}
        assert body.get("password") == "***redacted***"
        assert body.get("user_id") == "bob"

    async def test_health_not_logged(self, client, monkeypatch):
        monkeypatch.setattr(settings, "API_LOG_ENABLED", True)
        resp = await client.get("/health")
        assert resp.status_code == 200
        # Allow the event loop one tick — should still be empty.
        await asyncio.sleep(0.1)
        async with _TestSession() as s:
            rows = (await s.execute(select(ApiCallLog).where(ApiCallLog.path == "/health"))).scalars().all()
        assert rows == []

    async def test_master_switch_off_skips_writes(self, client, monkeypatch):
        monkeypatch.setattr(settings, "API_LOG_ENABLED", False)
        await _seed_user()
        await client.get("/v1/users/alice/profile")
        await asyncio.sleep(0.1)
        async with _TestSession() as s:
            rows = (await s.execute(select(ApiCallLog))).scalars().all()
        assert rows == []

    async def test_user_id_from_path(self, client, monkeypatch):
        monkeypatch.setattr(settings, "API_LOG_ENABLED", True)
        await _seed_user("simon")
        await client.get("/v1/users/simon/profile")
        rows = await _wait_for_log_rows(min_count=1)
        match = next(r for r in rows if r.path == "/v1/users/simon/profile")
        assert match.user_id == "simon"

    async def test_captures_user_agent_and_classifies_source(self, client, monkeypatch):
        monkeypatch.setattr(settings, "API_LOG_ENABLED", True)
        await _seed_user("alice")
        await client.get(
            "/v1/users/alice/profile",
            headers={"User-Agent": "curl/8.4.0", "X-Forwarded-For": "203.0.113.42"},
        )
        rows = await _wait_for_log_rows(min_count=1)
        match = next(r for r in rows if r.path == "/v1/users/alice/profile")
        assert match.user_agent == "curl/8.4.0"
        assert match.source_class == "cli"
        assert match.client_ip == "203.0.113.42"

    async def test_browser_classification(self, client, monkeypatch):
        monkeypatch.setattr(settings, "API_LOG_ENABLED", True)
        await _seed_user("alice")
        await client.get(
            "/v1/users/alice/profile",
            headers={"User-Agent": "Mozilla/5.0 Chrome/130.0.0.0 Safari/537.36"},
        )
        rows = await _wait_for_log_rows(min_count=1)
        match = next(r for r in rows if r.path == "/v1/users/alice/profile")
        assert match.source_class == "browser"


# ---------------------------------------------------------------------------
# Background batch writer (in-process buffer + ~1s flusher)
# ---------------------------------------------------------------------------


def _write_kwargs(**overrides):
    """Build a minimal valid write_log() kwargs dict for the buffer tests."""
    base = dict(
        method="GET",
        path="/v1/test",
        route_template="/v1/test",
        query_string=None,
        request_body=None,
        status_code=200,
        duration_ms=5,
        user_id=None,
        request_id=None,
        response_summary=None,
        response_size_bytes=None,
    )
    base.update(overrides)
    return base


class TestBatchWriter:
    async def test_write_log_returns_immediately_and_batches(self, monkeypatch):
        """write_log should enqueue without blocking on a DB commit; the
        flusher commits the whole batch in one transaction ~1 s later."""
        monkeypatch.setattr(settings, "API_LOG_ENABLED", True)

        # Push 25 rows back-to-back. With per-request commits this would be
        # 25 separate transactions; with the batch writer it's at most one.
        for i in range(25):
            await write_log(**_write_kwargs(path=f"/v1/test/{i}"))

        rows = await _wait_for_log_rows(min_count=25, timeout_s=3.0)
        assert len(rows) == 25
        assert {r.path for r in rows} == {f"/v1/test/{i}" for i in range(25)}

    async def test_write_log_disabled_is_noop(self, monkeypatch):
        monkeypatch.setattr(settings, "API_LOG_ENABLED", False)
        await write_log(**_write_kwargs(path="/v1/dropped"))
        await asyncio.sleep(1.2)  # past one flush interval
        async with _TestSession() as s:
            rows = (await s.execute(select(ApiCallLog))).scalars().all()
        assert rows == []

    async def test_queue_full_drops_silently(self, monkeypatch):
        """When the buffer is at capacity, write_log must not raise — debug
        rows are expendable, request handling must never break."""
        monkeypatch.setattr(settings, "API_LOG_ENABLED", True)
        # Shrink the queue so we can trip the limit deterministically.
        monkeypatch.setattr("app.services.api_call_log._MAX_QUEUE_SIZE", 4)
        # Stop the running writer (started by the autouse fixture) and
        # restart it with the patched size so the asyncio.Queue is bounded.
        await stop_log_writer()
        start_log_writer()

        # Fire more than the cap quickly. The flusher might drain some
        # mid-loop, but at least one must get dropped without raising.
        for i in range(50):
            await write_log(**_write_kwargs(path=f"/v1/q{i}"))


# ---------------------------------------------------------------------------
# Service-level: list_calls + purge_old
# ---------------------------------------------------------------------------


async def _insert_log_row(**overrides):
    async with _TestSession() as s:
        defaults = dict(
            created_at=int(time.time()),
            user_id="alice",
            method="GET",
            path="/v1/users/alice/profile",
            status_code=200,
            duration_ms=12,
        )
        defaults.update(overrides)
        s.add(ApiCallLog(**defaults))
        await s.commit()


class TestListCalls:
    async def test_filter_by_user(self):
        await _insert_log_row(user_id="alice")
        await _insert_log_row(user_id="bob")
        async with _TestSession() as s:
            rows, total = await list_calls(s, user_id="alice")
        assert total == 1
        assert rows[0]["user_id"] == "alice"

    async def test_filter_by_method(self):
        await _insert_log_row(method="GET")
        await _insert_log_row(method="POST", path="/v1/events")
        async with _TestSession() as s:
            rows, total = await list_calls(s, method="POST")
        assert total == 1
        assert rows[0]["method"] == "POST"

    async def test_filter_by_path_contains(self):
        await _insert_log_row(path="/v1/users/alice/profile")
        await _insert_log_row(path="/v1/recommend/alice")
        async with _TestSession() as s:
            rows, total = await list_calls(s, path_contains="recommend")
        assert total == 1
        assert "recommend" in rows[0]["path"]

    async def test_filter_by_status(self):
        await _insert_log_row(status_code=200)
        await _insert_log_row(status_code=500)
        async with _TestSession() as s:
            rows, total = await list_calls(s, status=500)
        assert total == 1
        assert rows[0]["status_code"] == 500

    async def test_include_events_false_hides_event_rows(self):
        await _insert_log_row(path="/v1/events", method="POST")
        await _insert_log_row(path="/v1/users/alice/profile")
        async with _TestSession() as s:
            rows, total = await list_calls(s, include_events=False)
        assert total == 1
        assert rows[0]["path"] != "/v1/events"

    async def test_pagination(self):
        for i in range(5):
            await _insert_log_row(path=f"/v1/users/u{i}/profile")
        async with _TestSession() as s:
            rows, total = await list_calls(s, limit=2, offset=0)
        assert total == 5
        assert len(rows) == 2

    async def test_filter_by_source(self):
        await _insert_log_row(source_class="browser", user_agent="Mozilla/5.0 Chrome/130.0")
        await _insert_log_row(source_class="cli", user_agent="curl/8.4.0")
        await _insert_log_row(source_class="cli", user_agent="python-requests/2.31.0")
        async with _TestSession() as s:
            rows, total = await list_calls(s, source="cli")
        assert total == 2
        assert all(r["source_class"] == "cli" for r in rows)

    async def test_filter_by_client_ip_contains(self):
        await _insert_log_row(client_ip="192.168.1.42", source_class="browser")
        await _insert_log_row(client_ip="10.0.0.5", source_class="mobile")
        async with _TestSession() as s:
            rows, total = await list_calls(s, client_ip_contains="192.168.")
        assert total == 1
        assert rows[0]["client_ip"] == "192.168.1.42"

    async def test_summary_includes_source_and_ip(self):
        await _insert_log_row(client_ip="203.0.113.5", source_class="browser")
        async with _TestSession() as s:
            rows, _ = await list_calls(s)
        assert rows[0]["client_ip"] == "203.0.113.5"
        assert rows[0]["source_class"] == "browser"

    async def test_uid_path_rows_surface_under_user_filter(self):
        """PATCH /v1/users/{uid} rows have empty user_id but still belong to
        the user — list_calls(user_id="alice") must include them by resolving
        alice's uid and OR-matching the numeric path."""
        async with _TestSession() as s:
            user = User(user_id="alice", display_name="Alice")
            s.add(user)
            await s.commit()
            await s.refresh(user)
            alice_uid = user.uid

        # PATCH row written by middleware: user_id is empty because the route
        # param was {uid} not {user_id}.
        await _insert_log_row(
            user_id=None,
            method="PATCH",
            path=f"/v1/users/{alice_uid}",
            status_code=200,
        )
        # Plus a normal user_id-shaped row, for sanity.
        await _insert_log_row(user_id="alice", path="/v1/users/alice/profile")
        # And an unrelated user.
        await _insert_log_row(user_id="bob", path="/v1/users/bob/profile")

        async with _TestSession() as s:
            rows, total = await list_calls(s, user_id="alice")
        assert total == 2
        paths = {r["path"] for r in rows}
        assert f"/v1/users/{alice_uid}" in paths
        assert "/v1/users/alice/profile" in paths

    async def test_unknown_user_filter_falls_back_to_exact_match(self):
        """When user_id doesn't resolve, behave like the old exact-match filter
        (don't blow up, don't return everything)."""
        await _insert_log_row(user_id="known", path="/v1/users/known/profile")
        await _insert_log_row(user_id=None, path="/v1/users/99/profile")

        async with _TestSession() as s:
            rows, total = await list_calls(s, user_id="ghost")
        assert total == 0
        assert rows == []


class TestPurgeOld:
    async def test_purges_old_rows(self):
        now = int(time.time())
        await _insert_log_row(created_at=now)
        await _insert_log_row(created_at=now - 8 * 86400)  # 8 days old
        async with _TestSession() as s:
            removed = await purge_old(s, retention_days=7)
            await s.commit()
        assert removed == 1
        async with _TestSession() as s:
            rows, _ = await list_calls(s)
        assert len(rows) == 1
