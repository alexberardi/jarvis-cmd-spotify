"""Spotify voice command — local playback via go-librespot's HTTP API.

Architecture:

  * **Search / metadata** → Spotify Web API (``web_client.py``)
  * **Play / pause / next / prev / volume / shuffle / repeat / status** →
    go-librespot's localhost HTTP API (``local_client.py``)

Spotify's Web API used to be the control plane for everything; it 5xx'd
constantly when the target device was our own librespot, and every action
needed a workaround (transfer_playback, device_id juggling, sink-input
polling, SIGTERM-as-pause). Routing playback through the local daemon
removes all of that — the daemon never hits Spotify's gateway to start or
stop a track.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from time import time as _now
from typing import Any

# Belt + suspenders: ensure our lib dir is on sys.path so the lazy imports
# of spotify_shared.* always resolve, even if discovery cache hasn't been
# refreshed since install. Path matches the Pantry install convention of
# ~/.jarvis/packages/<name>/<name>_lib/.
_LIB_DIR: str = str(Path.home() / ".jarvis" / "packages" / "spotify" / "spotify_lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:  # type: ignore[no-redef]
        def __init__(self, **kw: str) -> None:
            self._log = logging.getLogger(kw.get("service", __name__))

        def info(self, msg: str, **kw: object) -> None:
            self._log.info(msg)

        def warning(self, msg: str, **kw: object) -> None:
            self._log.warning(msg)

        def error(self, msg: str, **kw: object) -> None:
            self._log.error(msg)

        def debug(self, msg: str, **kw: object) -> None:
            self._log.debug(msg)


from jarvis_command_sdk import (
    AuthenticationConfig,
    CommandExample,
    CommandResponse,
    IJarvisCommand,
    IJarvisSecret,
    JarvisPackage,
    JarvisParameter,
    JarvisSecret,
    JarvisStorage,
    PreRouteResult,
    RequestInformation,
)


logger = JarvisLogger(service="cmd.spotify")


_REPEAT_MAP: dict[str, str] = {
    "off": "off",
    "no": "off",
    "none": "off",
    "track": "track",
    "song": "track",
    "one": "track",
    "current": "track",
    "all": "context",
    "queue": "context",
    "playlist": "context",
    "context": "context",
    "on": "context",
}


# How long to wait for go-librespot to report ``playback_ready`` after start.
# In the steady state the daemon is already running and ready; this matters
# only on a cold-start or a daemon restart after BT routing changed.
_READY_TIMEOUT_SECONDS: float = 12.0
_READY_POLL_SECONDS: float = 0.5


def _spotify_is_active() -> bool:
    """True if go-librespot is producing audio (uncorked PulseAudio sink-input).

    Ambiguous voice phrases like "stop"/"pause"/"skip" should only route to
    Spotify when Spotify is the audible player — otherwise they'd hijack
    commands meant for a different music service (Pandora, etc.). The daemon
    stays running as a Connect advertiser even when idle, so "process exists"
    is useless; instead, ask PulseAudio whether it's actively pulling samples
    from a ``go-librespot`` process.
    """
    import json as _json
    import subprocess

    try:
        result = subprocess.run(
            ["pactl", "-f", "json", "list", "sink-inputs"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    if result.returncode != 0:
        return False
    try:
        items = _json.loads(result.stdout or "[]")
    except (ValueError, TypeError):
        return False
    for item in items:
        props = item.get("properties") or {}
        if props.get("application.process.binary") == "go-librespot":
            if item.get("corked"):
                continue
            return True
    return False


class SpotifyCommand(IJarvisCommand):
    """Stream Spotify on this node — search, play, pause, skip, volume, shuffle, repeat."""

    def __init__(self) -> None:
        self._storage = JarvisStorage("spotify")

    # -- Metadata ------------------------------------------------------------

    @property
    def command_name(self) -> str:
        return "spotify"

    @property
    def description(self) -> str:
        # Kept short and parallel to Pandora's description. A longer
        # description (the prior version mentioned Premium + listed every
        # feature) destabilized the model's tool-call output formatting
        # for this command — the LLM emitted the call as XML text inside
        # the message field instead of populating the tool_calls array.
        return (
            "Play music on Spotify. Play tracks, artists, albums, or "
            "playlists; pause, skip, control volume, shuffle, and repeat."
        )

    @property
    def keywords(self) -> list[str]:
        return [
            "spotify", "play", "music", "song", "artist", "album",
            "playlist", "pause", "skip", "next", "previous",
            "volume", "shuffle", "repeat", "now playing",
        ]

    @property
    def associated_service(self) -> str:
        return "Spotify"

    # -- Parameters ----------------------------------------------------------

    @property
    def parameters(self) -> list[JarvisParameter]:
        return [
            JarvisParameter(
                "action", "string", required=True,
                enum_values=[
                    "play",
                    "pause",
                    "skip",
                    "previous",
                    "volume",
                    "shuffle",
                    "repeat",
                    "now_playing",
                ],
                description="The Spotify control action to perform",
            ),
            JarvisParameter(
                "query", "string", required=False,
                description=(
                    "What to play — track, artist, album, or playlist name. "
                    "Only used with action='play'. If omitted, resumes whatever "
                    "was last playing."
                ),
            ),
            JarvisParameter(
                "level", "int", required=False,
                description=(
                    "Volume level 0-100 (only used with action='volume')."
                ),
            ),
            JarvisParameter(
                "state", "string", required=False,
                enum_values=["on", "off", "track", "all"],
                description=(
                    "Toggle state for shuffle ('on'/'off') or repeat "
                    "('off'/'track'/'all')."
                ),
            ),
        ]

    # -- Secrets -------------------------------------------------------------

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        return [
            JarvisSecret(
                "SPOTIFY_CLIENT_ID",
                "Spotify Developer App Client ID — create one at developer.spotify.com",
                "integration", "string",
                is_sensitive=False, required=True,
                friendly_name="Spotify Client ID",
            ),
            JarvisSecret(
                "SPOTIFY_ACCESS_TOKEN",
                "OAuth access token (managed automatically)",
                "integration", "string",
                is_sensitive=True, required=False,
                friendly_name="Access Token",
            ),
            JarvisSecret(
                "SPOTIFY_REFRESH_TOKEN",
                "OAuth refresh token (managed automatically)",
                "integration", "string",
                is_sensitive=True, required=False,
                friendly_name="Refresh Token",
            ),
            JarvisSecret(
                "SPOTIFY_TOKEN_EXPIRES_AT",
                "Epoch seconds when SPOTIFY_ACCESS_TOKEN expires (managed automatically)",
                "integration", "string",
                is_sensitive=False, required=False,
                friendly_name="Token Expires At",
            ),
            JarvisSecret(
                "SPOTIFY_DEVICE_NAME",
                "Name shown for this node in your Spotify app's Devices list (default: Jarvis)",
                "node", "string",
                is_sensitive=False, required=False,
                friendly_name="Device Name",
            ),
            JarvisSecret(
                "SPOTIFY_USER_ID",
                "Spotify user ID (fetched automatically after first auth)",
                "integration", "string",
                is_sensitive=False, required=False,
                friendly_name="Spotify User ID",
            ),
        ]

    @property
    def required_packages(self) -> list[JarvisPackage]:
        return [JarvisPackage("httpx")]

    # -- Auth ----------------------------------------------------------------

    @property
    def authentication(self) -> AuthenticationConfig | None:
        client_id: str | None = self._storage.get_secret("SPOTIFY_CLIENT_ID", scope="integration")
        if not client_id:
            return None
        return AuthenticationConfig(
            type="oauth",
            provider="spotify",
            friendly_name="Spotify",
            client_id=client_id,
            keys=["access_token", "refresh_token"],
            authorize_url="https://accounts.spotify.com/authorize",
            exchange_url="https://accounts.spotify.com/api/token",
            scopes=[
                "user-read-playback-state",
                "user-modify-playback-state",
                "user-read-currently-playing",
                "streaming",
                "user-library-read",
                "playlist-read-private",
            ],
            supports_pkce=True,
            requires_background_refresh=True,
            refresh_token_secret_key="SPOTIFY_REFRESH_TOKEN",
        )

    def store_auth_values(self, values: dict[str, str]) -> None:
        if "access_token" in values:
            self._storage.set_secret(
                "SPOTIFY_ACCESS_TOKEN", values["access_token"],
                scope="integration",
            )
        if "refresh_token" in values:
            self._storage.set_secret(
                "SPOTIFY_REFRESH_TOKEN", values["refresh_token"],
                scope="integration",
            )
        if "expires_in" in values:
            try:
                expires_at: int = int(_now()) + int(values["expires_in"])
                self._storage.set_secret(
                    "SPOTIFY_TOKEN_EXPIRES_AT", str(expires_at),
                    scope="integration",
                )
            except (TypeError, ValueError):
                logger.warning(
                    "Spotify auth payload had non-numeric expires_in",
                    expires_in=values.get("expires_in"),
                )

    # -- Rules ---------------------------------------------------------------

    @property
    def rules(self) -> list[str]:
        return [
            "If user says 'play [X] on Spotify' or 'play [X]' (after Spotify context), use action='play' with query='[X]'.",
            "If user says 'play Spotify' with no specific content, use action='play' with no query (resumes last playback).",
            "If user says 'pause', 'stop', or 'stop the music', use action='pause'.",
            "If user says 'next', 'next song', or 'skip', use action='skip'.",
            "If user says 'previous', 'go back', or 'last song', use action='previous'.",
            "If user says 'volume to N' or 'set volume N', use action='volume' with level=N.",
            "If user says 'shuffle on/off', use action='shuffle' with state='on' or 'off'.",
            "If user says 'repeat track' or 'repeat this song', use action='repeat' with state='track'.",
            "If user says 'repeat all' or 'repeat playlist', use action='repeat' with state='all'.",
            "If user says 'turn off repeat', use action='repeat' with state='off'.",
            "If user says 'what's playing', use action='now_playing'.",
        ]

    # -- Pre-routing (deterministic phrases bypass the LLM) ------------------

    def pre_route(self, voice_command: str) -> PreRouteResult | None:
        text: str = voice_command.lower().strip().rstrip(".!?")

        # Explicit phrases — always claim them, even when Spotify isn't
        # the active player. "stop spotify" should stop Spotify no matter
        # what else is playing.
        explicit_map: dict[str, dict[str, Any]] = {
            "pause spotify": {"action": "pause"},
            "stop spotify": {"action": "pause"},
            "play spotify": {"action": "play"},
            "resume spotify": {"action": "play"},
        }
        if text in explicit_map:
            return PreRouteResult(arguments=explicit_map[text])

        # Ambiguous phrases — "stop", "pause", "skip", "next" could mean
        # Spotify OR Pandora (or another music service). Only claim them
        # when Spotify is currently producing audio.
        ambiguous_map: dict[str, dict[str, Any]] = {
            "pause": {"action": "pause"},
            "pause music": {"action": "pause"},
            "stop": {"action": "pause"},
            "stop music": {"action": "pause"},
            "stop the music": {"action": "pause"},
            "skip": {"action": "skip"},
            "skip song": {"action": "skip"},
            "next": {"action": "skip"},
            "next song": {"action": "skip"},
            "next track": {"action": "skip"},
            "previous": {"action": "previous"},
            "previous song": {"action": "previous"},
            "previous track": {"action": "previous"},
            "go back": {"action": "previous"},
            "last song": {"action": "previous"},
            "shuffle on": {"action": "shuffle", "state": "on"},
            "shuffle off": {"action": "shuffle", "state": "off"},
            "turn on shuffle": {"action": "shuffle", "state": "on"},
            "turn off shuffle": {"action": "shuffle", "state": "off"},
            "repeat off": {"action": "repeat", "state": "off"},
            "repeat track": {"action": "repeat", "state": "track"},
            "repeat song": {"action": "repeat", "state": "track"},
            "repeat all": {"action": "repeat", "state": "all"},
            "repeat playlist": {"action": "repeat", "state": "all"},
            "turn off repeat": {"action": "repeat", "state": "off"},
            "what's playing": {"action": "now_playing"},
            "what is playing": {"action": "now_playing"},
            "now playing": {"action": "now_playing"},
            "resume": {"action": "play"},
        }
        if text in ambiguous_map:
            if _spotify_is_active():
                return PreRouteResult(arguments=ambiguous_map[text])
            return None

        # "volume N" / "set volume to N" / "spotify volume N"
        m = re.match(
            r"^(?:set\s+)?(?:spotify\s+)?volume(?:\s+to)?\s+(\d{1,3})$",
            text,
        )
        if m:
            try:
                level: int = int(m.group(1))
            except ValueError:
                return None
            if 0 <= level <= 100:
                return PreRouteResult(arguments={"action": "volume", "level": level})
            return None

        # If the user explicitly named a different music service, don't
        # pre-route — let that service's pre_route (or the LLM) handle it.
        if re.search(r"\bon\s+(pandora|apple\s+music|youtube|amazon\s+music|tidal|soundcloud)\b", text):
            return None

        # "play X on spotify" / "play X" -> play with query
        m = re.match(
            r"^(?:play|put on|listen to|start)\s+"
            r"(.+?)"
            r"(?:\s+on\s+spotify)?$",
            text,
        )
        if m:
            query: str = m.group(1).strip()
            if query in ("", "spotify", "music", "something", "some music"):
                return PreRouteResult(arguments={"action": "play"})
            return PreRouteResult(arguments={"action": "play", "query": query})

        return None

    def post_process_tool_call(self, args: dict[str, Any], voice_command: str) -> dict[str, Any]:
        if args.get("action") == "play" and not args.get("query"):
            stripped: str = re.sub(
                r"^(?:play|put on|listen to|start)\s+",
                "",
                voice_command,
                flags=re.IGNORECASE,
            ).strip().rstrip(".!?")
            stripped = re.sub(r"\s+on\s+spotify$", "", stripped, flags=re.IGNORECASE)
            if stripped and stripped.lower() not in ("spotify", "music", "something", ""):
                args["query"] = stripped
        return args

    # -- Examples ------------------------------------------------------------

    def generate_prompt_examples(self) -> list[CommandExample]:
        return [
            CommandExample(
                voice_command="Play Radiohead on Spotify",
                expected_parameters={"action": "play", "query": "Radiohead"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="Play my Discover Weekly playlist",
                expected_parameters={"action": "play", "query": "Discover Weekly"},
            ),
            CommandExample(
                voice_command="Pause Spotify",
                expected_parameters={"action": "pause"},
            ),
            CommandExample(
                voice_command="Skip this song",
                expected_parameters={"action": "skip"},
            ),
            CommandExample(
                voice_command="Set Spotify volume to 50",
                expected_parameters={"action": "volume", "level": 50},
            ),
            CommandExample(
                voice_command="Turn on shuffle",
                expected_parameters={"action": "shuffle", "state": "on"},
            ),
            CommandExample(
                voice_command="Repeat this song",
                expected_parameters={"action": "repeat", "state": "track"},
            ),
        ]

    def generate_adapter_examples(self) -> list[CommandExample]:
        items: list[tuple[str, dict[str, Any]]] = [
            ("Play Radiohead on Spotify", {"action": "play", "query": "Radiohead"}),
            ("Play some Taylor Swift", {"action": "play", "query": "Taylor Swift"}),
            ("Play the Beatles", {"action": "play", "query": "the Beatles"}),
            ("Put on some jazz", {"action": "play", "query": "jazz"}),
            ("Listen to Daft Punk", {"action": "play", "query": "Daft Punk"}),
            ("Play my Discover Weekly", {"action": "play", "query": "Discover Weekly"}),
            ("Play the Today's Top Hits playlist", {"action": "play", "query": "Today's Top Hits"}),
            ("Play my chill playlist", {"action": "play", "query": "chill"}),
            ("Play Bohemian Rhapsody", {"action": "play", "query": "Bohemian Rhapsody"}),
            ("Play the Dark Side of the Moon album", {"action": "play", "query": "Dark Side of the Moon"}),
            ("Play Spotify", {"action": "play"}),
            ("Resume Spotify", {"action": "play"}),
            ("Pause", {"action": "pause"}),
            ("Pause Spotify", {"action": "pause"}),
            ("Stop the music", {"action": "pause"}),
            ("Skip", {"action": "skip"}),
            ("Skip this song", {"action": "skip"}),
            ("Next song", {"action": "skip"}),
            ("Next track", {"action": "skip"}),
            ("Previous song", {"action": "previous"}),
            ("Go back", {"action": "previous"}),
            ("Last song", {"action": "previous"}),
            ("Set volume to 60", {"action": "volume", "level": 60}),
            ("Spotify volume 30", {"action": "volume", "level": 30}),
            ("Turn the music down to 20", {"action": "volume", "level": 20}),
            ("Volume 80", {"action": "volume", "level": 80}),
            ("Turn on shuffle", {"action": "shuffle", "state": "on"}),
            ("Shuffle off", {"action": "shuffle", "state": "off"}),
            ("Stop shuffling", {"action": "shuffle", "state": "off"}),
            ("Repeat this song", {"action": "repeat", "state": "track"}),
            ("Repeat the playlist", {"action": "repeat", "state": "all"}),
            ("Turn off repeat", {"action": "repeat", "state": "off"}),
            ("What's playing", {"action": "now_playing"}),
            ("What song is this", {"action": "now_playing"}),
            ("Now playing", {"action": "now_playing"}),
        ]
        return [
            CommandExample(
                voice_command=vc,
                expected_parameters=params,
                is_primary=(i == 0),
            )
            for i, (vc, params) in enumerate(items)
        ]

    # -- Helpers -------------------------------------------------------------

    def _device_name(self) -> str:
        return self._storage.get_secret("SPOTIFY_DEVICE_NAME", scope="node") or "Jarvis"

    def _client_id(self) -> str | None:
        return self._storage.get_secret("SPOTIFY_CLIENT_ID", scope="integration")

    def _access_token(self) -> str | None:
        return self._storage.get_secret("SPOTIFY_ACCESS_TOKEN", scope="integration")

    def _refresh_token(self) -> str | None:
        return self._storage.get_secret("SPOTIFY_REFRESH_TOKEN", scope="integration")

    def _refresh_and_persist_tokens(self) -> str | None:
        """Use the refresh_token to get a fresh access_token. Returns the new
        access_token on success, or None if refresh failed."""
        from spotify_shared.auth import refresh_access_token

        client_id: str | None = self._client_id()
        refresh: str | None = self._refresh_token()
        if not client_id or not refresh:
            return None
        data: dict[str, Any] | None = refresh_access_token(
            client_id=client_id, refresh_token=refresh,
        )
        if not data:
            return None
        new_access: str | None = data.get("access_token")
        if not new_access:
            return None
        self._storage.set_secret("SPOTIFY_ACCESS_TOKEN", new_access, scope="integration")
        new_refresh: str | None = data.get("refresh_token")
        if new_refresh and new_refresh != refresh:
            self._storage.set_secret("SPOTIFY_REFRESH_TOKEN", new_refresh, scope="integration")
        expires_in: Any = data.get("expires_in")
        if isinstance(expires_in, (int, float)):
            expires_at: int = int(_now()) + int(expires_in)
            self._storage.set_secret(
                "SPOTIFY_TOKEN_EXPIRES_AT", str(expires_at),
                scope="integration",
            )
        return new_access

    def _spotify_user_id(self) -> str | None:
        """Return the Spotify user ID, fetching from /me on first call."""
        cached: str | None = self._storage.get_secret("SPOTIFY_USER_ID", scope="integration")
        if cached:
            return cached
        result, err = self._call_with_refresh(lambda c: c.me())
        if err or not result:
            return None
        user_id: str = result.get("id", "")
        if not user_id:
            return None
        self._storage.set_secret(
            "SPOTIFY_USER_ID", user_id,
            scope="integration", value_type="string",
        )
        return user_id

    def _ensure_daemon_running(self) -> None:
        """Start go-librespot. First call downloads the binary; subsequent
        calls are idempotent. Pairing happens via Zeroconf — the user
        selects this node in their phone's Spotify app once."""
        from spotify_shared import go_librespot_manager
        go_librespot_manager.start(device_name=self._device_name())

    def _make_local_client(self) -> Any:
        """Build a LocalClient pointing at the daemon's HTTP API.

        Also unpauses the daemon — wake-word ducking SIGSTOPs go-librespot
        during voice capture, and the matching SIGCONT only runs after the
        command returns. Without an explicit SIGCONT here, the HTTP request
        would hit a frozen process and time out.
        """
        from spotify_shared import go_librespot_manager
        from spotify_shared.local_client import LocalClient
        go_librespot_manager.ensure_running_unpaused()
        return LocalClient(base_url=go_librespot_manager.api_url())

    def _wait_for_ready(self, local_client: Any) -> bool:
        """Poll the daemon until ``playback_ready: true`` or the deadline.

        ``playback_ready`` flips true once the user has paired the node from
        their phone's Spotify app and go-librespot has established a session.
        On every subsequent call after first pairing, this returns true on
        the first poll because credentials are cached.
        """
        deadline: float = time.monotonic() + _READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if local_client.is_ready():
                return True
            time.sleep(_READY_POLL_SECONDS)
        return False

    def _make_client(self) -> Any | None:
        """Build a SpotifyClient (Web API, for search/metadata) with a valid token."""
        from spotify_shared.web_client import SpotifyClient

        token: str | None = self._access_token()
        if not token:
            token = self._refresh_and_persist_tokens()
            if not token:
                return None
        return SpotifyClient(access_token=token)

    def _call_with_refresh(self, fn: Any) -> tuple[Any, str | None]:
        """Run a closure against the Web API, refreshing the token once on 401."""
        from spotify_shared.web_client import SpotifyAuthError, SpotifyAPIError, SpotifyClient

        client = self._make_client()
        if client is None:
            return None, "not_authenticated"

        try:
            return fn(client), None
        except SpotifyAuthError:
            new_token: str | None = self._refresh_and_persist_tokens()
            if not new_token:
                return None, "auth_refresh_failed"
            client = SpotifyClient(access_token=new_token)
            try:
                return fn(client), None
            except SpotifyAuthError:
                return None, "auth_failed"
            except SpotifyAPIError as e:
                return None, f"api_error: {e}"
        except SpotifyAPIError as e:
            return None, f"api_error: {e}"

    # -- Execute -------------------------------------------------------------

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        action: str | None = kwargs.get("action")
        if not action:
            return CommandResponse.error_response(
                error_details="What would you like to do with Spotify?",
                context_data={"error": "missing_action"},
            )

        if not self._access_token() and not self._refresh_token():
            return CommandResponse.error_response(
                error_details=(
                    "Spotify isn't authenticated yet. Open Jarvis settings and "
                    "tap 'Authenticate with Spotify'."
                ),
                context_data={"error": "not_authenticated"},
            )

        handler_map: dict[str, Any] = {
            "play": self._handle_play,
            "pause": self._handle_pause,
            "skip": self._handle_skip,
            "previous": self._handle_previous,
            "volume": self._handle_volume,
            "shuffle": self._handle_shuffle,
            "repeat": self._handle_repeat,
            "now_playing": self._handle_now_playing,
        }
        handler = handler_map.get(action)
        if not handler:
            return CommandResponse.error_response(
                error_details=f"Unknown action: {action}",
                context_data={"error": "unknown_action"},
            )
        return handler(**kwargs)

    # -- Handlers ------------------------------------------------------------

    def _handle_play(self, **kwargs: Any) -> CommandResponse:
        from spotify_shared import go_librespot_manager
        from spotify_shared.go_librespot_manager import GoLibrespotMissingError
        from spotify_shared.local_client import LocalAPIError, LocalAPIUnavailable
        query: str | None = kwargs.get("query")

        try:
            self._ensure_daemon_running()
        except GoLibrespotMissingError as e:
            return CommandResponse.error_response(
                error_details=(
                    f"Spotify daemon isn't available: {e}. Re-install the Spotify "
                    f"package from the Pantry."
                ),
                context_data={"error": "daemon_missing"},
            )
        except Exception as e:
            logger.error("go-librespot start failed", error=str(e))
            return CommandResponse.error_response(
                error_details=f"Couldn't start the Spotify daemon: {e}",
                context_data={"error": "daemon_start_failed"},
            )

        local = self._make_local_client()
        if not self._wait_for_ready(local):
            paired: bool = go_librespot_manager.status(self._device_name()).paired
            if not paired:
                return CommandResponse.error_response(
                    error_details=(
                        f"Open Spotify on your phone, tap the Devices icon, and "
                        f"select '{self._device_name()}' to pair this node."
                    ),
                    context_data={"error": "not_paired", "device_name": self._device_name()},
                )
            return CommandResponse.error_response(
                error_details="The Spotify daemon didn't come up in time. Try again.",
                context_data={"error": "daemon_not_ready"},
            )

        # Resume path: no query → just resume whatever was last playing.
        if not query:
            try:
                local.resume()
            except (LocalAPIError, LocalAPIUnavailable) as e:
                return CommandResponse.error_response(
                    error_details=f"Couldn't resume Spotify: {e}",
                    context_data={"error": "resume_failed"},
                )
            return CommandResponse.success_response(
                context_data={"action": "play", "message": "Resumed Spotify"},
            )

        # Play-with-query path: resolve via Web API search, then play the
        # returned URI through the local daemon.
        search_result, err = self._call_with_refresh(lambda c: c.search(query))
        if err:
            return CommandResponse.error_response(
                error_details=f"Spotify search failed: {err}",
                context_data={"error": "search_failed", "query": query},
            )
        hit = search_result
        if hit is None:
            return CommandResponse.error_response(
                error_details=f"I couldn't find anything on Spotify for '{query}'.",
                context_data={"error": "no_results", "query": query},
            )

        try:
            local.play(uri=hit.uri)
        except (LocalAPIError, LocalAPIUnavailable) as e:
            return CommandResponse.error_response(
                error_details=f"Spotify play failed: {e}",
                context_data={"error": "play_failed"},
            )

        kind_label: str = {
            "track": "track", "album": "album",
            "playlist": "playlist", "artist": "music by",
        }.get(hit.kind, hit.kind)
        message: str = f"Playing {kind_label} {hit.display} on Spotify"

        return CommandResponse.success_response(
            context_data={"action": "play", "message": message},
        )

    def _handle_pause(self, **_kwargs: Any) -> CommandResponse:
        from spotify_shared.local_client import LocalAPIError, LocalAPIUnavailable

        local = self._make_local_client()
        try:
            local.pause()
        except (LocalAPIError, LocalAPIUnavailable) as e:
            return CommandResponse.error_response(
                error_details=f"Pause failed: {e}",
                context_data={"error": "pause_failed"},
            )
        return CommandResponse.success_response(
            context_data={"action": "pause", "message": "Spotify paused"},
        )

    def _handle_skip(self, **_kwargs: Any) -> CommandResponse:
        from spotify_shared.local_client import LocalAPIError, LocalAPIUnavailable

        local = self._make_local_client()
        try:
            local.next()
        except (LocalAPIError, LocalAPIUnavailable) as e:
            return CommandResponse.error_response(
                error_details=f"Skip failed: {e}",
                context_data={"error": "skip_failed"},
            )
        return CommandResponse.success_response(
            context_data={"action": "skip", "message": "Skipped"},
        )

    def _handle_previous(self, **_kwargs: Any) -> CommandResponse:
        from spotify_shared.local_client import LocalAPIError, LocalAPIUnavailable

        local = self._make_local_client()
        try:
            local.prev()
        except (LocalAPIError, LocalAPIUnavailable) as e:
            return CommandResponse.error_response(
                error_details=f"Previous failed: {e}",
                context_data={"error": "previous_failed"},
            )
        return CommandResponse.success_response(
            context_data={"action": "previous", "message": "Going back"},
        )

    def _handle_volume(self, **kwargs: Any) -> CommandResponse:
        from spotify_shared.local_client import LocalAPIError, LocalAPIUnavailable

        level_raw: Any = kwargs.get("level")
        if level_raw is None:
            return CommandResponse.error_response(
                error_details="What volume level should I set? (0 to 100)",
                context_data={"error": "missing_level"},
            )
        try:
            level: int = int(level_raw)
        except (TypeError, ValueError):
            return CommandResponse.error_response(
                error_details=f"'{level_raw}' isn't a valid volume — pick a number from 0 to 100.",
                context_data={"error": "invalid_level"},
            )
        if not (0 <= level <= 100):
            return CommandResponse.error_response(
                error_details="Volume must be between 0 and 100.",
                context_data={"error": "level_out_of_range"},
            )

        local = self._make_local_client()
        try:
            local.set_volume(level)
        except (LocalAPIError, LocalAPIUnavailable) as e:
            return CommandResponse.error_response(
                error_details=f"Volume change failed: {e}",
                context_data={"error": "volume_failed"},
            )
        return CommandResponse.success_response(
            context_data={
                "action": "volume", "level": level,
                "message": f"Volume set to {level}",
            },
        )

    def _handle_shuffle(self, **kwargs: Any) -> CommandResponse:
        from spotify_shared.local_client import LocalAPIError, LocalAPIUnavailable

        state_raw: Any = kwargs.get("state")
        if state_raw is None:
            return CommandResponse.error_response(
                error_details="Shuffle on or off?",
                context_data={"error": "missing_state"},
            )
        state: str = str(state_raw).lower()
        if state in ("on", "true", "yes"):
            on: bool = True
        elif state in ("off", "false", "no"):
            on = False
        else:
            return CommandResponse.error_response(
                error_details=f"Shuffle state must be 'on' or 'off' (got '{state_raw}').",
                context_data={"error": "invalid_state"},
            )

        local = self._make_local_client()
        try:
            local.set_shuffle(on)
        except (LocalAPIError, LocalAPIUnavailable) as e:
            return CommandResponse.error_response(
                error_details=f"Shuffle change failed: {e}",
                context_data={"error": "shuffle_failed"},
            )
        return CommandResponse.success_response(
            context_data={
                "action": "shuffle", "state": "on" if on else "off",
                "message": f"Shuffle {'on' if on else 'off'}",
            },
        )

    def _handle_repeat(self, **kwargs: Any) -> CommandResponse:
        from spotify_shared.local_client import LocalAPIError, LocalAPIUnavailable

        state_raw: Any = kwargs.get("state")
        if state_raw is None:
            return CommandResponse.error_response(
                error_details="Repeat off, track, or all?",
                context_data={"error": "missing_state"},
            )
        mode: str | None = _REPEAT_MAP.get(str(state_raw).lower())
        if mode is None:
            return CommandResponse.error_response(
                error_details=(
                    f"Repeat mode must be 'off', 'track', or 'all' (got '{state_raw}')."
                ),
                context_data={"error": "invalid_state"},
            )

        local = self._make_local_client()
        try:
            local.set_repeat(mode)
        except (LocalAPIError, LocalAPIUnavailable) as e:
            return CommandResponse.error_response(
                error_details=f"Repeat change failed: {e}",
                context_data={"error": "repeat_failed"},
            )
        spoken: str = {"off": "off", "track": "this track", "context": "the playlist"}[mode]
        return CommandResponse.success_response(
            context_data={
                "action": "repeat", "mode": mode,
                "message": f"Repeat {spoken}",
            },
        )

    def _handle_now_playing(self, **_kwargs: Any) -> CommandResponse:
        from spotify_shared.local_client import LocalAPIError, LocalAPIUnavailable

        local = self._make_local_client()
        try:
            st = local.status()
        except (LocalAPIError, LocalAPIUnavailable) as e:
            return CommandResponse.error_response(
                error_details=f"Couldn't read what's playing: {e}",
                context_data={"error": "now_playing_failed"},
            )
        if st.stopped or not st.track_uri:
            return CommandResponse.error_response(
                error_details="Nothing is playing on Spotify right now.",
                context_data={"error": "nothing_playing"},
            )
        artists: str = ", ".join(st.track_artists) if st.track_artists else "unknown artist"
        return CommandResponse.success_response(
            context_data={
                "action": "now_playing",
                "song": st.track_name,
                "artists": st.track_artists,
                "album": st.track_album,
                "is_playing": not st.paused,
                "message": f"{st.track_name} by {artists} from the album {st.track_album}",
            },
        )
