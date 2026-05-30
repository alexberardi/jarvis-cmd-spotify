"""Background agent that keeps the Spotify integration healthy on the node.

Runs on startup and every few minutes. Responsibilities:

  1. **Daemon keepalive** — start ``go-librespot`` if it isn't running, and
     restart it when the audio sink target changes (BT speaker connect /
     disconnect) so the new ``PULSE_SINK`` env takes effect. The binary
     itself auto-installs on first start (see ``spotify_shared/installer.py``).
  2. **OAuth token refresh** — exchange the refresh token for a fresh access
     token ahead of expiry, so the next voice command doesn't pay a 401
     round-trip. The command's own ``_call_with_refresh`` is the fallback;
     this is the proactive path.

If go-librespot can't be downloaded (offline first-boot, unsupported arch),
the keepalive step is skipped and logged — token refresh still runs so
pairing later "just works."
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

# Same belt-and-suspenders as command.py — make sure the package's lib dir
# is importable regardless of when discovery last refreshed. Matches the
# Pantry install convention of ~/.jarvis/packages/<name>/<name>_lib/.
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


from jarvis_command_sdk import (
    AgentSchedule,
    IJarvisAgent,
    IJarvisSecret,
    JarvisSecret,
    JarvisStorage,
)


logger = JarvisLogger(service="cmd.spotify.keepalive")


# Refresh the access token when this much (or less) time remains until expiry.
# Spotify tokens last 3600s; refreshing 10 min ahead keeps voice commands
# pay-no-401 in the steady state while leaving room for retries.
_REFRESH_LEAD_SECONDS: int = 600


class SpotifyKeepaliveAgent(IJarvisAgent):
    """Keep go-librespot alive + refresh OAuth tokens ahead of expiry."""

    def __init__(self) -> None:
        self._storage = JarvisStorage("spotify")
        self._last_status: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "spotify_keepalive"

    @property
    def description(self) -> str:
        return (
            "Keeps the Spotify Connect daemon running on the node and refreshes "
            "OAuth tokens before they expire so voice commands don't stall."
        )

    @property
    def schedule(self) -> AgentSchedule:
        # Run on startup, then check every 5 minutes. go-librespot is robust
        # and rarely dies; the cadence also drives proactive token refresh
        # (tokens last 3600s, refresh window is 600s ahead of expiry).
        return AgentSchedule(interval_seconds=300, run_on_startup=True)

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        return [
            JarvisSecret(
                "SPOTIFY_DEVICE_NAME",
                "Name shown for this node in your Spotify app's Devices list (default: Jarvis)",
                "integration", "string",
                is_sensitive=False, required=False,
                friendly_name="Device Name",
            ),
        ]

    async def run(self) -> None:
        """One tick: refresh tokens if needed, then keepalive go-librespot."""
        refreshed: bool = self._maybe_refresh_token()
        await self._keepalive_daemon()
        self._last_status["token_refreshed_this_tick"] = refreshed

    # -- Token refresh -------------------------------------------------------

    def _maybe_refresh_token(self) -> bool:
        """Refresh Spotify access token if expiry is within the lead window.

        Returns True if a refresh was attempted and succeeded, False
        otherwise. Swallows all exceptions — keepalive must keep running
        even if Spotify auth is broken.
        """
        refresh_token: str | None = self._storage.get_secret(
            "SPOTIFY_REFRESH_TOKEN", scope="integration",
        )
        client_id: str | None = self._storage.get_secret(
            "SPOTIFY_CLIENT_ID", scope="integration",
        )
        if not refresh_token or not client_id:
            return False

        expires_at_raw: str | None = self._storage.get_secret(
            "SPOTIFY_TOKEN_EXPIRES_AT", scope="integration",
        )
        try:
            expires_at: int = int(expires_at_raw) if expires_at_raw else 0
        except (TypeError, ValueError):
            expires_at = 0

        now: int = int(time.time())
        if expires_at and (expires_at - now) > _REFRESH_LEAD_SECONDS:
            return False

        from spotify_shared.auth import refresh_access_token

        data: dict[str, Any] | None = refresh_access_token(
            client_id=client_id, refresh_token=refresh_token,
        )
        if not data:
            logger.warning(
                "Spotify token refresh failed",
                expires_at=expires_at, now=now,
            )
            return False

        new_access: str | None = data.get("access_token")
        if not new_access:
            return False
        self._storage.set_secret(
            "SPOTIFY_ACCESS_TOKEN", new_access, scope="integration",
        )

        new_refresh: str | None = data.get("refresh_token")
        if new_refresh and new_refresh != refresh_token:
            self._storage.set_secret(
                "SPOTIFY_REFRESH_TOKEN", new_refresh, scope="integration",
            )

        expires_in: Any = data.get("expires_in")
        if isinstance(expires_in, (int, float)):
            new_expires_at: int = now + int(expires_in)
            self._storage.set_secret(
                "SPOTIFY_TOKEN_EXPIRES_AT", str(new_expires_at),
                scope="integration",
            )
            logger.info(
                "Spotify access token refreshed",
                expires_in=int(expires_in),
                rotated_refresh=bool(new_refresh and new_refresh != refresh_token),
            )
        else:
            logger.info("Spotify access token refreshed (no expires_in in response)")

        return True

    # -- Daemon keepalive ----------------------------------------------------

    async def _keepalive_daemon(self) -> None:
        """Ensure go-librespot is running with current BT routing.

        Idempotent in the steady state, but if the audio sink target
        changed since last tick (BT speaker connected/disconnected),
        restart go-librespot so the new ``PULSE_SINK`` env takes effect —
        the env is captured at process start, and the daemon doesn't
        re-read it during its lifetime.

        If the binary can't be downloaded (offline first-boot, unsupported
        arch), logs once per tick and returns cleanly — token refresh has
        already run.
        """
        from spotify_shared import go_librespot_manager
        from spotify_shared.go_librespot_manager import GoLibrespotMissingError

        try:
            from jarvis_command_sdk import BluetoothAudio
            current_sink = BluetoothAudio.target_sink()
        except ImportError:
            current_sink = None

        device_name: str = (
            self._storage.get_secret("SPOTIFY_DEVICE_NAME", scope="integration") or "Jarvis"
        )
        last_sink = self._last_status.get("audio_sink")
        was_running: bool = bool(self._last_status.get("running"))
        sink_changed: bool = current_sink != last_sink

        try:
            if sink_changed and was_running:
                logger.info(
                    "Audio sink changed; restarting go-librespot to pick up new routing",
                    from_sink=last_sink, to_sink=current_sink,
                )
                pid: int = go_librespot_manager.restart(device_name=device_name)
            else:
                pid = go_librespot_manager.start(device_name=device_name)
        except GoLibrespotMissingError as e:
            logger.warning(
                "go-librespot binary not available; pairing will fail until "
                "the binary can be downloaded.",
                error=str(e),
            )
            self._last_status = {
                "running": False,
                "error": "go_librespot_not_installed",
                "audio_sink": current_sink,
            }
            return
        except Exception as e:
            logger.error("go-librespot keepalive: start/restart failed", error=str(e))
            self._last_status = {"running": False, "error": str(e)}
            return

        st = go_librespot_manager.status(device_name)
        self._last_status = {
            "running": st.running,
            "paired": st.paired,
            "device_name": st.device_name,
            "pid": pid,
            "audio_sink": current_sink,
        }
        logger.info("go-librespot keepalive tick", **self._last_status)

    def get_context_data(self) -> dict[str, Any]:
        # No need to inject Spotify state into voice prompt — the command
        # checks live state on each call.
        return {}
