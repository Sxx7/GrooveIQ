"""
GrooveIQ – Algorithm configuration schema.

Defines every tunable parameter in the recommendation pipeline with
type constraints, valid ranges, default values, and UI grouping metadata.

Design:
  - Global config (not per-user).
  - Changes take effect on next pipeline run (not hot-reloaded mid-request).
  - LightGBM hyperparams trigger a full model retrain.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Per-group schemas
# ---------------------------------------------------------------------------


class TrackScoringConfig(BaseModel):
    """Weights and thresholds for computing satisfaction_score."""

    w_full_listen: float = Field(
        1.0, ge=-10, le=10, description="Weight for a full listen (completion >= 0.8 or dwell >= 30s)"
    )
    w_mid_listen: float = Field(0.2, ge=-10, le=10, description="Weight for a mid-length listen (2s-30s dwell)")
    w_early_skip: float = Field(-0.5, ge=-10, le=10, description="Default weight for an early skip (<2s dwell)")
    w_early_skip_playlist: float = Field(
        -0.75, ge=-10, le=10, description="Early skip weight in playlist/album context (strong rejection)"
    )
    w_early_skip_radio: float = Field(
        -0.25, ge=-10, le=10, description="Early skip weight in radio/search context (expected behaviour)"
    )
    w_like: float = Field(2.0, ge=-10, le=10, description="Weight for an explicit like")
    w_dislike: float = Field(-2.0, ge=-10, le=10, description="Weight for an explicit dislike")
    w_repeat: float = Field(1.5, ge=-10, le=10, description="Weight for a repeat action")
    w_playlist_add: float = Field(1.5, ge=-10, le=10, description="Weight for adding track to a playlist")
    w_queue_add: float = Field(0.5, ge=-10, le=10, description="Weight for adding track to the queue")
    w_heavy_seek: float = Field(-0.3, ge=-10, le=10, description="Penalty per excess seek above threshold")
    early_skip_ms: int = Field(
        2000, ge=100, le=30000, description="Milliseconds threshold for early skip classification"
    )
    mid_skip_ms: int = Field(
        30000, ge=1000, le=120000, description="Milliseconds threshold for mid-skip classification"
    )
    heavy_seek_threshold: int = Field(
        2, ge=1, le=20, description="Seeks per play above which heavy-seek penalty applies"
    )


class RerankerConfig(BaseModel):
    """Post-ranking diversity and business rule parameters."""

    artist_diversity_top_n: int = Field(
        10, ge=1, le=100, description="Number of top positions to enforce artist diversity in"
    )
    artist_max_per_top: int = Field(2, ge=1, le=20, description="Max tracks from the same artist in the top N")
    repeat_window_hours: float = Field(2.0, ge=0, le=168, description="Hours to suppress recently played tracks")
    freshness_boost: float = Field(0.10, ge=0, le=1, description="Score multiplier boost for never-played tracks")
    skip_threshold: int = Field(2, ge=1, le=50, description="Early skip count above which skip suppression activates")
    skip_demote_factor: float = Field(
        0.5, ge=0, le=1, description="Score multiplier for skip-suppressed tracks (lower = stronger demotion)"
    )
    exploration_fraction: float = Field(
        0.15, ge=0, le=0.5, description="Fraction of slots reserved for under-explored tracks"
    )
    exploration_low_plays: int = Field(
        3, ge=1, le=50, description="Play count below which a track is considered under-explored"
    )
    exploration_noise_scale: float = Field(0.25, ge=0, le=2, description="Noise magnitude for exploration scoring")
    min_duration_car: float = Field(
        90.0, ge=0, le=600, description="Minimum track duration (seconds) in car/speaker mode"
    )
    recently_engaged_boost: float = Field(
        0.25,
        ge=0,
        le=5,
        description=(
            "Cross-surface resurfacing: additive score boost on the single hottest recently-engaged "
            "candidate (+ boost * heat, see app.services.resurfacing), so a track the user just "
            "replayed / seeked-back / finished / liked keeps reappearing across radio, Discover and "
            "Library. Capped at one track per batch — like a single inserted rec. 0 = off."
        ),
    )


class CandidateSourceConfig(BaseModel):
    """Score multipliers for each candidate retrieval source."""

    content: float = Field(1.0, ge=0, le=5, description="FAISS content-based similarity (from seed track)")
    content_profile: float = Field(
        1.0, ge=0, le=5, description="FAISS content-based similarity (from user taste centroid)"
    )
    cf: float = Field(1.0, ge=0, le=5, description="Collaborative filtering")
    session_skipgram: float = Field(0.8, ge=0, le=5, description="Session skip-gram behavioural co-occurrence")
    lastfm_similar: float = Field(0.7, ge=0, le=5, description="Last.fm similar tracks (external CF)")
    sasrec: float = Field(0.6, ge=0, le=5, description="SASRec transformer next-track prediction")
    popular: float = Field(0.3, ge=0, le=5, description="Global popularity fallback")
    artist_recall: float = Field(0.2, ge=0, le=5, description="Recently heard artist tracks")


class TasteProfileConfig(BaseModel):
    """Taste profile builder parameters."""

    timescale_short_days: float = Field(
        7.0, ge=1, le=90, description="Short-term taste window (days) — captures current mood"
    )
    timescale_long_days: float = Field(
        365.0, ge=30, le=3650, description="Long-term taste window (days) — captures core identity"
    )
    top_tracks_limit: int = Field(50, ge=10, le=500, description="Number of top tracks to include in taste profile")
    lastfm_decay_interactions: float = Field(
        150.0, ge=10, le=1000, description="Interaction count at which Last.fm influence reaches ~37% (e^-1)"
    )
    onboarding_decay_interactions: float = Field(
        80.0, ge=10, le=500, description="Interaction count at which onboarding influence reaches ~37%"
    )
    enrichment_min_weight: float = Field(
        0.05, ge=0.001, le=0.5, description="Minimum weight below which Last.fm/onboarding enrichment is skipped"
    )


class RankerConfig(BaseModel):
    """LightGBM hyperparameters and training sample weights.

    WARNING: Changing these triggers a full model retrain on the next pipeline run.
    """

    n_estimators: int = Field(200, ge=10, le=2000, description="Number of boosting rounds (trees). [RETRAIN]")
    max_depth: int = Field(6, ge=2, le=20, description="Maximum tree depth. [RETRAIN]")
    learning_rate: float = Field(0.05, ge=0.001, le=1.0, description="Boosting learning rate. [RETRAIN]")
    num_leaves: int = Field(31, ge=4, le=256, description="Maximum number of leaves per tree. [RETRAIN]")
    min_child_samples: int = Field(5, ge=1, le=100, description="Minimum samples per leaf. [RETRAIN]")
    subsample: float = Field(0.8, ge=0.1, le=1.0, description="Row subsampling ratio. [RETRAIN]")
    colsample_bytree: float = Field(0.8, ge=0.1, le=1.0, description="Column subsampling ratio. [RETRAIN]")
    reg_alpha: float = Field(0.1, ge=0, le=10, description="L1 regularisation. [RETRAIN]")
    reg_lambda: float = Field(0.1, ge=0, le=10, description="L2 regularisation. [RETRAIN]")
    min_training_samples: int = Field(50, ge=5, le=1000, description="Minimum samples required to train. [RETRAIN]")
    weight_disliked: float = Field(3.0, ge=1, le=10, description="Sample weight for disliked tracks (hard negatives)")
    weight_heavy_skip: float = Field(2.0, ge=1, le=10, description="Sample weight for heavily skipped tracks")
    weight_strong_positive: float = Field(2.0, ge=1, le=10, description="Sample weight for liked/repeated tracks")
    weight_impression_negative: float = Field(
        1.5, ge=1, le=10, description="Sample weight for shown-but-not-played tracks"
    )


class RadioConfig(BaseModel):
    """Radio session parameters."""

    seed_weight: float = Field(0.50, ge=0, le=1, description="How much the seed anchor influences the drift embedding")
    feedback_weight: float = Field(
        0.30, ge=0, le=1, description="How much in-session feedback shifts the drift embedding"
    )
    profile_weight: float = Field(0.20, ge=0, le=1, description="How much the user's global taste profile contributes")
    source_drift: float = Field(1.2, ge=0, le=5, description="Score multiplier for drift-FAISS candidates")
    source_seed: float = Field(1.0, ge=0, le=5, description="Score multiplier for seed-FAISS candidates")
    source_content: float = Field(0.9, ge=0, le=5, description="Score multiplier for content similarity candidates")
    source_skipgram: float = Field(0.7, ge=0, le=5, description="Score multiplier for session skip-gram candidates")
    source_lastfm: float = Field(0.6, ge=0, le=5, description="Score multiplier for Last.fm similar candidates")
    source_cf: float = Field(0.4, ge=0, le=5, description="Score multiplier for collaborative filtering candidates")
    source_artist: float = Field(0.8, ge=0, le=5, description="Score multiplier for same-artist candidates")
    feedback_like_weight: float = Field(1.5, ge=0, le=5, description="Attraction weight when user likes a track")
    feedback_dislike_weight: float = Field(1.0, ge=0, le=5, description="Repulsion weight when user dislikes a track")
    feedback_skip_weight: float = Field(0.5, ge=0, le=5, description="Mild repulsion weight when user skips a track")
    feedback_decay: float = Field(0.9, ge=0.1, le=1, description="Exponential decay for older feedback signals")
    session_ttl_hours: float = Field(
        4.0, ge=0.5, le=24, description="Hours of inactivity before a radio session expires"
    )
    max_sessions: int = Field(50, ge=1, le=500, description="Maximum concurrent radio sessions")


class SessionEmbeddingsConfig(BaseModel):
    """Word2Vec session skip-gram training parameters."""

    embedding_dim: int = Field(64, ge=16, le=512, description="Embedding vector dimensionality. [RETRAIN]")
    window_size: int = Field(5, ge=1, le=20, description="Context window size (tracks before/after). [RETRAIN]")
    min_count: int = Field(2, ge=1, le=50, description="Ignore tracks appearing fewer than this many times. [RETRAIN]")
    epochs: int = Field(20, ge=1, le=100, description="Training iterations. [RETRAIN]")
    min_sessions: int = Field(10, ge=1, le=500, description="Minimum sessions required to train")
    min_vocab: int = Field(5, ge=2, le=100, description="Minimum unique tracks required to train")


# ---------------------------------------------------------------------------
# Recommendation modes — the "discovery dial"
# ---------------------------------------------------------------------------

# The labeled anchor points on the discovery dial. Locked product decision
# (see docs/recommendation-modes-plan.md). Reused by the request-side mode
# enum and the dial resolver in later chunks.
PRESET_NAMES: tuple[str, ...] = ("familiar", "balanced", "discovery", "deep_discovery")


class PresetConfig(BaseModel):
    """Anchor values for one point on the discovery dial.

    A preset re-weights knobs that already exist in the pipeline (exploration,
    freshness, anti-repetition window, per-source multipliers) plus a UCB-style
    acquisition *adjustment* and the proven-set novelty filter.  The adjustment
    is **additive on top of today's ranker score** —
    ``adj = ranker_score + kappa*sigma - lambda_proven*[is_proven]`` — never a
    replacement of the base ordering signal, so ``kappa==0`` and
    ``lambda_proven==0`` (familiar / balanced) leave the score untouched.  These
    defaults define what each preset *means* out of the box; all are tunable via
    the admin config API.
    """

    kappa: float = Field(
        0.0, ge=0, le=5, description="UCB exploration coefficient — additive weight on uncertainty (+ kappa*sigma)"
    )
    lambda_proven: float = Field(
        0.0, ge=0, le=5, description="Additive demotion applied to proven tracks (- lambda_proven) at the discovery end"
    )
    exploration_fraction: float = Field(
        0.15, ge=0, le=0.5, description="Fraction of slots reserved for under-explored tracks"
    )
    freshness_boost: float = Field(0.10, ge=0, le=1, description="Score multiplier boost for never-played tracks")
    novelty_filter: bool = Field(False, description="Exclude the user's proven set from candidates (the discovery end)")
    novelty_strength: float = Field(
        0.0, ge=0, le=1, description="How aggressively the novelty filter excludes the proven set (0=off, 1=full)"
    )
    novelty_weight: float = Field(
        0.0,
        ge=0,
        le=5,
        description=(
            "Continuous familiarity demotion applied at rerank: subtract "
            "novelty_weight * play-based familiarity from the final score. A smooth "
            "complement to the binary lambda_proven — sinks the whole familiar cluster "
            "proportionally so novel tracks can surface. 0 = off (familiar/balanced)."
        ),
    )
    repeat_window_hours: float = Field(
        2.0, ge=0, le=168, description="Hours to suppress recently played tracks (0 = favourites may recur)"
    )
    proven_mu_min: float = Field(
        0.6, ge=0, le=1, description="Minimum predicted engagement (mu) for a track to count as proven"
    )
    proven_sigma_max: float = Field(
        0.3, ge=0, le=1, description="Maximum uncertainty (sigma) for a track to count as proven"
    )
    # --- Radio-regime levers (single-user / no-crowd). Read off ``modes.active`` ---
    # These tune the *radio* path (radio.get_next_tracks) and the shared reranker.
    # All default to a no-op so a config that omits them (e.g. the persisted v4)
    # and the bare ``active`` preset reproduce today's behaviour exactly.
    proven_recall_mult: float = Field(
        0.0,
        ge=0,
        le=5,
        description=(
            "Radio: weight of the proven-set ∩ seed-neighbourhood recall source (0 = off). "
            "Higher toward the familiar end so radio surfaces the user's known/high-completion "
            "tracks that are also acoustically near the seed. Crowd-free (no cross-user CF)."
        ),
    )
    ranker_blend: float = Field(
        0.6,
        ge=0,
        le=1,
        description=(
            "Radio: weight of the LightGBM ranker (retention/completion) score in the radio blend; "
            "retrieval similarity gets (1 - this). 0.6 reproduces today's hardcoded blend. Higher "
            "(familiar) makes radio retention-dominant; lower (deep) lets retrieval/novelty lead."
        ),
    )
    familiarity_weight: float = Field(
        0.0,
        ge=0,
        le=5,
        description=(
            "Positive familiarity boost added at rerank: + familiarity_weight * min(1, play_count/8). "
            "The mirror of novelty_weight — actively up-ranks the user's known/proven tracks at the "
            "familiar end. 0 = off (balanced/discovery/deep), so the default path is byte-for-byte unchanged."
        ),
    )
    cooldown_alpha: float = Field(
        0.0,
        ge=0,
        le=1,
        description=(
            "Radio: strength of the graded play/serve-frequency repeat cooldown (0 = off). Demotes "
            "recently/often-served tracks by up to alpha, floored so favourites cool down and return "
            "rather than vanish — keeps 'proven' from collapsing into the same few tracks."
        ),
    )
    seed_anchor_weight: float = Field(
        0.5,
        ge=0,
        le=1,
        description=(
            "Radio anchoring (the 'Anchoring' axis): how tightly the next track sticks to the origin "
            "seed vs. roams the user's broad taste. High (familiar) hugs the seed; low (balanced / "
            "discovery / deep) roams the taste centroid so a random seed quickly falls back to the "
            "user's favourites. Blends the drift embedding: drift = anchor*seed + (1-anchor)*taste_centroid "
            "(+ feedback). Radio-only; the /recommend reranker ignores it."
        ),
    )
    semiknown_fraction: float = Field(
        0.0,
        ge=0,
        le=1,
        description=(
            "Discover quota: target fraction of radio slots filled from the SEMI-KNOWN tier (sampled "
            "before, never skipped, not yet proven). 0 = off (familiar / balanced / deep). The Discover "
            "posture sets ~0.3 so a mix is ~70% proven/known + ~30% sampled, never brand-new. Radio-only."
        ),
    )
    require_interaction: bool = Field(
        False,
        description=(
            "Discover floor: when true, exclude brand-new (zero-interaction) tracks from the radio pool "
            "so Discover stays 'proven + sampled', never 0-interaction. Off for Deep (admits the "
            "never-heard tail) and for familiar / balanced (whose ranking already favours the known). "
            "Radio-only."
        ),
    )
    source_weight_mult: dict[str, Annotated[float, Field(ge=0, le=5)]] = Field(
        default_factory=dict,
        description="Per-source candidate-weight multipliers (each 0-5); sources omitted here default to 1.0",
    )


class ModesConfig(BaseModel):
    """Discovery-dial preset definitions and dial->preset anchor positions.

    ``balanced`` is calibrated to reproduce today's fixed policy exactly
    (exploration_fraction=0.15, freshness_boost=0.10, kappa=0, no novelty
    filter), so the default unparameterised request does not regress.
    """

    familiar: PresetConfig = Field(
        default_factory=lambda: PresetConfig(
            kappa=0.0,
            exploration_fraction=0.0,
            freshness_boost=0.0,
            novelty_filter=False,
            novelty_strength=0.0,
            repeat_window_hours=0.0,
            proven_mu_min=0.6,
            proven_sigma_max=0.3,
            proven_recall_mult=1.5,
            ranker_blend=0.80,
            familiarity_weight=0.40,
            cooldown_alpha=0.35,
            seed_anchor_weight=0.85,
            source_weight_mult={
                "content_profile": 1.5,
                "cf": 1.4,
                "artist_recall": 1.3,
                "lastfm_similar": 0.6,
                "sasrec": 0.7,
                "popular": 0.5,
            },
        ),
        description="Play me what I love — proven favourites, retention-dominant, kept fresh by cooldown.",
    )
    balanced: PresetConfig = Field(
        default_factory=lambda: PresetConfig(
            kappa=0.0,
            exploration_fraction=0.15,
            freshness_boost=0.10,
            novelty_filter=False,
            novelty_strength=0.0,
            repeat_window_hours=2.0,
            proven_mu_min=0.6,
            proven_sigma_max=0.3,
            # Balanced now actively surfaces the user's proven favourites (familiarity_weight > 0)
            # and roams away from the seed (low seed_anchor_weight) — "play my favourites regardless
            # of where I started", the YouTube-Music fallback. The old byte-for-byte-with-legacy
            # invariant for the un-dialled /recommend path was deliberately dropped.
            proven_recall_mult=1.0,
            ranker_blend=0.65,
            familiarity_weight=0.30,
            cooldown_alpha=0.40,
            seed_anchor_weight=0.25,
            source_weight_mult={},
        ),
        description="Play my proven favourites, regardless of where the radio started. The default preset.",
    )
    discovery: PresetConfig = Field(
        default_factory=lambda: PresetConfig(
            # The user's "Discover": ~70% proven/known + ~30% semi-known (sampled before, not
            # skipped), NEVER brand-new — a gentler step than the old "mostly new" discovery. So:
            # no proven-set novelty exclusion (keep favourites), a mild proven uplift, roam off the
            # seed, exclude the zero-interaction tail, and fill ~30% of slots from the semi-known tier.
            kappa=0.0,
            lambda_proven=0.0,
            exploration_fraction=0.10,
            freshness_boost=0.05,
            novelty_filter=False,
            novelty_strength=0.0,
            novelty_weight=0.0,
            repeat_window_hours=2.0,
            proven_mu_min=0.6,
            proven_sigma_max=0.3,
            proven_recall_mult=0.6,
            ranker_blend=0.60,
            familiarity_weight=0.20,
            cooldown_alpha=0.30,
            seed_anchor_weight=0.20,
            semiknown_fraction=0.30,
            require_interaction=True,
            source_weight_mult={
                "content": 1.2,
                "lastfm_similar": 1.1,
                "cf": 0.75,
                "popular": 0.6,
            },
        ),
        description="Mostly my favourites, with a 30% slice of tracks I sampled before and didn't skip.",
    )
    deep_discovery: PresetConfig = Field(
        default_factory=lambda: PresetConfig(
            kappa=0.6,
            lambda_proven=0.6,
            exploration_fraction=0.50,
            freshness_boost=0.30,
            novelty_filter=True,
            novelty_strength=1.0,
            novelty_weight=0.5,
            repeat_window_hours=2.0,
            proven_mu_min=0.6,
            proven_sigma_max=0.3,
            proven_recall_mult=0.0,
            ranker_blend=0.40,
            cooldown_alpha=0.15,
            seed_anchor_weight=0.15,
            source_weight_mult={
                "content": 1.4,
                "lastfm_similar": 1.8,
                "sasrec": 1.6,
                "session_skipgram": 1.3,
                "content_profile": 0.6,
                "cf": 0.8,
                "popular": 0.4,
                "artist_recall": 0.3,
            },
        ),
        description="Surprise me — nothing I've heard.",
    )
    default_preset: str = Field("balanced", description="Preset used when a request specifies no discovery/mode value.")
    active: PresetConfig = Field(
        default_factory=PresetConfig,
        description=(
            "The dial-resolved preset for the current request. The recommend handler overrides this "
            "per-request (via the request-scoped config override). Its default is a no-op (kappa=0, "
            "lambda_proven=0, novelty_filter=off, no source multipliers) so the unparameterised request "
            "is byte-for-byte unchanged."
        ),
    )
    dial_anchors: dict[str, Annotated[float, Field(ge=0, le=1)]] = Field(
        default_factory=lambda: {
            "familiar": 0.0,
            "balanced": 0.3,
            "discovery": 0.6,
            "deep_discovery": 1.0,
        },
        description="Each preset's position on the continuous [0,1] discovery dial (used for interpolation).",
    )

    @field_validator("default_preset")
    @classmethod
    def _validate_default_preset(cls, v: str) -> str:
        if v not in PRESET_NAMES:
            raise ValueError(f"default_preset must be one of {PRESET_NAMES}, got {v!r}")
        return v


class ArtistRecoConfig(BaseModel):
    """Artist recommendation blend weights (GET /v1/recommend/{user}/artists).

    The four w_* terms are blended per candidate then shifted by the request
    ``mode`` (familiar | balanced | discover). Reads per-call via get_config();
    no model is trained.
    """

    w_content: float = Field(
        0.35, ge=0, le=1, description="Weight of the audio-centroid cosine (does this artist *sound* like your taste)"
    )
    w_ranker: float = Field(
        0.35,
        ge=0,
        le=1,
        description="Weight of the track-ranker roll-up (top-k mean of the artist's learned track scores)",
    )
    w_lastfm: float = Field(0.20, ge=0, le=1, description="Weight of the Last.fm similar/top signal")
    w_history: float = Field(
        0.10, ge=0, le=1, description="Weight of the legacy play-count/recency/satisfaction heuristic, as one input"
    )
    rollup_top_k: int = Field(
        5, ge=1, le=50, description="How many of an artist's top track scores are averaged for the ranker roll-up"
    )
    discovery_faiss_k: int = Field(
        200, ge=10, le=2000, description="Neighbour tracks pulled from the taste centroid for FAISS-based discovery"
    )
    content_reason_threshold: float = Field(
        0.6, ge=0, le=1, description="content_score above this emits the 'sounds like your taste' reason"
    )
    ranker_reason_threshold: float = Field(
        0.6, ge=0, le=1, description="ranker_rollup above this emits the 'you rate their tracks highly' reason"
    )


class AlbumRecoConfig(BaseModel):
    """Album recommendation blend weights (GET /v1/recommend/{user}/albums).

    Library-only roll-up surface: albums are grouped by (album_artist or
    artist, album) and scored by a ranker roll-up plus coverage, freshness, and
    audio coherence. Shifted by the request ``mode``. No model is trained.
    """

    w_content: float = Field(0.25, ge=0, le=1, description="Weight of the album audio-centroid cosine vs taste")
    w_ranker: float = Field(0.45, ge=0, le=1, description="Weight of the track-ranker roll-up over the album's tracks")
    w_coverage: float = Field(
        0.15, ge=0, le=1, description="Weight of library coverage (owned tracks / total album tracks)"
    )
    w_fresh: float = Field(
        0.15, ge=0, le=1, description="Weight of the 'rediscover' freshness signal (boost for long-unplayed albums)"
    )
    fresh_halflife_days: float = Field(
        60.0, ge=1, le=365, description="Half-life (days) of the freshness curve; longer = slower rediscover ramp"
    )
    min_album_tracks: int = Field(
        3, ge=1, le=50, description="Ignore albums with fewer owned tracks than this (filters singles-as-albums)"
    )
    rollup_top_k: int = Field(
        10, ge=1, le=50, description="Cap on how many of an album's top track scores are averaged for the roll-up"
    )


class ForgottenFavouritesConfig(BaseModel):
    """Forgotten-favourites blend (GET /v1/recommend/{user}/forgotten-favourites).

    Surfaces individual *tracks* the user demonstrably loved but hasn't played in
    a long time. Score is **multiplicative** — ``affinity * dormancy`` — so a
    track only surfaces when it is *both* a proven favourite *and* dormant; a
    weighted sum would leak in loved-but-recent or dormant-but-mediocre tracks.

    ``affinity`` (how much the user loved it) blends the normalised
    satisfaction_score, a like/repeat boost, and a play-count saturation term.
    ``dormancy`` (how long since the last play) uses the same exponential ramp as
    the album "rediscover" boost. Qualification gates keep it favourites-only.
    No model is trained — this is a read-only aggregation of track_interactions.
    """

    w_satisfaction: float = Field(
        0.6, ge=0, le=1, description="Weight of the normalised satisfaction_score in the affinity term"
    )
    w_likes: float = Field(0.25, ge=0, le=1, description="Weight of the like/repeat boost in the affinity term")
    w_plays: float = Field(0.15, ge=0, le=1, description="Weight of the play-count saturation in the affinity term")
    dormancy_halflife_days: float = Field(
        90.0, ge=1, le=730, description="Half-life (days) of the dormancy ramp; longer = slower revival ramp"
    )
    min_dormancy_days: float = Field(
        30.0,
        ge=0,
        le=365,
        description="A track must not have been played within this many days to qualify as 'forgotten'",
    )
    min_satisfaction: float = Field(
        0.5,
        ge=0,
        le=1,
        description="Minimum normalised satisfaction_score to qualify as a 'favourite' (per-user min-max scaled)",
    )
    min_play_count: int = Field(
        2, ge=1, le=100, description="Minimum lifetime play count to qualify (rules out one-off skips)"
    )
    likes_saturation: int = Field(
        3, ge=1, le=50, description="Combined like+repeat count at which the like/repeat boost saturates to 1.0"
    )
    plays_saturation: int = Field(
        25, ge=1, le=500, description="Play count at which the play-count term saturates to 1.0"
    )


class MixesConfig(BaseModel):
    """Session-clustered rotating mixes (GET /v1/users/{user}/session-mixes).

    The user's recently-engaged tracks are clustered on the session co-listening
    embedding (NOT genre/artist) into a handful of mixes that persist with a
    shelf-life: they rotate slowly (a stable ~80% core), go stale and archive
    when their vibe fades, and resurface later as "nostalgic" mixes. Inspired by
    Spotify's On Repeat -> Repeat Rewind handoff. No model is trained here —
    clustering reuses the session_embeddings Word2Vec vectors; tracks too thinly
    co-listened to have one fall back to their acoustic embedding and are kept
    only while later event data supports them.
    """

    enabled: bool = Field(False, description="Master switch for the session-mixes surface")

    window_days: int = Field(
        30,
        ge=7,
        le=120,
        description="Eligibility window: a track must have been played within this many days to enter a mix",
    )
    target_size: int = Field(28, ge=10, le=60, description="Target tracks per mix")
    min_size: int = Field(14, ge=4, le=40, description="Clusters smaller than this are dropped")
    max_size: int = Field(38, ge=12, le=80, description="Clusters larger than this are split")
    min_mixes: int = Field(3, ge=1, le=10, description="Floor on mix count")
    max_mixes: int = Field(6, ge=1, le=12, description="Ceiling on mix count")
    min_session_vectors: int = Field(
        20,
        ge=6,
        le=400,
        description="Cold-start gate: fewer in-vocab (co-listened) engaged tracks than this -> don't build (client falls back to genre mixes)",
    )

    refresh_days: float = Field(
        6.0,
        ge=0.5,
        le=60,
        description="Per-mix rotation cadence; a mix's membership is only re-rolled once it is older than this",
    )
    max_churn: float = Field(
        0.20,
        ge=0.0,
        le=1.0,
        description="Max fraction of a mix's tracks that may swap on one rotation (enforces the stable ~80% core)",
    )
    stale_days: float = Field(25.0, ge=1, le=180, description="A mix whose cluster no longer forms is archived")
    serve_cooldown_days: float = Field(
        14.0,
        ge=0,
        le=120,
        description="Anti-repeat spacing window (reserved for the temporal serve-cooldown; v1 enforces mostly-disjoint mixes)",
    )

    min_satisfaction: float = Field(
        0.0, ge=0, le=1, description="Optional floor on normalised satisfaction_score for a track to be mix-eligible"
    )

    nostalgia_dormancy_days: float = Field(
        45.0,
        ge=1,
        le=365,
        description="An archived mix becomes eligible to resurface as 'nostalgic' once dormant this long",
    )
    nostalgia_max: int = Field(
        2, ge=0, le=6, description="Max nostalgic mixes shown at once (0 disables the nostalgic surface)"
    )


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class AlgorithmConfigData(BaseModel):
    """
    Complete algorithm configuration.

    All fields have sensible defaults — an empty {} import produces
    the same behaviour as the original hardcoded values.
    """

    track_scoring: TrackScoringConfig = Field(default_factory=TrackScoringConfig)
    reranker: RerankerConfig = Field(default_factory=RerankerConfig)
    candidate_sources: CandidateSourceConfig = Field(default_factory=CandidateSourceConfig)
    taste_profile: TasteProfileConfig = Field(default_factory=TasteProfileConfig)
    ranker: RankerConfig = Field(default_factory=RankerConfig)
    radio: RadioConfig = Field(default_factory=RadioConfig)
    session_embeddings: SessionEmbeddingsConfig = Field(default_factory=SessionEmbeddingsConfig)
    modes: ModesConfig = Field(default_factory=ModesConfig)
    artist_reco: ArtistRecoConfig = Field(default_factory=ArtistRecoConfig)
    album_reco: AlbumRecoConfig = Field(default_factory=AlbumRecoConfig)
    forgotten_favourites: ForgottenFavouritesConfig = Field(default_factory=ForgottenFavouritesConfig)
    mixes: MixesConfig = Field(default_factory=MixesConfig)


# ---------------------------------------------------------------------------
# API request/response models
# ---------------------------------------------------------------------------


class AlgorithmConfigResponse(BaseModel):
    """Response when reading a config."""

    id: int
    version: int
    name: str | None = None
    config: AlgorithmConfigData
    is_active: bool
    created_at: int
    created_by: str | None = None


class AlgorithmConfigUpdate(BaseModel):
    """Request body for updating config. Partial updates supported."""

    name: str | None = None
    config: AlgorithmConfigData


class AlgorithmConfigImport(BaseModel):
    """Request body for importing a config (e.g. shared by another user)."""

    name: str | None = None
    config: dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Groups with metadata for the future GUI.
CONFIG_GROUPS: list[dict[str, Any]] = [
    {
        "key": "track_scoring",
        "label": "Track Scoring",
        "description": "Weights for computing per-track satisfaction scores from user behaviour signals.",
        "retrain_required": False,
    },
    {
        "key": "reranker",
        "label": "Reranker",
        "description": "Post-ranking diversity, freshness, exploration, and business rules.",
        "retrain_required": False,
    },
    {
        "key": "candidate_sources",
        "label": "Candidate Sources",
        "description": "Score multipliers controlling how much each retrieval source contributes to the candidate pool.",
        "retrain_required": False,
    },
    {
        "key": "taste_profile",
        "label": "Taste Profile",
        "description": "Parameters for building user taste profiles from listening history and external data.",
        "retrain_required": False,
    },
    {
        "key": "ranker",
        "label": "Ranking Model",
        "description": "LightGBM hyperparameters and training sample weights. Changes trigger a full model retrain.",
        "retrain_required": True,
    },
    {
        "key": "radio",
        "label": "Radio",
        "description": "Adaptive radio session parameters: seed anchoring, feedback sensitivity, candidate source weights.",
        "retrain_required": False,
    },
    {
        "key": "session_embeddings",
        "label": "Session Embeddings",
        "description": "Word2Vec skip-gram training parameters for behavioural co-occurrence embeddings.",
        "retrain_required": True,
    },
    {
        "key": "modes",
        "label": "Recommendation Modes (Discovery Dial)",
        "description": (
            "Per-preset definitions for the discovery dial (familiar → deep discovery): acquisition "
            "(kappa/lambda), novelty filter, proven thresholds, exploration/freshness, and per-source "
            "weight multipliers. 'balanced' is calibrated to reproduce today's default behaviour."
        ),
        "retrain_required": False,
    },
    {
        "key": "artist_reco",
        "label": "Artist Recommendations",
        "description": (
            "Blend weights for the artist recommender (content centroid, track-ranker roll-up, Last.fm, "
            "and the legacy heuristic). Shifted at serving time by the request mode (familiar/balanced/discover). "
            "No model is trained — this is a read-only aggregation of existing track signals."
        ),
        "retrain_required": False,
    },
    {
        "key": "album_reco",
        "label": "Album Recommendations",
        "description": (
            "Blend weights for the library-only album recommender: track-ranker roll-up, coverage, "
            "rediscover freshness, and audio coherence vs your taste. "
            "No model is trained — this is a read-only aggregation of existing track signals."
        ),
        "retrain_required": False,
    },
    {
        "key": "forgotten_favourites",
        "label": "Forgotten Favourites",
        "description": (
            "Track-level 'forgotten favourites' surface: tracks you demonstrably loved but haven't "
            "played in a long time. Score is affinity (satisfaction + likes/repeats + plays) × dormancy "
            "(time since last play), with qualification gates so only proven, dormant favourites surface. "
            "No model is trained — a read-only aggregation of track_interactions."
        ),
        "retrain_required": False,
    },
]


def get_defaults() -> AlgorithmConfigData:
    """Return the default configuration (matches original hardcoded values)."""
    return AlgorithmConfigData()


def get_defaults_dict() -> dict[str, Any]:
    """Return the default configuration as a plain dict."""
    return get_defaults().model_dump()
