"""Spotify voice command — local playback via spotifyd, Web API control."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any

# Belt + suspenders: ensure our lib dir is on sys.path so the lazy imports
# of spotify_shared.* always resolve, even if discovery cache hasn't been
# refreshed since install. The runtime *should* have already added this via
# register_package_lib_paths(), but we re-add to be safe — no-op if present.
_LIB_DIR: str = str(Path.home() / ".jarvis" / "packages" / "spotify" / "lib")
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


_DEVICE_REGISTRATION_TIMEOUT_SECONDS: float = 12.0


def _spotify_is_active() -> bool:
    """True if spotifyd has an active PulseAudio sink-input (= playing audio).

    Ambiguous voice phrases like "stop" / "pause" / "skip" should only route
    to Spotify when Spotify is the audible player — otherwise they'd hijack
    commands meant for a different music service (Pandora, etc.). Checking
    for an active PA sink-input distinguishes "spotifyd is running" (true
    almost always, since the daemon stays advertising for Connect) from
    "spotifyd is actually emitting sound right now."
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
        if props.get("application.process.binary") == "spotifyd":
            # Sink-input exists. Treat any non-muted, non-corked stream as
            # "playing." pactl reports `corked: true` for paused streams.
            if item.get("corked"):
                continue
            return True
    return False
_DEVICE_REGISTRATION_POLL_SECONDS: float = 1.0


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
        # the message field instead of populating the tool_calls array,
        # which the parser doesn't extract from. Pandora has a similar
        # short description and works first try.
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
            # No native_redirect_uri — use the relay bounce flow. The CC will
            # set redirect_uri to <JARVIS_RELAY_URL>/oauth/bounce, which is the
            # URL that needs to be registered in the Spotify Developer Dashboard.
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
        # when Spotify is currently producing audio (spotifyd has an
        # active PA sink-input). Otherwise return None so the next
        # command's pre_route (or the LLM) can handle it.
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
        # If the LLM picked 'play' but didn't extract a query, try extracting it
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

    # -- Helpers (delegate to spotify_shared/) ------------------------------

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
        # Spotify usually rotates the refresh token; persist if rotated
        new_refresh: str | None = data.get("refresh_token")
        if new_refresh and new_refresh != refresh:
            self._storage.set_secret("SPOTIFY_REFRESH_TOKEN", new_refresh, scope="integration")
        return new_access

    def _spotify_user_id(self) -> str | None:
        """Return the Spotify user ID, fetching from /me on first call."""
        cached: str | None = self._storage.get_secret("SPOTIFY_USER_ID", scope="integration")
        if cached:
            return cached
        # Fetch via /me — needs a valid access token
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
        """Start spotifyd. Auth happens via Zeroconf pairing on first run.

        spotifyd v0.4 doesn't accept Web API access_tokens as Connect-device
        credentials — only its own OAuth flow or Zeroconf-pairing produce
        credentials it will use. Zeroconf pairing requires one tap in the
        user's phone Spotify app per node, then is cached forever.
        """
        from spotify_shared import spotifyd_manager
        spotifyd_manager.start(device_name=self._device_name())

    def _wait_for_device(self, client: Any) -> Any | None:
        """Poll Spotify Web API for our spotifyd device to register."""
        from spotify_shared.web_client import SpotifyDevice  # noqa: F401

        target: str = self._device_name().lower()
        deadline: float = time.monotonic() + _DEVICE_REGISTRATION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            for d in client.list_devices():
                if d.name.lower() == target:
                    return d
            time.sleep(_DEVICE_REGISTRATION_POLL_SECONDS)
        return None

    def _make_client(self) -> Any | None:
        """Build a SpotifyClient with a valid access token, refreshing if needed."""
        from spotify_shared.web_client import SpotifyClient

        token: str | None = self._access_token()
        if not token:
            token = self._refresh_and_persist_tokens()
            if not token:
                return None
        return SpotifyClient(access_token=token)

    def _call_with_refresh(self, fn: Any) -> tuple[Any, str | None]:
        """Run a closure against the API, refreshing the token once on 401.

        Returns (result, error_message). On success, error_message is None.
        """
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

        # Quick check: does the user have OAuth set up at all?
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
        from spotify_shared import spotifyd_manager
        query: str | None = kwargs.get("query")

        # Make sure the spotifyd daemon is running so it shows up as a Connect
        # device. start() is idempotent — no-op if already running.
        try:
            self._ensure_daemon_running()
        except Exception as e:
            logger.error("spotifyd start failed", error=str(e))
            return CommandResponse.error_response(
                error_details=f"Couldn't start the Spotify daemon: {e}",
                context_data={"error": "daemon_start_failed"},
            )

        # Find our device. If it's not yet registered, wait briefly.
        client = self._make_client()
        if client is None:
            return CommandResponse.error_response(
                error_details="Spotify authentication failed. Re-authenticate from Jarvis settings.",
                context_data={"error": "auth_failed"},
            )

        device = self._wait_for_device(client)
        if device is None:
            paired: bool = spotifyd_manager.status(self._device_name()).paired
            if not paired:
                return CommandResponse.error_response(
                    error_details=(
                        f"Open Spotify on your phone, tap the Devices icon, and select "
                        f"'{self._device_name()}' to pair this node."
                    ),
                    context_data={"error": "not_paired", "device_name": self._device_name()},
                )
            return CommandResponse.error_response(
                error_details=(
                    f"The Spotify daemon didn't register as a Connect device. "
                    f"Try saying 'play' again."
                ),
                context_data={"error": "device_not_registered"},
            )

        # Search for the requested content (if any)
        hit = None
        if query:
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

        # Transfer playback to our device, then play
        _, err = self._call_with_refresh(
            lambda c: c.transfer_playback(device.id, play=False),
        )
        if err:
            return CommandResponse.error_response(
                error_details=f"Couldn't switch playback to {device.name}: {err}",
                context_data={"error": "transfer_failed"},
            )

        _, err = self._call_with_refresh(
            lambda c: c.play(device_id=device.id, hit=hit),
        )
        if err:
            return CommandResponse.error_response(
                error_details=f"Spotify play failed: {err}",
                context_data={"error": "play_failed"},
            )

        if hit is not None:
            kind_label: str = {
                "track": "track", "album": "album",
                "playlist": "playlist", "artist": "music by",
            }.get(hit.kind, hit.kind)
            message: str = f"Playing {kind_label} {hit.display} on Spotify"
        else:
            message = "Resumed Spotify"

        return CommandResponse.success_response(
            context_data={"action": "play", "device": device.name, "message": message},
        )

    def _handle_pause(self, **_kwargs: Any) -> CommandResponse:
        _, err = self._call_with_refresh(lambda c: c.pause())
        if err:
            return CommandResponse.error_response(
                error_details=f"Pause failed: {err}",
                context_data={"error": "pause_failed"},
            )
        return CommandResponse.success_response(
            context_data={"action": "pause", "message": "Spotify paused"},
        )

    def _handle_skip(self, **_kwargs: Any) -> CommandResponse:
        _, err = self._call_with_refresh(lambda c: c.next_track())
        if err:
            return CommandResponse.error_response(
                error_details=f"Skip failed: {err}",
                context_data={"error": "skip_failed"},
            )
        return CommandResponse.success_response(
            context_data={"action": "skip", "message": "Skipped"},
        )

    def _handle_previous(self, **_kwargs: Any) -> CommandResponse:
        _, err = self._call_with_refresh(lambda c: c.previous_track())
        if err:
            return CommandResponse.error_response(
                error_details=f"Previous failed: {err}",
                context_data={"error": "previous_failed"},
            )
        return CommandResponse.success_response(
            context_data={"action": "previous", "message": "Going back"},
        )

    def _handle_volume(self, **kwargs: Any) -> CommandResponse:
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

        _, err = self._call_with_refresh(lambda c: c.set_volume(level))
        if err:
            return CommandResponse.error_response(
                error_details=f"Volume change failed: {err}",
                context_data={"error": "volume_failed"},
            )
        return CommandResponse.success_response(
            context_data={
                "action": "volume", "level": level,
                "message": f"Volume set to {level}",
            },
        )

    def _handle_shuffle(self, **kwargs: Any) -> CommandResponse:
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

        _, err = self._call_with_refresh(lambda c: c.set_shuffle(on))
        if err:
            return CommandResponse.error_response(
                error_details=f"Shuffle change failed: {err}",
                context_data={"error": "shuffle_failed"},
            )
        return CommandResponse.success_response(
            context_data={
                "action": "shuffle", "state": "on" if on else "off",
                "message": f"Shuffle {'on' if on else 'off'}",
            },
        )

    def _handle_repeat(self, **kwargs: Any) -> CommandResponse:
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

        _, err = self._call_with_refresh(lambda c: c.set_repeat(mode))
        if err:
            return CommandResponse.error_response(
                error_details=f"Repeat change failed: {err}",
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
        result, err = self._call_with_refresh(lambda c: c.now_playing())
        if err:
            return CommandResponse.error_response(
                error_details=f"Couldn't read what's playing: {err}",
                context_data={"error": "now_playing_failed"},
            )
        if result is None:
            return CommandResponse.error_response(
                error_details="Nothing is playing on Spotify right now.",
                context_data={"error": "nothing_playing"},
            )
        artists: str = ", ".join(result.artists) if result.artists else "unknown artist"
        return CommandResponse.success_response(
            context_data={
                "action": "now_playing",
                "song": result.name,
                "artists": result.artists,
                "album": result.album,
                "is_playing": result.is_playing,
                "message": f"{result.name} by {artists} from the album {result.album}",
            },
        )
