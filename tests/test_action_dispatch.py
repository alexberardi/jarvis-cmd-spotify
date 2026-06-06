"""Action-dispatch defense.

The action enum doesn't expose "stop" (the system prompt tells the LLM to use
"pause" instead), but LLMs reach for the literal English verb anyway —
especially when "stop" is bundled with another command and bypasses the
exact-phrase pre-router. Without this alias the bundled-stop path looped to
max tool iterations in prod (2026-06-05).

This test pins the alias so the regression can't sneak back in.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from commands.spotify.command import SpotifyCommand


@pytest.fixture
def cmd() -> SpotifyCommand:
    with patch("commands.spotify.command.JarvisStorage"):
        return SpotifyCommand()


def test_stop_routes_to_pause_handler(cmd: SpotifyCommand) -> None:
    cmd._access_token = MagicMock(return_value="token")  # type: ignore[method-assign]
    cmd._refresh_token = MagicMock(return_value="refresh")  # type: ignore[method-assign]
    cmd._handle_pause = MagicMock(return_value="PAUSED")  # type: ignore[method-assign]

    result = cmd.run(MagicMock(), action="stop")

    cmd._handle_pause.assert_called_once()
    assert result == "PAUSED"


def test_pause_still_routes_to_pause_handler(cmd: SpotifyCommand) -> None:
    cmd._access_token = MagicMock(return_value="token")  # type: ignore[method-assign]
    cmd._refresh_token = MagicMock(return_value="refresh")  # type: ignore[method-assign]
    cmd._handle_pause = MagicMock(return_value="PAUSED")  # type: ignore[method-assign]

    result = cmd.run(MagicMock(), action="pause")

    cmd._handle_pause.assert_called_once()
    assert result == "PAUSED"


def test_unknown_action_still_errors(cmd: SpotifyCommand) -> None:
    cmd._access_token = MagicMock(return_value="token")  # type: ignore[method-assign]
    cmd._refresh_token = MagicMock(return_value="refresh")  # type: ignore[method-assign]

    result = cmd.run(MagicMock(), action="dance")

    assert result.context_data.get("error") == "unknown_action"
