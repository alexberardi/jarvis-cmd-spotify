"""Manage the spotifyd subprocess.

Auth flow (priority order):
  1. **OAuth-token credentials.json** — if the user has authenticated via the
     mobile app's Spotify OAuth flow, we have an access_token and the user's
     Spotify ID. We write a librespot-compatible credentials.json with
     ``auth_type: 3`` (``AUTHENTICATION_SPOTIFY_TOKEN``) before launching
     spotifyd. When this works, no Zeroconf pairing is needed — the daemon
     auths immediately as the user.
  2. **Cached credentials** — spotifyd persists creds to its cache_path after
     a successful login. Future launches read the cache directly.
  3. **Zeroconf discovery (fallback)** — if no creds are usable, spotifyd
     advertises via mDNS and the user pairs once via their phone's Spotify
     app. This is Spotify's only officially-supported path for self-hosted
     Connect devices.

Audio routing: BluetoothAudio.playback_env() injects PULSE_SINK into the
spotifyd subprocess environment, so when a Bluetooth speaker is connected
PulseAudio routes spotifyd's output to it. If no BT speaker is connected
spotifyd plays out the default (local) sink. The env is captured at process
start; if the BT speaker connects/disconnects later we restart spotifyd.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

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


try:
    from jarvis_command_sdk import BluetoothAudio
except ImportError:
    BluetoothAudio = None  # type: ignore[assignment,misc]

from spotify_shared.installer import (
    binary_path,
    ensure_installed,
    has_bundled_libssl,
    libssl_dir,
)

logger = JarvisLogger(service="cmd.spotify.daemon")


def _default_env() -> dict[str, str]:
    """Capture an env for spotifyd: BT routing + bundled libssl on LD_LIBRARY_PATH."""
    if BluetoothAudio is None:
        env: dict[str, str] = dict(os.environ)
    else:
        env = BluetoothAudio.playback_env()

    # If we bundled libssl1.1 alongside spotifyd, prepend it to LD_LIBRARY_PATH
    # so the dynamic linker finds it ahead of any system libs.
    if has_bundled_libssl():
        existing: str = env.get("LD_LIBRARY_PATH", "")
        bundled_str: str = str(libssl_dir())
        env["LD_LIBRARY_PATH"] = (
            f"{bundled_str}:{existing}" if existing else bundled_str
        )
    return env


def _config_root() -> Path:
    return Path.home() / ".jarvis" / "spotify"


def _cache_dir() -> Path:
    """spotifyd persists Connect credentials here after first pairing."""
    return _config_root() / "cache"


def _config_path() -> Path:
    return _config_root() / "spotifyd.conf"


def _pid_path() -> Path:
    return _config_root() / "spotifyd.pid"


def _backend_for_platform() -> str:
    """Pick the audio backend that lets BluetoothAudio.PULSE_SINK take effect."""
    import platform

    if platform.system().lower() == "linux":
        return "pulseaudio"
    return "rodio"  # macOS/other — uses CoreAudio under the hood


def _write_config(device_name: str) -> None:
    """Write spotifyd.conf so the daemon picks up our preferred settings.

    Discovery mode is enabled by leaving `username` unset — spotifyd advertises
    via Zeroconf and the user's Spotify app does the credential exchange.
    """
    backend: str = _backend_for_platform()
    cache_dir: Path = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    contents: str = "\n".join([
        "[global]",
        f'device_name = "{device_name}"',
        f'backend = "{backend}"',
        'bitrate = 320',
        'volume_normalisation = true',
        # initial_volume is mandatory — spotifyd defaults to 0 (silent) on
        # cold start, which makes Web-API-initiated playback look like the
        # device is broken: track loads, "play" succeeds, but no audio. 100
        # is full output and the user can attenuate downstream via PA's
        # sink volume or the HAT mixer.
        'initial_volume = 100',
        f'cache_path = "{cache_dir}"',
        'use_mpris = false',
        # device_type=Speaker tells Spotify Connect to treat this node as a
        # speaker rather than a generic computer — affects icon and routing
        # behavior in the user's Spotify app.
        'device_type = "speaker"',
        '',
    ])
    _config_root().mkdir(parents=True, exist_ok=True)
    _config_path().write_text(contents)


def _read_pid() -> int | None:
    if not _pid_path().exists():
        return None
    try:
        return int(_pid_path().read_text().strip())
    except (ValueError, OSError):
        return None


def _write_pid(pid: int) -> None:
    _pid_path().write_text(str(pid))


def _clear_pid() -> None:
    try:
        _pid_path().unlink()
    except FileNotFoundError:
        pass


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


@dataclass
class DaemonStatus:
    running: bool
    paired: bool
    device_name: str
    binary_path: str | None


def status(device_name: str) -> DaemonStatus:
    pid: int | None = _read_pid()
    running: bool = pid is not None and _process_alive(pid)
    paired: bool = has_cached_credentials()
    bin_path: str | None = str(binary_path()) if binary_path().exists() else None
    return DaemonStatus(
        running=running, paired=paired,
        device_name=device_name, binary_path=bin_path,
    )


def stop() -> None:
    """Terminate the spotifyd subprocess, if running."""
    pid: int | None = _read_pid()
    if pid is None:
        return
    if not _process_alive(pid):
        _clear_pid()
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid()
        return

    # Wait briefly for clean exit, then SIGKILL
    for _ in range(20):
        if not _process_alive(pid):
            _clear_pid()
            return
        time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _clear_pid()


def start(device_name: str, env: dict[str, str] | None = None) -> int:
    """Start the spotifyd daemon. Idempotent — returns existing PID if running.

    `env` is the full environment dict for the subprocess. Callers should pass
    BluetoothAudio.playback_env() so PulseAudio routes audio to a connected
    Bluetooth speaker.
    """
    existing_pid: int | None = _read_pid()
    if existing_pid is not None and _process_alive(existing_pid):
        return existing_pid

    bin_path: Path = ensure_installed()
    _write_config(device_name)

    proc_env: dict[str, str] = dict(env) if env is not None else _default_env()
    log_path: Path = _config_root() / "spotifyd.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Use --no-daemon so we capture the actual PID; spotifyd's built-in daemon
    # mode forks and we lose track of the child.
    cmd: list[str] = [
        str(bin_path),
        "--no-daemon",
        "--config-path", str(_config_path()),
    ]

    log_handle = open(log_path, "ab", buffering=0)  # noqa: SIM115 — handle owned by child
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            env=proc_env,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        log_handle.close()
        raise RuntimeError(f"spotifyd binary not executable: {e}") from e

    _write_pid(proc.pid)
    logger.info("spotifyd started", pid=proc.pid, device_name=device_name)
    return proc.pid


def restart(device_name: str, env: dict[str, str] | None = None) -> int:
    """Stop + start. Used when BT routing changes (BT speaker connect/disconnect)."""
    stop()
    return start(device_name=device_name, env=env)


def reset_pairing() -> None:
    """Wipe the cached pairing credentials so the user can re-pair from scratch."""
    cache: Path = _cache_dir()
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)


def write_oauth_credentials(spotify_user_id: str, access_token: str) -> Path:
    """Write a librespot-compatible credentials.json from an OAuth access token.

    Format: ``{"username": <spotify_id>, "auth_type": 3, "auth_data": <base64>}``

    ``auth_type: 3`` is ``AUTHENTICATION_SPOTIFY_TOKEN`` in librespot's protocol.
    Some librespot/spotifyd builds accept a Web API access_token as the auth_data
    payload — when they do, the daemon authenticates with no Zeroconf pairing
    required. When they don't, the daemon will fail to start; the caller should
    detect that, delete this file, and let spotifyd fall back to discovery.

    Returns the path to credentials.json.
    """
    cache: Path = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)

    auth_data_b64: str = base64.b64encode(access_token.encode("utf-8")).decode("ascii")
    credentials: dict = {
        "username": spotify_user_id,
        "auth_type": 3,  # AUTHENTICATION_SPOTIFY_TOKEN
        "auth_data": auth_data_b64,
    }
    creds_path: Path = cache / "credentials.json"
    creds_path.write_text(json.dumps(credentials))
    return creds_path


def has_cached_credentials() -> bool:
    """Whether spotifyd's cache has post-pairing credentials.

    spotifyd v0.4+ uses two cache subdirs:
      - ``cache/zeroconf/`` — credentials captured when the user tapped the
        device in their phone's Spotify app
      - ``cache/oauth/`` — credentials from spotifyd's own ``authenticate``
        OAuth flow (browser-based, redirect to localhost:8000)

    Either is enough to skip the pairing step on subsequent launches.
    """
    cache: Path = _cache_dir()
    zeroconf_creds: Path = cache / "zeroconf"
    oauth_creds: Path = cache / "oauth"
    if zeroconf_creds.is_dir() and any(zeroconf_creds.iterdir()):
        return True
    if oauth_creds.is_dir() and any(oauth_creds.iterdir()):
        return True
    return False


def daemon_log_tail(lines: int = 20) -> str:
    """Read the last N lines of spotifyd's log for diagnostics."""
    log_path: Path = _config_root() / "spotifyd.log"
    if not log_path.exists():
        return ""
    try:
        text: str = log_path.read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
