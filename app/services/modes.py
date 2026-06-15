"""
GrooveIQ – Discovery-dial resolution (Chunk 5).

This module is the **untrusted-input boundary** for the recommendation
"discovery dial".  The recommend endpoint accepts two optional, edge-validated
request params — ``discovery`` (a float clamped to ``[0, 1]`` by FastAPI) and
``mode`` (validated against :data:`PRESET_NAMES`) — and maps them, through the
*pure* :func:`resolve_dial`, onto a **whitelisted** request-scoped config
override (Chunk 1's ``apply_overrides``).

Two safety properties hold *by construction*:

* **No raw client value ever becomes an override key.**  The override is built
  from a fixed template; only the keys in :data:`OVERRIDE_WHITELIST` can ever
  appear, and :func:`_enforce_whitelist` strips anything else as defence in
  depth.  A crafted ``mode``/``discovery`` cannot reach an arbitrary
  ``AlgorithmConfig`` field (e.g. the ranker hyperparameters).
* **Override *values* come from the admin-tuned modes config, not the request.**
  The only request-derived number is the dial position, which selects or
  interpolates between admin-defined :class:`PresetConfig` anchors whose every
  field is Pydantic-clamped.  Interpolation is a convex combination, so the
  result stays within those same bounds — no clamping of our own is needed.

The override shape exactly matches what Chunk 4 consumes (see
``tests/test_modes_behavior.py::_preset_override``): ``modes.active`` (the
dial-resolved preset the reranker / candidate_gen read) plus the three
overlapping ``reranker`` knobs the preset tunes.

:func:`derive_reasons` is a small pure helper that turns a track's candidate
sources + the reranker's per-track action log into the human-readable
``reasons`` chips the response carries (Spotify's "because you listen to X").
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.algorithm_config_schema import PRESET_NAMES, ModesConfig, PresetConfig

# ---------------------------------------------------------------------------
# Override whitelist — the only config paths the dial is permitted to write.
# ---------------------------------------------------------------------------

# Top-level group -> the set of leaf keys the resolver may set inside it.
# ``modes.active`` is a full ``PresetConfig`` dump; its inner keys are further
# bounded to ``PresetConfig.model_fields`` by :func:`_enforce_whitelist`.
OVERRIDE_WHITELIST: dict[str, frozenset[str]] = {
    "modes": frozenset({"active"}),
    "reranker": frozenset({"exploration_fraction", "freshness_boost", "repeat_window_hours"}),
}

# The continuous numeric fields interpolated between dial anchors.  Booleans
# (``novelty_filter``) and dict fields (``source_weight_mult``) are handled
# explicitly in :func:`_lerp_preset`.
_NUMERIC_FIELDS: tuple[str, ...] = (
    "kappa",
    "lambda_proven",
    "exploration_fraction",
    "freshness_boost",
    "novelty_strength",
    "novelty_weight",
    "repeat_window_hours",
    "proven_mu_min",
    "proven_sigma_max",
    # Two-axis radio levers — interpolate so a continuous `discovery` float gives a smooth,
    # monotonic anchoring/novelty sweep. Named modes use the literal preset (no interpolation),
    # so they are unaffected; this only fills the gap for fractional dial values.
    "seed_anchor_weight",
    "semiknown_fraction",
)


@dataclass(frozen=True)
class DialResolution:
    """The outcome of resolving a request's dial position.

    ``overrides`` is the whitelisted, request-scoped config override to hand to
    ``apply_overrides``.  ``discovery`` is the resolved ``[0, 1]`` value to echo
    back in the response.  ``preset`` is the named preset when one was used
    (an explicit ``mode=`` or the default), or ``None`` for an interpolated
    point between anchors.
    """

    overrides: dict
    discovery: float
    preset: str | None


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


# ---------------------------------------------------------------------------
# Dial -> preset
# ---------------------------------------------------------------------------


def _sorted_anchors(modes_cfg: ModesConfig) -> list[tuple[float, str, PresetConfig]]:
    """The four presets ordered by their position on the ``[0, 1]`` dial."""
    anchors = [(float(modes_cfg.dial_anchors.get(name, 0.0)), name, getattr(modes_cfg, name)) for name in PRESET_NAMES]
    anchors.sort(key=lambda a: a[0])
    return anchors


def _lerp_preset(a: PresetConfig, b: PresetConfig, t: float) -> PresetConfig:
    """Linearly interpolate between two anchor presets at fraction ``t`` in ``[0, 1]``.

    Numeric fields lerp directly.  ``source_weight_mult`` lerps per source over
    the union of keys (a source absent from one anchor defaults to ``1.0``).
    ``novelty_filter`` engages smoothly with strength — it is on whenever any
    proven slice is excluded — so it reproduces each anchor's own flag exactly
    (every preset with ``novelty_filter=True`` also has ``novelty_strength>0``,
    and vice versa).
    """
    values: dict = {f: _lerp(getattr(a, f), getattr(b, f), t) for f in _NUMERIC_FIELDS}
    keys = set(a.source_weight_mult) | set(b.source_weight_mult)
    values["source_weight_mult"] = {
        k: _lerp(a.source_weight_mult.get(k, 1.0), b.source_weight_mult.get(k, 1.0), t) for k in keys
    }
    values["novelty_filter"] = values["novelty_strength"] > 0.0
    # require_interaction (the Discover brand-new floor) follows the semi-known quota, mirroring
    # how novelty_filter follows novelty_strength — on wherever a semi-known slice is requested.
    values["require_interaction"] = values["semiknown_fraction"] > 0.0
    # PresetConfig validates every field's range; a convex combination of two
    # in-range anchors is in range, so this never raises.
    return PresetConfig(**values)


def _interpolate_preset(d: float, modes_cfg: ModesConfig) -> tuple[PresetConfig, str | None]:
    """Resolve a continuous dial value to a preset, interpolating between anchors.

    Exact anchor hits return that named preset; values between anchors return a
    fresh interpolated ``PresetConfig`` with ``preset=None``.  Values outside the
    anchored range snap to the nearest extreme preset.
    """
    anchors = _sorted_anchors(modes_cfg)
    lo_pos, lo_name, lo_preset = anchors[0]
    hi_pos, hi_name, hi_preset = anchors[-1]
    if d <= lo_pos:
        return lo_preset, lo_name
    if d >= hi_pos:
        return hi_preset, hi_name
    for i in range(len(anchors) - 1):
        p0, n0, pre0 = anchors[i]
        p1, n1, pre1 = anchors[i + 1]
        if p0 <= d <= p1:
            if d == p0:
                return pre0, n0
            if d == p1:
                return pre1, n1
            return _lerp_preset(pre0, pre1, (d - p0) / (p1 - p0)), None
    # Unreachable given the clamps above; fall back to the top anchor.
    return hi_preset, hi_name


# ---------------------------------------------------------------------------
# Preset -> whitelisted override
# ---------------------------------------------------------------------------


def _preset_to_override(preset: PresetConfig) -> dict:
    """Build the request override for a resolved preset.

    Sets ``modes.active`` (read by the reranker's acquisition term and
    candidate_gen's source-weight / novelty filter) plus the three overlapping
    ``reranker`` knobs the preset tunes.  This is the exact shape Chunk 4's
    deep call sites consume via ``get_config()``.
    """
    return {
        "modes": {"active": preset.model_dump()},
        "reranker": {
            "exploration_fraction": preset.exploration_fraction,
            "freshness_boost": preset.freshness_boost,
            "repeat_window_hours": preset.repeat_window_hours,
        },
    }


def _enforce_whitelist(overrides: dict) -> dict:
    """Strip any key not in :data:`OVERRIDE_WHITELIST` (defence in depth).

    The resolver only ever builds whitelisted dicts, so on the happy path this
    is a no-op.  It exists so that a future bug — or a hand-built dict passed in
    — can never smuggle an arbitrary config key into the per-request merge.
    """
    clean: dict = {}
    for group, fields in overrides.items():
        allowed = OVERRIDE_WHITELIST.get(group)
        if allowed is None or not isinstance(fields, dict):
            continue
        kept: dict = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if group == "modes" and key == "active" and isinstance(value, dict):
                # ``active`` is a full PresetConfig dump — keep only real fields.
                value = {k: v for k, v in value.items() if k in PresetConfig.model_fields}
            kept[key] = value
        if kept:
            clean[group] = kept
    return clean


def resolve_dial(discovery: float | None, mode: str | None, modes_cfg: ModesConfig) -> DialResolution:
    """Map an edge-validated ``discovery`` / ``mode`` request to a safe override.

    Precedence: an explicit named ``mode`` wins over a raw ``discovery`` float;
    when neither is given the configured ``default_preset`` is used (``balanced``
    out of the box, which reproduces today's policy).

    The returned :class:`DialResolution` carries the whitelisted override and
    the resolved dial value to echo in the response.
    """
    if mode is not None:
        # Defence in depth — the route also validates this and returns 422.
        if mode not in PRESET_NAMES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {PRESET_NAMES}")
        preset = getattr(modes_cfg, mode)
        resolved = float(modes_cfg.dial_anchors.get(mode, 0.0))
        name: str | None = mode
    elif discovery is not None:
        resolved = _clip01(float(discovery))
        preset, name = _interpolate_preset(resolved, modes_cfg)
    else:
        name = modes_cfg.default_preset
        preset = getattr(modes_cfg, name)
        resolved = float(modes_cfg.dial_anchors.get(name, 0.0))

    return DialResolution(
        overrides=_enforce_whitelist(_preset_to_override(preset)),
        discovery=resolved,
        preset=name,
    )


# ---------------------------------------------------------------------------
# Per-track reason chips
# ---------------------------------------------------------------------------

# Candidate retrieval source -> a human-readable reason chip.
_SOURCE_REASONS: dict[str, str] = {
    "content": "matches_your_taste",
    "content_profile": "matches_your_taste",
    "cf": "fans_like_this",
    "lastfm_similar": "similar_listeners",
    "sasrec": "your_listening_pattern",
    "session_skipgram": "your_listening_pattern",
    "artist_recall": "artist_you_know",
    "popular": "popular",
}


def derive_reasons(sources: list[str], actions: list[dict]) -> list[str]:
    """Derive the per-track ``reasons`` chips from its sources + reranker actions.

    Intent/confidence reasons (from the reranker's gated acquisition + freshness
    actions) come first as the most salient signal, then source-derived reasons.
    The list is de-duplicated and order-stable so the output is deterministic.

    * ``acquisition`` action with ``is_proven`` -> ``"proven_favourite"``,
      otherwise ``"exploring"`` (the dial lifted it for its uncertainty).
    * ``freshness_boost`` action -> ``"new_to_you"`` (never played).
    * each candidate source maps via :data:`_SOURCE_REASONS`.
    """
    reasons: list[str] = []

    def add(tag: str | None) -> None:
        if tag and tag not in reasons:
            reasons.append(tag)

    for action in actions:
        kind = action.get("action")
        if kind == "acquisition":
            add("proven_favourite" if action.get("is_proven") else "exploring")
        elif kind == "freshness_boost":
            add("new_to_you")
        elif kind == "recently_engaged_boost":
            add("keep_listening")

    for source in sources:
        add(_SOURCE_REASONS.get(source))

    return reasons
