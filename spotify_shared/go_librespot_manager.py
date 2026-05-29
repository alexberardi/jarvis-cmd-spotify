"""Manage the go-librespot subprocess + its localhost HTTP API.

go-librespot replaces the Rust ``librespot``/``spotifyd`` we used to run for
two reasons:

  1. **Local HTTP control API.** go-librespot exposes a REST API on
     ``localhost:3678`` (``POST /player/play``, ``/pause``, ``/next``, etc.) —
     we drive all playback through that instead of Spotify's flaky Web API.
     The Web API stays in the picture only for search/metadata.
  2. **Same Connect-protocol playback layer** under the hood (it's a from-scratch
     Go reimplementation of librespot), so the user-facing pairing flow is
     unchanged: pair once from the phone's Spotify app, credentials cache in
     ``--config_dir``, future launches skip pairing.

Process lifecycle is owned via a PID file, identical to the prior manager.
Audio routing through PulseAudio + ``PULSE_SINK`` for Bluetooth follow-along
is unchanged.
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

from spotify_shared import installer


logger = JarvisLogger(service="cmd.spotify.daemon")


# Localhost port for go-librespot's HTTP API. 3678 is upstream's example port
# and doesn't collide with anything else we run on the node.
API_HOST: str = "127.0.0.1"
API_PORT: int = 3678


class GoLibrespotMissingError(RuntimeError):
    """Raised when the go-librespot binary can't be located AND can't be installed."""

    def __init__(self, detail: str = "") -> None:
        msg: str = (
            "go-librespot is not available on this node. Re-install the "
            "Spotify package or run the daemon manager once to trigger the "
            "binary download."
        )
        if detail:
            msg = f"{msg} (detail: {detail})"
        super().__init__(msg)


def _default_env() -> dict[str, str]:
    """Capture an env for go-librespot: Bluetooth audio routing via PulseAudio."""
    if BluetoothAudio is None:
        return dict(os.environ)
    return BluetoothAudio.playback_env()


def _config_root() -> Path:
    return Path.home() / ".jarvis" / "spotify"


def _go_librespot_config_dir() -> Path:
    """Where go-librespot looks for config.yml + caches credentials.

    Passed to the daemon via ``--config_dir``. Holding everything in our own
    namespace (instead of ``~/.config/go-librespot``) keeps Spotify package
    state colocated under ``~/.jarvis/spotify/``.
    """
    return _config_root() / "go-librespot"


def _pid_path() -> Path:
    return _config_root() / "go-librespot.pid"


def _log_path() -> Path:
    return _config_root() / "go-librespot.log"


def _config_yaml_path() -> Path:
    return _go_librespot_config_dir() / "config.yml"


def _credentials_path() -> Path:
    """Post-pairing credentials cache written by go-librespot itself."""
    return _go_librespot_config_dir() / "state.json"


def binary_path() -> Path | None:
    return installer.binary_path()


def is_installed() -> bool:
    return installer.is_installed()


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
    """True if a live go-librespot process exists at ``pid``.

    Binary-name validation guards against PID-reuse staleness: after a
    reboot the pidfile may still name a PID that's now in use by an
    unrelated process (empirically often a Python thread of jarvis-node
    itself, since both come up under uid 1000 early in boot). A bare
    existence check would incorrectly report the daemon as running.
    """
    from jarvis_command_sdk import process_alive
    return process_alive(pid, expected_comm="go-librespot")


@dataclass
class DaemonStatus:
    running: bool
    paired: bool
    device_name: str
    binary_path: str | None
    api_url: str


def status(device_name: str) -> DaemonStatus:
    pid: int | None = _read_pid()
    running: bool = pid is not None and _process_alive(pid)
    paired: bool = has_cached_credentials()
    bin_path: Path | None = binary_path()
    return DaemonStatus(
        running=running, paired=paired,
        device_name=device_name,
        binary_path=str(bin_path) if bin_path else None,
        api_url=api_url(),
    )


def api_url() -> str:
    return f"http://{API_HOST}:{API_PORT}"


def ensure_running_unpaused() -> None:
    """Send SIGCONT to the go-librespot process if it's been SIGSTOP'd.

    The node's wake-word handler SIGSTOPs go-librespot during voice capture
    to keep music out of the user's audio (see jarvis-node-setup's
    ``_pause_active_playback``). The matching SIGCONT runs *after* the
    command returns — so while our handler is running, the daemon could be
    paused and its HTTP API frozen with it.

    Calling this at the start of each handler guarantees the daemon is
    responsive before we hit ``localhost:3678``. SIGCONT is a no-op if the
    process is already running, so this is cheap in the common case.
    """
    pid: int | None = _read_pid()
    if pid is None or not _process_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGCONT)
    except (ProcessLookupError, PermissionError):
        # Race or perm issue — caller's subsequent HTTP call will surface a
        # real error if the daemon is actually unreachable.
        pass


def stop() -> None:
    """Terminate the go-librespot subprocess, if running."""
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


def _write_config(device_name: str) -> None:
    """Render config.yml for go-librespot.

    ``volume_steps: 100`` makes the HTTP API's ``volume`` field a direct
    0-100 percent — no mapping math in the local client.
    """
    cfg_dir: Path = _go_librespot_config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)

    yaml: str = (
        f"device_name: {device_name}\n"
        "device_type: speaker\n"
        "audio_backend: pulseaudio\n"
        "bitrate: 320\n"
        "volume_steps: 100\n"
        "initial_volume: 100\n"
        "normalisation_disabled: false\n"
        "log_level: info\n"
        "credentials:\n"
        "  type: zeroconf\n"
        "server:\n"
        "  enabled: true\n"
        f"  address: {API_HOST}\n"
        f"  port: {API_PORT}\n"
    )
    _config_yaml_path().write_text(yaml)


def _build_argv(bin_path: Path) -> list[str]:
    return [str(bin_path), "--config_dir", str(_go_librespot_config_dir())]


def start(device_name: str, env: dict[str, str] | None = None) -> int:
    """Start go-librespot. Idempotent — returns existing PID if running.

    Downloads the binary on first call if it isn't installed yet.
    ``env`` should normally come from ``BluetoothAudio.playback_env()`` so
    PulseAudio routes audio to a connected Bluetooth speaker; the env is
    captured at process start, so a connect/disconnect after start requires
    a ``restart()`` to take effect.
    """
    existing_pid: int | None = _read_pid()
    if existing_pid is not None and _process_alive(existing_pid):
        return existing_pid

    try:
        bin_path: Path = installer.ensure_installed()
    except RuntimeError as e:
        raise GoLibrespotMissingError(str(e)) from e

    _write_config(device_name)

    proc_env: dict[str, str] = dict(env) if env is not None else _default_env()
    _log_path().parent.mkdir(parents=True, exist_ok=True)

    argv: list[str] = _build_argv(bin_path)

    log_handle = open(_log_path(), "ab", buffering=0)  # noqa: SIM115 — handle owned by child
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
        raise RuntimeError(f"go-librespot binary not executable: {e}") from e

    _write_pid(proc.pid)
    logger.info("go-librespot started", pid=proc.pid, device_name=device_name)
    return proc.pid


def restart(device_name: str, env: dict[str, str] | None = None) -> int:
    """Stop + start. Used when BT routing changes (BT speaker connect/disconnect)."""
    stop()
    return start(device_name=device_name, env=env)


def reset_pairing() -> None:
    """Wipe cached credentials so the user can re-pair from scratch."""
    cfg_dir: Path = _go_librespot_config_dir()
    if cfg_dir.exists():
        shutil.rmtree(cfg_dir, ignore_errors=True)


def has_cached_credentials() -> bool:
    """Whether go-librespot has post-pairing credentials cached.

    go-librespot writes ``state.json`` to its ``--config_dir`` after the
    user pairs from their phone's Spotify app. Presence of that file =
    pairing already happened and future launches skip the discovery step.
    """
    return _credentials_path().is_file()


def daemon_log_tail(lines: int = 20) -> str:
    """Read the last N lines of the daemon's log for diagnostics."""
    log_path: Path = _log_path()
    if not log_path.exists():
        return ""
    try:
        text: str = log_path.read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
