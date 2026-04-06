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

import base64
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Playlist, PlaylistTrack, TrackFeatures

logger = logging.getLogger(__name__)


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


async def _load_tracks(session: AsyncSession) -> List[TrackFeatures]:
    """Load all analyzed tracks with non-null embeddings."""
    result = await session.execute(
        select(TrackFeatures)
        .where(TrackFeatures.embedding.isnot(None))
        .where(TrackFeatures.analysis_error.is_(None))
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Camelot wheel for harmonic mixing
# ---------------------------------------------------------------------------

# Maps (key, mode) → Camelot code (number 1–12, letter A/B)
_CAMELOT = {
    ("Ab", "minor"): (1, "A"), ("B",  "major"): (1, "B"),
    ("Eb", "minor"): (2, "A"), ("F#", "major"): (2, "B"),
    ("Bb", "minor"): (3, "A"), ("C#", "major"): (3, "B"),  ("Db", "major"): (3, "B"),
    ("F",  "minor"): (4, "A"), ("Ab", "major"): (4, "B"),
    ("C",  "minor"): (5, "A"), ("Eb", "major"): (5, "B"),
    ("G",  "minor"): (6, "A"), ("Bb", "major"): (6, "B"),
    ("D",  "minor"): (7, "A"), ("F",  "major"): (7, "B"),
    ("A",  "minor"): (8, "A"), ("C",  "major"): (8, "B"),
    ("E",  "minor"): (9, "A"), ("G",  "major"): (9, "B"),
    ("B",  "minor"): (10, "A"), ("D",  "major"): (10, "B"),
    ("F#", "minor"): (11, "A"), ("A",  "major"): (11, "B"),
    ("C#", "minor"): (12, "A"), ("E",  "major"): (12, "B"), ("Db", "minor"): (12, "A"),
}


def _camelot_compatible(code1: Tuple[int, str], code2: Tuple[int, str], max_distance: int = 1) -> bool:
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

def _generate_flow(tracks: List[TrackFeatures], seed_id: str, max_tracks: int) -> List[str]:
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

            score = cos * bpm_bonus * energy_bonus
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
# Strategy: Mood
# ---------------------------------------------------------------------------

def _generate_mood(tracks: List[TrackFeatures], mood: str, max_tracks: int) -> List[str]:
    """Filter tracks by mood tag confidence, order by energy arc."""
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

    # Sort by confidence, take top candidates
    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = [t for _, t in scored[:max_tracks]]

    # Re-order for pleasant listening: energy ramp up then cool down
    mid = len(candidates) // 2
    first_half = sorted(candidates[:mid], key=lambda t: t.energy or 0)
    second_half = sorted(candidates[mid:], key=lambda t: t.energy or 0, reverse=True)

    return [t.track_id for t in first_half + second_half]


# ---------------------------------------------------------------------------
# Strategy: Energy Curve
# ---------------------------------------------------------------------------

def _generate_energy_curve(tracks: List[TrackFeatures], curve: str, max_tracks: int) -> List[str]:
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

            score = energy_dist - sim_bonus
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

def _generate_key_compatible(tracks: List[TrackFeatures], seed_id: str, max_tracks: int) -> List[str]:
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

            score = cos * key_bonus
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
    seed_track_id: Optional[str],
    params: Optional[Dict[str, Any]],
    max_tracks: int,
) -> Playlist:
    """
    Generate a playlist and persist it.
    Returns the created Playlist with track_count and total_duration set.
    """
    tracks = await _load_tracks(session)
    if len(tracks) < 5:
        raise ValueError(f"Not enough analyzed tracks ({len(tracks)}). Need at least 5.")

    logger.info(f"Generating '{strategy}' playlist '{name}' from {len(tracks)} tracks")

    # Dispatch to strategy
    if strategy == "flow":
        track_ids = _generate_flow(tracks, seed_track_id, max_tracks)
    elif strategy == "mood":
        mood = (params or {}).get("mood", "happy")
        track_ids = _generate_mood(tracks, mood, max_tracks)
    elif strategy == "energy_curve":
        curve = (params or {}).get("curve", "ramp_up_cool_down")
        track_ids = _generate_energy_curve(tracks, curve, max_tracks)
    elif strategy == "key_compatible":
        track_ids = _generate_key_compatible(tracks, seed_track_id, max_tracks)
    else:
        raise ValueError(f"Unknown strategy: {strategy}")

    if not track_ids:
        raise ValueError("Strategy produced no tracks")

    # Compute total duration
    dur_map = {t.track_id: t.duration or 0 for t in tracks}
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
        session.add(PlaylistTrack(
            playlist_id=playlist.id,
            track_id=tid,
            position=pos,
        ))

    await session.flush()
    logger.info(f"Created playlist '{name}' (id={playlist.id}): {len(track_ids)} tracks, {total_dur:.0f}s")
    return playlist


async def get_playlist_with_tracks(
    session: AsyncSession, playlist_id: int
) -> Optional[Dict[str, Any]]:
    """Load a playlist with its tracks joined to track_features."""
    # Get playlist
    result = await session.execute(
        select(Playlist).where(Playlist.id == playlist_id)
    )
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
    result = await session.execute(
        select(Playlist).where(Playlist.id == playlist_id)
    )
    playlist = result.scalar_one_or_none()
    if not playlist:
        return False

    await session.execute(
        delete(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id)
    )
    await session.delete(playlist)
    return True
