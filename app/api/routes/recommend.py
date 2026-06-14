"""
GrooveIQ – Recommendation endpoint (Phase 4).

GET /v1/recommend/{user_id} — returns ranked track candidates
from content-based, collaborative filtering, and heuristic sources.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import check_user_access, require_admin, require_api_key
from app.core.user_id import validate_user_id
from app.db.session import get_session
from app.models.db import CoverArtCache, ListenEvent, TrackFeatures, TrackInteraction, User

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_model_version() -> str:
    from app.services.ranker import get_model_version

    return get_model_version() or "phase4-candidate-gen-v1"


# ---------------------------------------------------------------------------
# Mix-cache key buckets (Chunk 6)
# ---------------------------------------------------------------------------


def _dial_bucket(dial) -> str:
    """A stable, low-cardinality bucket for a resolved discovery dial.

    A named preset keys on its name; a free-floating ``discovery`` value buckets
    to two decimals so a continuous slider produces a bounded set of keys.
    """
    return dial.preset if dial.preset else f"d{dial.discovery:.2f}"


def _context_bucket(
    device_type: str | None,
    output_type: str | None,
    context_type: str | None,
    location_label: str | None,
    hour_of_day: int | None,
    day_of_week: int | None,
) -> str:
    """A stable key fragment for the ranking-affecting context params.

    These feed the ranker / reranker, so a mobile-in-the-gym mix must not be
    served to a desktop-at-home request — every context dimension is in the key.
    """
    return "|".join(
        "" if v is None else str(v)
        for v in (device_type, output_type, context_type, location_label, hour_of_day, day_of_week)
    )


def _make_payload_builder(session_factory, gen_kwargs: dict):
    """Build a self-contained, zero-arg async builder for the mix cache.

    Background rebuilds run detached from the request, so the builder opens its
    own session via ``session_factory`` (``AsyncSessionLocal`` in production;
    monkeypatched to the test sessionmaker under test).  Imported lazily so
    patching ``app.db.session.AsyncSessionLocal`` is honoured.
    """

    async def _builder():
        async with session_factory() as own_session:
            return await generate_recommendation_payload(own_session, **gen_kwargs)

    return _builder


async def generate_recommendation_payload(
    session: AsyncSession,
    *,
    user_id: str,
    dial,
    limit: int,
    model_version: str,
    seed_track_id: str | None = None,
    genre: str | None = None,
    mood: str | None = None,
    hour_of_day: int | None = None,
    day_of_week: int | None = None,
    device_type: str | None = None,
    output_type: str | None = None,
    context_type: str | None = None,
    location_label: str | None = None,
    want_debug: bool = False,
) -> dict:
    """Run the candidate-gen → rank → rerank → feature pipeline for one request.

    Returns a **session-detached** payload (plain dicts/lists/tuples — no ORM
    rows) so it is safe to cache and reuse across requests.  The caller layers
    on the per-request bits that must stay fresh: a new ``request_id`` and the
    audit dispatch. (Impressions are logged client-side, for tracks actually shown.)

    The discovery-dial overrides are applied *inside* this function (around
    candidate-gen and rerank), so it produces the same result whether invoked on
    the request's session or a detached background session.
    """
    from app.services.candidate_gen import get_candidates
    from app.services.request_config import apply_overrides

    overfetch = limit * 6 if (genre or mood) else limit * 3
    with apply_overrides(dial.overrides):
        candidates = await get_candidates(
            user_id=user_id,
            seed_track_id=seed_track_id,
            k=overfetch,
            session=session,
        )

    # Filter candidates by genre/mood if requested.
    if candidates and (genre or mood):
        cand_ids = [c["track_id"] for c in candidates]
        feat_q = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(cand_ids)))
        feat_lookup = {t.track_id: t for t in feat_q.scalars().all()}

        filtered = []
        for c in candidates:
            tf = feat_lookup.get(c["track_id"])
            if not tf:
                continue
            if genre and not (tf.genre and genre.lower() in tf.genre.lower()):
                continue
            if mood:
                tags = tf.mood_tags if isinstance(tf.mood_tags, list) else []
                if not any(
                    isinstance(tag, dict) and tag.get("label") == mood and tag.get("confidence", 0) > 0.3
                    for tag in tags
                ):
                    continue
            filtered.append(c)
        candidates = filtered

    empty_payload = {
        "reason": None,
        "reranked": [],
        "source_map": {},
        "sources_by_tid": {},
        "actions_by_tid": {},
        "track_meta": {},
        "audit_track_ids": [],
        "candidate_rows": [],
        "candidates_by_source": {},
        "debug_data": None,
    }

    if not candidates:
        # Diagnose why there are no candidates so the client can show a useful message.
        from sqlalchemy import func as sa_func

        track_count = (await session.execute(select(sa_func.count()).select_from(TrackFeatures))).scalar() or 0
        if track_count == 0:
            reason = "no_library"
        else:
            interaction_count = (
                await session.execute(
                    select(sa_func.count()).select_from(TrackInteraction).where(TrackInteraction.user_id == user_id)
                )
            ).scalar() or 0
            if interaction_count == 0:
                reason = "no_history"
            else:
                user_row = (
                    await session.execute(select(User.taste_profile).where(User.user_id == user_id))
                ).scalar_one_or_none()
                reason = "no_taste_profile" if not user_row else "no_candidates"
        return {**empty_payload, "reason": reason}

    candidate_ids = [c["track_id"] for c in candidates]

    # Per-candidate source attribution (a candidate may surface from multiple sources).
    sources_by_tid: dict[str, list[str]] = {}
    for c in candidates:
        sources_by_tid.setdefault(c["track_id"], []).append(c["source"])
    candidates_by_source_counts: dict[str, int] = {}
    for src_list in sources_by_tid.values():
        for s in src_list:
            candidates_by_source_counts[s] = candidates_by_source_counts.get(s, 0) + 1

    debug_data: dict | None = None
    if want_debug:
        from collections import defaultdict

        cbs = defaultdict(list)
        for c in candidates:
            cbs[c["source"]].append({"track_id": c["track_id"], "score": round(c["score"], 4)})
        debug_data = {"candidates_by_source": dict(cbs), "total_candidates": len(candidates)}

    # --- Step 1: Score candidates with LightGBM ranker (or fallback) ---
    from app.services.ranker import score_candidates

    scored = await score_candidates(
        user_id,
        candidate_ids,
        session,
        hour_of_day=hour_of_day,
        day_of_week=day_of_week,
        device_type=device_type,
        output_type=output_type,
        context_type=context_type,
        location_label=location_label,
    )

    # --- Step 1b: Discovery-dial per-source reweighting on the FINAL ranker score ---
    # candidate_gen applies source_weight_mult to the *recall* score, but the LightGBM
    # ranker then re-scores and discards that ordering — so the dial's "boost lastfm /
    # damp cf" intent never reaches the output. Re-apply the active preset's per-source
    # multipliers to the ranker score here so source posture actually moves the final
    # order. Empty dict (balanced / un-dialed) → no-op, so the default path is unchanged.
    from app.services.algorithm_config import get_config

    with apply_overrides(dial.overrides):
        _src_mult = dict(get_config().modes.active.source_weight_mult)
    if _src_mult:
        _src_of = {c["track_id"]: c["source"] for c in candidates}
        scored = sorted(
            ((tid, s * _src_mult.get(_src_of.get(tid, ""), 1.0)) for tid, s in scored),
            key=lambda x: x[1],
            reverse=True,
        )

    raw_score_by_tid = {tid: float(score) for tid, score in scored}
    pre_rerank_pos_by_tid = {tid: i for i, (tid, _) in enumerate(scored)}
    source_map = {c["track_id"]: c["source"] for c in candidates}

    if debug_data is not None:
        debug_data["pre_rerank"] = [
            {"track_id": tid, "score": round(score, 4), "position": i} for i, (tid, score) in enumerate(scored)
        ]

    # --- Step 2: Rerank for diversity / business rules ---
    from app.services.reranker import get_last_rerank_actions, rerank

    with apply_overrides(dial.overrides):
        reranked_full = await rerank(
            scored,
            user_id,
            session,
            device_type=device_type,
            output_type=output_type,
            collect_actions=True,
        )
    rerank_actions = get_last_rerank_actions()
    reranked = reranked_full[:limit]
    final_score_by_tid = {tid: float(score) for tid, score in reranked_full}
    final_pos_by_tid = {tid: i for i, (tid, _) in enumerate(reranked_full)}
    shown_tids = {tid for tid, _ in reranked}

    actions_by_tid: dict[str, list[dict]] = {}
    for action in rerank_actions:
        tid = action.get("track_id")
        if tid:
            actions_by_tid.setdefault(tid, []).append(action)

    # --- Feature vectors for the audit / debug (top-N by raw_score ∪ shown) ---
    audit_track_ids: list[str] = []
    feature_vectors: dict[str, dict] = {}
    if settings.RECO_AUDIT_ENABLED or want_debug:
        from app.services.feature_eng import FEATURE_COLUMNS, build_features

        cap = settings.RECO_AUDIT_MAX_CANDIDATES
        ranked_by_raw = sorted(raw_score_by_tid.items(), key=lambda x: x[1], reverse=True)
        top_n = [tid for tid, _ in ranked_by_raw[:cap]]
        audit_track_ids = list({*top_n, *shown_tids})

        feat_result = await build_features(
            user_id,
            audit_track_ids,
            session,
            hour_of_day=hour_of_day,
            day_of_week=day_of_week,
            device_type=device_type,
            output_type=output_type,
            context_type=context_type,
            location_label=location_label,
        )
        for idx, tid in enumerate(feat_result["track_ids"]):
            feature_vectors[tid] = {
                col: round(float(feat_result["features"][idx][ci]), 4) for ci, col in enumerate(FEATURE_COLUMNS)
            }

    if debug_data is not None:
        debug_data["reranker_actions"] = rerank_actions
        debug_data["feature_vectors"] = {tid: feature_vectors.get(tid, {}) for tid, _ in reranked}

    # --- Track metadata for the response (detached dicts, not ORM rows) ---
    final_ids = [tid for tid, _ in reranked]
    feat_meta_result = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(final_ids)))
    track_meta: dict[str, dict] = {}
    for tf in feat_meta_result.scalars().all():
        track_meta[tf.track_id] = {
            "media_server_id": tf.media_server_id,
            "title": tf.title,
            "artist": tf.artist,
            "album": tf.album,
            "genre": tf.genre,
            "bpm": tf.bpm,
            "key": tf.key,
            "mode": tf.mode,
            "energy": tf.energy,
            "danceability": tf.danceability,
            "valence": tf.valence,
            "mood_tags": tf.mood_tags,
            "duration": tf.duration,
        }

    # --- Audit candidate rows (deterministic from generation; no request_id) ---
    candidate_rows: list[dict] = []
    for tid in audit_track_ids:
        candidate_rows.append(
            {
                "track_id": tid,
                "sources": sources_by_tid.get(tid, []),
                "raw_score": raw_score_by_tid.get(tid, 0.0),
                "pre_rerank_position": pre_rerank_pos_by_tid.get(tid, -1),
                "final_score": final_score_by_tid.get(tid),
                "final_position": final_pos_by_tid.get(tid) if tid in final_pos_by_tid else None,
                "shown": tid in shown_tids,
                "reranker_actions": actions_by_tid.get(tid, []),
                "feature_vector": feature_vectors.get(tid, {}),
            }
        )

    return {
        "reason": None,
        "reranked": reranked,
        "source_map": source_map,
        "sources_by_tid": sources_by_tid,
        "actions_by_tid": actions_by_tid,
        "track_meta": track_meta,
        "audit_track_ids": audit_track_ids,
        "candidate_rows": candidate_rows,
        "candidates_by_source": candidates_by_source_counts,
        "debug_data": debug_data,
    }


class PrewarmRequest(BaseModel):
    """Body for ``POST /v1/users/{user_id}/mixes/prewarm``."""

    modes: list[str] | None = Field(None, description="Preset names to warm; defaults to all presets.")
    limit: int = Field(25, ge=1, le=100, description="Track count to warm (must match the client's request limit).")


@router.get(
    "/recommend/{user_id}",
    summary="Get track recommendations for a user",
    description="""
Returns a ranked list of recommended tracks for the given user.

Candidates are generated from multiple sources:
- **content**: FAISS-based acoustic similarity (from user profile or seed track)
- **content_profile**: acoustic similarity from user's taste centroid
- **cf**: collaborative filtering ("users who liked X also liked Y")
- **artist_recall**: tracks from recently listened artists
- **popular**: globally popular tracks (fallback)

Optionally provide a `seed_track_id` to bias results toward a specific track.

Each result includes the source tag for debugging.

Returns a `request_id` that ties this served list to the client-side
`reco_impression` events and the plays that follow. The server does **not** log
impressions itself — an impression means a track *actually shown* to the user,
which only the client knows; the served list is recorded in the audit tables.
""",
)
async def get_recommendations(
    user_id: str,
    seed_track_id: str = Query(None, description="Optional seed track to bias content candidates"),
    limit: int = Query(25, ge=1, le=100),
    # Context params (all optional — client sends what it knows).
    device_type: str = Query(None, description="Device class: mobile, desktop, speaker, car, web"),
    output_type: str = Query(
        None, description="Audio output: headphones, speaker, bluetooth_speaker, car_audio, built_in, airplay"
    ),
    context_type: str = Query(None, description="Listening context: playlist, album, radio, search, home_shelf"),
    location_label: str = Query(None, description="Semantic location: home, work, gym, commute"),
    hour_of_day: int = Query(None, ge=0, le=23, description="Client's local hour (0-23)"),
    day_of_week: int = Query(None, ge=1, le=7, description="Client's local day of week (1=Mon, 7=Sun)"),
    genre: str = Query(None, description="Filter candidates by genre (case-insensitive substring match)"),
    mood: str = Query(
        None,
        description=(
            "Filter candidates by mood tag label. Must be one of: "
            "happy, sad, aggressive, relaxed, party. Requires confidence > 0.3"
        ),
    ),
    # Discovery dial (recommendation modes) — see app/services/modes.py.
    discovery: float = Query(
        None,
        ge=0.0,
        le=1.0,
        description="Discovery dial: 0.0 = familiar (proven favourites) … 1.0 = deep discovery (nothing you've heard)",
    ),
    mode: str = Query(
        None,
        description=(
            "Named dial preset: familiar, balanced, discovery, deep_discovery. "
            "Takes precedence over 'discovery' when both are given."
        ),
    ),
    debug: bool = Query(
        False, description="Include debug info: candidates by source, feature vectors, reranker actions"
    ),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    request_t0 = time.monotonic()

    # Debug mode exposes model internals and feature vectors — restrict
    # to admin API keys regardless of environment.
    if debug:
        require_admin(_key)

    # Per-user authorization check.
    validate_user_id(user_id)
    check_user_access(_key, user_id)

    # Validate mood against the EffNet pipeline's actual labels — silently
    # filtering on unknown moods used to swallow every candidate and
    # surface as "no available tracks" client-side.
    if mood is not None:
        from app.services.audio_analysis import SUPPORTED_MOOD_LABELS

        if mood not in SUPPORTED_MOOD_LABELS:
            raise HTTPException(
                status_code=400,
                detail=(f"Unknown mood {mood!r}. Must be one of: {sorted(SUPPORTED_MOOD_LABELS)}."),
            )

    # Validate the dial preset name against the known presets (422 on unknown).
    if mode is not None:
        from app.models.algorithm_config_schema import PRESET_NAMES

        if mode not in PRESET_NAMES:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown mode {mode!r}. Must be one of: {list(PRESET_NAMES)}.",
            )

    # Verify user exists.
    result = await session.execute(select(User.user_id).where(User.user_id == user_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Not found.")

    # Verify seed track if provided.
    if seed_track_id:
        result = await session.execute(select(TrackFeatures.track_id).where(TrackFeatures.track_id == seed_track_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Not found.")

    # --- Resolve the discovery dial into a whitelisted, request-scoped override ---
    # A named `mode` wins over a raw `discovery` float; neither -> the default
    # preset (balanced, == today). resolve_dial is the untrusted-input boundary —
    # it only ever emits whitelisted keys. The override is applied inside
    # generate_recommendation_payload (around candidate_gen + rerank).
    from app.services import mix_cache
    from app.services import modes as modes_svc
    from app.services.algorithm_config import get_config, get_config_version

    dial = modes_svc.resolve_dial(discovery, mode, get_config().modes)
    model_version = _get_model_version()

    gen_kwargs = dict(
        user_id=user_id,
        dial=dial,
        limit=limit,
        model_version=model_version,
        seed_track_id=seed_track_id,
        genre=genre,
        mood=mood,
        hour_of_day=hour_of_day,
        day_of_week=day_of_week,
        device_type=device_type,
        output_type=output_type,
        context_type=context_type,
        location_label=location_label,
        want_debug=debug,
    )

    # --- Stale-while-revalidate mix cache (Chunk 6) ---
    # Cache only "plain" mode/dial requests — seed/genre/mood/debug carry params
    # that aren't in the key, so those bypass the cache and always generate fresh
    # (today's path, byte-for-byte). Impressions + audit always run per request,
    # so cache hits stay fully tracked.
    cacheable = bool(settings.MIX_CACHE_ENABLED) and not debug and not seed_track_id and not genre and not mood
    payload = None
    if cacheable:
        from app.db.session import AsyncSessionLocal

        cache_key = mix_cache.build_key(
            user_id=user_id,
            dial_bucket=_dial_bucket(dial),
            context_bucket=_context_bucket(
                device_type, output_type, context_type, location_label, hour_of_day, day_of_week
            ),
            limit=limit,
            model_version=model_version,
            config_version=get_config_version(),
        )
        cached, state = mix_cache.peek(cache_key)
        if state == "fresh":
            payload = cached
        elif state == "stale":
            payload = cached
            mix_cache.schedule_rebuild(cache_key, _make_payload_builder(AsyncSessionLocal, gen_kwargs))

    if payload is None:
        payload = await generate_recommendation_payload(session, **gen_kwargs)
        if cacheable:
            mix_cache.put(cache_key, payload)

    # --- No-candidates short-circuit ---
    if payload["reason"] is not None:
        return {
            "request_id": str(uuid.uuid4()),
            "tracks": [],
            "model_version": model_version,
            "user_id": user_id,
            "seed_track_id": seed_track_id,
            "discovery": dial.discovery,
            "reason": payload["reason"],
        }

    reranked = payload["reranked"]
    source_map = payload["source_map"]
    sources_by_tid = payload["sources_by_tid"]
    actions_by_tid = payload["actions_by_tid"]
    track_meta = payload["track_meta"]

    # Generate a request_id that ties this served list to the client-side
    # reco_impression events and the play events that follow, and to the audit row below.
    request_id = str(uuid.uuid4())

    # NOTE: The server intentionally does NOT write reco_impression events here.
    # A reco_impression means a track *actually shown to the user* — only the client
    # knows that, and it fires impressions for the tiles it renders. Serving the whole
    # reranked list is provenance, not an impression, and is captured by the audit
    # tables below (RecommendationRequestAudit / RecommendationCandidateAudit). Writing
    # one impression per reranked track previously polluted ranker training: the unshown
    # tail of every list became false "shown-but-not-played" negatives.

    # --- Fire-and-forget audit write ---
    if settings.RECO_AUDIT_ENABLED:
        from app.services.reco_audit import write_audit

        duration_ms = int((time.monotonic() - request_t0) * 1000)
        request_context_payload = {
            "device_type": device_type,
            "output_type": output_type,
            "context_type": context_type,
            "location_label": location_label,
            "hour_of_day": hour_of_day,
            "day_of_week": day_of_week,
            "genre": genre,
            "mood": mood,
        }
        asyncio.create_task(
            write_audit(
                request_id=request_id,
                user_id=user_id,
                surface="recommend_api",
                seed_track_id=seed_track_id,
                context_id=None,
                request_context=request_context_payload,
                model_version=model_version,
                config_version=get_config_version(),
                duration_ms=duration_ms,
                limit_requested=limit,
                candidates_by_source=payload["candidates_by_source"],
                candidate_rows=payload["candidate_rows"],
            )
        )

    # Build response.
    tracks = []
    for i, (tid, score) in enumerate(reranked):
        track_data = {
            "position": i,
            "track_id": tid,
            "source": source_map.get(tid, "unknown"),
            "score": round(score, 4),
            "reasons": modes_svc.derive_reasons(sources_by_tid.get(tid, []), actions_by_tid.get(tid, [])),
        }
        meta = track_meta.get(tid)
        if meta:
            track_data.update(meta)
        tracks.append(track_data)

    response = {
        "request_id": request_id,
        "model_version": model_version,
        "user_id": user_id,
        "seed_track_id": seed_track_id,
        "discovery": dial.discovery,
        "context": {
            "hour_of_day": hour_of_day,
            "day_of_week": day_of_week,
            "device_type": device_type,
            "output_type": output_type,
            "context_type": context_type,
            "location_label": location_label,
        },
        "tracks": tracks,
    }
    if debug and payload["debug_data"]:
        response["debug"] = payload["debug_data"]
    return response


@router.post(
    "/users/{user_id}/mixes/prewarm",
    status_code=202,
    summary="Prewarm a user's recommendation mixes",
    description=(
        "Warms the SWR mix cache for the given user's preset mixes in the "
        "background so a subsequent `GET /v1/recommend/{user_id}?mode=...` is "
        "served instantly. Rate-limited per (key, user); the background fan-out "
        "is bounded by a semaphore and a max-modes cap. Returns 202 immediately."
    ),
)
async def prewarm_mixes(
    user_id: str,
    body: PrewarmRequest | None = None,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    from app.core.security import check_rate_limit, hash_key
    from app.db.session import AsyncSessionLocal
    from app.models.algorithm_config_schema import PRESET_NAMES
    from app.services import mix_cache
    from app.services import modes as modes_svc
    from app.services.algorithm_config import get_config, get_config_version

    validate_user_id(user_id)
    check_user_access(_key, user_id)
    # Bound how often a caller can trigger background warm-ups for a user.
    check_rate_limit(f"mix_prewarm:{hash_key(_key)}:{user_id}", limit=settings.MIX_PREWARM_RATE_LIMIT_PER_MIN)

    result = await session.execute(select(User.user_id).where(User.user_id == user_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Not found.")

    body = body or PrewarmRequest()
    requested = body.modes if body.modes else list(PRESET_NAMES)
    # Drop unknown presets and cap the fan-out (resource bound).
    valid_modes = [m for m in requested if m in PRESET_NAMES][: settings.MIX_PREWARM_MAX_MODES]

    modes_cfg = get_config().modes
    model_version = _get_model_version()
    config_version = get_config_version()

    warmed: list[str] = []
    for m in valid_modes:
        dial = modes_svc.resolve_dial(None, m, modes_cfg)
        cache_key = mix_cache.build_key(
            user_id=user_id,
            dial_bucket=_dial_bucket(dial),
            context_bucket=_context_bucket(None, None, None, None, None, None),
            limit=body.limit,
            model_version=model_version,
            config_version=config_version,
        )
        gen_kwargs = dict(
            user_id=user_id,
            dial=dial,
            limit=body.limit,
            model_version=model_version,
            seed_track_id=None,
            genre=None,
            mood=None,
            hour_of_day=None,
            day_of_week=None,
            device_type=None,
            output_type=None,
            context_type=None,
            location_label=None,
            want_debug=False,
        )
        # get_or_build is a no-op when the key is already fresh, and is
        # semaphore-bounded, so repeated prewarms don't pile up work.
        asyncio.create_task(mix_cache.get_or_build(cache_key, _make_payload_builder(AsyncSessionLocal, gen_kwargs)))
        warmed.append(m)

    return {"status": "warming", "user_id": user_id, "modes": warmed, "limit": body.limit}


@router.get(
    "/users/{user_id}/mixes",
    summary="List suggested recommendation mixes (shelves) for a user",
    description=(
        "Returns a menu of suggested shelves, each a ready-to-call recommend "
        "request spec (mode + endpoint). Powers the multi-shelf home view."
    ),
)
async def list_mixes(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    from app.services.algorithm_config import get_config

    validate_user_id(user_id)
    check_user_access(_key, user_id)

    result = await session.execute(select(User.user_id).where(User.user_id == user_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Not found.")

    modes_cfg = get_config().modes
    shelves = [
        ("on_repeat", "On Repeat", "familiar"),
        ("your_mix", "Your Mix", "balanced"),
        ("discover", "Discover", "discovery"),
        ("deep_cuts", "Deep Cuts", "deep_discovery"),
    ]
    mixes = []
    for shelf_id, title, preset in shelves:
        if not hasattr(modes_cfg, preset):
            continue
        mixes.append(
            {
                "id": shelf_id,
                "title": title,
                "mode": preset,
                "discovery": float(modes_cfg.dial_anchors.get(preset, 0.0)),
                "endpoint": f"/v1/recommend/{user_id}?mode={preset}",
            }
        )
    return {"user_id": user_id, "mixes": mixes}


@router.get(
    "/recommend/{user_id}/history",
    summary="Get recommendation history for a user",
    description="Returns past recommendation impressions with whether the user streamed the recommended track.",
)
async def get_recommendation_history(
    user_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    from sqlalchemy import func as sa_func

    validate_user_id(user_id)
    check_user_access(_key, user_id)

    # Verify user exists.
    result = await session.execute(select(User.user_id).where(User.user_id == user_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Not found.")

    # Count impressions
    count_q = select(sa_func.count()).select_from(
        select(ListenEvent.id)
        .where(ListenEvent.user_id == user_id, ListenEvent.event_type == "reco_impression")
        .subquery()
    )
    total = (await session.execute(count_q)).scalar() or 0

    # Get impression events, most recent first
    q = (
        select(ListenEvent)
        .where(ListenEvent.user_id == user_id, ListenEvent.event_type == "reco_impression")
        .order_by(ListenEvent.timestamp.desc())
        .offset(offset)
        .limit(limit)
    )
    impressions = (await session.execute(q)).scalars().all()

    if not impressions:
        return {"total": total, "history": []}

    # Check which impressions led to streams (play_start/play_end with same request_id)
    request_ids = list({i.request_id for i in impressions if i.request_id})
    streamed_set = set()
    if request_ids:
        stream_q = select(ListenEvent.request_id, ListenEvent.track_id).where(
            ListenEvent.user_id == user_id,
            ListenEvent.request_id.in_(request_ids),
            ListenEvent.event_type.in_(["play_start", "play_end"]),
        )
        stream_rows = (await session.execute(stream_q)).all()
        streamed_set = {(r[0], r[1]) for r in stream_rows}

    # Get track metadata for all impression track_ids
    track_ids = list({i.track_id for i in impressions})
    feat_q = select(TrackFeatures).where(TrackFeatures.track_id.in_(track_ids))
    feat_map = {t.track_id: t for t in (await session.execute(feat_q)).scalars().all()}

    return {
        "total": total,
        "history": [
            {
                "timestamp": imp.timestamp,
                "track_id": imp.track_id,
                "media_server_id": feat_map[imp.track_id].media_server_id if imp.track_id in feat_map else None,
                "position": imp.position,
                "request_id": imp.request_id,
                "model_version": imp.model_version,
                "streamed": (imp.request_id, imp.track_id) in streamed_set if imp.request_id else False,
                "title": feat_map[imp.track_id].title if imp.track_id in feat_map else None,
                "artist": feat_map[imp.track_id].artist if imp.track_id in feat_map else None,
                "bpm": feat_map[imp.track_id].bpm if imp.track_id in feat_map else None,
                "energy": feat_map[imp.track_id].energy if imp.track_id in feat_map else None,
            }
            for imp in impressions
        ],
    }


@router.get(
    "/recommend/{user_id}/artists",
    summary="Get recommended artists for a user",
    description="""
Returns a ranked list of recommended artists for the given user,
derived from listening behavior, taste profile, and Last.fm similarity.

Sources:
- **listening**: Artists from the user's most-played/liked tracks, ranked by
  aggregate satisfaction and recency.
- **lastfm_similar**: Similar artists to the user's top artists via Last.fm API.
- **lastfm_top**: User's Last.fm top artists (if connected).

Each artist includes aggregate audio stats from their library tracks and
the user's listening history with that artist (if any).
""",
)
async def get_recommended_artists(
    user_id: str,
    limit: int = Query(20, ge=1, le=100),
    mode: str = Query("discover", description="Blend mode: familiar | balanced | discover"),
    include_discovery: bool = Query(True, description="Include FAISS/Last.fm discovery candidates"),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    validate_user_id(user_id)
    check_user_access(_key, user_id)

    # --- Verify user exists ---
    user_row = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
    if user_row is None:
        raise HTTPException(status_code=404, detail="User not found.")

    # --- Algorithm-driven blend: content centroid (FAISS) + ranker roll-up +
    #     Last.fm + the legacy play-count heuristic, shifted by `mode`. Degrades
    #     to the heuristic when embeddings and the ranker are both unavailable.
    #     See app/services/artist_reco.py.
    from app.services import artist_reco

    reco = await artist_reco.recommend_artists(
        session, user_id, mode=mode, limit=limit, include_discovery=include_discovery
    )
    ranked = reco["artists"]

    # --- Batch-fetch artist images from cover_art_cache ---
    import re

    _STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)

    def _cover_norm(s: str) -> str:
        n = s.strip().lower()
        if n.startswith("the "):
            n = n[4:]
        n = _STRIP_RE.sub("", n)
        return " ".join(n.split())

    artist_norms = {_cover_norm(a["name"]): a["name"] for a in ranked}
    img_map: dict[str, str] = {}
    if artist_norms:
        img_q = await session.execute(
            select(CoverArtCache.artist_norm, CoverArtCache.url).where(
                CoverArtCache.artist_norm.in_(list(artist_norms.keys())),
                CoverArtCache.title_norm == "",
            )
        )
        img_map = {r.artist_norm: r.url for r in img_q.all() if r.url}

    # Resolve any artists still without an image by hitting the download backend
    # (#56). resolve_artist_image reads the same cover_art_cache table first, so
    # any negative-cached entries inside the TTL skip the upstream call. We
    # parallelise across fresh sessions because AsyncSession is single-threaded;
    # cap the fan-out so a cold-cache page load doesn't fire dozens of upstream
    # calls at once.
    _ARTIST_IMG_RESOLVE_CAP = 12
    missing_artist_names = [artist_norms[n] for n in artist_norms if n not in img_map][:_ARTIST_IMG_RESOLVE_CAP]
    if missing_artist_names:
        import asyncio

        from app.db.session import AsyncSessionLocal
        from app.services.cover_art import resolve_artist_image

        async def _resolve_one(name: str) -> tuple[str, str | None]:
            try:
                async with AsyncSessionLocal() as own_session:
                    url = await resolve_artist_image(own_session, name)
                    await own_session.commit()
            except Exception:
                url = None
            return _cover_norm(name), url

        results = await asyncio.gather(
            *[_resolve_one(n) for n in missing_artist_names],
            return_exceptions=False,
        )
        for norm, url in results:
            if url:
                img_map[norm] = url

    for a in ranked:
        a["image_url"] = img_map.get(_cover_norm(a["name"]))

    # --- Fetch per-user top tracks for each artist ---
    artist_names = [a["name"] for a in ranked]
    top_tracks_q = (
        select(
            TrackFeatures.artist,
            TrackFeatures.track_id,
            TrackFeatures.media_server_id,
            TrackFeatures.title,
            TrackFeatures.album,
            TrackFeatures.duration,
            TrackInteraction.satisfaction_score,
            TrackInteraction.play_count,
        )
        .join(TrackInteraction, TrackInteraction.track_id == TrackFeatures.track_id)
        .where(
            TrackInteraction.user_id == user_id,
            TrackFeatures.artist.in_(artist_names),
        )
        .order_by(TrackInteraction.satisfaction_score.desc())
    )
    top_rows = (await session.execute(top_tracks_q)).all()

    # Group by artist, take top 5 per artist
    from collections import defaultdict

    tracks_by_artist: dict[str, list] = defaultdict(list)
    for r in top_rows:
        if len(tracks_by_artist[r.artist]) < 5:
            tracks_by_artist[r.artist].append(
                {
                    "track_id": r.track_id,
                    "media_server_id": r.media_server_id,
                    "title": r.title,
                    "album": r.album,
                    "duration": r.duration,
                    "satisfaction_score": round(r.satisfaction_score, 4) if r.satisfaction_score else 0,
                    "play_count": r.play_count or 0,
                }
            )

    for a in ranked:
        a["top_tracks"] = tracks_by_artist.get(a["name"], [])

    return {
        "user_id": user_id,
        "mode": reco["mode"],
        "generated_at": reco["generated_at"],
        "total": len(ranked),
        "artists": ranked,
    }


@router.get(
    "/recommend/{user_id}/albums",
    summary="Get recommended albums for a user",
    description="""
Returns a ranked list of in-library albums for the given user, scored by
blending the track ranker (rolled up to album level), library coverage, a
"rediscover" freshness boost, and audio coherence with the user's taste.

Albums are grouped by ``(album_artist or artist, album)``. This is a
library-only surface — discovery / acquire handles are not wired in this pass
(``acquire`` is always null).

Modes: **familiar** (proven, coverage-led), **balanced**, **discover**
(audio-coherence-led). Each album carries ``sources``/``reasons``/``signals``
for "Because you listen to …"-style badges.
""",
)
async def get_recommended_albums(
    user_id: str,
    limit: int = Query(20, ge=1, le=100),
    mode: str = Query("discover", description="Blend mode: familiar | balanced | discover"),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    validate_user_id(user_id)
    check_user_access(_key, user_id)

    user_row = (await session.execute(select(User.user_id).where(User.user_id == user_id))).scalar_one_or_none()
    if user_row is None:
        raise HTTPException(status_code=404, detail="User not found.")

    from app.services import album_reco

    reco = await album_reco.recommend_albums(session, user_id, mode=mode, limit=limit)
    albums = reco["albums"]

    # --- Resolve cover art for the returned albums (capped fan-out across
    #     fresh sessions, mirroring the artists handler). resolve_cover_art
    #     reads the same cover_art_cache first, so warm entries skip upstream.
    _ALBUM_COVER_RESOLVE_CAP = 12
    cover_tasks = [
        (i, a["album_artist"], a["album"]) for i, a in enumerate(albums) if a.get("album_artist") and a.get("album")
    ][:_ALBUM_COVER_RESOLVE_CAP]
    if cover_tasks:
        import asyncio

        from app.db.session import AsyncSessionLocal
        from app.services.cover_art import resolve_cover_art

        async def _resolve_album_cover(idx: int, artist: str, album: str) -> tuple[int, str | None]:
            try:
                async with AsyncSessionLocal() as own_session:
                    url = await resolve_cover_art(own_session, artist, album)
                    await own_session.commit()
            except Exception:
                url = None
            return idx, url

        results = await asyncio.gather(*[_resolve_album_cover(i, ar, al) for i, ar, al in cover_tasks])
        for idx, url in results:
            if url:
                albums[idx]["cover_url"] = url

    return {
        "user_id": user_id,
        "mode": reco["mode"],
        "generated_at": reco["generated_at"],
        "total": len(albums),
        "albums": albums,
    }


@router.get(
    "/stats/model",
    summary="Get recommendation model stats and evaluation metrics",
    description="Returns ranker training info, latest offline evaluation metrics, and impression-to-stream stats.",
)
async def get_model_stats(
    _key: str = Depends(require_api_key),
):
    from app.core.security import require_admin

    require_admin(_key)
    from app.services.evaluation import get_model_report

    return await get_model_report()
