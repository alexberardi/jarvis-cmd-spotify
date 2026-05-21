"""Manage the librespot subprocess (apt-installed via the raspotify package).

The user installs ``raspotify`` (which ships ``/usr/bin/librespot``) via apt;
we run librespot ourselves with CLI args so we control the device name, cache
location, and PulseAudio sink for Bluetooth routing — instead of using
raspotify's own systemd unit, which doesn't expose any of that.

Auth flow (Spotify Connect only — librespot 0.8.0 + PKCE handle this):
  1. **Cached credentials** — after a successful Zeroconf pairing, librespot
     writes ``credentials.json`` to ``--cache``. Future launches read it.
  2. **Zeroconf discovery (fallback)** — if no cache, librespot advertises
     via mDNS and the user pairs once from their phone's Spotify app.

Audio routing: BluetoothAudio.playback_env() injects ``PULSE_SINK`` so when
a Bluetooth speaker is connected PulseAudio routes librespot's output to it.
The env is captured at process start; if the BT speaker connect/disconnects
later, restart the daemon for the new env to take effect.
"""

from __future__ import annotations

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

logger = JarvisLogger(service="cmd.spotify.daemon")


class LibrespotMissingError(RuntimeError):
    """Raised when the librespot binary isn't installed.

    Carries an apt-install hint so the command surfaces a clear message.
    raspotify (which provides /usr/bin/librespot) is added to the node's
    apt sources by jarvis-node-setup/install.sh.
    """

    def __init__(self) -> None:
        super().__init__(
            "librespot is not installed. The 'raspotify' package should "
            "have been installed automatically — try `sudo apt install raspotify` "
            "or re-run install.sh on the node."
        )


def _default_env() -> dict[str, str]:
    """Capture an env for librespot: Bluetooth audio routing via PulseAudio."""
    if BluetoothAudio is None:
        return dict(os.environ)
    return BluetoothAudio.playback_env()


def _config_root() -> Path:
    return Path.home() / ".jarvis" / "spotify"


def _cache_dir() -> Path:
    """librespot persists Connect credentials + audio cache here."""
    return _config_root() / "cache"


def _pid_path() -> Path:
    return _config_root() / "librespot.pid"


def binary_path() -> Path | None:
    """Locate the apt-installed librespot binary, or None if missing.

    Order: ``shutil.which("librespot")`` → ``/usr/bin/librespot`` → ``/usr/local/bin/librespot``.
    """
    located: str | None = shutil.which("librespot")
    if located:
        return Path(located)
    for candidate in (Path("/usr/bin/librespot"), Path("/usr/local/bin/librespot")):
        if candidate.exists():
            return candidate
    return None


def is_installed() -> bool:
    return binary_path() is not None


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
    bin_path: Path | None = binary_path()
    return DaemonStatus(
        running=running, paired=paired,
        device_name=device_name,
        binary_path=str(bin_path) if bin_path else None,
    )


def stop() -> None:
    """Terminate the librespot subprocess, if running."""
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


def _build_argv(bin_path: Path, device_name: str) -> list[str]:
    """librespot CLI args. Mirrors the validated POC flag set."""
    cache: Path = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    return [
        str(bin_path),
        "--name", device_name,
        "--bitrate", "320",
        "--backend", "pulseaudio",
        "--device-type", "speaker",
        # initial_volume is mandatory — librespot defaults to 50 (softvol).
        # Going to 100 means voice-API-initiated playback isn't accidentally
        # silent and the user can attenuate downstream via PA sink volume.
        "--initial-volume", "100",
        "--enable-volume-normalisation",
        "--cache", str(cache),
        # No --system-cache: let creds + audio share the same dir (simpler).
    ]


def start(device_name: str, env: dict[str, str] | None = None) -> int:
    """Start librespot. Idempotent — returns existing PID if running.

    `env` is the full environment dict for the subprocess. Callers should pass
    BluetoothAudio.playback_env() so PulseAudio routes audio to a connected
    Bluetooth speaker.

    Raises LibrespotMissingError if the binary isn't installed.
    """
    existing_pid: int | None = _read_pid()
    if existing_pid is not None and _process_alive(existing_pid):
        return existing_pid

    bin_path: Path | None = binary_path()
    if bin_path is None:
        raise LibrespotMissingError()

    proc_env: dict[str, str] = dict(env) if env is not None else _default_env()
    log_path: Path = _config_root() / "librespot.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    argv: list[str] = _build_argv(bin_path, device_name)

    log_handle = open(log_path, "ab", buffering=0)  # noqa: SIM115 — handle owned by child
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            env=proc_env,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        log_handle.close()
        raise RuntimeError(f"librespot binary not executable: {e}") from e

    _write_pid(proc.pid)
    logger.info("librespot started", pid=proc.pid, device_name=device_name)
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


def has_cached_credentials() -> bool:
    """Whether librespot's cache has post-pairing credentials.

    librespot writes ``credentials.json`` to ``--cache`` after a successful
    Zeroconf pairing. Presence of that file = pairing already happened.
    """
    cred_file: Path = _cache_dir() / "credentials.json"
    return cred_file.is_file()


def daemon_log_tail(lines: int = 20) -> str:
    """Read the last N lines of librespot's log for diagnostics."""
    log_path: Path = _config_root() / "librespot.log"
    if not log_path.exists():
        return ""
    try:
        text: str = log_path.read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
