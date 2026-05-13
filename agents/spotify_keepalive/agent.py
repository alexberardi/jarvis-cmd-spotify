"""Background agent that keeps spotifyd running on the node.

Runs on startup and every few minutes. The agent:
  1. Ensures the spotifyd binary + libssl1.1 sidecar are installed (downloads
     once if missing)
  2. Starts spotifyd in Zeroconf discovery mode if it isn't running
  3. Restarts it if the daemon process has died

This is what makes the Spotify Connect device "Jarvis" appear in the user's
phone Spotify app continuously, without anyone running a CLI command.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Same belt-and-suspenders as command.py — make sure the package's lib dir is
# importable regardless of when discovery last refreshed.
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


from jarvis_command_sdk import (
    AgentSchedule,
    IJarvisAgent,
    IJarvisSecret,
    JarvisSecret,
    JarvisStorage,
)


logger = JarvisLogger(service="cmd.spotify.keepalive")


class SpotifyKeepaliveAgent(IJarvisAgent):
    """Keep spotifyd alive on the node so Spotify Connect always sees Jarvis."""

    def __init__(self) -> None:
        self._storage = JarvisStorage("spotify")
        self._last_status: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "spotify_keepalive"

    @property
    def description(self) -> str:
        return (
            "Keeps the spotifyd Spotify Connect daemon running so the node "
            "appears in the user's Spotify app device list."
        )

    @property
    def schedule(self) -> AgentSchedule:
        # Run on startup, then check every 5 minutes. spotifyd is robust and
        # rarely dies, so this is more about handling jarvis-node restarts +
        # picking up new BT routing on subsequent loops.
        return AgentSchedule(interval_seconds=300, run_on_startup=True)

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        # Use the same SPOTIFY_DEVICE_NAME that the command uses (per-node).
        return [
            JarvisSecret(
                "SPOTIFY_DEVICE_NAME",
                "Name shown for this node in your Spotify app's Devices list (default: Jarvis)",
                "node", "string",
                is_sensitive=False, required=False,
                friendly_name="Device Name",
            ),
        ]

    async def run(self) -> None:
        """Ensure spotifyd is running with current BT routing.

        Idempotent in the steady state, but if the audio sink target
        changed since last tick (BT speaker connected/disconnected),
        restart spotifyd so the new PULSE_SINK env takes effect — the
        env is captured at process start, and the daemon doesn't
        re-read it during its lifetime.
        """
        from spotify_shared import spotifyd_manager

        try:
            from jarvis_command_sdk import BluetoothAudio
            current_sink = BluetoothAudio.target_sink()
        except ImportError:
            current_sink = None

        device_name: str = (
            self._storage.get_secret("SPOTIFY_DEVICE_NAME", scope="node") or "Jarvis"
        )
        last_sink = self._last_status.get("audio_sink")
        was_running: bool = bool(self._last_status.get("running"))
        sink_changed: bool = current_sink != last_sink

        try:
            if sink_changed and was_running:
                logger.info(
                    "Audio sink changed; restarting spotifyd to pick up new routing",
                    from_sink=last_sink, to_sink=current_sink,
                )
                pid: int = spotifyd_manager.restart(device_name=device_name)
            else:
                pid = spotifyd_manager.start(device_name=device_name)
        except Exception as e:
            logger.error("spotifyd keepalive: start/restart failed", error=str(e))
            self._last_status = {"running": False, "error": str(e)}
            return

        st = spotifyd_manager.status(device_name)
        self._last_status = {
            "running": st.running,
            "paired": st.paired,
            "device_name": st.device_name,
            "pid": pid,
            "audio_sink": current_sink,
        }
        logger.info("spotifyd keepalive tick", **self._last_status)

    def get_context_data(self) -> dict[str, Any]:
        # No need to inject Spotify state into voice prompt — the command
        # checks live state on each call.
        return {}
