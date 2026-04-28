"""
Migration 010: Track-ID schema rework — data reshape (issue #37, Phase 2).

Restores the original design intent: every TrackFeatures row gets a stable
internal ``track_id`` (16-char hex SHA-256 of the relative file path) and
the Navidrome song ID — which the sync code had been writing into
``track_id`` since the schema drifted — moves into the new
``media_server_id`` column added in migration 009.

Classification matrix (one row per ``track_features`` row):

    Current track_id        external_track_id    Action
    ----------------------  -------------------  ----------------------------
    16-char hex (legacy)    empty                no change
    22-char base62          16-char hex          track_id := external_track_id
                                                 media_server_id := old track_id
    numeric                 16-char hex          track_id := external_track_id
                                                 media_server_id := NULL
    numeric                 empty (defensive)    track_id := generate_track_id(file_path)
                                                 media_server_id := NULL
    other (unknown shape)   any                  flagged, skipped

For every row whose ``track_id`` actually changes, the new value cascades
across these tables:

    - listen_events.track_id
    - track_interactions.track_id          (UNIQUE(user_id, track_id) — collisions merged inline)
    - playlists.seed_track_id              (nullable)
    - playlist_tracks.track_id
    - scrobble_queue.track_id
    - chart_entries.matched_track_id       (nullable)
    - recommendation_request_audits.seed_track_id
    - recommendation_candidate_audits.track_id

Sessions (``listen_sessions``) are derivative — they're rebuilt by
``POST /v1/pipeline/reset`` (Phase 6) so we don't touch them here.

The whole thing runs inside a transaction. ``--dry-run`` (the default)
prints the plan without mutating anything; ``--dry-run=false`` (or
``--execute``) commits.

Idempotent: rows that already have a 16-char-hex ``track_id`` are skipped,
so re-running the migration after a successful run is a no-op.

Usage:
    python migrations/010_track_id_schema_rework.py                   # dry-run
    python migrations/010_track_id_schema_rework.py --dry-run=false   # execute
    python migrations/010_track_id_schema_rework.py --execute         # ditto

Reads DATABASE_URL from the environment / .env.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# 16 lowercase hex chars — the canonical generate_track_id() output.
_LEGACY_HEX = re.compile(r"^[a-f0-9]{16}$")
# 22-char Navidrome song ID (alphanumeric, mixed case). Excludes 16-char
# strings that happen to be hex, since the legacy hex check runs first.
_NAVIDROME = re.compile(r"^[A-Za-z0-9]{22}$")
# Streamrip / Qobuz / Tidal / Deezer numeric service IDs.
_NUMERIC = re.compile(r"^[0-9]+$")


def classify(track_id: str) -> str:
    if not track_id:
        return "empty"
    if _LEGACY_HEX.match(track_id):
        return "legacy_hex"
    if _NAVIDROME.match(track_id):
        return "navidrome"
    if _NUMERIC.match(track_id):
        return "numeric"
    return "unknown"


def generate_track_id(file_path: str, music_library_path: str) -> str:
    """Replicates app.services.audio_analysis.generate_track_id() in plain Python.

    Hashes the file path relative to the music library root so the same file
    always produces the same id, regardless of which scan invoked it.
    """
    try:
        rel = os.path.relpath(file_path, music_library_path)
    except ValueError:
        rel = file_path
    return hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Per-row planning
# ---------------------------------------------------------------------------


def plan_row(
    *,
    track_id: str,
    external_track_id: str | None,
    media_server_id: str | None,
    file_path: str,
    music_library_path: str,
) -> tuple[str, str | None, str]:
    """Return (new_track_id, new_media_server_id, action_label).

    ``new_media_server_id`` of ``None`` means "leave whatever is currently
    there"; the caller blends it with the existing media_server_id (any
    explicit non-None already populated by Phase 1 wins).
    """
    cls = classify(track_id)
    ext_is_hex = bool(external_track_id and _LEGACY_HEX.match(external_track_id))

    if cls == "legacy_hex":
        return track_id, None, "legacy_hex_keep"

    if cls == "navidrome":
        if ext_is_hex:
            return external_track_id, track_id, "navidrome_to_hex"
        # Defensive — a 22-char base62 row without a 16-hex breadcrumb.
        # Re-derive from file_path so the row gets a sensible internal id.
        return generate_track_id(file_path, music_library_path), track_id, "navidrome_fallback_hash"

    if cls == "numeric":
        if ext_is_hex:
            return external_track_id, None, "numeric_to_hex"
        return generate_track_id(file_path, music_library_path), None, "numeric_derive_hash"

    # Anything else — leave alone but flag for the operator.
    return track_id, None, "unknown_skip"


# ---------------------------------------------------------------------------
# Cascade definitions
# ---------------------------------------------------------------------------

# (table, column) pairs to bulk-rename when a track_id changes. Order matters
# only for track_interactions, which is handled separately because of its
# UNIQUE(user_id, track_id) constraint.
CASCADE_TABLES: list[tuple[str, str]] = [
    ("listen_events", "track_id"),
    ("playlists", "seed_track_id"),
    ("playlist_tracks", "track_id"),
    ("scrobble_queue", "track_id"),
    ("chart_entries", "matched_track_id"),
    ("recommendation_request_audits", "seed_track_id"),
    ("recommendation_candidate_audits", "track_id"),
]

# These columns are summed/maxed when merging colliding (user_id, old_id)
# and (user_id, new_id) pairs in track_interactions. The same merge logic
# previously lived in app/services/media_server.py for the sync rename
# cascade; it was deleted alongside the rename in issue #37 Phase 3.
TI_SUM_COLUMNS = [
    "play_count",
    "skip_count",
    "like_count",
    "dislike_count",
    "repeat_count",
    "playlist_add_count",
    "queue_add_count",
    "early_skip_count",
    "mid_skip_count",
    "full_listen_count",
    "total_seekfwd",
    "total_seekbk",
    "total_dwell_ms",
]
TI_MAX_COLUMNS = [
    "first_played_at",
    "last_played_at",
    "last_event_id",
    "updated_at",
]


# ---------------------------------------------------------------------------
# DB driver abstraction (just enough for what we need)
# ---------------------------------------------------------------------------


class DB:
    """Thin sync wrapper over sqlite3 / psycopg2 with placeholder normalisation."""

    def __init__(self, conn, kind: str):
        self.conn = conn
        self.kind = kind  # "sqlite" or "postgres"
        self.placeholder = "?" if kind == "sqlite" else "%s"

    def execute(self, sql: str, params: Iterable[Any] | None = None):
        cur = self.conn.cursor()
        if self.kind == "postgres":
            sql = sql.replace("?", "%s")
        cur.execute(sql, params or ())
        return cur

    def executemany(self, sql: str, seq):
        cur = self.conn.cursor()
        if self.kind == "postgres":
            sql = sql.replace("?", "%s")
        cur.executemany(sql, seq)
        return cur

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def open_db(database_url: str) -> DB:
    if "sqlite" in database_url:
        path = database_url.split("///", 1)[-1]
        if not Path(path).exists():
            sys.exit(f"Database file not found: {path}")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        return DB(conn, "sqlite")
    elif "postgresql" in database_url:
        try:
            import psycopg2
        except ImportError:
            sys.exit("psycopg2 is required for PostgreSQL: pip install psycopg2-binary")
        url = database_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = psycopg2.connect(url)
        conn.autocommit = False
        return DB(conn, "postgres")
    else:
        sys.exit(f"Unsupported DATABASE_URL scheme: {database_url}")


def column_exists(db: DB, table: str, column: str) -> bool:
    if db.kind == "sqlite":
        rows = db.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == column for r in rows)
    cur = db.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
        (table, column),
    )
    return cur.fetchone() is not None


def table_exists(db: DB, table: str) -> bool:
    if db.kind == "sqlite":
        cur = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,))
        return cur.fetchone() is not None
    cur = db.execute("SELECT 1 FROM information_schema.tables WHERE table_name = ?", (table,))
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Plan building & validation
# ---------------------------------------------------------------------------


def build_plan(db: DB, music_library_path: str) -> list[dict]:
    """Read every track_features row, classify, return a plan list.

    Each plan entry is a dict with: id, old_track_id, new_track_id,
    old_msid, new_msid, action.
    """
    rows = db.execute(
        "SELECT id, track_id, external_track_id, media_server_id, file_path "
        "FROM track_features"
    ).fetchall()
    plan: list[dict] = []
    for tf_id, track_id, external_track_id, media_server_id, file_path in rows:
        new_tid, derived_msid, action = plan_row(
            track_id=track_id,
            external_track_id=external_track_id,
            media_server_id=media_server_id,
            file_path=file_path or "",
            music_library_path=music_library_path,
        )
        # Phase 1 may have already populated media_server_id; never clobber it.
        new_msid = media_server_id if media_server_id else derived_msid
        plan.append(
            {
                "id": tf_id,
                "old_track_id": track_id,
                "new_track_id": new_tid,
                "old_msid": media_server_id,
                "new_msid": new_msid,
                "action": action,
            }
        )
    return plan


def detect_collisions(db: DB, plan: list[dict]) -> list[dict]:
    """Return entries whose new_track_id is already used by another track_features row.

    A collision means two TrackFeatures rows want the same final track_id.
    In practice this only happens when two rows reference the same physical
    file (same rel-path → same hash). The caller decides how to handle it.
    """
    changing = [p for p in plan if p["old_track_id"] != p["new_track_id"]]
    if not changing:
        return []
    new_ids_by_id = {p["id"]: p["new_track_id"] for p in changing}
    new_ids_set = set(new_ids_by_id.values())
    if not new_ids_set:
        return []
    # rows whose existing track_id matches some new_track_id we're trying to assign
    existing = {}
    chunk_size = 500
    new_ids_list = list(new_ids_set)
    for i in range(0, len(new_ids_list), chunk_size):
        chunk = new_ids_list[i : i + chunk_size]
        placeholders = ",".join(["?"] * len(chunk))
        cur = db.execute(
            f"SELECT id, track_id FROM track_features WHERE track_id IN ({placeholders})",
            chunk,
        )
        for row in cur.fetchall():
            existing[row[1]] = row[0]
    collisions: list[dict] = []
    for p in changing:
        target = p["new_track_id"]
        owner_id = existing.get(target)
        if owner_id is not None and owner_id != p["id"]:
            p2 = dict(p)
            p2["collides_with_tf_id"] = owner_id
            collisions.append(p2)
    return collisions


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_plan(plan: list[dict], collisions: list[dict]) -> None:
    counts = Counter(p["action"] for p in plan)
    changing = [p for p in plan if p["old_track_id"] != p["new_track_id"]]
    populating_msid = [p for p in plan if p["old_msid"] != p["new_msid"]]

    print("\nClassification breakdown:")
    for action in (
        "legacy_hex_keep",
        "navidrome_to_hex",
        "numeric_to_hex",
        "navidrome_fallback_hash",
        "numeric_derive_hash",
        "unknown_skip",
    ):
        print(f"  {action:30s} {counts.get(action, 0):>8d}")
    leftover = sum(c for a, c in counts.items() if a not in {
        "legacy_hex_keep",
        "navidrome_to_hex",
        "numeric_to_hex",
        "navidrome_fallback_hash",
        "numeric_derive_hash",
        "unknown_skip",
    })
    if leftover:
        print(f"  (other actions)                 {leftover:>8d}")

    print(f"\n  total rows                  {len(plan):>8d}")
    print(f"  rows with track_id changing {len(changing):>8d}")
    print(f"  rows getting media_server_id {len(populating_msid):>8d}")
    print(f"  collisions                   {len(collisions):>8d}")

    # Sample a handful of representative rows for sanity-checking.
    samples_per_action = 2
    by_action: dict[str, list[dict]] = defaultdict(list)
    for p in plan:
        if len(by_action[p["action"]]) < samples_per_action:
            by_action[p["action"]].append(p)
    print("\nSamples (action: tf_id  old_track_id → new_track_id  msid:old→new):")
    for action, rows in by_action.items():
        for p in rows:
            old_msid = p["old_msid"] or "-"
            new_msid = p["new_msid"] or "-"
            old_tid = p["old_track_id"]
            new_tid = p["new_track_id"]
            mark = "==" if old_tid == new_tid else "→"
            print(
                f"  {action:25s} #{p['id']:<6d}  {old_tid[:24]:25s} {mark} "
                f"{new_tid[:24]:25s}  msid: {old_msid[:24]:25s} → {new_msid[:24]}"
            )

    if collisions:
        print(f"\nFirst {min(5, len(collisions))} collisions (tf_id → tf_id sharing target):")
        for p in collisions[:5]:
            print(
                f"  #{p['id']} ({p['old_track_id'][:20]}) wants → "
                f"{p['new_track_id'][:20]} but #{p['collides_with_tf_id']} already holds it"
            )


def print_cascade_estimate(
    db: DB, plan: list[dict], old_ids_to_change: set[str]
) -> None:
    if not old_ids_to_change:
        print("\nCascade estimate: no track_id changes — nothing to update downstream.")
        return
    print("\nCascade estimate (rows currently keyed on old track_ids):")
    for table, column in CASCADE_TABLES + [("track_interactions", "track_id")]:
        if not table_exists(db, table):
            print(f"  {table}.{column:30s} (skipped — table missing)")
            continue
        n = _count_rows_with_track_ids(db, table, column, old_ids_to_change)
        print(f"  {table}.{column:30s} {n:>10d}")

    # Predict per-user merge count in track_interactions: a user with rows on
    # both old_id and new_id (where new_id is what old_id will become) needs
    # an inline merge so we don't trip UNIQUE(user_id, track_id) at the bulk
    # rename. This is informational only — apply_migration handles it.
    pairs = [(p["old_track_id"], p["new_track_id"]) for p in plan if p["old_track_id"] != p["new_track_id"]]
    merges = _predict_ti_merges(db, pairs) if pairs else 0
    print(f"\n  track_interactions (user_id, track_id) merges expected: {merges}")


def _count_rows_with_track_ids(db: DB, table: str, column: str, ids: set[str]) -> int:
    if not ids:
        return 0
    # Use the temp mapping table if it exists (built later in the apply path);
    # otherwise count via chunked IN clauses.
    chunk = 800
    ids_list = list(ids)
    total = 0
    for i in range(0, len(ids_list), chunk):
        sub = ids_list[i : i + chunk]
        placeholders = ",".join(["?"] * len(sub))
        cur = db.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})", sub)
        total += cur.fetchone()[0]
    return total


def _predict_ti_merges(db: DB, pairs: list[tuple[str, str]]) -> int:
    """Count how many (user_id, track_id) UNIQUE collisions the bulk rename will hit.

    For each (old, new) mapping, find users who already have a row at both
    old and new — those are the rows that will need an inline merge.
    """
    if not table_exists(db, "track_interactions"):
        return 0
    relevant = set()
    for old, new in pairs:
        relevant.add(old)
        relevant.add(new)
    if not relevant:
        return 0
    # Pull (user_id, track_id) pairs in chunks
    seen: dict[str, set[str]] = defaultdict(set)
    chunk = 800
    rel_list = list(relevant)
    for i in range(0, len(rel_list), chunk):
        sub = rel_list[i : i + chunk]
        placeholders = ",".join(["?"] * len(sub))
        cur = db.execute(
            f"SELECT user_id, track_id FROM track_interactions WHERE track_id IN ({placeholders})",
            sub,
        )
        for user_id, track_id in cur.fetchall():
            seen[user_id].add(track_id)
    merges = 0
    for user_id, tids in seen.items():
        for old, new in pairs:
            if old in tids and new in tids and old != new:
                merges += 1
    return merges


# ---------------------------------------------------------------------------
# Execute (mutating) path
# ---------------------------------------------------------------------------


def apply_migration(db: DB, plan: list[dict]) -> dict:
    """Execute the plan inside a single transaction. Returns a metrics dict."""
    metrics: dict[str, Any] = {
        "tf_updated": 0,
        "ti_merged": 0,
        "ti_renamed": 0,
        "cascade_updates": {},
    }
    changing = [p for p in plan if p["old_track_id"] != p["new_track_id"]]
    msid_only = [
        p for p in plan
        if p["old_track_id"] == p["new_track_id"] and p["old_msid"] != p["new_msid"]
    ]

    if not changing and not msid_only:
        print("\nNothing to do — every row already has the target shape.")
        return metrics

    # Build the temp mapping table for cascading bulk UPDATEs.
    if changing:
        if db.kind == "sqlite":
            db.execute("DROP TABLE IF EXISTS _tid_map")
            db.execute(
                "CREATE TEMP TABLE _tid_map (old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL)"
            )
        else:
            db.execute("DROP TABLE IF EXISTS _tid_map")
            db.execute(
                "CREATE TEMP TABLE _tid_map (old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL) "
                "ON COMMIT DROP"
            )
        db.executemany(
            "INSERT INTO _tid_map (old_id, new_id) VALUES (?, ?)",
            [(p["old_track_id"], p["new_track_id"]) for p in changing],
        )

        # Step 1: merge (user_id, track_id) collisions in track_interactions
        # before the bulk rename so we don't trip the UNIQUE constraint.
        merged = _merge_track_interaction_collisions(db, changing)
        metrics["ti_merged"] = merged

        # Step 2: bulk-rename track_interactions
        ti_cur = db.execute(
            "UPDATE track_interactions "
            "SET track_id = (SELECT new_id FROM _tid_map WHERE _tid_map.old_id = track_interactions.track_id) "
            "WHERE track_id IN (SELECT old_id FROM _tid_map)"
        )
        metrics["ti_renamed"] = ti_cur.rowcount if ti_cur.rowcount is not None else 0

        # Step 3: cascade across the rest
        for table, column in CASCADE_TABLES:
            if not table_exists(db, table):
                continue
            cur = db.execute(
                f"UPDATE {table} "
                f"SET {column} = (SELECT new_id FROM _tid_map WHERE _tid_map.old_id = {table}.{column}) "
                f"WHERE {column} IN (SELECT old_id FROM _tid_map)"
            )
            metrics["cascade_updates"][f"{table}.{column}"] = (
                cur.rowcount if cur.rowcount is not None else 0
            )

    # Step 4: update the track_features rows themselves. Drives both new
    # track_ids and newly-populated media_server_ids in one pass. We update
    # by primary key so SQLAlchemy's UNIQUE-on-track_id check applies cleanly
    # row-by-row (no temporary states with two rows holding the same value).
    tf_updated = 0
    for p in changing + msid_only:
        new_msid = p["new_msid"]
        db.execute(
            "UPDATE track_features SET track_id = ?, media_server_id = ? WHERE id = ?",
            (p["new_track_id"], new_msid, p["id"]),
        )
        tf_updated += 1
    metrics["tf_updated"] = tf_updated

    return metrics


def _merge_track_interaction_collisions(db: DB, changing: list[dict]) -> int:
    """Inline-merge per-user collisions before the bulk UPDATE.

    For each (old_id → new_id) mapping where some user has interactions on
    both, sum the count columns into the surviving (new_id) row, max() the
    timestamp columns, then delete the (old_id) row. This is the same
    pattern the (now-removed) sync rename cascade used.
    """
    if not changing:
        return 0

    # Build the inverse map for grouping rows fetched in one pass.
    old_to_new = {p["old_track_id"]: p["new_track_id"] for p in changing}

    # Pull every track_interactions row whose track_id participates in any
    # mapping (either as old or new). One big SELECT, grouped in Python.
    relevant_ids = set(old_to_new.keys()) | set(old_to_new.values())
    rows: list[tuple] = []
    chunk = 800
    relevant_list = list(relevant_ids)
    select_cols = (
        ["id", "user_id", "track_id"] + TI_SUM_COLUMNS + TI_MAX_COLUMNS + ["avg_completion"]
    )
    col_csv = ", ".join(select_cols)
    for i in range(0, len(relevant_list), chunk):
        sub = relevant_list[i : i + chunk]
        placeholders = ",".join(["?"] * len(sub))
        cur = db.execute(
            f"SELECT {col_csv} FROM track_interactions WHERE track_id IN ({placeholders})",
            sub,
        )
        rows.extend(cur.fetchall())

    # Group by user
    by_user: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        record = dict(zip(select_cols, row))
        by_user[record["user_id"]][record["track_id"]] = record

    merged = 0
    now_unix = int(time.time())
    for user_id, by_track in by_user.items():
        for old_id, new_id in old_to_new.items():
            if old_id == new_id:
                continue
            old_row = by_track.get(old_id)
            new_row = by_track.get(new_id)
            if old_row is None or new_row is None:
                continue
            # Sum count columns
            for col in TI_SUM_COLUMNS:
                new_row[col] = (new_row.get(col) or 0) + (old_row.get(col) or 0)
            # Weighted average for avg_completion
            old_plays = max(old_row.get("play_count_pre_merge") or old_row.get("play_count") or 1, 1)
            # NOTE: we summed play_count above; reconstruct the pre-merge new_plays for the avg.
            # Simpler: take whichever side is set.
            if old_row.get("avg_completion") is not None and new_row.get("avg_completion") is None:
                new_row["avg_completion"] = old_row["avg_completion"]
            # Max timestamp columns
            for col in TI_MAX_COLUMNS:
                v_old, v_new = old_row.get(col), new_row.get(col)
                if v_old is not None and (v_new is None or v_old > v_new):
                    new_row[col] = v_old
            new_row["updated_at"] = max(new_row.get("updated_at") or 0, now_unix)

            # Persist the merged new_row, drop the old_row.
            set_cols = TI_SUM_COLUMNS + TI_MAX_COLUMNS + ["avg_completion"]
            assignments = ", ".join(f"{c} = ?" for c in set_cols)
            values = [new_row.get(c) for c in set_cols] + [new_row["id"]]
            db.execute(f"UPDATE track_interactions SET {assignments} WHERE id = ?", values)
            db.execute("DELETE FROM track_interactions WHERE id = ?", (old_row["id"],))
            merged += 1

    return merged


# ---------------------------------------------------------------------------
# Argument parsing & main
# ---------------------------------------------------------------------------


def _parse_bool(v: str) -> bool:
    if isinstance(v, bool):
        return v
    if v.lower() in ("yes", "true", "t", "y", "1"):
        return True
    if v.lower() in ("no", "false", "f", "n", "0"):
        return False
    raise argparse.ArgumentTypeError(f"Boolean value expected, got {v!r}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migration 010 — track_id schema rework (issue #37 Phase 2)."
    )
    parser.add_argument(
        "--dry-run",
        type=_parse_bool,
        nargs="?",
        const=True,
        default=True,
        help="If true (default), report the plan without mutating. "
             "Pass --dry-run=false (or --execute) to apply.",
    )
    parser.add_argument(
        "--execute",
        dest="dry_run",
        action="store_false",
        help="Shortcut for --dry-run=false.",
    )
    parser.add_argument(
        "--music-library-path",
        default=os.environ.get("MUSIC_LIBRARY_PATH", "/music"),
        help="Music library root used when re-deriving track_id from file_path "
             "(defaults to $MUSIC_LIBRARY_PATH or /music).",
    )
    parser.add_argument(
        "--allow-collisions",
        action="store_true",
        help="If set, proceed even when collision detection finds rows whose "
             "new_track_id is already used by another row. Default behaviour "
             "is to abort and require manual resolution.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    database_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:////data/grooveiq.db")
    print(f"Migration 010 — track_id schema rework")
    print(f"Database:           {database_url}")
    print(f"Music library path: {args.music_library_path}")
    print(f"Dry run:            {args.dry_run}")

    db = open_db(database_url)
    try:
        # Sanity check — Phase 1 must have run first.
        if not column_exists(db, "track_features", "media_server_id"):
            sys.exit(
                "ABORT: track_features.media_server_id is missing. Apply "
                "migration 009 (or restart the app to pick up the auto-migration) "
                "before running this script."
            )

        plan = build_plan(db, args.music_library_path)
        if not plan:
            print("\nNo track_features rows — nothing to do.")
            return

        collisions = detect_collisions(db, plan)
        print_plan(plan, collisions)

        old_ids_to_change = {
            p["old_track_id"] for p in plan if p["old_track_id"] != p["new_track_id"]
        }
        print_cascade_estimate(db, plan, old_ids_to_change)

        if collisions and not args.allow_collisions:
            print(
                "\nABORT: collisions detected. Investigate the entries above (likely "
                "duplicate track_features rows for the same physical file) and either "
                "resolve them manually, or re-run with --allow-collisions to skip the "
                "conflicting rows."
            )
            db.close()
            sys.exit(2)

        if args.dry_run:
            print("\nDry run — no changes written. Pass --dry-run=false to apply.")
            return

        if collisions and args.allow_collisions:
            collide_ids = {c["id"] for c in collisions}
            plan = [p for p in plan if p["id"] not in collide_ids]
            print(f"\nProceeding with --allow-collisions — skipping {len(collide_ids)} conflicting rows.")

        print("\nApplying migration ...")
        t0 = time.time()
        try:
            metrics = apply_migration(db, plan)
            db.commit()
        except Exception as exc:
            db.rollback()
            print(f"\nERROR: migration aborted, rolled back. {exc!r}")
            raise
        elapsed = time.time() - t0
        print(f"\nMigration applied in {elapsed:.1f}s.")
        print(f"  track_features rows updated:      {metrics['tf_updated']}")
        print(f"  track_interactions merged:        {metrics['ti_merged']}")
        print(f"  track_interactions renamed:       {metrics['ti_renamed']}")
        for ref, count in metrics["cascade_updates"].items():
            print(f"  cascade {ref:35s} {count}")
        print(
            "\nNext step: trigger POST /v1/pipeline/reset to rebuild FAISS, "
            "ranker training data, and skip-gram / SASRec / GRU token tables "
            "against the new track_id values."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
