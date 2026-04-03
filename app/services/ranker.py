"""
GrooveIQ – LightGBM ranking model (Phase 4, Step 6).

Trains a LGBMRegressor on satisfaction_score labels and stores the
model as a module-level singleton for fast inference.

Fallback: when no model is trained, `score_candidates` returns
satisfaction_score-based ranking (same as the pre-ranker behaviour).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.db.session import AsyncSessionLocal
from app.services.feature_eng import FEATURE_COLUMNS, NUM_FEATURES, build_features, build_training_data

logger = logging.getLogger(__name__)

# Singleton state.
_lock = threading.Lock()
_model: Optional[object] = None  # lightgbm.LGBMRegressor
_model_version: Optional[str] = None
_model_stats: Dict[str, Any] = {}

# Config.
_MIN_TRAINING_SAMPLES = 50
_MODEL_DIR = os.environ.get("GROOVEIQ_MODEL_DIR", "/data/models")


def _create_model():
    """
    Create the best available gradient boosting model.
    Prefers LightGBM, falls back to scikit-learn GBR if libomp is missing.
    """
    try:
        import lightgbm as lgb
        return lgb.LGBMRegressor(
            n_estimators=200,
            max_depth=6,
            learning_rate=0.05,
            num_leaves=31,
            min_child_samples=5,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=0.1,
            reg_lambda=0.1,
            random_state=42,
            verbose=-1,
        ), "lgbm"
    except (ImportError, OSError):
        from sklearn.ensemble import GradientBoostingRegressor
        logger.info("LightGBM unavailable, using sklearn GradientBoostingRegressor.")
        return GradientBoostingRegressor(
            n_estimators=200,
            max_depth=6,
            learning_rate=0.05,
            subsample=0.8,
            random_state=42,
        ), "sklearn-gbr"


def _save_model(model, engine: str, version: str) -> Optional[str]:
    """Persist model to disk. Returns saved path or None."""
    try:
        model_dir = Path(_MODEL_DIR)
        model_dir.mkdir(parents=True, exist_ok=True)
        if engine == "lgbm":
            saved_path = str(model_dir / f"ranker_{version}.lgb")
            model.booster_.save_model(saved_path)
        else:
            import joblib
            saved_path = str(model_dir / f"ranker_{version}.pkl")
            joblib.dump(model, saved_path)
        logger.info(f"Ranker model saved: {saved_path}")
        return saved_path
    except Exception as e:
        logger.warning(f"Could not save ranker model to disk: {e}")
        return None


async def train_model() -> Dict[str, Any]:
    """
    Train a ranking model on all track_interactions.

    Prefers LightGBM; falls back to sklearn GBR if LightGBM is unavailable.
    Returns summary dict with training_samples, model_version, etc.
    """
    async with AsyncSessionLocal() as session:
        data = await build_training_data(session)

    n = data["n_samples"]
    if n < _MIN_TRAINING_SAMPLES:
        logger.warning(f"Ranker: only {n} samples (<{_MIN_TRAINING_SAMPLES}), skipping training.")
        return {"trained": False, "training_samples": n, "reason": "insufficient_data"}

    features = data["features"]
    labels = data["labels"]

    model, engine = _create_model()

    if engine == "lgbm":
        model.fit(features, labels, feature_name=FEATURE_COLUMNS)
    else:
        model.fit(features, labels)

    version = f"{engine}-{int(time.time())}"
    saved_path = _save_model(model, engine, version)

    stats = {
        "trained": True,
        "training_samples": n,
        "n_features": NUM_FEATURES,
        "model_version": version,
        "engine": engine,
        "trained_at": int(time.time()),
        "saved_path": saved_path,
    }

    with _lock:
        global _model, _model_version, _model_stats
        _model = model
        _model_version = version
        _model_stats = stats

    logger.info(f"Ranker trained: {n} samples, engine={engine}, version={version}")
    return stats


async def score_candidates(
    user_id: str,
    candidate_track_ids: List[str],
    session,
    hour_of_day: Optional[int] = None,
    day_of_week: Optional[int] = None,
) -> List[Tuple[str, float]]:
    """
    Score candidate tracks using the trained model.

    Returns list of (track_id, score) sorted descending by score.
    Falls back to satisfaction_score-based ranking if no model is trained.
    """
    with _lock:
        model = _model

    # Build feature vectors.
    result = await build_features(
        user_id, candidate_track_ids, session,
        hour_of_day=hour_of_day, day_of_week=day_of_week,
    )

    track_ids = result["track_ids"]
    features = result["features"]

    if not track_ids:
        return []

    if model is not None:
        scores = model.predict(features)
    else:
        # Fallback: use satisfaction_score feature (index 8 in FEATURE_COLUMNS).
        sat_idx = FEATURE_COLUMNS.index("satisfaction_score")
        scores = features[:, sat_idx]

    # Sort by score descending.
    scored = list(zip(track_ids, scores.tolist()))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def get_model_version() -> Optional[str]:
    with _lock:
        return _model_version


def get_model_stats() -> Dict[str, Any]:
    with _lock:
        if not _model_stats:
            return {"trained": False}
        return dict(_model_stats)


def is_ready() -> bool:
    with _lock:
        return _model is not None
