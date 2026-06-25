"""
GrooveIQ – Playlist generation service.

Implements four strategies for building playlists from analyzed tracks:
  1. Flow       — greedy chain from seed track, smooth BPM/energy transitions
  2. Mood       — filter by dominant mood tag, order by energy arc
  3. Energy     — match tracks to a target energy curve profile
  4. Key-compat — chain harmonically compatible keys (Camelot wheel)

All strategies use the 64-dim embedding vectors for cosine similarity
when breaking ties or filling gaps.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Playlist, PlaylistTrack, TrackFeatures, TrackInteraction

logger = logging.getLogger(__name__)

# Optional: pin BLAS to a single thread for the text-ranking matmul so it can't
# fan out across every core and starve the other asyncio.to_thread users
# (scanner walk, analysis worker, FAISS build). Falls back to a no-op when
# threadpoolctl isn't installed — the matmul is a single (N,512)·(512,) matvec,
# so single-threaded BLAS is plenty and the pin is mostly anti-starvation
# insurance.
try:
    from threadpoolctl import threadpool_limits as _threadpool_limits
except Exception:  # pragma: no cover - threadpoolctl ships transitively via sklearn
    _threadpool_limits = None


def _blas_limit():
    """Context manager that caps BLAS threads at 1 (no-op if threadpoolctl absent)."""
    if _threadpool_limits is not None:
        return _threadpool_limits(limits=1)
    return contextlib.nullcontext()


# Serialize the offloaded text-ranking work. With a single uvicorn worker, the
# "Playlists for You" shelf fires ~6 text generations near-simultaneously; left
# unbounded each would load ~144k CLAP rows (~400 MB) and run a full-catalog
# matmul at once, spiking memory and holding DB connections — the exact pool
# exhaustion this fix targets. One-at-a-time keeps peak memory and BLAS usage
# bounded while the event loop stays free (callers await the semaphore).
_TEXT_GEN_CONCURRENCY = 1
_text_sem: asyncio.Semaphore | None = None
_text_sem_loop: asyncio.AbstractEventLoop | None = None


def _text_gen_semaphore() -> asyncio.Semaphore:
    """Lazily create the text-gen semaphore, rebinding if the running loop
    changed (each pytest-asyncio test gets a fresh loop; prod has exactly one)."""
    global _text_sem, _text_sem_loop
    loop = asyncio.get_running_loop()
    if _text_sem is None or _text_sem_loop is not loop:
        _text_sem = asyncio.Semaphore(_TEXT_GEN_CONCURRENCY)
        _text_sem_loop = loop
    return _text_sem


TASTE_ALPHA = 0.5  # how hard taste tilts each strategy's score. 0 = off, ~0.5 keeps
# the strategy's intrinsic objective (BPM/energy/key/CLAP) in charge. Tune later.

# Upper bound on how many of the user's tracks carry a taste weight — keeps the
# lookup cheap regardless of history size.
_TASTE_POOL_CAP = 8000


async def _taste_weights(
    user_id: str,
    session: AsyncSession,
    *,
    cap: int = _TASTE_POOL_CAP,
) -> dict[str, float]:
    """Per-track taste weight in [0,1] = the user's own engagement
    (``satisfaction_score``) for tracks they've actually played.

    Tracks the user hasn't engaged with are simply absent, so ``.get(tid, 0.0)``
    makes them a no-op in the blend — the mix therefore tilts toward FAMILIAR
    music, which is the goal of personalization here. A cold user with no
    interactions yields ``{}`` (clean no-op).

    This reads the per-user interaction labels directly (bounded by ``cap``)
    rather than scoring the whole ~180k-track catalog through the ranker: that
    earlier approach 500'd on the real catalog (one SQL ``IN(...)`` over every id
    exceeded asyncpg's 32767 bind-param limit) and, restricted to engaged tracks,
    min-max-collapsed to no boost when a user's loved tracks scored alike.
    """
    rows = await session.execute(
        select(TrackInteraction.track_id, TrackInteraction.satisfaction_score)
        .where(TrackInteraction.user_id == user_id)
        .order_by(TrackInteraction.last_played_at.desc())
        .limit(cap)
    )
    weights: dict[str, float] = {}
    for tid, sat in rows.all():
        if sat is not None and sat > 0.0:
            weights[tid] = float(sat)
    return weights


class PlaylistServiceUnavailableError(Exception):
    """A strategy could not run for a transient/operational reason.

    Distinguishes "the request was fine but the service isn't ready" (CLAP
    disabled, no embeddings backfilled yet, model failed to load) from "the
    user supplied bad params" (missing prompt, unknown curve, etc.). The
    route maps this to HTTP 503; plain ``ValueError`` continues to map to
    HTTP 400. See issue #91 / #89-followup.
    """


def utc_day_bucket(now: datetime | None = None) -> str:
    """Current UTC day as ``YYYY-MM-DD`` — the time bucket for the playlist cache."""
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


def compute_cache_key(
    *,
    created_by: str | None,
    strategy: str,
    seed_track_id: str | None,
    params: dict[str, Any] | None,
    max_tracks: int,
    bucket_date: str,
    user_id: str | None = None,
) -> str:
    """Stable per-day idempotency key for a playlist generation request.

    Excludes ``name`` so the frontend can vary the title without busting the
    cache. ``params`` is canonicalised via ``sort_keys`` so dict ordering can't
    produce a different hash for the same logical request. See issue #89.

    ``user_id`` segments the cache per personalized user; the ``or ""`` default
    keeps the hash byte-identical for legacy (user-agnostic) callers and also
    fixes a latent bug where two users sharing one API key collided on a single
    cached playlist.
    """
    payload = {
        "owner": created_by or "",
        "user": user_id or "",
        "strategy": strategy,
        "seed": seed_track_id or "",
        "params": params or {},
        "max_tracks": max_tracks,
        "day": bucket_date,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_embedding(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype=np.float32)


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom < 1e-9:
        return 0.0
    return float(np.dot(a, b) / denom)


async def _load_tracks(session: AsyncSession) -> list[TrackFeatures]:
    """Load all analyzed tracks with non-null embeddings."""
    result = await session.execute(
        select(TrackFeatures).where(TrackFeatures.embedding.isnot(None)).where(TrackFeatures.analysis_error.is_(None))
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Camelot wheel for harmonic mixing
# ---------------------------------------------------------------------------

# Maps (key, mode) → Camelot code (number 1–12, letter A/B)
_CAMELOT = {
    ("Ab", "minor"): (1, "A"),
    ("B", "major"): (1, "B"),
    ("Eb", "minor"): (2, "A"),
    ("F#", "major"): (2, "B"),
    ("Bb", "minor"): (3, "A"),
    ("C#", "major"): (3, "B"),
    ("Db", "major"): (3, "B"),
    ("F", "minor"): (4, "A"),
    ("Ab", "major"): (4, "B"),
    ("C", "minor"): (5, "A"),
    ("Eb", "major"): (5, "B"),
    ("G", "minor"): (6, "A"),
    ("Bb", "major"): (6, "B"),
    ("D", "minor"): (7, "A"),
    ("F", "major"): (7, "B"),
    ("A", "minor"): (8, "A"),
    ("C", "major"): (8, "B"),
    ("E", "minor"): (9, "A"),
    ("G", "major"): (9, "B"),
    ("B", "minor"): (10, "A"),
    ("D", "major"): (10, "B"),
    ("F#", "minor"): (11, "A"),
    ("A", "major"): (11, "B"),
    ("C#", "minor"): (12, "A"),
    ("E", "major"): (12, "B"),
    ("Db", "minor"): (12, "A"),
}


def _camelot_compatible(code1: tuple[int, str], code2: tuple[int, str], max_distance: int = 1) -> bool:
    """Check if two Camelot codes are harmonically compatible."""
    n1, l1 = code1
    n2, l2 = code2
    # Same code
    if n1 == n2 and l1 == l2:
        return True
    # Same number, different letter (relative major/minor)
    if n1 == n2 and l1 != l2:
        return True
    # Adjacent numbers, same letter
    if l1 == l2:
        diff = min(abs(n1 - n2), 12 - abs(n1 - n2))
        return diff <= max_distance
    return False


# ---------------------------------------------------------------------------
# Strategy: Flow
# ---------------------------------------------------------------------------


def _generate_flow(
    tracks: list[TrackFeatures], seed_id: str, max_tracks: int, taste: dict[str, float] | None = None
) -> list[str]:
    """Greedy chain from seed, preferring smooth BPM/energy transitions."""
    seed = next((t for t in tracks if t.track_id == seed_id), None)
    if not seed:
        raise ValueError(f"Seed track '{seed_id}' not found in analyzed tracks")

    # Pre-decode all embeddings
    embeddings = {}
    for t in tracks:
        if t.embedding:
            try:
                embeddings[t.track_id] = _decode_embedding(t.embedding)
            except Exception:
                pass

    result = [seed.track_id]
    used = {seed.track_id}
    current = seed

    for _ in range(max_tracks - 1):
        best_id = None
        best_score = -1.0

        cur_bpm = current.bpm or 120
        cur_energy = current.energy or 0.5

        for t in tracks:
            if t.track_id in used:
                continue

            # BPM filter: prefer within ±8, allow up to ±15
            bpm_diff = abs((t.bpm or 120) - cur_bpm)
            if bpm_diff > 15:
                continue
            bpm_bonus = 1.0 if bpm_diff <= 8 else 0.7

            # Energy filter: prefer within ±0.15, allow up to ±0.25
            energy_diff = abs((t.energy or 0.5) - cur_energy)
            if energy_diff > 0.25:
                continue
            energy_bonus = 1.0 if energy_diff <= 0.15 else 0.7

            # Cosine similarity
            cos = 0.5
            if current.track_id in embeddings and t.track_id in embeddings:
                cos = _cosine_sim(embeddings[current.track_id], embeddings[t.track_id])

            score = cos * bpm_bonus * energy_bonus * (1 + TASTE_ALPHA * (taste or {}).get(t.track_id, 0.0))
            if score > best_score:
                best_score = score
                best_id = t.track_id

        if best_id is None:
            break

        result.append(best_id)
        used.add(best_id)
        current = next(t for t in tracks if t.track_id == best_id)

    return result


# ---------------------------------------------------------------------------
# Strategy: Path (sonic bridge between two tracks)
# ---------------------------------------------------------------------------


def _slerp(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    """
    Spherical linear interpolation between two unit-norm vectors.

    Embeddings live on the unit sphere (L2-normalised) so linear interpolation
    pulls waypoints toward the origin and distorts similarity. Slerp walks the
    great-circle arc between ``a`` and ``b`` instead, keeping every waypoint
    on the sphere.
    """
    a_n = a / (np.linalg.norm(a) + 1e-9)
    b_n = b / (np.linalg.norm(b) + 1e-9)
    dot = float(np.clip(np.dot(a_n, b_n), -1.0, 1.0))
    # If vectors are almost colinear, fall back to linear interp (slerp degenerate).
    if abs(dot) > 0.9995:
        return (1.0 - t) * a_n + t * b_n
    omega = np.arccos(dot)
    sin_omega = np.sin(omega)
    return (np.sin((1.0 - t) * omega) / sin_omega) * a_n + (np.sin(t * omega) / sin_omega) * b_n


def _generate_path(
    tracks: list[TrackFeatures],
    from_id: str,
    to_id: str,
    max_tracks: int,
    taste: dict[str, float] | None = None,
) -> list[str]:
    """
    Build a sonic bridge from ``from_id`` to ``to_id``.

    Walks the great-circle arc between the two tracks' embeddings in
    ``max_tracks - 2`` equal steps. At each waypoint, picks the nearest
    not-yet-used library track by cosine similarity. The endpoints of the
    returned list are always the seed and target tracks themselves.

    Returns ``[from_id, waypoint_1, ..., waypoint_{N-2}, to_id]``.
    """
    from_track = next((t for t in tracks if t.track_id == from_id), None)
    to_track = next((t for t in tracks if t.track_id == to_id), None)
    if from_track is None:
        raise ValueError(f"Seed track '{from_id}' not found in analyzed tracks")
    if to_track is None:
        raise ValueError(f"Target track '{to_id}' not found in analyzed tracks")
    if from_id == to_id:
        raise ValueError("seed_track_id and target_track_id must differ")
    if not from_track.embedding or not to_track.embedding:
        raise ValueError("Both seed and target tracks need an embedding")
    if max_tracks < 3:
        # With <3 we'd only have endpoints — no "path" to speak of.
        raise ValueError("'path' strategy requires max_tracks >= 3")

    # Pre-decode every candidate embedding once.
    embeddings: dict[str, np.ndarray] = {}
    for t in tracks:
        if t.embedding:
            try:
                vec = _decode_embedding(t.embedding)
                norm = np.linalg.norm(vec)
                if norm > 1e-9:
                    embeddings[t.track_id] = vec / norm
            except Exception:
                pass

    a = embeddings.get(from_id)
    b = embeddings.get(to_id)
    if a is None or b is None:
        raise ValueError("Failed to decode embedding for seed or target")

    # N intermediate waypoints evenly spaced on the arc (endpoints excluded).
    n_waypoints = max_tracks - 2
    waypoints = [_slerp(a, b, (i + 1) / (n_waypoints + 1)) for i in range(n_waypoints)]

    used: set[str] = {from_id, to_id}
    picks: list[str] = []

    for wp in waypoints:
        best_id = None
        best_score = -2.0
        for tid, vec in embeddings.items():
            if tid in used:
                continue
            score = float(np.dot(wp, vec)) * (
                1 + TASTE_ALPHA * (taste or {}).get(tid, 0.0)
            )  # both unit-norm → cosine, biased toward loved tracks
            if score > best_score:
                best_score = score
                best_id = tid
        if best_id is None:
            break  # ran out of candidates
        used.add(best_id)
        picks.append(best_id)

    return [from_id, *picks, to_id]


# ---------------------------------------------------------------------------
# Strategy: Text (natural-language prompt → CLAP similarity ranking)
# ---------------------------------------------------------------------------


def _generate_text(
    tracks: list[TrackFeatures],
    prompt: str,
    max_tracks: int,
    taste: dict[str, float] | None = None,
) -> list[str]:
    """
    Rank every CLAP-embedded track by cosine similarity to the prompt.

    Requires ``CLAP_ENABLED=true`` and CLAP embeddings populated on tracks.
    Raises ``ValueError`` (→ 400) when CLAP isn't available so the API
    surfaces the misconfiguration cleanly.

    NOTE: this Python-loop implementation is no longer on the request hot path —
    ``generate_playlist`` routes ``strategy='text'`` through the vectorized,
    thread-offloaded :func:`_generate_text_vectorized` (which never blocks the
    event loop and never hydrates full ORM rows). It is retained as the
    readable reference and as the oracle the parity test checks the vectorized
    ranking against.
    """
    from app.core.config import settings

    if not settings.CLAP_ENABLED:
        raise PlaylistServiceUnavailableError("'text' strategy requires CLAP_ENABLED=true")
    if not prompt or not prompt.strip():
        raise ValueError("params.prompt is required for 'text' strategy")

    from app.services import clap_text

    try:
        query_vec = clap_text.encode_text(prompt)
    except Exception as e:
        raise PlaylistServiceUnavailableError(f"CLAP text encoding failed: {e}") from e

    scored: list[tuple[float, str]] = []
    for t in tracks:
        if not t.clap_embedding:
            continue
        try:
            vec = np.frombuffer(base64.b64decode(t.clap_embedding), dtype=np.float32)
            if vec.size != query_vec.size:
                continue
            norm = np.linalg.norm(vec)
            if norm < 1e-9:
                continue
            cos = float(np.dot(query_vec, vec / norm))
            cos *= 1 + TASTE_ALPHA * (taste or {}).get(t.track_id, 0.0)
            scored.append((cos, t.track_id))
        except Exception:
            continue

    if not scored:
        raise PlaylistServiceUnavailableError(
            "No tracks have CLAP embeddings yet. The CLAP audio backfill is still "
            "running — poll GET /v1/tracks/clap/stats for coverage, or trigger "
            "POST /v1/tracks/clap/backfill (admin) if it has not started."
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    return [tid for _, tid in scored[:max_tracks]]


_NO_CLAP_MESSAGE = (
    "No tracks have CLAP embeddings yet. The CLAP audio backfill is still "
    "running — poll GET /v1/tracks/clap/stats for coverage, or trigger "
    "POST /v1/tracks/clap/backfill (admin) if it has not started."
)


def _generate_text_vectorized(
    track_ids: list[str],
    clap_b64s: list[str],
    query_vec: np.ndarray,
    taste: dict[str, float],
    max_tracks: int,
) -> list[str]:
    """Vectorized equivalent of :func:`_generate_text` — runs in a worker thread.

    Receives only plain values (no ORM rows, no AsyncSession) so it is safe to
    hand to ``asyncio.to_thread``. Decodes all CLAP payloads into one contiguous
    ``(N, dim)`` matrix, then does a single matvec + per-row normalize + taste
    multiply + ``argpartition`` top-k instead of ~144k Python iterations each
    doing their own ``norm``/``dot``. BLAS is pinned to one thread for the
    duration. Selection is deterministic: score desc, then ``track_id`` asc.

    Parity contract (locked by ``test_text_vectorized_matches_reference``): for
    any input this returns the same ranking the reference loop would, modulo the
    explicit ``track_id`` tiebreak on exact score ties.
    """
    dim = int(query_vec.size)
    expected_bytes = dim * 4  # float32

    # Decode once. Skip undecodable / wrong-sized payloads, matching the
    # reference loop's `vec.size != query_vec.size: continue` guard.
    raw_parts: list[bytes] = []
    valid_ids: list[str] = []
    for tid, b64 in zip(track_ids, clap_b64s):
        if not b64:
            continue
        try:
            raw = base64.b64decode(b64)
        except Exception:
            continue
        if len(raw) != expected_bytes:
            continue
        raw_parts.append(raw)
        valid_ids.append(tid)

    if not valid_ids:
        return []

    with _blas_limit():
        q = np.ascontiguousarray(query_vec, dtype=np.float32)
        mat = np.frombuffer(b"".join(raw_parts), dtype=np.float32).reshape(len(valid_ids), dim)

        dots = mat @ q  # (N,)
        norms = np.linalg.norm(mat, axis=1)  # (N,)
        safe = norms > 1e-9
        # cosine where the row has a usable norm, -inf elsewhere (so zero-norm
        # rows can never win — same exclusion the reference loop applies).
        cosine = np.full(len(valid_ids), -np.inf, dtype=np.float64)
        np.divide(dots, norms, out=cosine, where=safe)

        if taste:
            taste_mult = np.fromiter(
                (1.0 + TASTE_ALPHA * taste.get(tid, 0.0) for tid in valid_ids),
                dtype=np.float64,
                count=len(valid_ids),
            )
            cosine *= taste_mult  # taste >= 0 → multiplier >= 1 > 0, so -inf stays -inf

    # Deterministic total order: cosine desc, then track_id asc. The tiebreak
    # must drive *selection*, not just the final ordering — argpartition would
    # pick an arbitrary subset among exact ties (e.g. when every candidate has
    # the same embedding). lexsort's last key is primary, so primary = -cosine
    # (ascending ⇒ cosine descending) with track_id ascending as the tiebreak;
    # zero-norm (-inf cosine ⇒ +inf key) rows sort to the very end.
    ids_arr = np.asarray(valid_ids)
    order = np.lexsort((ids_arr, -cosine))

    k = min(max_tracks, len(valid_ids))
    selected: list[str] = []
    for i in order[: max(k, 0)]:
        if not np.isfinite(cosine[i]):
            break  # remaining rows are all -inf (zero-norm); exclude them
        selected.append(valid_ids[int(i)])
    return selected


async def _load_text_candidates(session: AsyncSession) -> list[tuple[str, str]]:
    """Load only ``(track_id, clap_embedding)`` for tracks that carry a CLAP vector.

    Deliberately does NOT call :func:`_load_tracks`, which hydrates full ORM rows
    for every analyzed track across all wide columns (Text embeddings, JSON
    mood_tags/raw_features, lyrics, ...) — that full-table hydration is the
    original event-loop blocker / pool exhauster. Selecting two columns as Core
    rows skips ORM hydration entirely; the base64 strings are decoded later in a
    worker thread.
    """
    result = await session.execute(
        select(TrackFeatures.track_id, TrackFeatures.clap_embedding).where(TrackFeatures.clap_embedding.isnot(None))
    )
    return [(tid, emb) for tid, emb in result.all()]


async def _durations_for(session: AsyncSession, track_ids: list[str]) -> dict[str, float]:
    """Fetch durations for just the selected ids (<= max_tracks rows).

    The text path never loads the full catalog, so it can't build the duration
    map from in-memory rows like the other strategies — it queries the handful
    of chosen tracks instead.
    """
    if not track_ids:
        return {}
    result = await session.execute(
        select(TrackFeatures.track_id, TrackFeatures.duration).where(TrackFeatures.track_id.in_(track_ids))
    )
    return {tid: (dur or 0) for tid, dur in result.all()}


async def _generate_text_offloaded(
    session: AsyncSession,
    prompt: str,
    max_tracks: int,
    taste: dict[str, float],
) -> list[str]:
    """Async wrapper for the text strategy: validate + encode on the loop, then
    run the catalog-wide ranking in a worker thread under the concurrency gate.

    The event loop only ever does small, bounded work here (CLAP text encode +
    a 2-column candidate query); the ~144k-row decode/matmul is offloaded so it
    can't block the single uvicorn worker's loop. The semaphore is held across
    the candidate load *and* the offload so peak memory / connection pressure is
    one request's worth at a time, not the whole "Playlists for You" burst.
    """
    from app.core.config import settings

    if not settings.CLAP_ENABLED:
        raise PlaylistServiceUnavailableError("'text' strategy requires CLAP_ENABLED=true")
    if not prompt or not prompt.strip():
        raise ValueError("params.prompt is required for 'text' strategy")

    from app.services import clap_text

    # Encode on the event loop (not in the worker thread): this single-flights
    # the CLAP model load via clap_text._load's double-checked lock and keeps the
    # lru_cache off any concurrent-call path. See app/services/clap_text.py.
    try:
        query_vec = clap_text.encode_text(prompt)
    except Exception as e:
        raise PlaylistServiceUnavailableError(f"CLAP text encoding failed: {e}") from e

    async with _text_gen_semaphore():
        candidates = await _load_text_candidates(session)
        if not candidates:
            raise PlaylistServiceUnavailableError(_NO_CLAP_MESSAGE)
        track_ids = [tid for tid, _ in candidates]
        clap_b64s = [emb for _, emb in candidates]
        logger.info("Generating 'text' playlist from %d CLAP candidates (offloaded)", len(candidates))
        selected = await asyncio.to_thread(
            _generate_text_vectorized, track_ids, clap_b64s, query_vec, taste, max_tracks
        )

    if not selected:
        # Candidates existed but none decoded to a usable vector.
        raise PlaylistServiceUnavailableError(_NO_CLAP_MESSAGE)
    return selected


# ---------------------------------------------------------------------------
# Strategy: Mood
# ---------------------------------------------------------------------------


def _generate_mood(
    tracks: list[TrackFeatures], mood: str, max_tracks: int, taste: dict[str, float] | None = None
) -> list[str]:
    """Filter tracks by mood tag confidence, order by energy arc."""
    from app.services.audio_analysis import SUPPORTED_MOOD_LABELS

    if mood not in SUPPORTED_MOOD_LABELS:
        # Belt-and-braces: PlaylistCreate already validates this at the
        # API boundary, but the service is also reachable via internal
        # callers / tests, so reject defensively here too.
        raise ValueError(f"Mood {mood!r} is not supported. Must be one of: {sorted(SUPPORTED_MOOD_LABELS)}.")

    scored = []
    for t in tracks:
        if not t.mood_tags:
            continue
        tags = t.mood_tags if isinstance(t.mood_tags, list) else []
        conf = 0.0
        for tag in tags:
            if isinstance(tag, dict) and tag.get("label") == mood:
                conf = tag.get("confidence", 0.0)
                break
        if conf > 0.3:
            scored.append((conf, t))

    if not scored:
        raise ValueError(f"No tracks found with mood '{mood}' (confidence > 0.3)")

    # Sort by confidence (taste-biased), take top candidates. Taste only changes
    # which high-confidence tracks survive the truncation, not the arc shape below.
    scored.sort(key=lambda x: x[0] * (1 + TASTE_ALPHA * (taste or {}).get(x[1].track_id, 0.0)), reverse=True)
    candidates = [t for _, t in scored[:max_tracks]]

    # Re-order for pleasant listening: energy ramp up then cool down
    mid = len(candidates) // 2
    first_half = sorted(candidates[:mid], key=lambda t: t.energy or 0)
    second_half = sorted(candidates[mid:], key=lambda t: t.energy or 0, reverse=True)

    return [t.track_id for t in first_half + second_half]


# ---------------------------------------------------------------------------
# Strategy: Energy Curve
# ---------------------------------------------------------------------------


def _generate_energy_curve(
    tracks: list[TrackFeatures], curve: str, max_tracks: int, taste: dict[str, float] | None = None
) -> list[str]:
    """Match tracks to a target energy profile."""
    n = min(max_tracks, len(tracks))

    # Build target energy array
    if curve == "ramp_up":
        targets = np.linspace(0.2, 0.9, n)
    elif curve == "cool_down":
        targets = np.linspace(0.9, 0.2, n)
    elif curve == "ramp_up_cool_down":
        peak = int(n * 0.6)
        targets = np.concatenate([np.linspace(0.2, 0.9, peak), np.linspace(0.85, 0.3, n - peak)])
    elif curve == "steady_high":
        targets = np.full(n, 0.8) + np.random.uniform(-0.05, 0.05, n)
    elif curve == "steady_low":
        targets = np.full(n, 0.3) + np.random.uniform(-0.05, 0.05, n)
    else:
        targets = np.linspace(0.2, 0.9, n)

    # Pre-decode embeddings
    embeddings = {}
    for t in tracks:
        if t.embedding:
            try:
                embeddings[t.track_id] = _decode_embedding(t.embedding)
            except Exception:
                pass

    result = []
    used = set()
    prev_embed = None

    for target_energy in targets:
        best_id = None
        best_score = float("inf")

        for t in tracks:
            if t.track_id in used:
                continue
            energy = t.energy or 0.5
            energy_dist = abs(energy - target_energy)

            # Tiebreak by similarity to previous track
            sim_bonus = 0.0
            if prev_embed is not None and t.track_id in embeddings:
                sim_bonus = _cosine_sim(prev_embed, embeddings[t.track_id]) * 0.1

            # MINIMIZER: lower score wins, so taste is SUBTRACTED (loved → lower cost).
            score = energy_dist - sim_bonus - TASTE_ALPHA * (taste or {}).get(t.track_id, 0.0)
            if score < best_score:
                best_score = score
                best_id = t.track_id

        if best_id is None:
            break

        result.append(best_id)
        used.add(best_id)
        prev_embed = embeddings.get(best_id)

    return result


# ---------------------------------------------------------------------------
# Strategy: Key-Compatible (Camelot)
# ---------------------------------------------------------------------------


def _generate_key_compatible(
    tracks: list[TrackFeatures], seed_id: str, max_tracks: int, taste: dict[str, float] | None = None
) -> list[str]:
    """Chain tracks using Camelot wheel harmonic compatibility."""
    seed = next((t for t in tracks if t.track_id == seed_id), None)
    if not seed:
        raise ValueError(f"Seed track '{seed_id}' not found in analyzed tracks")

    # Pre-decode embeddings and map Camelot codes
    embeddings = {}
    camelot = {}
    for t in tracks:
        if t.embedding:
            try:
                embeddings[t.track_id] = _decode_embedding(t.embedding)
            except Exception:
                pass
        if t.key and t.mode:
            code = _CAMELOT.get((t.key, t.mode))
            if code:
                camelot[t.track_id] = code

    result = [seed.track_id]
    used = {seed.track_id}
    current_code = camelot.get(seed.track_id)

    for _ in range(max_tracks - 1):
        best_id = None
        best_score = -1.0

        for t in tracks:
            if t.track_id in used:
                continue

            t_code = camelot.get(t.track_id)

            # Key compatibility scoring
            if current_code and t_code:
                if not _camelot_compatible(current_code, t_code, max_distance=2):
                    continue
                key_bonus = 1.0 if _camelot_compatible(current_code, t_code, max_distance=1) else 0.6
            else:
                key_bonus = 0.3  # no key data, low priority

            # Cosine similarity
            cos = 0.5
            cur_id = result[-1]
            if cur_id in embeddings and t.track_id in embeddings:
                cos = _cosine_sim(embeddings[cur_id], embeddings[t.track_id])

            score = cos * key_bonus * (1 + TASTE_ALPHA * (taste or {}).get(t.track_id, 0.0))
            if score > best_score:
                best_score = score
                best_id = t.track_id

        if best_id is None:
            break

        result.append(best_id)
        used.add(best_id)
        current_code = camelot.get(best_id, current_code)

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_playlist(
    session: AsyncSession,
    name: str,
    strategy: str,
    seed_track_id: str | None,
    params: dict[str, Any] | None,
    max_tracks: int,
    user_id: str | None = None,
) -> Playlist:
    """
    Generate a playlist and persist it.
    Returns the created Playlist with track_count and total_duration set.

    When ``user_id`` is given, each strategy's per-candidate score is biased by
    the user's taste (via the recommend ranker) so the playlist prefers loved
    tracks while the strategy's sonic objective still dictates order. With
    ``user_id=None`` the behavior is byte-identical to the user-agnostic path.
    """
    # Taste-weight map (per-user engagement) folded into every strategy's score.
    # Bounded by construction (the user's own interactions, not the whole catalog)
    # and biased toward familiar tracks. Empty (no-op) for the user-agnostic /
    # cold-user path.
    taste = await _taste_weights(user_id, session) if user_id else {}

    if strategy == "text":
        # Scoped, vectorized, thread-offloaded path. It never hydrates the full
        # ~180k wide ORM rows and never blocks the event loop with the
        # catalog-wide CLAP scan — the two confirmed causes of the playlist 500s
        # / API-wide pool exhaustion. Duration is fetched for just the chosen ids.
        prompt = (params or {}).get("prompt", "")
        track_ids = await _generate_text_offloaded(session, prompt, max_tracks, taste=taste)
        dur_map = await _durations_for(session, track_ids)
    else:
        tracks = await _load_tracks(session)
        if len(tracks) < 5:
            raise ValueError(f"Not enough analyzed tracks ({len(tracks)}). Need at least 5.")

        logger.info(f"Generating '{strategy}' playlist '{name}' from {len(tracks)} tracks")

        # Dispatch to strategy
        if strategy == "flow":
            track_ids = _generate_flow(tracks, seed_track_id, max_tracks, taste=taste)
        elif strategy == "mood":
            mood = (params or {}).get("mood", "happy")
            track_ids = _generate_mood(tracks, mood, max_tracks, taste=taste)
        elif strategy == "energy_curve":
            curve = (params or {}).get("curve", "ramp_up_cool_down")
            track_ids = _generate_energy_curve(tracks, curve, max_tracks, taste=taste)
        elif strategy == "key_compatible":
            track_ids = _generate_key_compatible(tracks, seed_track_id, max_tracks, taste=taste)
        elif strategy == "path":
            target = (params or {}).get("target_track_id")
            track_ids = _generate_path(tracks, seed_track_id, target, max_tracks, taste=taste)
        else:
            raise ValueError(f"Unknown strategy: {strategy}")

        dur_map = {t.track_id: t.duration or 0 for t in tracks}

    if not track_ids:
        raise ValueError("Strategy produced no tracks")

    # Compute total duration
    total_dur = sum(dur_map.get(tid, 0) for tid in track_ids)

    # Persist
    playlist = Playlist(
        name=name,
        strategy=strategy,
        seed_track_id=seed_track_id,
        params=params,
        track_count=len(track_ids),
        total_duration=round(total_dur, 1),
        created_at=int(time.time()),
    )
    session.add(playlist)
    await session.flush()  # get playlist.id

    for pos, tid in enumerate(track_ids):
        session.add(
            PlaylistTrack(
                playlist_id=playlist.id,
                track_id=tid,
                position=pos,
            )
        )

    await session.flush()
    logger.info(f"Created playlist '{name}' (id={playlist.id}): {len(track_ids)} tracks, {total_dur:.0f}s")
    return playlist


async def get_playlist_with_tracks(session: AsyncSession, playlist_id: int) -> dict[str, Any] | None:
    """Load a playlist with its tracks joined to track_features."""
    # Get playlist
    result = await session.execute(select(Playlist).where(Playlist.id == playlist_id))
    playlist = result.scalar_one_or_none()
    if not playlist:
        return None

    # Get tracks with features
    result = await session.execute(
        select(PlaylistTrack, TrackFeatures)
        .outerjoin(TrackFeatures, PlaylistTrack.track_id == TrackFeatures.track_id)
        .where(PlaylistTrack.playlist_id == playlist_id)
        .order_by(PlaylistTrack.position)
    )
    rows = result.all()

    tracks = []
    for pt, tf in rows:
        track = {
            "position": pt.position,
            "track_id": pt.track_id,
            "media_server_id": tf.media_server_id if tf else None,
            "title": tf.title if tf else None,
            "artist": tf.artist if tf else None,
            "album": tf.album if tf else None,
            "bpm": tf.bpm if tf else None,
            "key": tf.key if tf else None,
            "mode": tf.mode if tf else None,
            "energy": tf.energy if tf else None,
            "danceability": tf.danceability if tf else None,
            "valence": tf.valence if tf else None,
            "mood_tags": tf.mood_tags if tf else None,
            "duration": tf.duration if tf else None,
        }
        tracks.append(track)

    return {
        "id": playlist.id,
        "name": playlist.name,
        "strategy": playlist.strategy,
        "seed_track_id": playlist.seed_track_id,
        "params": playlist.params,
        "track_count": playlist.track_count,
        "total_duration": playlist.total_duration,
        "created_at": playlist.created_at,
        "tracks": tracks,
    }


async def delete_playlist(session: AsyncSession, playlist_id: int) -> bool:
    """Delete a playlist and its tracks. Returns True if found."""
    result = await session.execute(select(Playlist).where(Playlist.id == playlist_id))
    playlist = result.scalar_one_or_none()
    if not playlist:
        return False

    await session.execute(delete(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id))
    await session.delete(playlist)
    return True
