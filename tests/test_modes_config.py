"""
GrooveIQ – Tests for the discovery-dial 'modes' config group (Chunk 2).

Validates preset defaults, the no-regression calibration of ``balanced``
against today's live reranker values, field constraints, and migration
safety (a stored config missing ``modes`` back-fills defaults).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.algorithm_config_schema import (
    PRESET_NAMES,
    AlgorithmConfigData,
    ModesConfig,
    PresetConfig,
    RerankerConfig,
    get_defaults,
)


def test_all_presets_present_with_default_balanced():
    modes = get_defaults().modes
    for name in ("familiar", "balanced", "discovery", "deep_discovery"):
        assert isinstance(getattr(modes, name), PresetConfig)
    assert modes.default_preset == "balanced"
    assert set(PRESET_NAMES) == {"familiar", "balanced", "discovery", "deep_discovery"}


def test_balanced_reproduces_today_live_values():
    """balanced must mirror the live RerankerConfig defaults — the no-regression contract."""
    reranker_defaults = RerankerConfig()
    balanced = get_defaults().modes.balanced

    assert balanced.exploration_fraction == reranker_defaults.exploration_fraction == 0.15
    assert balanced.freshness_boost == reranker_defaults.freshness_boost == 0.10
    assert balanced.repeat_window_hours == reranker_defaults.repeat_window_hours == 2.0
    # No acquisition tilt, no novelty filter, no source re-weighting -> identical to today.
    assert balanced.kappa == 0.0
    assert balanced.novelty_filter is False
    assert balanced.source_weight_mult == {}


def test_familiar_posture_is_exploitation():
    familiar = get_defaults().modes.familiar
    assert familiar.kappa == 0.0
    assert familiar.exploration_fraction == 0.0
    assert familiar.freshness_boost == 0.0
    assert familiar.novelty_filter is False
    # Relaxed anti-repetition so favourites may recur.
    assert familiar.repeat_window_hours == 0.0


def test_discovery_presets_enable_novelty_and_rising_exploration():
    modes = get_defaults().modes
    assert modes.discovery.novelty_filter is True
    assert modes.deep_discovery.novelty_filter is True
    # The dial monotonically increases exploration and the acquisition coefficient.
    assert (
        modes.deep_discovery.exploration_fraction
        >= modes.discovery.exploration_fraction
        >= modes.balanced.exploration_fraction
    )
    assert modes.deep_discovery.kappa >= modes.discovery.kappa >= modes.balanced.kappa


def test_dial_anchors_span_unit_interval_in_order():
    anchors = get_defaults().modes.dial_anchors
    assert anchors["familiar"] == 0.0
    assert anchors["deep_discovery"] == 1.0
    assert anchors["familiar"] < anchors["balanced"] < anchors["discovery"] < anchors["deep_discovery"]


def test_constraints_reject_out_of_range_proven_mu():
    with pytest.raises(ValidationError):
        PresetConfig(proven_mu_min=2.0)


def test_constraints_reject_out_of_range_exploration_fraction():
    with pytest.raises(ValidationError):
        PresetConfig(exploration_fraction=0.9)  # exceeds the 0.5 cap


def test_constraints_reject_out_of_range_source_multiplier():
    # Per-value [0, 5] constraint on the source_weight_mult dict values.
    with pytest.raises(ValidationError):
        PresetConfig(source_weight_mult={"cf": 99.0})


def test_default_preset_must_be_a_known_preset():
    with pytest.raises(ValidationError):
        ModesConfig(default_preset="bogus")


def test_config_missing_modes_backfills_defaults():
    """Migration safety: an old stored config without `modes` validates and fills defaults."""
    raw = {"reranker": {"freshness_boost": 0.10}}  # as an old DB row would look
    cfg = AlgorithmConfigData.model_validate(raw)
    assert isinstance(cfg.modes, ModesConfig)
    assert cfg.modes.default_preset == "balanced"
    assert cfg.modes.balanced.exploration_fraction == 0.15


def test_empty_config_produces_full_modes_group():
    """An empty {} import yields the complete default modes group."""
    cfg = AlgorithmConfigData.model_validate({})
    assert cfg.modes.familiar.novelty_filter is False
    assert cfg.modes.deep_discovery.novelty_filter is True
    # Round-trips through a dump (export/import path) without losing the group.
    redumped = AlgorithmConfigData.model_validate(cfg.model_dump())
    assert redumped.modes.default_preset == "balanced"
