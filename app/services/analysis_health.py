"""Library-wide invariants over ``track_features``.

Layer 3 of the analysis testing strategy: not unit tests, but assertions
about the shape of the data the analysis pipeline has actually produced
across the live library. Catches the failure mode behind every #88-style
silent value-corruption bug — the analysis ran, no exception was raised,
but the values are wrong.

Each invariant is a single SQL aggregate (count or average) compared
against an expected value. A failure means a real, observable property
of the library is wrong, not a stylistic complaint.

Why this layer exists when Layers 1 + 2 already test individual
fixtures and ONNX I/O contracts:

  - **Distribution drift** (#88 valence pinned at 0.018 across 67k
    tracks; today's danceability mean of 0.937) is invisible at the
    single-track level — every track passes the `[0, 1]` bound — but
    the library-wide average is the smoking gun.
  - **Inversion bugs** (#88 mood_sad/relaxed/party col inversion; #99
    voice_instrumental col inversion) keep values in the right RANGE
    but flipped. The library's mean against a known prior catches them.
  - **NULL leaks** (#42 zero-norm; #83 wrong ONNX output index) are
    only visible at scale: "37 % of analyzed rows have NULL embedding".

Run on demand via ``GET /v1/admin/analysis-health``. Cheap enough
(seconds, not minutes) to call from a periodic monitor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import TrackFeatures

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Invariant:
    """A single library-wide assertion.

    The SQL must return a single scalar value. ``kind`` determines how
    that value is compared against ``bounds``:

        expect_zero      — value must be 0 (e.g. count of bad rows)
        expect_in_range  — value must be inside (lo, hi)
        expect_at_least  — value must be ≥ bounds (single threshold)
    """

    name: str
    description: str
    catches: str
    severity: str  # "error" | "warn"
    sql: str
    kind: str  # "expect_zero" | "expect_in_range" | "expect_at_least"
    bounds: tuple[float, float] | float | None = None
    skip_if_total_below: int = 0


@dataclass(frozen=True)
class Result:
    invariant: Invariant
    status: str  # "ok" | "fail" | "skipped" | "error"
    actual: float | int | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        bounds = self.invariant.bounds
        return {
            "name": self.invariant.name,
            "description": self.invariant.description,
            "catches": self.invariant.catches,
            "severity": self.invariant.severity,
            "kind": self.invariant.kind,
            "bounds": list(bounds) if isinstance(bounds, tuple) else bounds,
            "status": self.status,
            "actual": self.actual,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------

# Many distribution invariants want to scope to "the latest analysis_version
# the library has any rows of" so a half-finished migration doesn't wash
# them out. Inlined as a subquery — works on SQLite and Postgres alike.
_LATEST_VERSION = "(SELECT MAX(analysis_version) FROM track_features WHERE analysis_error IS NULL)"


INVARIANTS: list[Invariant] = [
    # ---- Range bugs: catches #88-loudness, #88-bpm, #88-valence -----------
    #
    # All range invariants are scoped to ``analysis_version = MAX(...)`` —
    # the dashboard's job is "is the *current* pipeline producing bad
    # values?", not "is there any stale data anywhere?". During a multi-
    # day rescan, pre-fix rows naturally outnumber latest-version rows; we
    # rely on ``latest_version_coverage`` (below) to surface migration
    # progress separately.
    Invariant(
        name="loudness_in_lufs_range",
        description="Loudness must be in [-100, 0] LUFS for analyzed tracks on the latest analysis_version.",
        catches="#88 Stevens-law power-sum (output 0–5896)",
        severity="error",
        sql=(
            "SELECT COUNT(*) FROM track_features "
            "WHERE analysis_error IS NULL AND analyzed_at IS NOT NULL "
            f"AND analysis_version = {_LATEST_VERSION} "
            "AND loudness IS NOT NULL AND (loudness > 0 OR loudness < -100)"
        ),
        kind="expect_zero",
    ),
    Invariant(
        name="bpm_plausible",
        description="BPM must be in [30, 250] (else the analyser clamps to NULL) on the latest analysis_version.",
        catches="#88 RhythmExtractor degenerate output (738.28)",
        severity="error",
        sql=(
            "SELECT COUNT(*) FROM track_features "
            f"WHERE analysis_version = {_LATEST_VERSION} "
            "AND bpm IS NOT NULL AND (bpm < 30 OR bpm > 250)"
        ),
        kind="expect_zero",
    ),
    Invariant(
        name="valence_unit_interval",
        description="Valence must be in [0, 1] on the latest analysis_version.",
        catches="#88 approachability_regression mapping (output -5 to 711)",
        severity="error",
        sql=(
            "SELECT COUNT(*) FROM track_features "
            f"WHERE analysis_version = {_LATEST_VERSION} "
            "AND valence IS NOT NULL AND (valence < 0 OR valence > 1)"
        ),
        kind="expect_zero",
    ),
    Invariant(
        name="all_unit_interval_features_in_range",
        description="energy / danceability / acousticness / instrumentalness / "
        "speechiness / mood_* confidences all in [0, 1] on the "
        "latest analysis_version.",
        catches="generic numerical drift in any unit-interval head",
        severity="error",
        sql=(
            "SELECT COUNT(*) FROM track_features "
            f"WHERE analysis_version = {_LATEST_VERSION} "
            "AND analysis_error IS NULL "
            "AND ((energy IS NOT NULL AND (energy < 0 OR energy > 1)) "
            "  OR (danceability IS NOT NULL AND (danceability < 0 OR danceability > 1)) "
            "  OR (acousticness IS NOT NULL AND (acousticness < 0 OR acousticness > 1)) "
            "  OR (instrumentalness IS NOT NULL AND (instrumentalness < 0 OR instrumentalness > 1)) "
            "  OR (speechiness IS NOT NULL AND (speechiness < 0 OR speechiness > 1)))"
        ),
        kind="expect_zero",
    ),
    # Pre-version stale-row warning — surfaces the case where rows from
    # earlier analysis_versions still have visibly broken values that the
    # rescan hasn't reached yet. WARN severity because it's expected
    # during migration; only failure-relevant if it persists after the
    # rescan completes.
    Invariant(
        name="loudness_in_lufs_range_any_version",
        description="Loudness in [-100, 0] LUFS across the *entire* library, "
        "including pre-current-version stale rows. Often noisy "
        "during a rescan. Cross-reference latest_version_coverage.",
        catches="lingering pre-fix stale rows / failed migration",
        severity="warn",
        sql=(
            "SELECT COUNT(*) FROM track_features "
            "WHERE analysis_error IS NULL AND analyzed_at IS NOT NULL "
            "AND loudness IS NOT NULL AND (loudness > 0 OR loudness < -100)"
        ),
        kind="expect_zero",
    ),
    # ---- NULL leaks: catches #42, #83 -------------------------------------
    Invariant(
        name="embedding_coverage_for_latest_version",
        description="Successfully analyzed tracks at the latest analysis_version "
        "must have an embedding. NULL on success = the embedding "
        "builder's degenerate-norm guard fired (or the wrong ONNX "
        "output was used).",
        catches="#42 zero-norm hidden as base64; #83 wrong ONNX output index",
        severity="error",
        sql=(
            "SELECT COUNT(*) FROM track_features "
            "WHERE analyzed_at IS NOT NULL AND analysis_error IS NULL "
            f"AND analysis_version = {_LATEST_VERSION} "
            "AND embedding IS NULL"
        ),
        kind="expect_zero",
    ),
    # ---- Distribution / inversion: catches #99 + future #88-shapes --------
    Invariant(
        name="instrumentalness_avg_below_voice_majority",
        description="Library-wide AVG(instrumentalness) on the latest version "
        "should be < 0.5. EffNet's voice/instrumental head puts "
        "most music in the voice class, so inverting the column "
        "read pushes the stored 'instrumentalness' average above "
        "0.5. Pre-fix today: 0.68. Post-#99: ~0.32.",
        catches="#99 voice_instrumental column inversion",
        severity="error",
        sql=(
            "SELECT AVG(instrumentalness) FROM track_features "
            f"WHERE analysis_version = {_LATEST_VERSION} AND analysis_error IS NULL"
        ),
        kind="expect_in_range",
        bounds=(0.05, 0.5),
        skip_if_total_below=100,
    ),
    Invariant(
        name="valence_distribution_not_collapsed",
        description="AVG(valence) on the latest version should be in [0.2, 0.8]. "
        "Outside that means a head got pinned at one extreme — pre-#88 "
        "valence avg was 0.018 across 67k tracks (mood_happy compression).",
        catches="#88-style head-output compression",
        severity="warn",
        sql=(
            "SELECT AVG(valence) FROM track_features "
            f"WHERE analysis_version = {_LATEST_VERSION} AND analysis_error IS NULL"
        ),
        kind="expect_in_range",
        bounds=(0.2, 0.8),
        skip_if_total_below=100,
    ),
    Invariant(
        name="danceability_distribution_not_pinned_high",
        description="AVG(danceability) should be in [0.3, 0.85]. The EffNet "
        "danceability head has a strong 'danceable' prior — values "
        "above 0.85 indicate the same shape of bug as #88's mood_happy "
        "compression, just at the high end.",
        catches="EffNet-head bias / dead-feature detection",
        severity="warn",
        sql=(
            "SELECT AVG(danceability) FROM track_features "
            f"WHERE analysis_version = {_LATEST_VERSION} AND analysis_error IS NULL"
        ),
        kind="expect_in_range",
        bounds=(0.3, 0.85),
        skip_if_total_below=100,
    ),
    # ---- Migration health -------------------------------------------------
    Invariant(
        name="latest_version_coverage",
        description="A non-trivial fraction of the library should be on the "
        "latest analysis_version. <1% means a rescan stalled or "
        "the version bump never triggered re-analysis.",
        catches="rescan stuck / version bump never triggered",
        severity="warn",
        sql=(
            "SELECT CAST(SUM(CASE WHEN analysis_version = "
            f"{_LATEST_VERSION} AND analysis_error IS NULL "
            "THEN 1 ELSE 0 END) AS REAL) / NULLIF(COUNT(*), 0) "
            "FROM track_features"
        ),
        kind="expect_at_least",
        bounds=0.01,
        skip_if_total_below=100,
    ),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_invariants(session: AsyncSession) -> list[Result]:
    """Apply every invariant against the DB. Never raises; query failures
    are encoded in ``Result.status="error"``."""
    total = (await session.execute(select(func.count(TrackFeatures.id)))).scalar() or 0

    results: list[Result] = []
    for inv in INVARIANTS:
        if inv.skip_if_total_below and total < inv.skip_if_total_below:
            results.append(
                Result(
                    invariant=inv,
                    status="skipped",
                    actual=None,
                    message=f"library too small ({total} rows < threshold {inv.skip_if_total_below})",
                )
            )
            continue
        try:
            row = (await session.execute(text(inv.sql))).fetchone()
            value = row[0] if row else None
            results.append(_evaluate(inv, value))
        except Exception as e:
            results.append(
                Result(
                    invariant=inv,
                    status="error",
                    actual=None,
                    message=f"query failed: {type(e).__name__}: {e}",
                )
            )
    return results


def _evaluate(inv: Invariant, value: Any) -> Result:
    if value is None:
        return Result(
            invariant=inv,
            status="skipped",
            actual=None,
            message="query returned NULL (no rows match the scope)",
        )

    numeric = float(value)

    if inv.kind == "expect_zero":
        ok = numeric == 0
        msg = f"got {value}, expected 0"
    elif inv.kind == "expect_in_range":
        if not isinstance(inv.bounds, tuple):
            return Result(
                invariant=inv,
                status="error",
                actual=value,
                message=f"{inv.name}: expect_in_range needs (lo, hi) tuple bounds, got {inv.bounds!r}",
            )
        lo, hi = inv.bounds
        ok = lo <= numeric <= hi
        msg = f"got {numeric:.4f}, expected in [{lo}, {hi}]"
    elif inv.kind == "expect_at_least":
        if not isinstance(inv.bounds, (int, float)):
            return Result(
                invariant=inv,
                status="error",
                actual=value,
                message=f"{inv.name}: expect_at_least needs scalar bounds, got {inv.bounds!r}",
            )
        ok = numeric >= inv.bounds
        msg = f"got {numeric:.4f}, expected ≥ {inv.bounds}"
    else:
        return Result(invariant=inv, status="error", actual=value, message=f"unknown kind: {inv.kind}")

    return Result(
        invariant=inv,
        status="ok" if ok else "fail",
        actual=value,
        message=msg,
    )


def overall_status(results: list[Result]) -> str:
    """Roll up per-invariant results into one of ``ok | warn | fail``.

    Any error-severity failure → ``fail``. Warn-severity failures alone
    → ``warn``. Otherwise → ``ok`` (skipped invariants don't degrade
    the library state).
    """
    has_error_fail = any(r.status == "fail" and r.invariant.severity == "error" for r in results)
    if has_error_fail:
        return "fail"
    has_warn_fail = any(r.status == "fail" and r.invariant.severity == "warn" for r in results)
    if has_warn_fail:
        return "warn"
    return "ok"
