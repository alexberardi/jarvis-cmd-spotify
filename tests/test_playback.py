"""The IJarvisMusicCommand side: what the node's display, the app and the built-in transport
commands see. Every state is read back from go-librespot's /status."""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from commands.spotify.command import SpotifyCommand
from spotify_shared.local_client import LocalAPIUnavailable, LocalStatus

PLAYING = LocalStatus(
    stopped=False, paused=False, buffering=False, volume=40, volume_steps=100,
    repeat_track=False, repeat_context=True, shuffle_context=False,
    track_uri="spotify:track:1", track_name="Song", track_artists=["A", "B"],
    track_album="Album", position_ms=61500, duration_ms=200000,
    album_cover_url="https://i.scdn.co/image/ab67",
)


@pytest.fixture
def cmd() -> SpotifyCommand:
    with patch("commands.spotify.command.JarvisStorage"):
        return SpotifyCommand()


@pytest.fixture
def daemon():
    """The go-librespot API every LocalClient talks to."""
    api = MagicMock()
    api.status.return_value = PLAYING
    with patch("spotify_shared.local_client.LocalClient", return_value=api):
        yield api


def test_status_reads_the_daemon(cmd: SpotifyCommand, daemon: MagicMock) -> None:
    st = cmd.playback_status()
    assert st.to_dict() == {
        "state": "playing", "media_title": "Song", "media_artist": "A, B", "media_album": "Album",
        "context": "", "actions": ["play", "pause", "next", "previous", "seek", "volume", "shuffle", "repeat"],
        "position": 61.5, "duration": 200.0, "volume": 40, "shuffle": False, "repeat": "all",
    }
    daemon.status.return_value = replace(PLAYING, paused=True, volume=32, volume_steps=64, repeat_track=True)
    st = cmd.playback_status()
    assert (st.state, st.volume, st.repeat) == ("paused", 50, "one")


@pytest.mark.parametrize("status", [replace(PLAYING, stopped=True), replace(PLAYING, track_uri="")])
def test_nothing_playing(cmd: SpotifyCommand, daemon: MagicMock, status: LocalStatus) -> None:
    daemon.status.return_value = status
    assert cmd.playback_status() is None


def test_daemon_down_is_nothing_playing(cmd: SpotifyCommand, daemon: MagicMock) -> None:
    daemon.status.side_effect = LocalAPIUnavailable("refused")
    assert cmd.playback_status() is None


def test_controls(cmd: SpotifyCommand, daemon: MagicMock) -> None:
    cmd._make_local_client = MagicMock(return_value=daemon)  # type: ignore[method-assign]
    cmd.playback_control("seek", position=12.25)
    daemon.seek.assert_called_once_with(12250)
    cmd.playback_control("volume", volume=70)
    daemon.set_volume.assert_called_once_with(70)
    cmd.playback_control("shuffle")
    daemon.set_shuffle.assert_called_once_with(True)
    assert cmd.playback_control("play").state == "playing"
    daemon.resume.assert_called_once()
    with pytest.raises(ValueError):
        cmd.playback_control("like")


@pytest.mark.parametrize(("track", "context", "sent"), [
    (False, False, "context"),  # off → all
    (False, True, "track"),     # all → one
    (True, False, "off"),       # one → off
])
def test_repeat_cycles(cmd: SpotifyCommand, daemon: MagicMock, track: bool, context: bool, sent: str) -> None:
    daemon.status.return_value = replace(PLAYING, repeat_track=track, repeat_context=context)
    cmd._make_local_client = MagicMock(return_value=daemon)  # type: ignore[method-assign]
    cmd.playback_control("repeat")
    daemon.set_repeat.assert_called_once_with(sent)


def test_artwork_is_fetched_once_per_cover(cmd: SpotifyCommand, daemon: MagicMock) -> None:
    resp = MagicMock(content=b"\xff\xd8\xffjpeg", headers={"content-type": "image/jpeg"})
    with patch("httpx.get", return_value=resp) as get:
        art = cmd.playback_artwork()
        assert (art.data, art.mime) == (b"\xff\xd8\xffjpeg", "image/jpeg")
        assert cmd.playback_artwork() is art
        get.assert_called_once_with("https://i.scdn.co/image/ab67", timeout=5.0)


@pytest.mark.parametrize("url", ["", "http://i.scdn.co/image/x", "https://evil.example/i.scdn.co", "https://scdn.co.evil.example/x"])
def test_artwork_only_from_spotifys_cdn(cmd: SpotifyCommand, daemon: MagicMock, url: str) -> None:
    daemon.status.return_value = replace(PLAYING, album_cover_url=url)
    with patch("httpx.get") as get:
        assert cmd.playback_artwork() is None
        get.assert_not_called()
