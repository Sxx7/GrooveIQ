"""
GrooveIQ — Tests for mood-label validation across the API surface.

The EffNet pipeline only emits 5 mood labels (happy/sad/aggressive/relaxed/
party). Earlier the iOS app silently sent mood=energetic and got an empty
``tracks: []`` response with reason ``no_candidates`` — the filter just
matched nothing instead of rejecting the unknown label. These tests pin the
contract so the failure mode can't return.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.schemas import OnboardingRequest, PlaylistCreate, PlaylistStrategy
from app.services.audio_analysis import SUPPORTED_MOOD_LABELS


def _has_python_311_features() -> bool:
    """playlist_service.py uses datetime.UTC which only exists in 3.11+.
    The runtime image is on 3.12; skip the service-import test elsewhere."""
    try:
        from datetime import UTC  # noqa: F401

        return True
    except ImportError:
        return False


class TestSupportedMoodLabelsConstant:
    def test_matches_effnet_mood_models(self):
        """The constant must stay in sync with `mood_models` in
        analysis_worker.py — that's the actual source of truth for what
        labels the pipeline can produce."""
        import inspect

        from app.services import analysis_worker

        src = inspect.getsource(analysis_worker)
        for label in SUPPORTED_MOOD_LABELS:
            assert f'"{label}":' in src, f"Constant has {label!r} but analysis_worker doesn't"

    def test_is_immutable(self):
        """frozenset prevents accidental mutation across the codebase."""
        assert isinstance(SUPPORTED_MOOD_LABELS, frozenset)

    def test_exactly_the_five_effnet_labels(self):
        assert frozenset({"happy", "sad", "aggressive", "relaxed", "party"}) == SUPPORTED_MOOD_LABELS


# ---------------------------------------------------------------------------
# PlaylistCreate (POST /v1/playlists) — pydantic-level validation
# ---------------------------------------------------------------------------


class TestPlaylistCreateMoodValidation:
    def test_accepts_each_supported_mood(self):
        for mood in SUPPORTED_MOOD_LABELS:
            p = PlaylistCreate(name="x", strategy=PlaylistStrategy.MOOD, params={"mood": mood})
            assert p.params["mood"] == mood

    def test_rejects_energetic(self):
        """The exact label that broke the iOS app."""
        with pytest.raises(ValidationError, match="energetic"):
            PlaylistCreate(name="x", strategy=PlaylistStrategy.MOOD, params={"mood": "energetic"})

    def test_rejects_other_invalid_moods(self):
        for bad in ("chill", "workout", "acoustic", "electronic", "RandomNonsense"):
            with pytest.raises(ValidationError):
                PlaylistCreate(name="x", strategy=PlaylistStrategy.MOOD, params={"mood": bad})

    def test_rejects_missing_mood_param(self):
        with pytest.raises(ValidationError, match=r"params\.mood is required"):
            PlaylistCreate(name="x", strategy=PlaylistStrategy.MOOD, params={})


# ---------------------------------------------------------------------------
# Service-layer defensive check (internal callers / tests bypass pydantic)
# ---------------------------------------------------------------------------


class TestPlaylistServiceMoodGuard:
    @pytest.mark.skipif(not _has_python_311_features(), reason="playlist_service uses datetime.UTC (3.11+)")
    def test_generate_mood_rejects_unknown(self):
        from app.services.playlist_service import _generate_mood

        with pytest.raises(ValueError, match="energetic"):
            _generate_mood([], "energetic", max_tracks=10)


# ---------------------------------------------------------------------------
# OnboardingRequest — silently drops unknown moods (forgiving for clients)
# ---------------------------------------------------------------------------


class TestOnboardingMoodFilter:
    def test_drops_unknown_moods_keeps_valid_ones(self):
        """Mixed list — keep what we recognise, silently drop the rest. Older
        client versions (or new-feature flag drifts) shouldn't break onboarding."""
        req = OnboardingRequest(
            mood_preferences=["happy", "energetic", "relaxed", "workout", "ChIlL"],
        )
        assert req.mood_preferences == ["happy", "relaxed"]

    def test_all_unknown_moods_becomes_empty_list(self):
        # OnboardingRequest still requires *some* field overall, so combine
        # with another field to satisfy at_least_one_field.
        req = OnboardingRequest(
            mood_preferences=["energetic", "chill"],
            favourite_genres=["rock"],
        )
        assert req.mood_preferences == []

    def test_case_insensitive_matching(self):
        req = OnboardingRequest(mood_preferences=["HAPPY", "Sad", "PaRty"])
        # The validator lowercases for comparison; here all three match the
        # supported set after .lower(), so all three survive.
        assert set(req.mood_preferences) == {"HAPPY", "Sad", "PaRty"}

    def test_empty_input_passes_through(self):
        req = OnboardingRequest(mood_preferences=None, favourite_genres=["rock"])
        assert req.mood_preferences is None
