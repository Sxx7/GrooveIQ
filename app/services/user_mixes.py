"""
GrooveIQ – Session-clustered rotating mixes (the "Made for you / More mixes" engine).

Clusters a user's recently-engaged tracks on the session co-listening embedding
(``session_embeddings``, a Word2Vec skip-gram over listening sessions) — NOT
genre/artist — into a handful of mixes that persist with a shelf-life lifecycle:

    active    shown + rotated slowly (<= max_churn swap per refresh -> stable ~80% core)
    stale     its cluster no longer forms (transient)
    archived  hidden; membership snapshot kept
    nostalgic (read-derived) an archived mix resurfaced after dormancy

Tracks too thinly co-listened to have a session vector fall back to their acoustic
embedding (``TrackFeatures.embedding``); they are placed into the nearest cluster and
kept only while subsequent event data (reflected in their engagement score) supports
them — a skipped fallback track sinks in the ranking and rotates out.

Design notes
------------
* Naming is intentionally omitted. A mix carries a generic ``ordinal`` (Mix 1, Mix 2
  …); the client shows "Mix N" or nothing. (Mood/energy names collapse for narrow-taste
  users — validated against prod, 2026-06-23.)
* Clustering reuses ``session_embeddings``; nostalgia reuses the archived snapshot. No
  new model is trained.
* Anti-repeat in v1 = mostly-disjoint mixes (a track lands in at most one active mix per
  rebuild) + the churn cap + engagement ranking. A temporal serve-cooldown
  (``serve_cooldown_days``) is reserved for a follow-up.
"""

from __future__ import annotations

import base64
import logging
import time

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Mix, MixTrack, TrackFeatures, TrackInteraction, User
from app.services import session_embeddings as se
from app.services.algorithm_config import get_config

logger = logging.getLogger(__name__)
DAY = 86400


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _l2(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a)) or 1.0
    nb = float(np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / (na * nb))


def _decode_emb(s: str | None) -> np.ndarray | None:
    if not s:
        return None
    try:
        a = np.frombuffer(base64.b64decode(s), dtype=np.float32)
        return a if a.size >= 8 else None
    except Exception:
        return None


def _engagement_score(it: TrackInteraction) -> float:
    """Per-user engagement in [0, 1]. Prefers the normalised satisfaction_score
    (the trained label); falls back to a like/repeat/full-listen blend."""
    if it.satisfaction_score is not None:
        return float(it.satisfaction_score)
    return min(
        1.0,
        ((it.like_count or 0) + (it.repeat_count or 0) * 0.8 + (it.full_listen_count or 0) * 0.5) / 4.0,
    )


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------


async def _engaged_pool(db: AsyncSession, user_id: str, cfg, now: int) -> dict[str, float]:
    """Recently-engaged tracks {track_id: engagement_score} within the window."""
    cutoff = now - cfg.window_days * DAY
    rows = (
        await db.execute(
            select(TrackInteraction).where(
                TrackInteraction.user_id == user_id,
                TrackInteraction.last_played_at.isnot(None),
                TrackInteraction.last_played_at >= cutoff,
                TrackInteraction.dislike_count == 0,
            )
        )
    ).scalars().all()
    pool: dict[str, float] = {}
    for r in rows:
        if (r.play_count or 0) >= 2 or (r.like_count or 0) > 0 or (r.repeat_count or 0) > 0 or (r.full_listen_count or 0) > 0:
            score = _engagement_score(r)
            if score >= cfg.min_satisfaction:
                pool[r.track_id] = score
    return pool


async def _embeddings(db: AsyncSession, track_ids: list[str]) -> dict[str, np.ndarray]:
    """Acoustic embedding per track_id (for the session-vocab fallback)."""
    out: dict[str, np.ndarray] = {}
    for i in range(0, len(track_ids), 400):
        chunk = track_ids[i : i + 400]
        for tid, emb in (
            await db.execute(
                select(TrackFeatures.track_id, TrackFeatures.embedding).where(TrackFeatures.track_id.in_(chunk))
            )
        ).all():
            v = _decode_emb(emb)
            if v is not None:
                out[tid] = v
    return out


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _balanced_clusters(n: int, X: np.ndarray, cfg) -> list[list[int]]:
    """KMeans then size-balance: split clusters > max_size, drop those < min_size,
    cap at max_mixes. Returns a list of index-lists into the row order of ``X``.

    Validated against prod (2026-06-23): vanilla KMeans produced [330,…,1]; this
    balancing yields ~target-sized clusters."""
    from sklearn.cluster import KMeans

    if n == 0:
        return []
    k0 = max(cfg.min_mixes, min(cfg.max_mixes, max(1, round(n / cfg.target_size))))
    if n < cfg.min_size * 2 or k0 < 2:
        return [list(range(n))]
    km = KMeans(n_clusters=k0, n_init=10, random_state=42).fit(X)
    queue = [[i for i in range(n) if km.labels_[i] == c] for c in range(k0)]
    out: list[list[int]] = []
    while queue:
        cl = queue.pop()
        if len(cl) > cfg.max_size and len(cl) >= 4:
            sub = KMeans(n_clusters=2, n_init=10, random_state=42).fit(X[cl])
            g0 = [cl[i] for i in range(len(cl)) if sub.labels_[i] == 0]
            g1 = [cl[i] for i in range(len(cl)) if sub.labels_[i] == 1]
            if min(len(g0), len(g1)) >= 2:
                queue.extend([g0, g1])
            else:
                out.append(cl)
        else:
            out.append(cl)
    out.sort(key=len, reverse=True)
    kept = [c for c in out if len(c) >= cfg.min_size] or out[: cfg.min_mixes]
    return kept[: cfg.max_mixes]


def _rotate(old: list[str], desired_ranked: list[str], cfg) -> list[str]:
    """Slowly evolve a mix toward the engagement-ranked top ``target_size`` of its
    cluster, swapping at most ``max_churn`` of ``target_size`` per refresh so a
    stable ~80% core persists.

    ``desired_ranked`` is the cluster's full membership, best-engaged first; the
    ideal membership is its top ``target_size``. A hysteresis band (top
    ``size + max_swap``) keeps a track that's hovering just past the cut from
    oscillating in and out. A brand-new mix (empty ``old``) is just the top target."""
    size = cfg.target_size
    target = desired_ranked[:size]
    if not old:
        return list(target)
    max_swap = max(1, round(size * cfg.max_churn))
    band = set(desired_ranked[: size + max_swap])  # "still fine" hysteresis band
    fell_out = [t for t in old if t not in band]  # dropped out of the cluster / past the band
    removed = set(fell_out[:max_swap])  # cap removals -> keep the stable core
    kept = [t for t in old if t not in removed][:size]
    budget = min(max_swap, size - len(kept))
    adds = [t for t in target if t not in kept][:budget]  # promote the best not already in
    out = kept + adds
    if len(out) < size:  # backfill toward target_size
        for t in desired_ranked:
            if t not in out:
                out.append(t)
            if len(out) >= size:
                break
    return out[:size]


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


async def _active_session_mixes(db: AsyncSession, user_id: str) -> list[Mix]:
    return list(
        (
            await db.execute(
                select(Mix).where(Mix.user_id == user_id, Mix.kind == "session", Mix.state == "active")
            )
        ).scalars().all()
    )


async def _member_added(db: AsyncSession, mix_id: int) -> dict[str, int]:
    rows = (
        await db.execute(select(MixTrack.track_id, MixTrack.added_at).where(MixTrack.mix_id == mix_id))
    ).all()
    return {tid: added for tid, added in rows}


async def _ordered_member_ids(db: AsyncSession, mix_id: int) -> list[str]:
    rows = (
        await db.execute(
            select(MixTrack.track_id).where(MixTrack.mix_id == mix_id).order_by(MixTrack.position)
        )
    ).all()
    return [r[0] for r in rows]


async def _write_members(
    db: AsyncSession, mix: Mix, members: list[str], pool: dict[str, float], provisional: set[str], now: int
) -> None:
    """Replace a mix's MixTrack rows, preserving ``added_at`` for retained tracks."""
    old_added = await _member_added(db, mix.id)
    await db.execute(delete(MixTrack).where(MixTrack.mix_id == mix.id))
    for pos, tid in enumerate(members):
        db.add(
            MixTrack(
                mix_id=mix.id,
                track_id=tid,
                position=pos,
                score=pool.get(tid),
                provisional=tid in provisional,
                added_at=old_added.get(tid, now),
            )
        )
    mix.track_count = len(members)


# ---------------------------------------------------------------------------
# Rebuild (clustering + reconciliation + persistence)
# ---------------------------------------------------------------------------


async def rebuild_user_mixes(db: AsyncSession, user_id: str, *, now: int | None = None) -> dict:
    """Rebuild one user's session mixes. Idempotent and self-reconciling: an
    existing mix whose cluster still forms keeps its id and stable core; a mix
    whose cluster is gone is archived; new clusters become new mixes."""
    cfg = get_config().mixes
    now = int(now if now is not None else time.time())

    if not se.is_ready():
        se.load_latest()

    pool = await _engaged_pool(db, user_id, cfg, now)
    vec_map = se.get_vectors(list(pool.keys()))  # session vectors (in-vocab only)
    backbone = [t for t in pool if t in vec_map]

    if len(backbone) < cfg.min_session_vectors:
        # Cold start: not enough co-listening signal. Archive any active mixes
        # (their world is gone) so the client falls back to genre mixes.
        archived = await _archive_active(db, user_id, now)
        await db.commit()
        return {"user_id": user_id, "built": 0, "reason": "cold_start", "engaged": len(pool),
                "backbone": len(backbone), "archived": archived}

    X = np.vstack([_l2(vec_map[t]) for t in backbone])
    clusters = _balanced_clusters(len(backbone), X, cfg)

    # Acoustic embeddings for the session-vocab fallback.
    acoustic = await _embeddings(db, list(pool.keys()))

    # Build the desired clusters: session centroid (for re-matching), acoustic
    # centroid (for assigning fallback tracks), and the engagement-ranked members.
    desired: list[dict] = []
    for cl in clusters:
        ids = [backbone[i] for i in cl]
        sess_centroid = np.mean([vec_map[t] for t in ids], axis=0)
        ac_vecs = [acoustic[t] for t in ids if t in acoustic]
        ac_centroid = _l2(np.mean(ac_vecs, axis=0)) if ac_vecs else None
        desired.append({"session_centroid": sess_centroid, "ac_centroid": ac_centroid, "members": set(ids)})

    # Assign acoustic-only (no session vector) tracks to the nearest cluster.
    provisional: set[str] = set()
    for t in pool:
        if t in vec_map or t not in acoustic or not desired:
            continue
        av = _l2(acoustic[t])
        best_j, best_c = -1, -2.0
        for j, d in enumerate(desired):
            if d["ac_centroid"] is None:
                continue
            c = float(np.dot(av, d["ac_centroid"]))
            if c > best_c:
                best_c, best_j = c, j
        if best_j >= 0:
            desired[best_j]["members"].add(t)
            provisional.add(t)

    for d in desired:
        d["ranked"] = sorted(d["members"], key=lambda t: -pool.get(t, 0.0))

    # ---- reconcile with existing active mixes by session-centroid cosine ----
    existing = await _active_session_mixes(db, user_id)
    MATCH_MIN = 0.5
    matched: dict[int, Mix] = {}
    used_mix: set[int] = set()
    for di, d in enumerate(desired):
        best_mix, best_c = None, MATCH_MIN
        for m in existing:
            if m.id in used_mix or not m.centroid:
                continue
            c = _cos(d["session_centroid"], np.asarray(m.centroid, dtype=np.float32))
            if c > best_c:
                best_c, best_mix = c, m
        if best_mix is not None:
            matched[di] = best_mix
            used_mix.add(best_mix.id)

    # Pass 1: reserve members of matched mixes that are NOT due for rotation, so
    # due/new mixes can't poach their stable tracks.
    placed: set[str] = set()
    keep_members: dict[int, list[str]] = {}
    for di, d in enumerate(desired):
        m = matched.get(di)
        if m is not None and (m.expires_at or 0) > now:
            members = await _ordered_member_ids(db, m.id)
            keep_members[di] = members
            placed.update(members)

    # Pass 2: build/rotate the due and new mixes.
    persisted: list[Mix] = []
    for di, d in enumerate(desired):
        m = matched.get(di)
        if di in keep_members:  # matched + not due -> leave membership untouched
            m.state = "active"
            m.centroid = [float(x) for x in d["session_centroid"]]
            persisted.append(m)
            continue
        old_members = await _ordered_member_ids(db, m.id) if m is not None else []
        ranked = [t for t in d["ranked"] if t not in placed]
        new_members = _rotate(old_members, ranked, cfg)
        placed.update(new_members)
        if m is None:
            m = Mix(user_id=user_id, kind="session", state="active", created_at=now)
            db.add(m)
            await db.flush()  # assign m.id
        m.state = "active"
        m.centroid = [float(x) for x in d["session_centroid"]]
        m.refreshed_at = now
        m.expires_at = now + int(cfg.refresh_days * DAY)
        await _write_members(db, m, new_members, pool, provisional, now)
        persisted.append(m)

    # Existing active mixes that matched no cluster -> archived (vibe gone).
    for m in existing:
        if m.id not in used_mix:
            m.state = "archived"
            m.archived_at = now

    # Generic ordinals (largest mix first) over the surviving active set.
    persisted.sort(key=lambda m: -(m.track_count or 0))
    for i, m in enumerate(persisted):
        m.ordinal = i + 1

    await db.commit()
    return {
        "user_id": user_id,
        "built": len(persisted),
        "engaged": len(pool),
        "backbone": len(backbone),
        "fallback": len(provisional),
        "sizes": [m.track_count for m in persisted],
    }


async def _archive_active(db: AsyncSession, user_id: str, now: int) -> int:
    rows = await _active_session_mixes(db, user_id)
    for m in rows:
        m.state = "archived"
        m.archived_at = now
    return len(rows)


async def rebuild_all(db_factory=None) -> dict:
    """Rebuild every active user's mixes. Scheduler entry point."""
    if db_factory is None:
        from app.db.session import AsyncSessionLocal as db_factory  # noqa: N813
    if not se.is_ready():
        se.load_latest()
    summary = {"users": 0, "built": 0, "cold_start": 0}
    async with db_factory() as db:
        user_ids = [r[0] for r in (await db.execute(select(User.user_id).where(User.is_active.is_(True)))).all()]
    for uid in user_ids:
        try:
            async with db_factory() as db:
                res = await rebuild_user_mixes(db, uid)
            summary["users"] += 1
            if res.get("built"):
                summary["built"] += res["built"]
            elif res.get("reason") == "cold_start":
                summary["cold_start"] += 1
        except Exception as e:  # one user must not break the batch
            logger.warning("user_mixes rebuild failed for %s: %s", uid, e)
    logger.info("user_mixes rebuild_all: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Read side (hydrated for the client)
# ---------------------------------------------------------------------------


async def _hydrate(db: AsyncSession, track_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(track_ids), 400):
        chunk = track_ids[i : i + 400]
        for row in (
            await db.execute(
                select(
                    TrackFeatures.track_id,
                    TrackFeatures.media_server_id,
                    TrackFeatures.title,
                    TrackFeatures.artist,
                    TrackFeatures.album,
                    TrackFeatures.duration,
                ).where(TrackFeatures.track_id.in_(chunk))
            )
        ).all():
            out[row[0]] = {
                "media_server_id": row[1],
                "title": row[2],
                "artist": row[3],
                "album": row[4],
                "duration": row[5],
            }
    return out


async def _serialise(db: AsyncSession, mixes: list[Mix]) -> list[dict]:
    if not mixes:
        return []
    all_rows = (
        await db.execute(
            select(MixTrack.mix_id, MixTrack.track_id, MixTrack.position)
            .where(MixTrack.mix_id.in_([m.id for m in mixes]))
            .order_by(MixTrack.mix_id, MixTrack.position)
        )
    ).all()
    by_mix: dict[int, list[tuple[str, int]]] = {}
    for mix_id, tid, pos in all_rows:
        by_mix.setdefault(mix_id, []).append((tid, pos))
    meta = await _hydrate(db, [tid for _, tid, _ in all_rows])
    result = []
    for m in mixes:
        tracks = []
        for tid, pos in by_mix.get(m.id, []):
            h = meta.get(tid, {})
            tracks.append(
                {
                    "position": pos,
                    "track_id": tid,
                    "media_server_id": h.get("media_server_id"),
                    "title": h.get("title"),
                    "artist": h.get("artist"),
                    "album": h.get("album"),
                    "duration": h.get("duration"),
                }
            )
        result.append(
            {"mix_id": m.id, "ordinal": m.ordinal, "kind": m.kind, "track_count": len(tracks), "tracks": tracks}
        )
    return result


async def get_session_mixes(db: AsyncSession, user_id: str) -> list[dict]:
    """Active session mixes for a user, ordered by ordinal. Empty when cold-start
    (the client then falls back to genre mixes)."""
    mixes = list(
        (
            await db.execute(
                select(Mix)
                .where(Mix.user_id == user_id, Mix.kind == "session", Mix.state == "active")
                .order_by(Mix.ordinal)
            )
        ).scalars().all()
    )
    return await _serialise(db, mixes)


async def get_nostalgic_mixes(db: AsyncSession, user_id: str, *, now: int | None = None) -> list[dict]:
    """Archived mixes that have been dormant long enough to resurface as
    'nostalgic' ("you previously liked"). Read-derived — no separate stored
    object. Self-hides (empty) until an archive ages past the dormancy gate."""
    cfg = get_config().mixes
    if cfg.nostalgia_max <= 0:
        return []
    now = int(now if now is not None else time.time())
    cutoff = now - int(cfg.nostalgia_dormancy_days * DAY)
    mixes = list(
        (
            await db.execute(
                select(Mix)
                .where(
                    Mix.user_id == user_id,
                    Mix.state == "archived",
                    Mix.archived_at.isnot(None),
                    Mix.archived_at <= cutoff,
                )
                .order_by(Mix.archived_at.desc())
                .limit(cfg.nostalgia_max)
            )
        ).scalars().all()
    )
    serialised = await _serialise(db, mixes)
    for s in serialised:
        s["kind"] = "nostalgic"
    return serialised
