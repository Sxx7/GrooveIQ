"""
GrooveIQ – Personalized music news feed from Reddit.

Fetches posts from music subreddits via Reddit's public JSON API,
parses artist names from titles, and scores posts per-user based on
their taste profile.  Cache is in-memory (posts are ephemeral, 48h max).

Architecture follows the lastfm_candidates.py pattern:
  - Module-level cache rebuilt on a schedule.
  - Personalization scoring happens at query time (no DB writes).
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Genre-to-subreddit mapping
# ---------------------------------------------------------------------------

GENRE_TO_SUBS: Dict[str, List[str]] = {
    "hip-hop":      ["hiphopheads", "rap"],
    "hip hop":      ["hiphopheads", "rap"],
    "rap":          ["hiphopheads", "rap"],
    "indie":        ["indieheads", "indie"],
    "indie rock":   ["indieheads", "indie"],
    "electronic":   ["electronicmusic", "EDM"],
    "edm":          ["electronicmusic", "EDM"],
    "metal":        ["Metal", "metalcore"],
    "r&b":          ["rnb", "soul"],
    "rnb":          ["rnb", "soul"],
    "soul":         ["rnb", "soul"],
    "pop":          ["popheads"],
    "rock":         ["rock", "classicrock"],
    "jazz":         ["Jazz"],
    "folk":         ["folk"],
    "punk":         ["punk"],
    "country":      ["country"],
    "classical":    ["classicalmusic"],
    "ambient":      ["ambient"],
    "lo-fi":        ["LofiHipHop"],
    "lofi":         ["LofiHipHop"],
    "shoegaze":     ["shoegaze"],
    "post-rock":    ["postrock"],
    "synthwave":    ["synthwave", "outrun"],
    "drum and bass": ["DnB"],
    "dnb":          ["DnB"],
    "house":        ["House"],
    "techno":       ["Techno"],
    "k-pop":        ["kpop"],
    "kpop":         ["kpop"],
    "latin":        ["LatinMusic"],
    "reggae":       ["reggae"],
    "blues":        ["blues"],
}

SUB_TO_GENRES: Dict[str, List[str]] = {}
for _genre, _subs in GENRE_TO_SUBS.items():
    for _sub in _subs:
        SUB_TO_GENRES.setdefault(_sub.lower(), []).append(_genre)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RedditPost:
    id: str
    subreddit: str
    title: str
    url: str
    permalink: str
    score: int
    num_comments: int
    created_utc: int
    flair: Optional[str]
    selftext_snippet: str
    thumbnail: Optional[str]
    domain: str
    is_self: bool
    parsed_artists: List[str] = field(default_factory=list)
    parsed_tag: Optional[str] = None
    is_fresh: bool = False


# ---------------------------------------------------------------------------
# Title parsing
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"\[([^\]]+)\]")
_ARTIST_TITLE_RE = re.compile(
    r"^(.+?)\s*[-\u2013\u2014]\s*(.+?)(?:\s*\[.*\])?\s*(?:\(.*\))?\s*$"
)

_FRESH_TAGS = {"fresh", "fresh video", "fresh album", "fresh ep", "fresh single",
               "fresh performance", "fresh stream", "fresh leak"}
_KNOWN_TAGS = _FRESH_TAGS | {"news", "discussion", "article", "ama", "hype",
                              "shots fired", "game thread", "daily discussion",
                              "album of the year", "aoty"}


def _parse_title(title: str) -> Tuple[List[str], Optional[str], bool]:
    """Extract artist names, primary tag, and is_fresh from a post title.

    Returns (artists, tag, is_fresh).
    """
    tags = _TAG_RE.findall(title)
    tag = None
    is_fresh = False
    for t in tags:
        t_lower = t.strip().lower()
        if t_lower in _FRESH_TAGS:
            is_fresh = True
            tag = "FRESH"
        elif t_lower in _KNOWN_TAGS and tag is None:
            tag = t.strip().upper()

    # Strip all bracket tags for artist parsing
    cleaned = _TAG_RE.sub("", title).strip()

    artists: List[str] = []
    m = _ARTIST_TITLE_RE.match(cleaned)
    if m:
        raw_artist = m.group(1).strip()
        # Handle "Artist1 ft. Artist2" / "Artist1 & Artist2"
        for part in re.split(r"\s+(?:ft\.?|feat\.?|x|&|,)\s+", raw_artist, flags=re.IGNORECASE):
            part = part.strip().strip("'\"")
            if part and len(part) > 1:
                artists.append(part)

    return artists, tag, is_fresh


def _normalize_artist(name: str) -> str:
    """Lowercase, strip 'the ', trim punctuation for matching."""
    n = name.strip().lower()
    if n.startswith("the "):
        n = n[4:]
    return n.strip("'\".,!?")


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_news_cache: Dict[str, List[RedditPost]] = {}  # subreddit_lower -> posts
_cache_built_at: int = 0
_artist_index: Dict[str, List[str]] = {}  # normalized_artist -> list of post IDs


def get_cache_age_minutes() -> float:
    if _cache_built_at == 0:
        return float("inf")
    return (time.time() - _cache_built_at) / 60.0


def _rebuild_artist_index() -> None:
    """Rebuild the artist -> post_id lookup from current cache."""
    global _artist_index
    idx: Dict[str, List[str]] = {}
    for posts in _news_cache.values():
        for p in posts:
            for a in p.parsed_artists:
                na = _normalize_artist(a)
                idx.setdefault(na, []).append(p.id)
    _artist_index = idx


# ---------------------------------------------------------------------------
# Reddit fetcher
# ---------------------------------------------------------------------------

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
_FETCH_TIMEOUT = 15.0
_THROTTLE_SECONDS = 2.0


async def _fetch_subreddit(client: httpx.AsyncClient, subreddit: str) -> List[RedditPost]:
    """Fetch posts from a single subreddit via Reddit's public JSON API."""
    url = f"https://www.reddit.com/r/{subreddit}/.json"
    params = {"limit": settings.NEWS_MAX_POSTS_PER_SUB, "raw_json": 1}

    try:
        resp = await client.get(url, params=params)
        if resp.status_code == 429:
            logger.warning("Reddit rate-limited on r/%s, skipping", subreddit)
            return []
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch r/%s: %s", subreddit, exc)
        return []

    posts: List[RedditPost] = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if not d or d.get("stickied") or d.get("over_18"):
            continue

        artists, tag, is_fresh = _parse_title(d.get("title", ""))

        selftext = d.get("selftext", "") or ""
        thumb = d.get("thumbnail")
        if thumb in ("self", "default", "nsfw", "spoiler", "", None):
            thumb = None

        posts.append(RedditPost(
            id=d.get("name", ""),
            subreddit=d.get("subreddit", subreddit),
            title=d.get("title", ""),
            url=d.get("url", ""),
            permalink="https://www.reddit.com" + d.get("permalink", ""),
            score=d.get("score", 0),
            num_comments=d.get("num_comments", 0),
            created_utc=int(d.get("created_utc", 0)),
            flair=d.get("link_flair_text"),
            selftext_snippet=selftext[:200],
            thumbnail=thumb,
            domain=d.get("domain", ""),
            is_self=d.get("is_self", False),
            parsed_artists=artists,
            parsed_tag=tag,
            is_fresh=is_fresh,
        ))

    return posts


async def refresh_cache() -> Dict[str, Any]:
    """Fetch all configured subreddits and rebuild the in-memory cache.

    Called by the scheduler. Returns summary dict.
    """
    import asyncio

    subs_to_fetch: Set[str] = set(settings.news_subreddits_list)

    # Also add genre-specific subreddits for active users' taste profiles.
    try:
        from app.db.session import AsyncSessionLocal
        from app.models.db import User
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(User.taste_profile).where(User.taste_profile.isnot(None))
            )).scalars().all()
            for profile in rows:
                if not isinstance(profile, dict):
                    continue
                # Extract genres from Last.fm cache and mood preferences
                lastfm_genres = []
                if "lastfm_genres" in profile:
                    lastfm_genres = profile["lastfm_genres"]
                elif "mood_preferences" in profile:
                    lastfm_genres = list(profile["mood_preferences"].keys())
                for genre in lastfm_genres:
                    g_lower = genre.strip().lower()
                    if g_lower in GENRE_TO_SUBS:
                        subs_to_fetch.update(GENRE_TO_SUBS[g_lower])
    except Exception as exc:
        logger.warning("Could not read user profiles for genre subs: %s", exc)

    if not subs_to_fetch:
        return {"fetched": 0, "reason": "no_subreddits"}

    logger.info("Refreshing news cache from %d subreddits", len(subs_to_fetch))

    new_cache: Dict[str, List[RedditPost]] = {}
    total_posts = 0

    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT,
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    ) as client:
        for sub in sorted(subs_to_fetch):
            posts = await _fetch_subreddit(client, sub)
            if posts:
                new_cache[sub.lower()] = posts
                total_posts += len(posts)
            await asyncio.sleep(_THROTTLE_SECONDS)

    global _news_cache, _cache_built_at
    with _lock:
        _news_cache.clear()
        _news_cache.update(new_cache)
        _cache_built_at = int(time.time())
        _rebuild_artist_index()

    logger.info("News cache rebuilt: %d posts from %d subreddits", total_posts, len(new_cache))
    return {"fetched": total_posts, "subreddits": len(new_cache)}


# ---------------------------------------------------------------------------
# Personalization scoring
# ---------------------------------------------------------------------------

def _build_user_artist_set(taste_profile: Optional[Dict]) -> Set[str]:
    """Build a normalized set of artist names the user likes."""
    artists: Set[str] = set()
    if not taste_profile:
        return artists

    # From top tracks (each has artist field)
    for t in taste_profile.get("top_tracks", []):
        if isinstance(t, dict) and t.get("artist"):
            artists.add(_normalize_artist(t["artist"]))

    # From Last.fm top artists
    for a in taste_profile.get("lastfm_top_artists", []):
        name = a.get("name", "") if isinstance(a, dict) else str(a)
        if name:
            artists.add(_normalize_artist(name))

    return artists


def _build_user_genre_set(taste_profile: Optional[Dict]) -> Set[str]:
    """Build a normalized set of genres the user prefers."""
    genres: Set[str] = set()
    if not taste_profile:
        return genres

    for g in taste_profile.get("lastfm_genres", []):
        genres.add(g.strip().lower())

    return genres


def _score_post(
    post: RedditPost,
    now: float,
    user_artists: Set[str],
    user_genres: Set[str],
    max_age_hours: float,
) -> Optional[float]:
    """Score a single post for a user. Returns None if too old."""
    age_hours = (now - post.created_utc) / 3600.0
    if age_hours > max_age_hours or age_hours < 0:
        return None

    # Popularity: log-scaled score (0-1)
    popularity = min(1.0, math.log1p(post.score) / math.log1p(10000))

    # Recency: 12-hour half-life exponential decay
    recency = math.exp(-0.693 * age_hours / 12.0)

    # Personal relevance (composite, 0-1)
    # -- Artist match (0 or 1)
    artist_match = 0.0
    for a in post.parsed_artists:
        if _normalize_artist(a) in user_artists:
            artist_match = 1.0
            break

    # -- Genre-subreddit match
    sub_genres = SUB_TO_GENRES.get(post.subreddit.lower(), [])
    genre_overlap = sum(1 for g in sub_genres if g in user_genres)
    genre_match = min(1.0, genre_overlap * 0.33)

    # -- FRESH bonus
    fresh_bonus = 1.0 if post.is_fresh else 0.0

    # -- Engagement signal (high comments)
    engagement = min(1.0, post.num_comments / 500.0)

    personal_relevance = (
        0.50 * artist_match
        + 0.30 * genre_match
        + 0.10 * fresh_bonus
        + 0.10 * engagement
    )

    final_score = (
        0.20 * popularity
        + 0.25 * recency
        + 0.55 * personal_relevance
    )

    return final_score


def _relevance_reasons(
    post: RedditPost,
    user_artists: Set[str],
    user_genres: Set[str],
) -> List[str]:
    """Generate human-readable relevance reasons for a post."""
    reasons: List[str] = []
    for a in post.parsed_artists:
        if _normalize_artist(a) in user_artists:
            reasons.append("artist_match")
            break
    sub_genres = SUB_TO_GENRES.get(post.subreddit.lower(), [])
    if any(g in user_genres for g in sub_genres):
        reasons.append("genre_match")
    if post.is_fresh:
        reasons.append("fresh")
    if post.num_comments > 50:
        reasons.append("high_engagement")
    return reasons


def get_personalized_feed(
    taste_profile: Optional[Dict],
    limit: int = 25,
    tag_filter: Optional[str] = None,
    subreddit_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Score and rank all cached posts for a user.

    Returns a list of dicts ready for JSON serialization.
    """
    now = time.time()
    max_age = settings.NEWS_MAX_AGE_HOURS

    user_artists = _build_user_artist_set(taste_profile)
    user_genres = _build_user_genre_set(taste_profile)

    scored: List[Tuple[float, RedditPost]] = []

    with _lock:
        for sub_key, posts in _news_cache.items():
            if subreddit_filter and sub_key != subreddit_filter.lower():
                continue
            for post in posts:
                if tag_filter and (post.parsed_tag or "").upper() != tag_filter.upper():
                    continue
                score = _score_post(post, now, user_artists, user_genres, max_age)
                if score is not None:
                    scored.append((score, post))

    # Sort descending by score
    scored.sort(key=lambda x: x[0], reverse=True)

    results: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    for score, post in scored:
        if post.id in seen_ids:
            continue
        seen_ids.add(post.id)
        if len(results) >= limit:
            break

        age_hours = (now - post.created_utc) / 3600.0
        reasons = _relevance_reasons(post, user_artists, user_genres)

        results.append({
            "id": post.id,
            "title": post.title,
            "url": post.url,
            "reddit_url": post.permalink,
            "subreddit": post.subreddit,
            "score": post.score,
            "num_comments": post.num_comments,
            "created_utc": post.created_utc,
            "age_hours": round(age_hours, 1),
            "flair": post.flair,
            "thumbnail": post.thumbnail,
            "domain": post.domain,
            "is_fresh": post.is_fresh,
            "parsed_artists": post.parsed_artists,
            "parsed_tag": post.parsed_tag,
            "relevance_score": round(score, 3),
            "relevance_reasons": reasons,
        })

    return results
