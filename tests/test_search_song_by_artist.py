"""Tests for "{song} by {artist}" resolution in `web_client`.

The general catalog search ranks ARTIST hits first, so "play Bohemian
Rhapsody by Queen" used to play Queen's top track instead of the song.
`search()` now detects the "by" qualifier and does a track-scoped lookup
first (falling through to normal resolution on a miss).
"""
from __future__ import annotations

from typing import Any

from unittest.mock import patch

import pytest

from spotify_shared.web_client import SpotifyClient, _split_song_by_artist


@pytest.fixture(autouse=True)
def _reset_playlist_cache() -> Any:
    SpotifyClient._playlists_cache.clear()
    yield
    SpotifyClient._playlists_cache.clear()


@pytest.fixture
def client() -> SpotifyClient:
    return SpotifyClient(access_token="fake-token")


# ── _split_song_by_artist ─────────────────────────────────────────────────


def test_split_simple() -> None:
    assert _split_song_by_artist("Bohemian Rhapsody by Queen") == (
        "Bohemian Rhapsody", "Queen",
    )


def test_split_uses_last_by_for_titles_containing_by() -> None:
    # Splitting on the FIRST " by " would mangle "Stand By Me".
    assert _split_song_by_artist("Stand By Me by Ben E. King") == (
        "Stand by Me", "Ben E. King",
    )


def test_split_leading_by_in_title_is_protected() -> None:
    assert _split_song_by_artist("By the Way by Red Hot Chili Peppers") == (
        "By the Way", "Red Hot Chili Peppers",
    )


def test_split_no_by_returns_empty() -> None:
    assert _split_song_by_artist("Bohemian Rhapsody") == ("", "")


# ── search() routing ──────────────────────────────────────────────────────


def _by_artist_request(method: str, path: str, *, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    """Mock: track-scoped query returns the specific song; the general
    catalog query would return the ARTIST first (the old wrong behavior)."""
    q: str = (params or {}).get("q", "")
    if "track:" in q and "artist:" in q:
        return {"tracks": {"items": [{
            "uri": "spotify:track:bohemian",
            "name": "Bohemian Rhapsody",
            "artists": [{"name": "Queen"}],
        }]}}
    return {
        "artists": {"items": [{"uri": "spotify:artist:queen", "name": "Queen"}]},
        "tracks": {"items": []},
        "albums": {"items": []},
    }


def test_song_by_artist_returns_specific_track_not_artist(client: SpotifyClient) -> None:
    with patch.object(client, "_request", side_effect=_by_artist_request), \
         patch.object(client, "list_user_playlists", return_value=[]):
        hit = client.search("bohemian rhapsody by queen")
    assert hit is not None
    assert hit.kind == "track"
    assert hit.uri == "spotify:track:bohemian"
    assert hit.display == "Bohemian Rhapsody by Queen"


def test_song_by_artist_falls_back_to_catalog_when_no_track(client: SpotifyClient) -> None:
    """If the targeted track search finds nothing, normal resolution runs."""
    def side(method: str, path: str, *, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        q: str = (params or {}).get("q", "")
        if "track:" in q:
            return {"tracks": {"items": []}}
        return {
            "artists": {"items": [{"uri": "spotify:artist:y", "name": "Y"}]},
            "tracks": {"items": []},
            "albums": {"items": []},
        }

    with patch.object(client, "_request", side_effect=side), \
         patch.object(client, "list_user_playlists", return_value=[]):
        hit = client.search("obscure track by y")
    assert hit is not None
    assert hit.kind == "artist"  # fell through to the general catalog


def test_plain_artist_query_unaffected(client: SpotifyClient) -> None:
    """No " by " → no track-scoped lookup; artist-first behavior preserved."""
    catalog = {
        "artists": {"items": [{"uri": "spotify:artist:queen", "name": "Queen"}]},
        "tracks": {"items": []},
        "albums": {"items": []},
    }
    with patch.object(client, "_request", return_value=catalog) as req, \
         patch.object(client, "list_user_playlists", return_value=[]):
        hit = client.search("queen")
    assert hit is not None
    assert hit.kind == "artist"
    # Only the general search ran — no extra track-scoped request.
    assert req.call_count == 1
