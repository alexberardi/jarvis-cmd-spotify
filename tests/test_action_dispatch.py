"""Action-dispatch defense.

LLMs reach for the literal English verb "stop" for "stop the music" /
"stop spotify" — even though the system prompt tells them to use "pause" —
especially when the phrase bypasses the exact-phrase node pre-router (e.g. the
mobile-chat path, where the LLM picks the action in command-center).

Two things must be true for that to work, and BOTH have bitten prod:
1. The runtime must route action="stop" to the pause handler (handler_map
   alias). Missing this looped to max tool iterations in prod (2026-06-05).
2. "stop" must be in the advertised action enum. command-center validates
   tool args against the enum and rejects action="stop" as an invalid
   parameter BEFORE the call reaches the node, so the alias alone is not
   enough. Missing this caused the 2026-06-18 prod regression: "stop spotify"
   → invalid-param guard → the model fell back to control_device and turned
   off the kettle, or looped to "Maximum tool execution iterations reached."

These tests pin both so the regression can't sneak back in.
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


def test_stop_is_in_action_enum(cmd: SpotifyCommand) -> None:
    """command-center validates tool args against this enum before dispatching
    to the node, so "stop" must be advertised or the call is rejected as an
    invalid parameter (the 2026-06-18 prod regression). The runtime alias in
    test_stop_routes_to_pause_handler is not reachable without this."""
    action_param = next(p for p in cmd.parameters if p.name == "action")
    assert "stop" in action_param.enum_values


def test_unknown_action_still_errors(cmd: SpotifyCommand) -> None:
    cmd._access_token = MagicMock(return_value="token")  # type: ignore[method-assign]
    cmd._refresh_token = MagicMock(return_value="refresh")  # type: ignore[method-assign]

    result = cmd.run(MagicMock(), action="dance")

    assert result.context_data.get("error") == "unknown_action"
