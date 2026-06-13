"""
GrooveIQ — lyrics acquisition cascade.

``resolve_lyrics(track, ...)`` walks a priority cascade and returns the best
available lyrics, preferring *real* sources over machine transcription:

    tier 1  embedded tags  (USLT/SYLT, Vorbis LYRICS, MP4 ©lyr) — read from file
    tier 2  LRCLIB         (lrclib.net free API; synced + plain)
    tier 3  ASR fallback   (faster-whisper GPU sidecar) — voiced tracks only

Quality ladder (higher = better for DISPLAY):

    4  embedded_synced     3  lrclib_synced
    2  embedded_plain      1  lrclib_plain
    0  asr_synced (approx) -1 asr_plain (approx)

The cascade picks the highest-quality result across the real tiers (so a
lrclib *synced* hit beats an embedded *plain* one), and only ever falls through
to ASR when no real lyrics exist **and** the track is voiced. ASR never
overwrites a real source. The ``instrumentalness`` gate blocks ASR only — a
genuine embedded/LRCLIB tag on a nominally-instrumental track (spoken word,
skits) is still trusted.

Everything degrades gracefully: no embedded tag → LRCLIB → ASR → none; no
LRCLIB → embedded + ASR; no ASR sidecar → tiers 1–2 only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.config import settings
from app.services.audio_analysis import LYRICS_VERSION
from app.services.metadata_reader import _strip_lrc_timestamps, read_embedded_lyrics

logger = logging.getLogger(__name__)

# --- Quality ranks (display preference) ---
QUALITY_EMBEDDED_SYNCED = 4
QUALITY_LRCLIB_SYNCED = 3
QUALITY_EMBEDDED_PLAIN = 2
QUALITY_LRCLIB_PLAIN = 1
QUALITY_ASR_SYNCED = 0
QUALITY_ASR_PLAIN = -1

# --- Resolution outcomes (the drain maps these to LyricsRequest statuses) ---
OUTCOME_FOUND = "found"  # got displayable/usable lyrics (embedded|lrclib|asr)
OUTCOME_INSTRUMENTAL = "instrumental"  # confirmed instrumental (lrclib tag or gate)
OUTCOME_NO_LYRICS = "no_lyrics"  # searched all available tiers, nothing found
OUTCOME_SEARCH_ERROR = "search_error"  # a tier failed transiently — re-queue
OUTCOME_ASR_DEFERRED = "asr_deferred"  # voiced + needs ASR, but the GPU budget is spent — re-queue


@dataclass
class LyricsResolution:
    """Outcome of one cascade walk for a single track."""

    outcome: str
    source: str | None = None  # embedded|lrclib|asr|instrumental|none
    quality: int | None = None
    plain: str | None = None
    synced: str | None = None
    language: str | None = None
    is_explicit: bool | None = None
    detail: str | None = None  # which tier / error, for telemetry
    asr_used: bool = False  # True iff a transcribe() call was actually made (GPU budget)
    # True once the cheap tiers (embedded + LRCLIB) have been exhausted with a
    # definitive "no lyrics" — lets the drain skip them on later ASR retries.
    cheap_exhausted: bool = False


def _plain_from(synced: str | None, plain: str | None) -> str | None:
    """Prefer an explicit plain copy; otherwise derive one by stripping LRC."""
    if plain:
        return plain
    if synced:
        return _strip_lrc_timestamps(synced)
    return None


async def resolve_lyrics(
    track,
    *,
    lrclib_client=None,
    asr_client=None,
    allow_asr: bool = True,
    skip_cheap_tiers: bool = False,
) -> LyricsResolution:
    """Resolve lyrics for ``track`` (a TrackFeatures row).

    ``lrclib_client`` / ``asr_client`` may be injected (tests, or a pooled
    singleton); both default to the configured singletons when their tier is
    enabled. ``asr_client`` is only used when ASR is enabled and configured.

    ``allow_asr`` lets the drain withhold ASR when the GPU budget for the
    hour/tick is spent: a voiced track that would otherwise be transcribed then
    returns ``OUTCOME_ASR_DEFERRED`` (re-queue, no penalty) instead of burning
    budget or being wrongly marked ``no_lyrics``.

    ``skip_cheap_tiers`` jumps straight to the ASR tier, skipping embedded +
    LRCLIB. The drain sets this for rows already known to have exhausted the
    cheap tiers (``cheap_exhausted``) so ASR retries don't re-hammer LRCLIB.
    """
    artist = getattr(track, "artist", None)
    title = getattr(track, "title", None)
    album = getattr(track, "album", None)
    duration = getattr(track, "duration", None)
    file_path = getattr(track, "file_path", None)
    instrumentalness = getattr(track, "instrumentalness", None)

    emb_plain = emb_synced = None
    lrclib_synced = lrclib_plain = None
    lrclib_instrumental = False
    lrclib_error: str | None = None

    if not skip_cheap_tiers:
        # --- Tier 1: embedded tags (read directly from the file) -------------
        if file_path:
            try:
                emb = read_embedded_lyrics(file_path)
                emb_plain, emb_synced = emb.get("plain"), emb.get("synced")
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Embedded read failed for %s: %s", file_path, exc)

        # An embedded *synced* tag is the best possible (q4) — short-circuit.
        if emb_synced:
            return LyricsResolution(
                outcome=OUTCOME_FOUND,
                source="embedded",
                quality=QUALITY_EMBEDDED_SYNCED,
                plain=_plain_from(emb_synced, emb_plain),
                synced=emb_synced,
                detail="embedded_synced",
            )

        # --- Tier 2: LRCLIB --------------------------------------------------
        if settings.lyrics_lrclib_enabled:
            client = lrclib_client
            if client is None:
                from app.services.lrclib import get_lrclib_client

                client = get_lrclib_client()
            outcome = await client.lookup(artist, title, album, duration)
            if not outcome.ok:
                lrclib_error = outcome.error
            elif outcome.result.found:
                if outcome.result.instrumental:
                    lrclib_instrumental = True
                else:
                    lrclib_synced = outcome.result.synced
                    lrclib_plain = outcome.result.plain

    # --- Pick the best real source (quality ladder) --------------------------
    if lrclib_synced:  # q3 — beats embedded_plain (q2)
        return LyricsResolution(
            outcome=OUTCOME_FOUND,
            source="lrclib",
            quality=QUALITY_LRCLIB_SYNCED,
            plain=_plain_from(lrclib_synced, lrclib_plain),
            synced=lrclib_synced,
            detail="lrclib_synced",
        )
    if emb_plain:  # q2
        return LyricsResolution(
            outcome=OUTCOME_FOUND,
            source="embedded",
            quality=QUALITY_EMBEDDED_PLAIN,
            plain=emb_plain,
            detail="embedded_plain",
        )
    if lrclib_plain:  # q1
        return LyricsResolution(
            outcome=OUTCOME_FOUND,
            source="lrclib",
            quality=QUALITY_LRCLIB_PLAIN,
            plain=lrclib_plain,
            detail="lrclib_plain",
        )
    if lrclib_instrumental:
        return LyricsResolution(
            outcome=OUTCOME_INSTRUMENTAL,
            source="instrumental",
            detail="lrclib_instrumental",
        )

    # No real lyrics. If LRCLIB couldn't be reached we can't make a sound
    # cascade decision (it may well have lyrics) — re-queue rather than guess
    # instrumental or burn GPU on ASR.
    if lrclib_error:
        return LyricsResolution(
            outcome=OUTCOME_SEARCH_ERROR,
            detail=f"lrclib: {lrclib_error}",
        )

    # Past this point the cheap tiers (embedded + LRCLIB) have definitively
    # returned nothing — record it so the drain can skip them on ASR retries.

    # --- Instrumental gate ---------------------------------------------------
    # If the audio model says this is instrumental, don't transcribe — mark it.
    if instrumentalness is not None and instrumentalness >= settings.LYRICS_ASR_INSTRUMENTAL_MAX:
        return LyricsResolution(
            outcome=OUTCOME_INSTRUMENTAL,
            source="instrumental",
            detail=f"gate(instrumentalness={instrumentalness:.2f})",
            cheap_exhausted=True,
        )

    # --- Tier 3: ASR (voiced tracks only) ------------------------------------
    asr_possible = bool(settings.lyrics_asr_enabled and asr_client is not None and file_path)
    if asr_possible and not allow_asr:
        # The GPU budget is spent — defer rather than guess no_lyrics.
        return LyricsResolution(outcome=OUTCOME_ASR_DEFERRED, detail="asr_budget_exhausted", cheap_exhausted=True)
    if asr_possible:
        asr = await asr_client.transcribe(file_path)
        if not asr.ok:
            return LyricsResolution(
                outcome=OUTCOME_SEARCH_ERROR, detail=f"asr: {asr.error}", asr_used=True, cheap_exhausted=True
            )
        if asr.text or asr.synced:
            synced = asr.synced
            plain = asr.text or _plain_from(synced, None)
            return LyricsResolution(
                outcome=OUTCOME_FOUND,
                source="asr",
                quality=QUALITY_ASR_SYNCED if synced else QUALITY_ASR_PLAIN,
                plain=plain,
                synced=synced,
                language=getattr(asr, "language", None),
                detail="asr",
                asr_used=True,
                cheap_exhausted=True,
            )
        # ASR ran and found no speech — treat as no lyrics.
        return LyricsResolution(
            outcome=OUTCOME_NO_LYRICS, source="none", detail="asr_no_speech", asr_used=True, cheap_exhausted=True
        )

    # Nothing found and ASR not available — terminal-ish "no lyrics".
    return LyricsResolution(
        outcome=OUTCOME_NO_LYRICS, source="none", detail="exhausted_available_tiers", cheap_exhausted=True
    )


def apply_resolution(track, res: LyricsResolution) -> None:
    """Write a resolution back onto a TrackFeatures row.

    SEARCH_ERROR writes nothing (the row is left for a later retry). All other
    outcomes stamp ``lyrics_version`` / ``lyrics_fetched_at`` so the row records
    which cascade version processed it.
    """
    import time

    now = int(time.time())
    if res.outcome == OUTCOME_FOUND:
        track.lyrics_plain = res.plain
        track.lyrics_synced = res.synced
        track.lyrics_source = res.source
        track.lyrics_quality = res.quality
        track.lyrics_language = res.language
        if res.is_explicit is not None:
            track.is_explicit = res.is_explicit
        track.lyrics_version = LYRICS_VERSION
        track.lyrics_fetched_at = now
    elif res.outcome == OUTCOME_INSTRUMENTAL:
        track.lyrics_plain = None
        track.lyrics_synced = None
        track.lyrics_source = "instrumental"
        track.lyrics_quality = None
        track.lyrics_version = LYRICS_VERSION
        track.lyrics_fetched_at = now
    elif res.outcome == OUTCOME_NO_LYRICS:
        track.lyrics_source = "none"
        track.lyrics_quality = None
        track.lyrics_version = LYRICS_VERSION
        track.lyrics_fetched_at = now
    # OUTCOME_SEARCH_ERROR: intentionally leave the row untouched.
