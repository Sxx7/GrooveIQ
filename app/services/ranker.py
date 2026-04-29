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
from typing import Any

from app.db.session import AsyncSessionLocal
from app.services.algorithm_config import get_config
from app.services.feature_eng import FEATURE_COLUMNS, NUM_FEATURES, build_features, build_training_data

logger = logging.getLogger(__name__)

# Singleton state.
_lock = threading.Lock()
_model: object | None = None  # lightgbm.LGBMRegressor
_model_version: str | None = None
_model_stats: dict[str, Any] = {}

# Config.
_MODEL_DIR = os.environ.get("GROOVEIQ_MODEL_DIR", "/data/models")


def _create_model():
    """
    Create the best available gradient boosting model.
    Prefers LightGBM, falls back to scikit-learn GBR if libomp is missing.
    """
    cfg = get_config().ranker
    try:
        import lightgbm as lgb

        return lgb.LGBMRegressor(
            n_estimators=cfg.n_estimators,
            max_depth=cfg.max_depth,
            learning_rate=cfg.learning_rate,
            num_leaves=cfg.num_leaves,
            min_child_samples=cfg.min_child_samples,
            subsample=cfg.subsample,
            colsample_bytree=cfg.colsample_bytree,
            reg_alpha=cfg.reg_alpha,
            reg_lambda=cfg.reg_lambda,
            random_state=42,
            verbose=-1,
        ), "lgbm"
    except (ImportError, OSError):
        from sklearn.ensemble import GradientBoostingRegressor

        logger.info("LightGBM unavailable, using sklearn GradientBoostingRegressor.")
        return GradientBoostingRegressor(
            n_estimators=cfg.n_estimators,
            max_depth=cfg.max_depth,
            learning_rate=cfg.learning_rate,
            subsample=cfg.subsample,
            random_state=42,
        ), "sklearn-gbr"


def _save_model(model, engine: str, version: str) -> str | None:
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


def load_latest() -> bool:
    """
    Load the most recently saved ranker model from ``_MODEL_DIR`` into the
    in-memory singleton. Returns True on success, False if no file is found
    or loading fails.

    Called from the FastAPI lifespan so the ranker is warm immediately after a
    container restart, instead of falling back to the satisfaction_score
    baseline until the next scheduled pipeline run. (#43)
    """
    model_dir = Path(_MODEL_DIR)
    if not model_dir.exists():
        return False

    # Prefer the newer LightGBM .lgb files; fall back to sklearn .pkl.
    candidates = sorted(
        list(model_dir.glob("ranker_*.lgb")) + list(model_dir.glob("ranker_*.pkl")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return False

    path = candidates[0]
    try:
        if path.suffix == ".lgb":
            import lightgbm as lgb

            model = lgb.Booster(model_file=str(path))
            engine = "lgbm-booster"
        else:
            import joblib

            model = joblib.load(str(path))
            engine = "sklearn-gbr"
    except Exception as e:
        logger.warning(f"Ranker load_latest failed for {path}: {e}")
        return False

    version = path.stem.replace("ranker_", "")

    feature_importances: dict[str, float] = {}
    try:
        if hasattr(model, "feature_importance"):
            importances = model.feature_importance()
        else:
            importances = model.feature_importances_
        for i, col in enumerate(FEATURE_COLUMNS):
            if i < len(importances):
                feature_importances[col] = float(importances[i])
    except Exception:
        pass

    stats = {
        "trained": True,
        "training_samples": None,
        "n_features": NUM_FEATURES,
        "model_version": version,
        "engine": engine,
        "trained_at": int(path.stat().st_mtime),
        "saved_path": str(path),
        "feature_importances": feature_importances,
        "loaded_from_disk": True,
    }

    with _lock:
        global _model, _model_version, _model_stats
        _model = model
        _model_version = version
        _model_stats = stats

    logger.info(f"Ranker loaded from disk: {path.name} (engine={engine})")
    return True


def _train_ranker_sync(features, labels, sample_weights=None) -> tuple:
    """CPU-bound model training.  Runs in a thread executor."""
    model, engine = _create_model()
    if engine == "lgbm":
        model.fit(features, labels, sample_weight=sample_weights, feature_name=FEATURE_COLUMNS)
    else:
        model.fit(features, labels, sample_weight=sample_weights)
    return model, engine


async def train_model() -> dict[str, Any]:
    """
    Train a ranking model on all track_interactions.

    Prefers LightGBM; falls back to sklearn GBR if LightGBM is unavailable.
    Returns summary dict with training_samples, model_version, etc.
    """
    import asyncio

    async with AsyncSessionLocal() as session:
        data = await build_training_data(session)

    cfg = get_config().ranker
    n = data["n_samples"]
    if n < cfg.min_training_samples:
        logger.warning(f"Ranker: only {n} samples (<{cfg.min_training_samples}), skipping training.")
        return {"trained": False, "training_samples": n, "reason": "insufficient_data"}

    features = data["features"]
    labels = data["labels"]
    sample_weights = data.get("sample_weights")

    # Run CPU-heavy model training in a thread so the event loop stays responsive.
    loop = asyncio.get_running_loop()
    model, engine = await loop.run_in_executor(
        None,
        _train_ranker_sync,
        features,
        labels,
        sample_weights,
    )

    version = f"{engine}-{int(time.time())}"
    saved_path = _save_model(model, engine, version)

    # Extract feature importances.
    feature_importances = {}
    try:
        importances = model.feature_importances_
        for i, col in enumerate(FEATURE_COLUMNS):
            feature_importances[col] = float(importances[i])
    except Exception:
        pass

    stats = {
        "trained": True,
        "training_samples": n,
        "n_features": NUM_FEATURES,
        "model_version": version,
        "engine": engine,
        "trained_at": int(time.time()),
        "saved_path": saved_path,
        "feature_importances": feature_importances,
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
    candidate_track_ids: list[str],
    session,
    hour_of_day: int | None = None,
    day_of_week: int | None = None,
    device_type: str | None = None,
    output_type: str | None = None,
    context_type: str | None = None,
    location_label: str | None = None,
) -> list[tuple[str, float]]:
    """
    Score candidate tracks using the trained model.

    Returns list of (track_id, score) sorted descending by score.
    Falls back to satisfaction_score-based ranking if no model is trained.
    """
    with _lock:
        model = _model

    # Build feature vectors.
    result = await build_features(
        user_id,
        candidate_track_ids,
        session,
        hour_of_day=hour_of_day,
        day_of_week=day_of_week,
        device_type=device_type,
        output_type=output_type,
        context_type=context_type,
        location_label=location_label,
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


def get_model_version() -> str | None:
    with _lock:
        return _model_version


def get_model_stats() -> dict[str, Any]:
    with _lock:
        if not _model_stats:
            return {"trained": False}
        return dict(_model_stats)


def is_ready() -> bool:
    with _lock:
        return _model is not None
