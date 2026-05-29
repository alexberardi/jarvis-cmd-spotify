"""Tests for `web_client.SpotifyClient` playlist resolution.

Covers two behaviors:

  - `find_user_playlist(allow_substring=...)` — exact/prefix always run;
    substring only when the caller signals explicit intent.
  - `search()` — multi-word exact-playlist match is promoted ahead of catalog
    so that "play favorite songs 2026" finds the user's playlist instead of
    a coincidental track. Single-word queries keep catalog-first to avoid
    a "Coffee" playlist intercepting the song "Coffee".
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from spotify_shared.web_client import SpotifyClient


@pytest.fixture(autouse=True)
def _reset_playlist_cache() -> Iterator[None]:
    """Clear the class-level cache between tests so they don't bleed into each other."""
    SpotifyClient._playlists_cache.clear()
    yield
    SpotifyClient._playlists_cache.clear()


@pytest.fixture
def client() -> SpotifyClient:
    return SpotifyClient(access_token="fake-token")


# ── find_user_playlist matching modes ─────────────────────────────────────


def test_exact_match_wins(client: SpotifyClient) -> None:
    playlists = [
        {"name": "Workout", "uri": "spotify:playlist:1", "id": "1"},
        {"name": "Workout Pump", "uri": "spotify:playlist:2", "id": "2"},
    ]
    with patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.find_user_playlist("workout")
    assert hit is not None
    assert hit.uri == "spotify:playlist:1"
    assert hit.display == "Workout"


def test_prefix_match_when_no_exact(client: SpotifyClient) -> None:
    playlists = [
        {"name": "Running Mix", "uri": "spotify:playlist:1", "id": "1"},
    ]
    with patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.find_user_playlist("running")
    assert hit is not None
    assert hit.uri == "spotify:playlist:1"


def test_substring_skipped_by_default(client: SpotifyClient) -> None:
    """Default `allow_substring=False` — interior substrings must not match."""
    playlists = [
        {"name": "Coffee Shop Vibes", "uri": "spotify:playlist:1", "id": "1"},
    ]
    with patch.object(client, "list_user_playlists", return_value=playlists):
        # "shop" is in the middle, not a prefix — must not match.
        hit = client.find_user_playlist("shop")
    assert hit is None


def test_substring_allowed_when_intent_explicit(client: SpotifyClient) -> None:
    playlists = [
        {"name": "Coffee Shop Vibes", "uri": "spotify:playlist:1", "id": "1"},
    ]
    with patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.find_user_playlist("shop", allow_substring=True)
    assert hit is not None
    assert hit.uri == "spotify:playlist:1"


def test_substring_only_when_no_exact_or_prefix(client: SpotifyClient) -> None:
    """Exact and prefix matches must beat substring matches."""
    playlists = [
        {"name": "Coffee Shop Vibes", "uri": "spotify:playlist:sub", "id": "sub"},
        {"name": "Coffee", "uri": "spotify:playlist:exact", "id": "exact"},
    ]
    with patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.find_user_playlist("coffee", allow_substring=True)
    assert hit is not None
    assert hit.uri == "spotify:playlist:exact"


# ── fuzzy matching (homophones, alt spellings, STT slips) ─────────────────


def test_fuzzy_handles_stt_homophone(client: SpotifyClient) -> None:
    """STT often turns 'Night' into 'Knight' — fuzzy must bridge that."""
    playlists = [
        {"name": "Jungle Night", "uri": "spotify:playlist:jn", "id": "jn"},
    ]
    with patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.find_user_playlist("jungle knight", allow_substring=True)
    assert hit is not None
    assert hit.uri == "spotify:playlist:jn"


def test_fuzzy_handles_z_for_s_spelling(client: SpotifyClient) -> None:
    """User says 'bangers', playlist named 'Bangerz' (or vice versa)."""
    playlists = [
        {"name": "Running Bangerz", "uri": "spotify:playlist:rb", "id": "rb"},
    ]
    with patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.find_user_playlist("running bangers", allow_substring=True)
    assert hit is not None
    assert hit.uri == "spotify:playlist:rb"


def test_fuzzy_handles_token_in_longer_name(client: SpotifyClient) -> None:
    """Fuzzy single-token match inside a multi-word playlist name."""
    playlists = [
        {"name": "Jungle Night Vibes", "uri": "spotify:playlist:jnv", "id": "jnv"},
    ]
    with patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.find_user_playlist("jungle knight", allow_substring=True)
    assert hit is not None
    assert hit.uri == "spotify:playlist:jnv"


def test_fuzzy_off_by_default(client: SpotifyClient) -> None:
    """Without explicit intent we don't fuzzy match — too loose for unprompted searches."""
    playlists = [
        {"name": "Jungle Night", "uri": "spotify:playlist:jn", "id": "jn"},
    ]
    with patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.find_user_playlist("jungle knight")
    assert hit is None


def test_fuzzy_rejects_obvious_mismatch(client: SpotifyClient) -> None:
    """Fuzzy must not return spurious matches when there's no real overlap."""
    playlists = [
        {"name": "Classical Concertos", "uri": "spotify:playlist:cc", "id": "cc"},
    ]
    with patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.find_user_playlist("jungle knight", allow_substring=True)
    assert hit is None


def test_fuzzy_picks_best_match_among_options(client: SpotifyClient) -> None:
    """When several playlists fuzzy-match, pick the closest one."""
    playlists = [
        {"name": "Jungle Mix", "uri": "spotify:playlist:jm", "id": "jm"},
        {"name": "Jungle Night Sessions", "uri": "spotify:playlist:jns", "id": "jns"},
    ]
    with patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.find_user_playlist("jungle knight", allow_substring=True)
    assert hit is not None
    # "Jungle Night Sessions" shares both query tokens (jungle + night~knight)
    # while "Jungle Mix" only shares one.
    assert hit.uri == "spotify:playlist:jns"


def test_fuzzy_respects_short_word_threshold(client: SpotifyClient) -> None:
    """Short words (≤4 chars) require exact match — otherwise 'play' would fuzzy to 'pay', etc."""
    playlists = [
        {"name": "Pay Day", "uri": "spotify:playlist:pd", "id": "pd"},
    ]
    with patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.find_user_playlist("play day", allow_substring=True)
    assert hit is None


def test_empty_query_returns_none(client: SpotifyClient) -> None:
    hit = client.find_user_playlist("")
    assert hit is None


# ── search() exact-playlist promotion ─────────────────────────────────────


def _catalog_track(name: str = "Coffee", artist: str = "Sylvan Esso") -> dict[str, Any]:
    return {
        "artists": {"items": []},
        "tracks": {"items": [{
            "uri": "spotify:track:t",
            "name": name,
            "artists": [{"name": artist}],
        }]},
        "albums": {"items": []},
    }


def _empty_catalog() -> dict[str, Any]:
    return {"artists": {"items": []}, "tracks": {"items": []}, "albums": {"items": []}}


def test_single_word_exact_playlist_does_not_block_catalog(client: SpotifyClient) -> None:
    """`play coffee` should still find the song, even if user has a Coffee playlist."""
    playlists = [{"name": "Coffee", "uri": "spotify:playlist:coffee", "id": "c"}]
    with patch.object(client, "_request", return_value=_catalog_track("Coffee", "Sylvan Esso")), \
         patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.search("coffee")
    assert hit is not None
    assert hit.kind == "track"
    assert hit.uri == "spotify:track:t"


def test_multi_word_exact_playlist_promoted_over_catalog(client: SpotifyClient) -> None:
    """`play favorite songs 2026` should prefer the exact-match playlist."""
    playlists = [
        {"name": "Favorite Songs 2026", "uri": "spotify:playlist:fav", "id": "fav"},
    ]
    with patch.object(client, "_request", return_value=_catalog_track("Favorite Songs")), \
         patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.search("favorite songs 2026")
    assert hit is not None
    assert hit.kind == "playlist"
    assert hit.uri == "spotify:playlist:fav"


def test_multi_word_no_exact_playlist_falls_to_catalog(client: SpotifyClient) -> None:
    playlists = [
        {"name": "Something Else", "uri": "spotify:playlist:x", "id": "x"},
    ]
    with patch.object(client, "_request", return_value=_catalog_track("Bohemian Rhapsody", "Queen")), \
         patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.search("bohemian rhapsody")
    assert hit is not None
    assert hit.kind == "track"


def test_search_promotes_multi_word_prefix_playlist_over_catalog(client: SpotifyClient) -> None:
    """Multi-word query with playlist prefix match beats catalog artist."""
    playlists = [
        {"name": "Feel Good Acoustic Vibes", "uri": "spotify:playlist:fga", "id": "fga"},
    ]
    catalog = {
        "artists": {"items": [{"uri": "spotify:artist:a", "name": "Acoustic Heartstrings"}]},
        "tracks": {"items": []},
        "albums": {"items": []},
    }
    with patch.object(client, "_request", return_value=catalog), \
         patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.search("feel good acoustic")
    assert hit is not None
    assert hit.kind == "playlist"
    assert hit.uri == "spotify:playlist:fga"


def test_search_promotes_multi_word_fuzzy_playlist_over_catalog(client: SpotifyClient) -> None:
    """Multi-word fuzzy playlist match (STT slip) beats catalog artist."""
    playlists = [
        {"name": "Jungle Night", "uri": "spotify:playlist:jn", "id": "jn"},
    ]
    catalog = {
        "artists": {"items": [{"uri": "spotify:artist:a", "name": "Knight Riders"}]},
        "tracks": {"items": []},
        "albums": {"items": []},
    }
    with patch.object(client, "_request", return_value=catalog), \
         patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.search("jungle knight")
    assert hit is not None
    assert hit.kind == "playlist"
    assert hit.uri == "spotify:playlist:jn"


def test_search_single_word_still_catalog_first(client: SpotifyClient) -> None:
    """Promotion is multi-word only — 'play coffee' still finds the song."""
    playlists = [
        {"name": "Coffee Shop Vibes", "uri": "spotify:playlist:c", "id": "c"},
    ]
    with patch.object(client, "_request", return_value=_catalog_track("Coffee", "Sylvan Esso")), \
         patch.object(client, "list_user_playlists", return_value=playlists):
        hit = client.search("coffee")
    assert hit is not None
    assert hit.kind == "track"


def test_no_catalog_no_playlist_returns_none(client: SpotifyClient) -> None:
    with patch.object(client, "_request", return_value=_empty_catalog()), \
         patch.object(client, "list_user_playlists", return_value=[]):
        hit = client.search("never gonna match xyz")
    assert hit is None


# ── playlist cache ────────────────────────────────────────────────────────


def test_playlist_cache_reuses_within_ttl(client: SpotifyClient) -> None:
    playlists = [{"name": "Workout", "uri": "spotify:playlist:1", "id": "1"}]
    with patch.object(client, "list_user_playlists", return_value=playlists) as fetch:
        client.find_user_playlist("workout")
        client.find_user_playlist("workout")
    assert fetch.call_count == 1
