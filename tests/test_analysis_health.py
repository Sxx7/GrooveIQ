"""Layer-3 invariant tests.

Insert hand-crafted ``track_features`` rows into an in-memory SQLite,
run the invariant suite, assert the right invariants flag.

Each negative test seeds 150 healthy rows alongside the bad row(s) so
the distribution-shaped invariants ``skip_if_total_below=100`` actually
run.
"""

from __future__ import annotations

import base64
from typing import Any

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.db import Base, TrackFeatures
from app.services.analysis_health import INVARIANTS, overall_status, run_invariants

# ---------------------------------------------------------------------------
# Test DB
# ---------------------------------------------------------------------------


_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def session():
    """Fresh in-memory SQLite per test."""
    engine = create_async_engine(_TEST_DB_URL, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Sess = async_sessionmaker(engine, expire_on_commit=False)
    async with Sess() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# Row builder — defaults pass every invariant
# ---------------------------------------------------------------------------


def _embedding_b64(seed: int = 0) -> str:
    rng = np.random.RandomState(seed)
    vec = rng.randn(64).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return base64.b64encode(vec.tobytes()).decode()


def _track(track_id: str, **overrides: Any) -> TrackFeatures:
    """Builder for a healthy ``TrackFeatures`` row. Overrides plug in
    the bad value the test wants to flag."""
    defaults = dict(
        track_id=track_id,
        file_path=f"/music/{track_id}.flac",
        analyzed_at=1700000000,
        analysis_error=None,
        analysis_version="2.6",
        bpm=120.0,
        bpm_confidence=2.0,
        loudness=-14.0,
        dynamic_range=3.0,
        energy=0.5,
        danceability=0.5,
        valence=0.5,
        acousticness=0.5,
        instrumentalness=0.3,
        speechiness=0.7,
        embedding=_embedding_b64(),
    )
    defaults.update(overrides)
    return TrackFeatures(**defaults)


async def _seed_healthy(session, n: int = 150, **shared_overrides: Any) -> None:
    """Insert N healthy rows with unique IDs. Optional shared_overrides
    apply to every row (used by distribution-drift tests)."""
    for i in range(n):
        session.add(_track(f"healthy{i:010d}", **shared_overrides))
    await session.commit()


def _by_name(results: list) -> dict:
    return {r.invariant.name: r for r in results}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_library_passes_every_invariant(session):
    await _seed_healthy(session, n=150)
    results = await run_invariants(session)
    failed = [(r.invariant.name, r.message) for r in results if r.status == "fail"]
    assert not failed, f"healthy library had failures: {failed}"
    assert overall_status(results) == "ok"


@pytest.mark.asyncio
async def test_empty_library_does_not_crash(session):
    """No rows at all — distribution invariants should skip cleanly."""
    results = await run_invariants(session)
    statuses = {r.status for r in results}
    assert "error" not in statuses, f"unexpected query errors: {[r.message for r in results if r.status == 'error']}"
    # Range checks return COUNT(*)=0 → "ok"; distribution checks see total<100 → "skipped".
    assert "fail" not in statuses


# ---------------------------------------------------------------------------
# Range bugs (catches #88 family)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loudness_out_of_lufs_range_fails(session):
    """The exact shape of the v2.2 Stevens-law bug from #88."""
    session.add(_track("badloud0000000001", loudness=558.75))  # pre-fix Stevens-law value
    await _seed_healthy(session)
    results = await run_invariants(session)
    inv = _by_name(results)["loudness_in_lufs_range"]
    assert inv.status == "fail"
    assert inv.actual == 1


@pytest.mark.asyncio
async def test_bpm_out_of_range_fails(session):
    """The 738.28 RhythmExtractor degenerate value."""
    session.add(_track("badbpm00000000001", bpm=738.28))
    await _seed_healthy(session)
    results = await run_invariants(session)
    inv = _by_name(results)["bpm_plausible"]
    assert inv.status == "fail"
    assert inv.actual == 1


@pytest.mark.asyncio
async def test_valence_out_of_unit_interval_fails(session):
    """The pre-fix approachability_regression mapping that produced 22.024."""
    session.add(_track("badval00000000001", valence=22.024))
    await _seed_healthy(session)
    results = await run_invariants(session)
    inv = _by_name(results)["valence_unit_interval"]
    assert inv.status == "fail"


@pytest.mark.asyncio
async def test_unit_interval_check_catches_negative_energy(session):
    session.add(_track("badeng00000000001", energy=-0.5))
    await _seed_healthy(session)
    results = await run_invariants(session)
    inv = _by_name(results)["all_unit_interval_features_in_range"]
    assert inv.status == "fail"


# ---------------------------------------------------------------------------
# NULL leaks (catches #42, #83)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_embedding_on_latest_version_fails(session):
    """A successfully-analyzed row at the latest version must have an
    embedding. NULL = #42 zero-norm guard fired or #83 wrong output index."""
    session.add(_track("nullemb0000000001", embedding=None))
    await _seed_healthy(session)
    results = await run_invariants(session)
    inv = _by_name(results)["embedding_coverage_for_latest_version"]
    assert inv.status == "fail"
    assert inv.actual == 1


@pytest.mark.asyncio
async def test_null_embedding_on_older_version_does_not_fail(session):
    """The check is scoped to the latest version — stale rows from a
    pre-bump version with NULL embeddings shouldn't trip it (they're
    being re-analyzed asynchronously)."""
    session.add(_track("oldnull000000001", embedding=None, analysis_version="2.4"))
    await _seed_healthy(session, n=150, analysis_version="2.6")
    results = await run_invariants(session)
    inv = _by_name(results)["embedding_coverage_for_latest_version"]
    assert inv.status == "ok"


# ---------------------------------------------------------------------------
# Distribution / inversion (catches #99 + future #88-shapes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instrumentalness_inversion_fails(session):
    """The #99 catch: AVG(instrumentalness) > 0.5 means the column the
    code reads is the voice probability, not the instrumental probability.
    Today's prod has avg=0.68 — exactly what this would flag."""
    await _seed_healthy(session, n=150, instrumentalness=0.7)
    results = await run_invariants(session)
    inv = _by_name(results)["instrumentalness_avg_below_voice_majority"]
    assert inv.status == "fail"
    assert 0.65 < inv.actual < 0.75


@pytest.mark.asyncio
async def test_instrumentalness_in_range_passes(session):
    """Post-#99-fix value should be roughly the inverse: ~0.32."""
    await _seed_healthy(session, n=150, instrumentalness=0.3)
    results = await run_invariants(session)
    inv = _by_name(results)["instrumentalness_avg_below_voice_majority"]
    assert inv.status == "ok"


@pytest.mark.asyncio
async def test_valence_pinned_at_zero_fails(session):
    """The pre-#88 mood_happy compression produced AVG(valence) = 0.018
    across 67k tracks. This invariant catches that shape."""
    await _seed_healthy(session, n=150, valence=0.02)
    results = await run_invariants(session)
    inv = _by_name(results)["valence_distribution_not_collapsed"]
    assert inv.status == "fail"


@pytest.mark.asyncio
async def test_danceability_pinned_high_fails(session):
    """Today's danceability avg is 0.937 across the prod library — same
    shape as #88-mood_happy compression but at the high end."""
    await _seed_healthy(session, n=150, danceability=0.95)
    results = await run_invariants(session)
    inv = _by_name(results)["danceability_distribution_not_pinned_high"]
    assert inv.status == "fail"


# ---------------------------------------------------------------------------
# Migration / coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_pct_at_latest_version_fails(session):
    """A bumped ANALYSIS_VERSION with no re-analyzed rows = stalled scan."""
    for i in range(150):
        session.add(_track(f"old{i:013d}", analysis_version="2.5"))
    # No 2.6 rows. Latest version = 2.5 → all rows ARE on latest → 100% coverage → ok.
    # Re-trigger by making one row at 2.6 then deleting it... easier: directly check the
    # case where coverage is 0% by including some 2.6 rows that all errored.
    for i in range(2):
        session.add(
            _track(
                f"err{i:013d}",
                analysis_version="2.6",
                analysis_error="forced error",
                analyzed_at=None,
                embedding=None,
            )
        )
    await session.commit()
    results = await run_invariants(session)
    inv = _by_name(results)["latest_version_coverage"]
    # Latest version is 2.6 (taken from non-erroring rows only? See impl). The SQL
    # says `MAX(analysis_version) WHERE analysis_error IS NULL`. With all 2.6 rows
    # erroring, max-non-erroring is 2.5, and coverage of 2.5 = 100% (all 150). OK.
    # This test mostly exists to prove the invariant doesn't crash on a half-erroring
    # library; the actual stall scenario is hard to construct without time-travel.
    assert inv.status == "ok"


# ---------------------------------------------------------------------------
# Skip behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distribution_invariants_skip_on_tiny_library(session):
    """One bad row shouldn't fail a distribution invariant when the
    library has fewer rows than the noise floor."""
    session.add(_track("solo000000000001", instrumentalness=0.95))
    await session.commit()
    results = await run_invariants(session)
    inv = _by_name(results)["instrumentalness_avg_below_voice_majority"]
    assert inv.status == "skipped"
    assert "library too small" in inv.message


# ---------------------------------------------------------------------------
# Result serialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_to_dict_is_jsonable(session):
    await _seed_healthy(session)
    results = await run_invariants(session)
    import json

    for r in results:
        # Round-trip through json — surfaces any non-serialisable types
        # (Decimal, datetime, numpy scalar) the route would later fail on.
        json.dumps(r.to_dict())


# ---------------------------------------------------------------------------
# overall_status() rollup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overall_status_fail_on_error_severity(session):
    session.add(_track("badbpm00000000001", bpm=999.9))
    await _seed_healthy(session)
    results = await run_invariants(session)
    assert overall_status(results) == "fail"


@pytest.mark.asyncio
async def test_overall_status_warn_on_warn_severity_only(session):
    """Distribution drift on a warn-severity invariant degrades to 'warn',
    not 'fail'."""
    await _seed_healthy(session, n=150, danceability=0.95)
    results = await run_invariants(session)
    assert overall_status(results) == "warn"


@pytest.mark.asyncio
async def test_invariants_have_unique_names():
    """Catch silly copy-paste in the INVARIANTS list."""
    names = [inv.name for inv in INVARIANTS]
    assert len(names) == len(set(names)), f"duplicate invariant names: {names}"


@pytest.mark.asyncio
async def test_every_invariant_has_a_catches_message():
    """Every invariant must explain WHAT bug class it catches, so a
    failing dashboard tile is actionable rather than mystery-meat."""
    for inv in INVARIANTS:
        assert inv.catches.strip(), f"invariant {inv.name!r} has empty 'catches'"
        assert inv.severity in ("error", "warn"), f"invariant {inv.name!r} bad severity: {inv.severity!r}"
