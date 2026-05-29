"""Tests for `_detect_playlist_intent`.

Voice phrases that signal the user wants a playlist (vs. a track / artist /
album) need a different search path. This module validates the detector:

  - "play my X playlist" / "play the X playlist" → strong intent, cleaned X
  - "play my X" (no "playlist" word) → soft intent (playlist-first, catalog fallback)
  - everything else → no intent (existing catalog-first search)
"""
from __future__ import annotations

import pytest

from commands.spotify.command import _detect_playlist_intent


class TestStrongIntent:
    """Voice phrase contains the literal word 'playlist' — clearest signal."""

    def test_my_x_playlist(self) -> None:
        intent, cleaned = _detect_playlist_intent(
            voice_command="play my running playlist",
            query="my running playlist",
        )
        assert intent == "strong"
        assert cleaned == "running"

    def test_the_x_playlist(self) -> None:
        intent, cleaned = _detect_playlist_intent(
            voice_command="play the chill playlist",
            query="the chill playlist",
        )
        assert intent == "strong"
        assert cleaned == "chill"

    def test_x_playlist_no_qualifier(self) -> None:
        intent, cleaned = _detect_playlist_intent(
            voice_command="play workout playlist",
            query="workout playlist",
        )
        assert intent == "strong"
        assert cleaned == "workout"

    def test_playlist_x_prefix_form(self) -> None:
        intent, cleaned = _detect_playlist_intent(
            voice_command="play playlist favorite songs 2026",
            query="playlist favorite songs 2026",
        )
        assert intent == "strong"
        assert cleaned == "favorite songs 2026"

    def test_multi_word_playlist_name(self) -> None:
        intent, cleaned = _detect_playlist_intent(
            voice_command="play my favorite songs 2026 playlist",
            query="my favorite songs 2026 playlist",
        )
        assert intent == "strong"
        assert cleaned == "favorite songs 2026"


class TestSoftIntent:
    """`play my X` without the word 'playlist' — ambiguous, try playlist then catalog."""

    def test_my_discover_weekly(self) -> None:
        intent, cleaned = _detect_playlist_intent(
            voice_command="play my discover weekly",
            query="my discover weekly",
        )
        assert intent == "soft"
        assert cleaned == "discover weekly"

    def test_my_workout(self) -> None:
        intent, cleaned = _detect_playlist_intent(
            voice_command="play my workout",
            query="my workout",
        )
        assert intent == "soft"
        assert cleaned == "workout"


class TestNoIntent:
    """No playlist signal — existing catalog-first behavior should run."""

    def test_plain_artist_name(self) -> None:
        intent, cleaned = _detect_playlist_intent(
            voice_command="play radiohead",
            query="radiohead",
        )
        assert intent == "none"
        assert cleaned == "radiohead"

    def test_play_the_beatles_is_artist(self) -> None:
        # `the` alone isn't a playlist signal — common artist prefix.
        intent, cleaned = _detect_playlist_intent(
            voice_command="play the beatles",
            query="the beatles",
        )
        assert intent == "none"
        assert cleaned == "the beatles"

    def test_play_a_song_title(self) -> None:
        intent, cleaned = _detect_playlist_intent(
            voice_command="play bohemian rhapsody",
            query="bohemian rhapsody",
        )
        assert intent == "none"
        assert cleaned == "bohemian rhapsody"

    def test_play_favorite_songs_2026_no_marker(self) -> None:
        # No `my`, no `playlist` — keep existing behavior. The `search()`
        # exact-playlist promotion handles this case downstream.
        intent, cleaned = _detect_playlist_intent(
            voice_command="play favorite songs 2026",
            query="favorite songs 2026",
        )
        assert intent == "none"
        assert cleaned == "favorite songs 2026"


class TestEdgeCases:
    def test_trailing_punctuation(self) -> None:
        intent, cleaned = _detect_playlist_intent(
            voice_command="play my running playlist.",
            query="my running playlist",
        )
        assert intent == "strong"
        assert cleaned == "running"

    def test_just_my_playlist_collapses_to_none(self) -> None:
        # After stripping `my` and `playlist`, nothing useful remains.
        intent, cleaned = _detect_playlist_intent(
            voice_command="play my playlist",
            query="my playlist",
        )
        assert intent == "none"

    def test_play_my_at_start_requires_word_after(self) -> None:
        # `play my` alone — query is empty after cleaning. Fall through.
        intent, _cleaned = _detect_playlist_intent(
            voice_command="play my ",
            query="",
        )
        assert intent == "none"

    def test_put_on_my_x(self) -> None:
        # `put on my X` should detect intent — same lead verb as the play regex.
        intent, cleaned = _detect_playlist_intent(
            voice_command="put on my workout",
            query="my workout",
        )
        assert intent == "soft"
        assert cleaned == "workout"
