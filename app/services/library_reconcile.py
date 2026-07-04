"""
GrooveIQ — move reconciliation for the library scanner.

beets and similar taggers MOVE and RETAG files. GrooveIQ derives ``track_id``
from the file's path (``generate_track_id``), so a move mints a brand-new
track_id: the scanner would insert a duplicate ``track_features`` row and strand
the original row's listening history (events, interactions, playlist membership)
on the now-orphaned old id.

This module detects that case via the stable MusicBrainz recording id — which
beets writes into every file it manages and the analysis worker already reads
into ``track_features.musicbrainz_track_id`` — and lets the scanner repoint the
existing row + migrate its history onto the new path-derived id instead.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.db import (
    ListenEvent,
    MixTrack,
    PlaylistTrack,
    ScrobbleQueue,
    TrackFeatures,
    TrackInteraction,
)

logger = logging.getLogger(__name__)

# Tables whose rows are keyed by our internal track_id and represent behavioural
# history or user content that must follow a moved track. Every one has a plain
# (non-unique) track_id column, so migrating onto a freshly-seen path id is a
# collision-free straight UPDATE.
#
# Deliberately EXCLUDED:
#   * recommendation_request_audits / recommendation_candidate_audits — these are
#     point-in-time snapshots; rewriting them would falsify a historical record.
#   * chart_entries.matched_track_id — a cache rebuilt on the next chart refresh.
#   * lyrics_requests — its track_id is UNIQUE (collision risk) and its content
#     already rides on the repointed track_features row, so the fetch-dedup state
#     is harmless to drop.
_HISTORY_MODELS = (
    ListenEvent,
    TrackInteraction,
    PlaylistTrack,
    MixTrack,
    ScrobbleQueue,
)


async def find_moved_row(session: AsyncSession, mbid: str | None, new_track_id: str) -> TrackFeatures | None:
    """Return the single ``track_features`` row with this MusicBrainz id whose
    file has vanished from disk — i.e. the same recording moved/retagged to a new
    path.

    Returns ``None`` when there is no MBID, no such row, or the match is
    ambiguous (two live duplicates share an MBID) so distinct rows are never
    collapsed. The on-disk re-stat is authoritative: a candidate whose old file
    still exists is a genuine duplicate, not a move, and is skipped.
    """
    if not mbid:
        return None
    rows = (
        (await session.execute(select(TrackFeatures).where(TrackFeatures.musicbrainz_track_id == mbid))).scalars().all()
    )
    gone = [r for r in rows if r.track_id != new_track_id and not (r.file_path and os.path.isfile(r.file_path))]
    if len(gone) == 1:
        return gone[0]
    return None


async def migrate_track_id(session: AsyncSession, old_track_id: str, new_track_id: str) -> dict[str, int]:
    """Repoint every behavioural-history / user-content row from ``old_track_id``
    to ``new_track_id`` within the caller's transaction.

    The new id is a freshly seen path hash, so there is never a unique-constraint
    collision on the target. Does NOT touch ``track_features`` itself (the caller
    repoints that row's own ``track_id``) nor point-in-time audit rows. Returns
    per-table migrated-row counts.
    """
    counts: dict[str, int] = {}
    for model in _HISTORY_MODELS:
        res = await session.execute(update(model).where(model.track_id == old_track_id).values(track_id=new_track_id))
        counts[model.__tablename__] = res.rowcount or 0
    return counts
