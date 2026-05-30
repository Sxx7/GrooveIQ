"""
GrooveIQ -- Charts API routes.

Exposes Last.fm-sourced charts (top tracks, top artists) globally,
by genre tag, and by country. Charts are rebuilt periodically by the
scheduler and stored in the chart_entries table.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import require_admin, require_api_key
from app.db.session import get_session
from app.models.db import ChartEntry, CoverArtCache, DiscoveryRequest, TrackFeatures
from app.models.schemas import ChartDownloadRequest
from app.services.charts import _normalize as _chart_norm
from app.services.cover_art import _normalize as _normalize_cover_key

logger = logging.getLogger(__name__)
router = APIRouter()

# --- Snapshot helpers (issue #75) ------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COMPARE_RE = re.compile(r"^(\d+)d$", re.IGNORECASE)


async def _resolve_snapshot_date(
    session: AsyncSession,
    chart_type: str,
    scope: str,
    as_of: str | None = None,
) -> str | None:
    """The snapshot date to serve: the latest one, or the newest on-or-before ``as_of``.

    ISO 'YYYY-MM-DD' strings compare lexicographically == chronologically, so a
    plain ``<=`` does date math. Returning the nearest snapshot on-or-before
    ``as_of`` (rather than requiring an exact hit) keeps the API robust to a
    build that was skipped on a given day. Returns None when no snapshot exists.
    """
    q = select(func.max(ChartEntry.snapshot_date)).where(
        ChartEntry.chart_type == chart_type,
        ChartEntry.scope == scope,
    )
    if as_of is not None:
        q = q.where(ChartEntry.snapshot_date <= as_of)
    return (await session.execute(q)).scalar()


def _identity_key(chart_type: str, artist_name: str | None, track_title: str | None) -> tuple[str, str]:
    """Cross-snapshot identity for delta matching.

    Tracks are keyed by (artist, title); artists by (artist, ""). Normalised so
    casing / punctuation / "the " drift between days doesn't break matching.
    """
    a = _chart_norm(artist_name or "")
    if chart_type == "top_tracks":
        return (a, _chart_norm(track_title or ""))
    return (a, "")


async def _snapshot_position_map(
    session: AsyncSession,
    chart_type: str,
    scope: str,
    snap_date: str,
) -> dict[tuple[str, str], int]:
    """identity_key -> position for one snapshot (used to compute deltas)."""
    rows = (
        await session.execute(
            select(ChartEntry.artist_name, ChartEntry.track_title, ChartEntry.position).where(
                ChartEntry.chart_type == chart_type,
                ChartEntry.scope == scope,
                ChartEntry.snapshot_date == snap_date,
            )
        )
    ).all()
    out: dict[tuple[str, str], int] = {}
    for artist_name, track_title, position in rows:
        # setdefault: if a normalised identity appears twice, the higher rank
        # (lower position, encountered first under ORDER-free scan) is arbitrary
        # but stable enough — duplicates within one snapshot are not expected.
        out.setdefault(_identity_key(chart_type, artist_name, track_title), position)
    return out


def _shift_date(iso_date: str, days: int) -> str:
    """ISO date shifted back by ``days`` (for ?compare= anchors)."""
    return (datetime.strptime(iso_date, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")


def _media_server_auth_params() -> str | None:
    """Pre-compute media server auth query string (reusable across entries)."""
    server_type = (settings.MEDIA_SERVER_TYPE or "").lower()
    base = (settings.MEDIA_SERVER_URL or "").rstrip("/")
    if not base:
        return None

    if server_type == "navidrome":
        user = settings.MEDIA_SERVER_USER
        password = settings.MEDIA_SERVER_PASSWORD
        if not user or not password:
            return None
        import hashlib
        import secrets as _secrets

        salt = _secrets.token_hex(8)
        # MD5(password + salt) is mandated by the Subsonic API spec for auth tokens.
        token = hashlib.md5((password + salt).encode()).hexdigest()  # nosemgrep
        return f"u={user}&t={token}&s={salt}&v=1.16.1&c=grooveiq"
    elif server_type == "plex":
        token = settings.MEDIA_SERVER_TOKEN or ""
        return f"X-Plex-Token={token}" if token else None

    return None


def _build_cover_url(tf: TrackFeatures, auth_qs: str | None) -> str | None:
    """Build a media-server cover art URL for a matched track.

    Navidrome (Subsonic API): /rest/getCoverArt.view?id=<media_server_id>&size=300

    Returns None if no media server is configured or the row hasn't been
    matched to one yet (media_server_id IS NULL).
    """
    msid = tf.media_server_id
    if not msid or not auth_qs or not settings.MEDIA_SERVER_URL:
        return None

    base = settings.MEDIA_SERVER_URL.rstrip("/")
    server_type = (settings.MEDIA_SERVER_TYPE or "").lower()

    if server_type == "navidrome":
        return f"{base}/rest/getCoverArt.view?id={msid}&size=300&{auth_qs}"

    return None


@router.get(
    "/charts",
    summary="List available charts",
    description="Returns all chart type + scope combinations currently stored.",
)
async def list_charts(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # Count entries at each chart's *latest* snapshot only (issue #75) — without
    # this the count sums every retained day. Per-chart MAX (not a single global
    # MAX) so a chart that failed to build on the most recent day still reports
    # its own freshest snapshot.
    latest = (
        select(
            ChartEntry.chart_type,
            ChartEntry.scope,
            func.max(ChartEntry.snapshot_date).label("msd"),
        )
        .group_by(ChartEntry.chart_type, ChartEntry.scope)
        .subquery()
    )
    result = await session.execute(
        select(
            ChartEntry.chart_type,
            ChartEntry.scope,
            func.count().label("entries"),
            func.max(ChartEntry.fetched_at).label("fetched_at"),
            latest.c.msd.label("snapshot_date"),
        )
        .join(
            latest,
            (ChartEntry.chart_type == latest.c.chart_type)
            & (ChartEntry.scope == latest.c.scope)
            & (ChartEntry.snapshot_date == latest.c.msd),
        )
        .group_by(ChartEntry.chart_type, ChartEntry.scope, latest.c.msd)
        .order_by(ChartEntry.chart_type, ChartEntry.scope)
    )
    rows = result.all()
    return {
        "charts": [
            {
                "chart_type": r.chart_type,
                "scope": r.scope,
                "entries": r.entries,
                "fetched_at": r.fetched_at,
                "snapshot_date": r.snapshot_date,
            }
            for r in rows
        ],
    }


@router.get(
    "/charts/stats",
    summary="Chart statistics",
    description="Returns summary stats about stored charts: total entries, library match rate, last build time.",
)
async def chart_stats(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # Aggregate over the latest snapshot only (issue #75) so totals/match-rate
    # describe the current charts, not the sum of every retained day. Charts in
    # one build share a snapshot date, so a single global MAX is the right scope.
    latest = (await session.execute(select(func.max(ChartEntry.snapshot_date)))).scalar()

    latest_filter = [ChartEntry.snapshot_date == latest] if latest else []

    total = (await session.execute(select(func.count()).select_from(ChartEntry).where(*latest_filter))).scalar() or 0

    matched = (
        await session.execute(
            select(func.count()).select_from(ChartEntry).where(ChartEntry.matched_track_id.isnot(None), *latest_filter)
        )
    ).scalar() or 0

    last_fetch = (await session.execute(select(func.max(ChartEntry.fetched_at)))).scalar()

    chart_count = (
        await session.execute(
            select(func.count()).select_from(
                select(ChartEntry.chart_type, ChartEntry.scope)
                .where(*latest_filter)
                .group_by(ChartEntry.chart_type, ChartEntry.scope)
                .subquery()
            )
        )
    ).scalar() or 0

    # How many distinct days of history we're retaining (powers the date picker).
    snapshot_count = (await session.execute(select(func.count(func.distinct(ChartEntry.snapshot_date))))).scalar() or 0

    from app.core.config import settings as _settings
    from app.workers.scheduler import get_job_next_run

    return {
        "total_entries": total,
        "library_matches": matched,
        "match_rate": round(matched / total, 3) if total > 0 else 0,
        "chart_count": chart_count,
        "last_fetched_at": last_fetch,
        "latest_snapshot_date": latest,
        "snapshot_count": snapshot_count,
        "auto_rebuild_enabled": bool(_settings.charts_enabled),
        "interval_hours": _settings.CHARTS_INTERVAL_HOURS,
        "next_run_at": get_job_next_run("charts_build"),
    }


@router.get(
    "/charts/{chart_type}",
    summary="Get chart entries",
    description="""
Returns chart entries for the given chart type.

**chart_type**: `top_tracks` or `top_artists`

**scope** (optional): Filter by scope. Examples:
- `global` — worldwide chart (default)
- `tag:rock` — genre/tag chart
- `geo:germany` — country chart

Entries are sorted by chart position. Each entry includes library match info
(`matched_track_id` is set when the track/artist exists in your library).

By default the **latest** daily snapshot is returned (issue #75). Use
`?as_of=YYYY-MM-DD` to view a historical snapshot (the newest one on or before
that date), and `?compare=7d` (also `1d`/`30d`/`<int>d`) to annotate each entry
with `position_change` (positive = climbed) and `previously` vs. that many days
ago — `previously: null` marks a new entry.
""",
)
async def get_chart(
    chart_type: str,
    scope: str = Query("global", max_length=128, description="Chart scope: global, tag:<name>, geo:<country>"),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    as_of: str | None = Query(
        None, description="Historical snapshot date YYYY-MM-DD (newest snapshot on or before this date)"
    ),
    compare: str | None = Query(None, description="Annotate position deltas vs N days ago: 1d | 7d | 30d | <int>d"),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    if chart_type not in ("top_tracks", "top_artists"):
        raise HTTPException(status_code=400, detail="chart_type must be 'top_tracks' or 'top_artists'")
    if as_of is not None and not _DATE_RE.match(as_of):
        raise HTTPException(status_code=400, detail="as_of must be a date in YYYY-MM-DD form")

    # Resolve which snapshot to serve (latest, or newest on/before as_of).
    target_date = await _resolve_snapshot_date(session, chart_type, scope, as_of)
    if target_date is None:
        # No matching snapshot. Distinguish never-built / scope-not-configured /
        # as_of-too-early so the error is actionable.
        any_charts = (await session.execute(select(func.count()).select_from(ChartEntry))).scalar() or 0
        if any_charts == 0:
            raise HTTPException(
                status_code=404,
                detail="No chart data available. Run POST /v1/charts/build first.",
            )
        scope_has_any = (
            await session.execute(
                select(func.count())
                .select_from(ChartEntry)
                .where(ChartEntry.chart_type == chart_type, ChartEntry.scope == scope)
            )
        ).scalar() or 0
        if scope_has_any and as_of:
            raise HTTPException(
                status_code=404,
                detail=f"No snapshot on or before {as_of} for {chart_type}/{scope}.",
            )
        # Charts exist but not for this scope — list available scopes.
        available = (
            (await session.execute(select(ChartEntry.scope).where(ChartEntry.chart_type == chart_type).distinct()))
            .scalars()
            .all()
        )
        raise HTTPException(
            status_code=404,
            detail=f"No chart data for scope '{scope}'. "
            f"Available scopes for {chart_type}: {', '.join(sorted(available)) or 'none'}. "
            f"Configure CHARTS_TAGS or CHARTS_COUNTRIES in .env and rebuild.",
        )

    # Whether we're viewing the freshest snapshot. The live library re-match
    # below only runs in this case — re-matching mutates rows, and rewriting a
    # historical snapshot's in_library against the *current* library would
    # corrupt the record of what the chart looked like that day.
    latest_date = target_date if as_of is None else await _resolve_snapshot_date(session, chart_type, scope)
    is_latest = target_date == latest_date

    count_q = (
        select(func.count())
        .select_from(ChartEntry)
        .where(
            ChartEntry.chart_type == chart_type,
            ChartEntry.scope == scope,
            ChartEntry.snapshot_date == target_date,
        )
    )
    total = (await session.execute(count_q)).scalar() or 0

    q = (
        select(ChartEntry)
        .where(
            ChartEntry.chart_type == chart_type,
            ChartEntry.scope == scope,
            ChartEntry.snapshot_date == target_date,
        )
        .order_by(ChartEntry.position)
        .offset(offset)
        .limit(limit)
    )
    entries = (await session.execute(q)).scalars().all()

    # Live library re-match (#55). Two failure modes are fixed here:
    #   (a) a user added tracks after the last chart build, so an entry that
    #       was unmatched at build time should now be promoted; and
    #   (b) the legacy track-chart builder set ``matched_track_id`` correctly
    #       but never set ``in_library`` — so existing rows can have a
    #       non-null match id with ``in_library=False``. Re-derive both at
    #       serve time. Mutations persist via the session's auto-commit so
    #       the row is self-healing on next read.
    #
    # Only for the latest snapshot (issue #75): re-matching writes to the rows,
    # and a historical snapshot's in_library must reflect the library *as it was*
    # that day, not today's.
    if is_latest:
        from app.services.charts import (
            _build_artist_lookup,
            _build_library_lookup,
        )
        from app.services.charts import (
            _strip_features as _chart_strip_features,
        )

        if chart_type == "top_tracks":
            live_lookup = await _build_library_lookup(session)
            for e in entries:
                if not e.artist_name or not e.track_title:
                    continue
                if not e.matched_track_id:
                    a = _chart_norm(e.artist_name)
                    t = _chart_norm(e.track_title)
                    tid = live_lookup.get((a, t))
                    if not tid:
                        # "Stateside + Zara Larsson" / "(feat. X)" -> bare title.
                        t_stripped = _chart_norm(_chart_strip_features(e.track_title))
                        if t_stripped and t_stripped != t:
                            tid = live_lookup.get((a, t_stripped))
                    if tid:
                        e.matched_track_id = tid
                # in_library follows matched_track_id; covers both fresh promotions
                # and legacy rows where the boolean drifted from the id.
                if e.matched_track_id and not e.in_library:
                    e.in_library = True
        else:  # top_artists
            live_artist_lookup = await _build_artist_lookup(session)
            for e in entries:
                if not e.artist_name:
                    continue
                if not e.in_library:
                    tracks = live_artist_lookup.get(_chart_norm(e.artist_name), [])
                    if tracks:
                        e.matched_track_id = tracks[0]
                        e.in_library = True
                        e.library_track_count = len(tracks)

    # Enrich matched tracks with metadata.
    matched_ids = [e.matched_track_id for e in entries if e.matched_track_id]
    feat_map = {}
    if matched_ids:
        feat_q = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(matched_ids)))
        feat_map = {t.track_id: t for t in feat_q.scalars().all()}

    # Pre-compute media server auth for cover art URLs.
    cover_auth = _media_server_auth_params() if feat_map else None

    # Cover-art-cache fallback for entries with no usable cover. This covers two
    # cases: (a) the track was never matched to the library and Last.fm didn't
    # ship an image_url at chart-build time, and (b) matched_track_id has gone
    # stale because the TrackFeatures row was pruned after the chart was built —
    # without the fallback those entries would render the music-note placeholder.
    # We unconditionally look up cover_art_cache for every entry, even matched
    # ones. The frontend uses image_url as a fallback when library.cover_url
    # 404s (e.g. when media_server_id points at a song Navidrome no longer
    # knows about because it was deleted/moved), so giving it a Spotify CDN
    # URL to fall back on is strictly better than nothing.
    cover_cache_map: dict[tuple[str, str], str] = {}
    keys_a: set[str] = set()
    keys_t: set[str] = set()
    for e in entries:
        if e.image_url or not e.artist_name or not e.track_title:
            continue
        a_norm = _normalize_cover_key(e.artist_name)
        t_norm = _normalize_cover_key(e.track_title)
        if a_norm and t_norm:
            keys_a.add(a_norm)
            keys_t.add(t_norm)
    if keys_a:
        cc_q = await session.execute(
            select(CoverArtCache).where(
                CoverArtCache.artist_norm.in_(list(keys_a)),
                CoverArtCache.title_norm.in_(list(keys_t)),
            )
        )
        for c in cc_q.scalars().all():
            if c.url:
                cover_cache_map[(c.artist_norm, c.title_norm)] = c.url

    # Build Lidarr status lookup for unmatched artists.
    unmatched_artists = list({e.artist_name for e in entries if not e.matched_track_id and e.artist_name})
    lidarr_status_map = {}
    if unmatched_artists:
        discovery_q = await session.execute(
            select(DiscoveryRequest.artist_name, DiscoveryRequest.status)
            .where(DiscoveryRequest.artist_name.in_(unmatched_artists))
            .order_by(DiscoveryRequest.created_at.desc())
        )
        for artist_name, status in discovery_q.all():
            if artist_name not in lidarr_status_map:
                lidarr_status_map[artist_name] = status

    items = []
    for e in entries:
        # Effective image_url: take what the chart builder stored; if absent and
        # we have a cover_art_cache hit, use that instead (fixes stale-match and
        # never-resolved cases at render time).
        eff_image_url = e.image_url
        if not eff_image_url and e.artist_name and e.track_title:
            a_norm = _normalize_cover_key(e.artist_name)
            t_norm = _normalize_cover_key(e.track_title)
            eff_image_url = cover_cache_map.get((a_norm, t_norm))

        item = {
            "position": e.position,
            "artist_name": e.artist_name,
            "playcount": e.playcount,
            "listeners": e.listeners,
            "in_library": e.in_library,
            "matched_track_id": e.matched_track_id,
            "image_url": eff_image_url,
        }
        if chart_type == "top_tracks":
            item["track_title"] = e.track_title

        if chart_type == "top_artists":
            item["library_track_count"] = e.library_track_count

        # Lidarr status for items not in library.
        if not e.in_library:
            lidarr_st = lidarr_status_map.get(e.artist_name)
            if lidarr_st == "sent":
                item["lidarr_status"] = "downloading"
            elif lidarr_st == "in_lidarr":
                item["lidarr_status"] = "in_lidarr"
            elif lidarr_st == "pending":
                item["lidarr_status"] = "pending"
            elif lidarr_st == "failed":
                item["lidarr_status"] = "failed"
            else:
                item["lidarr_status"] = None  # not sent to Lidarr

        # Enrich with local metadata if matched.
        tf = feat_map.get(e.matched_track_id) if e.matched_track_id else None
        if tf:
            item["library"] = {
                "track_id": tf.track_id,
                "media_server_id": tf.media_server_id,
                "title": tf.title,
                "artist": tf.artist,
                "album": tf.album,
                "genre": tf.genre,
                "bpm": tf.bpm,
                "energy": tf.energy,
                "duration": tf.duration,
                "cover_url": _build_cover_url(tf, cover_auth),
            }

        items.append(item)

    resp = {
        "chart_type": chart_type,
        "scope": scope,
        "snapshot_date": target_date,
        "total": total,
        "fetched_at": entries[0].fetched_at if entries else None,
        "entries": items,
    }

    # Position deltas vs N days ago (issue #75). Match entries by normalised
    # identity against the comparison snapshot; positive position_change means
    # the entry climbed (lower position number). previously=None marks an entry
    # absent from the comparison snapshot (NEW / re-entry).
    if compare is not None:
        m = _COMPARE_RE.match(compare.strip())
        if not m:
            raise HTTPException(status_code=400, detail="compare must look like '1d', '7d', or '30d'")
        n_days = int(m.group(1))
        anchor = _shift_date(target_date, n_days)
        cmp_date = await _resolve_snapshot_date(session, chart_type, scope, anchor)
        resp["compare"] = compare
        resp["compared_to"] = cmp_date
        if cmp_date and cmp_date < target_date:
            prev_map = await _snapshot_position_map(session, chart_type, scope, cmp_date)
            for item, e in zip(items, entries):
                prev_pos = prev_map.get(_identity_key(chart_type, e.artist_name, e.track_title))
                item["previously"] = prev_pos
                item["position_change"] = None if prev_pos is None else prev_pos - e.position
        else:
            # No comparison snapshot old enough — nothing to diff against.
            for item in items:
                item["previously"] = None
                item["position_change"] = None

    return resp


@router.get(
    "/charts/{chart_type}/snapshots",
    summary="List available chart snapshot dates",
    description="Distinct daily snapshot dates retained for this chart/scope (newest first). Powers the date picker.",
)
async def list_chart_snapshots(
    chart_type: str,
    scope: str = Query("global", max_length=128),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    if chart_type not in ("top_tracks", "top_artists"):
        raise HTTPException(status_code=400, detail="chart_type must be 'top_tracks' or 'top_artists'")
    rows = (
        (
            await session.execute(
                select(ChartEntry.snapshot_date)
                .where(
                    ChartEntry.chart_type == chart_type,
                    ChartEntry.scope == scope,
                    ChartEntry.snapshot_date.isnot(None),
                )
                .distinct()
                .order_by(ChartEntry.snapshot_date.desc())
            )
        )
        .scalars()
        .all()
    )
    return {"chart_type": chart_type, "scope": scope, "count": len(rows), "snapshots": list(rows)}


@router.get(
    "/charts/{chart_type}/track/{artist}/{title}/history",
    summary="Position history for one chart entry",
    description=(
        "Full position trajectory for a single entry across retained snapshots. "
        "For `top_artists`, `title` is ignored (match is on artist only) — pass any placeholder."
    ),
)
async def get_chart_entry_history(
    chart_type: str,
    artist: str,
    title: str,
    scope: str = Query("global", max_length=128),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    if chart_type not in ("top_tracks", "top_artists"):
        raise HTTPException(status_code=400, detail="chart_type must be 'top_tracks' or 'top_artists'")

    conds = [
        ChartEntry.chart_type == chart_type,
        ChartEntry.scope == scope,
        ChartEntry.snapshot_date.isnot(None),
        func.lower(ChartEntry.artist_name) == artist.strip().lower(),
    ]
    if chart_type == "top_tracks":
        conds.append(func.lower(ChartEntry.track_title) == title.strip().lower())

    rows = (
        await session.execute(
            select(
                ChartEntry.snapshot_date,
                ChartEntry.position,
                ChartEntry.playcount,
                ChartEntry.listeners,
            )
            .where(*conds)
            .order_by(ChartEntry.snapshot_date)
        )
    ).all()

    return {
        "chart_type": chart_type,
        "scope": scope,
        "artist": artist,
        "title": title if chart_type == "top_tracks" else None,
        "count": len(rows),
        "history": [
            {
                "snapshot_date": r.snapshot_date,
                "position": r.position,
                "playcount": r.playcount,
                "listeners": r.listeners,
            }
            for r in rows
        ],
    }


@router.post(
    "/charts/build",
    summary="Trigger chart rebuild",
    description="Fetches fresh charts from Last.fm, matches to library, and optionally sends missing artists to Lidarr.",
)
async def trigger_chart_build(
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    from app.services.charts import build_charts

    result = await build_charts()
    return {"status": "completed", "result": result}


@router.post(
    "/charts/download",
    summary="Download a chart track via Spotizerr",
    description=(
        "Trigger download of a chart track via Spotizerr. "
        "Provide either a chart position or artist_name + track_title. "
        "Returns the Spotizerr task_id for status tracking."
    ),
)
async def download_chart_track(
    body: ChartDownloadRequest,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    if not settings.spotizerr_enabled:
        raise HTTPException(
            status_code=503,
            detail="Spotizerr not configured. Set SPOTIZERR_URL in .env.",
        )

    # Resolve artist + title from chart entry or request body.
    artist_name = body.artist_name
    track_title = body.track_title

    if body.position is not None:
        # Scope to the latest snapshot (issue #75) — there is now one row per
        # position *per day*, so an unscoped query would raise MultipleResultsFound.
        latest_date = await _resolve_snapshot_date(session, body.chart_type, body.scope)
        entry = (
            await session.execute(
                select(ChartEntry).where(
                    ChartEntry.chart_type == body.chart_type,
                    ChartEntry.scope == body.scope,
                    ChartEntry.position == body.position,
                    ChartEntry.snapshot_date == latest_date,
                )
            )
        ).scalar_one_or_none()

        if not entry:
            raise HTTPException(
                status_code=404,
                detail=f"No chart entry at position {body.position} in {body.chart_type}/{body.scope}.",
            )
        artist_name = entry.artist_name
        track_title = entry.track_title

    if not artist_name or not track_title:
        raise HTTPException(
            status_code=400,
            detail="Could not determine artist and track title for download.",
        )

    from app.services.spotizerr import search_and_download

    result = await search_and_download(artist_name, track_title)
    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"No Spotify match found for '{artist_name} - {track_title}'.",
        )

    return {
        "status": result.get("status", "unknown"),
        "task_id": result.get("task_id", ""),
        "artist_name": artist_name,
        "track_title": track_title,
        "spotify_id": result.get("spotify_id", ""),
        "matched_artist": result.get("matched_artist", ""),
        "matched_title": result.get("matched_title", ""),
    }
