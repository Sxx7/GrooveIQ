"""GrooveIQ — Tests for the Heat playlist engine (app/services/heat_playlist.py).

Builds a small play-event stream in an in-memory SQLite DB and asserts the
blended heat score behaves as designed:

  * recent play intensity ranks a heavily-played track above a lightly-played
    one (the resurfacing full_listen<=3 cap bug must NOT reappear)
  * the succession bonus lifts a track played back-to-back over one with the
    same play count spread out
  * recency decay favours recent engagement
  * is_hero_eligible flips at the configured threshold
  * the min_plays / min_heat floors gate the pool
  * the service is isolated from resurfacing.py
"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.algorithm_config_schema import AlgorithmConfigData, HeatConfig
from app.models.db import Base, ListenEvent
from app.services import heat_playlist

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
_Session = async_sessionmaker(_engine, expire_on_commit=False)
_DAY = 86400
_MIN = 60
_USER = "u_heat"


def _now() -> int:
    return int(time.time())


def _cfg(**kw) -> AlgorithmConfigData:
    return AlgorithmConfigData(heat=HeatConfig(**kw))


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def patch_config(monkeypatch):
    monkeypatch.setattr(heat_playlist, "get_config", _cfg)


async def _play(s, tid, ts, *, full=True):
    """A play_start + a full-listen (or early-skip) play_end at time ts."""
    s.add(ListenEvent(user_id=_USER, track_id=tid, event_type="play_start", timestamp=ts))
    s.add(
        ListenEvent(
            user_id=_USER,
            track_id=tid,
            event_type="play_end",
            timestamp=ts + 1,
            value=0.95 if full else 0.05,
            dwell_ms=200_000 if full else 500,
        )
    )


async def _ranked(**cfgkw):
    monkey_cfg = _cfg(**cfgkw)
    # patch_config already points get_config at _cfg() with defaults; override per-call here.
    heat_playlist.get_config = lambda: monkey_cfg  # type: ignore[assignment]
    async with _Session() as s:
        return await heat_playlist.get_heat_tracks(s, _USER, limit=50, now=_now())


# ---------------------------------------------------------------------------
# Ranking: intensity is not capped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heavily_played_outranks_lightly_played():
    """A 20-play track beats a 3-play track — the cap bug (both would tie) must not recur."""
    now = _now()
    async with _Session() as s:
        for i in range(20):
            await _play(s, "HOT", now - i * _DAY // 4)  # 20 plays over ~5 days
        for i in range(3):
            await _play(s, "MILD", now - i * _DAY // 4)  # 3 plays
        await s.commit()
    ranked, _ = await _ranked()
    order = [tid for tid, _, _ in ranked]
    assert order.index("HOT") < order.index("MILD")
    heat = {tid: h for tid, h, _ in ranked}
    assert heat["HOT"] > heat["MILD"] * 1.5  # comfortably, not a tie


# ---------------------------------------------------------------------------
# Succession bonus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_succession_lifts_back_to_back_track():
    """Two tracks with the same play count: the one played back-to-back scores higher."""
    now = _now()
    async with _Session() as s:
        # SUCC: 6 plays all within a few minutes of each other (back-to-back)
        for i in range(6):
            await _play(s, "SUCC", now - 3 * _DAY + i * 4 * _MIN)
        # SPREAD: 6 plays, one per day (never in succession) — interleave a filler so the
        # SUCC run isn't broken and SPREAD is never adjacent to itself.
        for i in range(6):
            await _play(s, "SPREAD", now - (i + 1) * _DAY)
        await s.commit()
    ranked, _ = await _ranked()
    heat = {tid: h for tid, h, _ in ranked}
    sig = {tid: sg for tid, _, sg in ranked}
    assert sig["SUCC"]["succession_count"] >= 4
    assert sig["SPREAD"]["succession_count"] == 0
    assert heat["SUCC"] > heat["SPREAD"]


@pytest.mark.asyncio
async def test_succession_ignores_gaps_and_interleaving():
    """Same-track replays too far apart, or interleaved by another track, are not succession."""
    now = _now()
    async with _Session() as s:
        # A A with a 90-min gap -> NOT succession (gap > 20 min default)
        await _play(s, "GAP", now - _DAY)
        await _play(s, "GAP", now - _DAY + 90 * _MIN)
        # A B A close together -> the B breaks adjacency, so A-A is not counted
        await _play(s, "INTL", now - 2 * _DAY)
        await _play(s, "OTHER", now - 2 * _DAY + 2 * _MIN)
        await _play(s, "INTL", now - 2 * _DAY + 4 * _MIN)
        await s.commit()
    ranked, _ = await _ranked()
    sig = {tid: sg for tid, _, sg in ranked}
    assert sig.get("GAP", {}).get("succession_count", 0) == 0
    assert sig.get("INTL", {}).get("succession_count", 0) == 0


# ---------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_beats_old_for_equal_plays():
    now = _now()
    async with _Session() as s:
        for i in range(8):
            await _play(s, "RECENT", now - i * _DAY // 8)  # last day
        for i in range(8):
            await _play(s, "OLD", now - 18 * _DAY - i * _DAY // 8)  # ~18 days ago
        await s.commit()
    ranked, _ = await _ranked()
    heat = {tid: h for tid, h, _ in ranked}
    assert heat["RECENT"] > heat["OLD"]


# ---------------------------------------------------------------------------
# Hero eligibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hero_eligibility_flips_at_threshold():
    """Below hero_min_tracks hot tracks -> not eligible; at/above -> eligible."""
    now = _now()
    async with _Session() as s:
        # 5 clearly-hot tracks (many recent full listens)
        for t in range(5):
            for i in range(10):
                await _play(s, f"HOT{t}", now - i * _DAY // 10)
        await s.commit()
    _, elig5 = await _ranked(hero_min_tracks=8)
    assert elig5 is False  # only 5 hot tracks
    _, elig_lo = await _ranked(hero_min_tracks=5)
    assert elig_lo is True  # threshold lowered to 5


@pytest.mark.asyncio
async def test_hero_ignores_stale_hot_tracks():
    """A track hot but last played outside hero_window_days does not count toward the hero."""
    now = _now()
    async with _Session() as s:
        for t in range(10):
            for i in range(10):
                await _play(s, f"STALE{t}", now - 14 * _DAY - i * _DAY // 10)  # ~14 days old
        await s.commit()
    _, elig = await _ranked(hero_min_tracks=8, hero_window_days=7)
    assert elig is False  # plenty of plays, but all outside the 7-day hero window


# ---------------------------------------------------------------------------
# Floors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_min_plays_floor_excludes_one_off():
    now = _now()
    async with _Session() as s:
        await _play(s, "ONEOFF", now - _DAY)  # single play
        for i in range(5):
            await _play(s, "REAL", now - i * _DAY // 5)
        await s.commit()
    ranked, _ = await _ranked(min_plays=2)
    ids = [tid for tid, _, _ in ranked]
    assert "ONEOFF" not in ids
    assert "REAL" in ids


@pytest.mark.asyncio
async def test_disabled_returns_empty():
    now = _now()
    async with _Session() as s:
        for i in range(10):
            await _play(s, "X", now - i * _DAY // 10)
        await s.commit()
    ranked, elig = await _ranked(enabled=False)
    assert ranked == []
    assert elig is False


# ---------------------------------------------------------------------------
# Isolation + reason chips
# ---------------------------------------------------------------------------


def test_service_does_not_import_resurfacing():
    """Isolation: the Heat service must not import resurfacing / user_mixes / affinity_radio,
    so its scoring can never be coupled to those surfaces (the docstring may *name* them)."""
    import ast

    with open("app/services/heat_playlist.py") as fh:
        tree = ast.parse(fh.read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    for forbidden in ("app.services.resurfacing", "app.services.user_mixes", "app.services.affinity_radio"):
        assert forbidden not in imported


def test_heat_reason_chips():
    assert heat_playlist.heat_reason({"succession_count": 3, "plays": 5}) == "on repeat"
    assert heat_playlist.heat_reason({"succession_count": 0, "plays": 7}) == "played 7× recently"
    assert heat_playlist.heat_reason({"succession_count": 0, "plays": 1}) == "heating up"
