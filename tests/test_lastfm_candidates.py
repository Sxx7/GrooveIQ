"""
GrooveIQ – Tests for Last.fm candidate fuzzy title matching (coverage fix).

Covers _base_title (remix/feat/version stripping, conservative) and
_match_to_library (exact-first, base-title fallback, both directions).
"""

from __future__ import annotations

from app.services.lastfm_candidates import _base_title, _match_to_library


class TestBaseTitle:
    def test_strips_remix_paren(self):
        assert _base_title("Broken Angel (Alex Skrindo Remix)") == "broken angel"
        assert _base_title("Always Loved A Film (Michael Woods Remix)") == "always loved a film"

    def test_strips_bracket_remix(self):
        assert _base_title("Song [Club Mix]") == "song"

    def test_strips_feat(self):
        assert _base_title("You Can't Change Me feat. Raye") == "you can't change me"
        assert _base_title("Track ft. Someone") == "track"
        assert _base_title("Tune (feat. Guest)") == "tune"

    def test_strips_dash_version(self):
        assert _base_title("Till The World Ends - DJ Kue Club Remix") == "till the world ends"
        assert _base_title("Song - Radio Edit") == "song"

    def test_strips_nested_and_multiple(self):
        # Only version groups are removed; a non-version group is kept.
        assert _base_title("Song (Deluxe) (Live)") == "song (deluxe)"
        assert _base_title("Song (Remastered 2011)") == "song"

    def test_preserves_non_version_parentheticals(self):
        # These are distinct songs, not variants — must NOT collapse.
        assert _base_title("Clair de Lune (Suite bergamasque)") == "clair de lune (suite bergamasque)"
        assert _base_title("Gangsta's Paradise") == "gangsta's paradise"

    def test_does_not_false_match_feat_substring(self):
        assert _base_title("Feature Presentation") == "feature presentation"
        assert _base_title("Featherweight") == "featherweight"

    def test_plain_title_lowercased(self):
        assert _base_title("AD ASTRA") == "ad astra"


class TestMatchToLibrary:
    def _lookups(self):
        # Library has the base "song" and a remix of "tune".
        lib = {("artist", "song"): "t_song", ("artist", "tune (x remix)"): "t_tune_remix"}
        base = {("artist", "song"): "t_song", ("artist", "tune"): "t_tune_remix"}
        return lib, base

    def test_exact_match_not_fuzzy(self):
        lib, base = self._lookups()
        tid, fuzzy = _match_to_library("Artist", "Song", lib, base)
        assert tid == "t_song" and fuzzy is False

    def test_lastfm_base_matches_library_remix(self):
        # Last.fm returns the base "Tune"; library only has "Tune (X Remix)".
        lib, base = self._lookups()
        tid, fuzzy = _match_to_library("Artist", "Tune", lib, base)
        assert tid == "t_tune_remix" and fuzzy is True

    def test_lastfm_remix_matches_library_base(self):
        # Last.fm returns "Song (Some Remix)"; library only has base "Song".
        lib, base = self._lookups()
        tid, fuzzy = _match_to_library("Artist", "Song (Some Remix)", lib, base)
        assert tid == "t_song" and fuzzy is True

    def test_no_match(self):
        lib, base = self._lookups()
        tid, fuzzy = _match_to_library("Artist", "Unknown", lib, base)
        assert tid is None and fuzzy is False
