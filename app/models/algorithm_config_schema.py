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

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Per-group schemas
# ---------------------------------------------------------------------------

class TrackScoringConfig(BaseModel):
    """Weights and thresholds for computing satisfaction_score."""

    w_full_listen: float = Field(1.0, ge=-10, le=10, description="Weight for a full listen (completion >= 0.8 or dwell >= 30s)")
    w_mid_listen: float = Field(0.2, ge=-10, le=10, description="Weight for a mid-length listen (2s-30s dwell)")
    w_early_skip: float = Field(-0.5, ge=-10, le=10, description="Default weight for an early skip (<2s dwell)")
    w_early_skip_playlist: float = Field(-0.75, ge=-10, le=10, description="Early skip weight in playlist/album context (strong rejection)")
    w_early_skip_radio: float = Field(-0.25, ge=-10, le=10, description="Early skip weight in radio/search context (expected behaviour)")
    w_like: float = Field(2.0, ge=-10, le=10, description="Weight for an explicit like")
    w_dislike: float = Field(-2.0, ge=-10, le=10, description="Weight for an explicit dislike")
    w_repeat: float = Field(1.5, ge=-10, le=10, description="Weight for a repeat action")
    w_playlist_add: float = Field(1.5, ge=-10, le=10, description="Weight for adding track to a playlist")
    w_queue_add: float = Field(0.5, ge=-10, le=10, description="Weight for adding track to the queue")
    w_heavy_seek: float = Field(-0.3, ge=-10, le=10, description="Penalty per excess seek above threshold")
    early_skip_ms: int = Field(2000, ge=100, le=30000, description="Milliseconds threshold for early skip classification")
    mid_skip_ms: int = Field(30000, ge=1000, le=120000, description="Milliseconds threshold for mid-skip classification")
    heavy_seek_threshold: int = Field(2, ge=1, le=20, description="Seeks per play above which heavy-seek penalty applies")


class RerankerConfig(BaseModel):
    """Post-ranking diversity and business rule parameters."""

    artist_diversity_top_n: int = Field(10, ge=1, le=100, description="Number of top positions to enforce artist diversity in")
    artist_max_per_top: int = Field(2, ge=1, le=20, description="Max tracks from the same artist in the top N")
    repeat_window_hours: float = Field(2.0, ge=0, le=168, description="Hours to suppress recently played tracks")
    freshness_boost: float = Field(0.10, ge=0, le=1, description="Score multiplier boost for never-played tracks")
    skip_threshold: int = Field(2, ge=1, le=50, description="Early skip count above which skip suppression activates")
    skip_demote_factor: float = Field(0.5, ge=0, le=1, description="Score multiplier for skip-suppressed tracks (lower = stronger demotion)")
    exploration_fraction: float = Field(0.15, ge=0, le=0.5, description="Fraction of slots reserved for under-explored tracks")
    exploration_low_plays: int = Field(3, ge=1, le=50, description="Play count below which a track is considered under-explored")
    exploration_noise_scale: float = Field(0.25, ge=0, le=2, description="Noise magnitude for exploration scoring")
    min_duration_car: float = Field(90.0, ge=0, le=600, description="Minimum track duration (seconds) in car/speaker mode")


class CandidateSourceConfig(BaseModel):
    """Score multipliers for each candidate retrieval source."""

    content: float = Field(1.0, ge=0, le=5, description="FAISS content-based similarity (from seed track)")
    content_profile: float = Field(1.0, ge=0, le=5, description="FAISS content-based similarity (from user taste centroid)")
    cf: float = Field(1.0, ge=0, le=5, description="Collaborative filtering")
    session_skipgram: float = Field(0.8, ge=0, le=5, description="Session skip-gram behavioural co-occurrence")
    lastfm_similar: float = Field(0.7, ge=0, le=5, description="Last.fm similar tracks (external CF)")
    sasrec: float = Field(0.6, ge=0, le=5, description="SASRec transformer next-track prediction")
    popular: float = Field(0.3, ge=0, le=5, description="Global popularity fallback")
    artist_recall: float = Field(0.2, ge=0, le=5, description="Recently heard artist tracks")


class TasteProfileConfig(BaseModel):
    """Taste profile builder parameters."""

    timescale_short_days: float = Field(7.0, ge=1, le=90, description="Short-term taste window (days) — captures current mood")
    timescale_long_days: float = Field(365.0, ge=30, le=3650, description="Long-term taste window (days) — captures core identity")
    top_tracks_limit: int = Field(50, ge=10, le=500, description="Number of top tracks to include in taste profile")
    lastfm_decay_interactions: float = Field(150.0, ge=10, le=1000, description="Interaction count at which Last.fm influence reaches ~37% (e^-1)")
    onboarding_decay_interactions: float = Field(80.0, ge=10, le=500, description="Interaction count at which onboarding influence reaches ~37%")
    enrichment_min_weight: float = Field(0.05, ge=0.001, le=0.5, description="Minimum weight below which Last.fm/onboarding enrichment is skipped")


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
    weight_impression_negative: float = Field(1.5, ge=1, le=10, description="Sample weight for shown-but-not-played tracks")


class RadioConfig(BaseModel):
    """Radio session parameters."""

    seed_weight: float = Field(0.50, ge=0, le=1, description="How much the seed anchor influences the drift embedding")
    feedback_weight: float = Field(0.30, ge=0, le=1, description="How much in-session feedback shifts the drift embedding")
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
    session_ttl_hours: float = Field(4.0, ge=0.5, le=24, description="Hours of inactivity before a radio session expires")
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


# ---------------------------------------------------------------------------
# API request/response models
# ---------------------------------------------------------------------------

class AlgorithmConfigResponse(BaseModel):
    """Response when reading a config."""
    id: int
    version: int
    name: Optional[str] = None
    config: AlgorithmConfigData
    is_active: bool
    created_at: int
    created_by: Optional[str] = None


class AlgorithmConfigUpdate(BaseModel):
    """Request body for updating config. Partial updates supported."""
    name: Optional[str] = None
    config: AlgorithmConfigData


class AlgorithmConfigImport(BaseModel):
    """Request body for importing a config (e.g. shared by another user)."""
    name: Optional[str] = None
    config: Dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Groups with metadata for the future GUI.
CONFIG_GROUPS: List[Dict[str, Any]] = [
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
]


def get_defaults() -> AlgorithmConfigData:
    """Return the default configuration (matches original hardcoded values)."""
    return AlgorithmConfigData()


def get_defaults_dict() -> Dict[str, Any]:
    """Return the default configuration as a plain dict."""
    return get_defaults().model_dump()
