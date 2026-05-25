"""Auto-install the go-librespot binary from GitHub releases.

We bundle the playback layer ourselves rather than installing via apt because
there's no official apt repo for go-librespot — the only deliverable shape
upstream ships is the GitHub release tarball.

go-librespot is statically linked, so there's no libssl shim to worry about
(unlike the spotifyd v0.4 binaries this package used to ship, which needed a
libssl1.1 sidecar on Debian 12+). The whole installer is just: pick the right
asset for this platform, download, extract, chmod +x.

macOS dev machines: no Linux binary applies. We expect ``go-librespot`` to be
on ``PATH`` (via ``brew install go-librespot``) and return that path instead
of trying to download — the brew binary is the supported macOS path upstream.
"""

from __future__ import annotations

import platform
import shutil
import stat
import tarfile
import urllib.request
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


logger = JarvisLogger(service="cmd.spotify.installer")


GO_LIBRESPOT_VERSION: str = "v0.7.2"
RELEASE_URL_TEMPLATE: str = (
    "https://github.com/devgianlu/go-librespot/releases/download/"
    f"{GO_LIBRESPOT_VERSION}/{{asset}}"
)


@dataclass
class _PlatformAsset:
    asset: str            # tarball filename in the release
    archive_member: str   # basename of the binary inside the tarball


def _is_raspberry_pi() -> bool:
    """True if /proc/cpuinfo identifies this kernel as running on a Pi.

    On armv6 there are two upstream tarballs — a generic ``linux_armv6`` and
    a Pi-tuned ``linux_armv6_rpi`` (uses VFP / hardware floating point). The
    rpi variant is what Pi Zero / Pi 1 want.
    """
    try:
        info: str = Path("/proc/cpuinfo").read_text(errors="replace")
    except OSError:
        return False
    lowered: str = info.lower()
    return "raspberry pi" in lowered or "bcm" in lowered


def _detect_asset() -> _PlatformAsset | None:
    """Map (system, machine) → go-librespot release asset.

    Returns None for darwin — we rely on a Homebrew-installed binary on macOS.
    """
    system: str = platform.system().lower()
    machine: str = platform.machine().lower()

    if system != "linux":
        return None

    if machine in ("x86_64", "amd64"):
        return _PlatformAsset("go-librespot_linux_x86_64.tar.gz", "go-librespot")
    if machine in ("aarch64", "arm64"):
        return _PlatformAsset("go-librespot_linux_arm64.tar.gz", "go-librespot")
    if machine.startswith("armv6") or machine == "armhf":
        # Pi Zero / Pi 1 fall here. Prefer the rpi-tuned variant when we can
        # tell it's a Pi; the plain armv6 build is the safe fallback for
        # other armv6 boards.
        if _is_raspberry_pi():
            return _PlatformAsset(
                "go-librespot_linux_armv6_rpi.tar.gz", "go-librespot",
            )
        return _PlatformAsset(
            "go-librespot_linux_armv6.tar.gz", "go-librespot",
        )
    if machine.startswith("armv7"):
        # No dedicated armv7 build in the release; armv6 is forward-compatible
        # (a Pi 2/3 running 32-bit OS will still execute armv6 code).
        return _PlatformAsset(
            "go-librespot_linux_armv6.tar.gz", "go-librespot",
        )

    return None


def install_dir() -> Path:
    """Where the bundled binary lives once installed."""
    return Path.home() / ".jarvis" / "spotify" / "bin"


def bundled_binary_path() -> Path:
    return install_dir() / "go-librespot"


def binary_path() -> Path | None:
    """Resolve the go-librespot binary path, preferring our bundled copy.

    Order:
      1. ``$JARVIS_SPOTIFY_BIN`` env override (for testing / pinning)
      2. Our bundled copy at ``~/.jarvis/spotify/bin/go-librespot``
      3. Whatever ``shutil.which`` finds on PATH (brew-installed on macOS)
    """
    import os

    override: str | None = os.environ.get("JARVIS_SPOTIFY_BIN")
    if override:
        p: Path = Path(override)
        if p.exists():
            return p

    bundled: Path = bundled_binary_path()
    if bundled.exists() and bundled.is_file():
        return bundled

    located: str | None = shutil.which("go-librespot")
    if located:
        return Path(located)

    return None


def is_installed() -> bool:
    return binary_path() is not None


def ensure_installed() -> Path:
    """Return the path to go-librespot, downloading + installing if missing.

    On Linux: downloads the right tarball for this platform and extracts the
    binary into ``install_dir()``. On macOS: looks for a brew-installed
    binary on PATH; raises if missing.

    Raises RuntimeError on unsupported platforms or download failures.
    """
    existing: Path | None = binary_path()
    if existing is not None:
        return existing

    if platform.system().lower() == "darwin":
        raise RuntimeError(
            "go-librespot is not installed. On macOS install it via Homebrew:\n"
            "  brew install go-librespot"
        )

    asset_info: _PlatformAsset | None = _detect_asset()
    if asset_info is None:
        raise RuntimeError(
            f"No go-librespot build available for {platform.system()} "
            f"{platform.machine()}."
        )

    install_dir().mkdir(parents=True, exist_ok=True)
    download_url: str = RELEASE_URL_TEMPLATE.format(asset=asset_info.asset)
    archive_path: Path = install_dir() / asset_info.asset

    logger.info("Downloading go-librespot", url=download_url)
    try:
        with urllib.request.urlopen(download_url, timeout=120) as resp:
            archive_path.write_bytes(resp.read())
    except Exception as e:
        raise RuntimeError(f"Failed to download go-librespot: {e}") from e

    logger.info("Extracting go-librespot archive", path=str(archive_path))
    try:
        with tarfile.open(archive_path, "r:*") as tar:
            member = _find_binary_member(tar, asset_info.archive_member)
            if member is None:
                raise RuntimeError(
                    f"go-librespot binary not found inside {asset_info.asset}"
                )
            extracted_obj = tar.extractfile(member)
            if extracted_obj is None:
                raise RuntimeError("go-librespot archive member is not a regular file")
            bundled_binary_path().write_bytes(extracted_obj.read())
    finally:
        try:
            archive_path.unlink()
        except OSError:
            pass

    current_mode: int = bundled_binary_path().stat().st_mode
    bundled_binary_path().chmod(
        current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
    )

    logger.info("go-librespot installed", path=str(bundled_binary_path()))
    return bundled_binary_path()


def _find_binary_member(
    tar: tarfile.TarFile, expected_name: str,
) -> tarfile.TarInfo | None:
    """Find the go-librespot binary inside the tarball.

    Releases ship the binary at the archive root (no nested versioned dir),
    but walk all members to stay tolerant of layout changes.
    """
    for m in tar.getmembers():
        if not m.isfile():
            continue
        if Path(m.name).name == expected_name:
            return m
    return None


def uninstall() -> None:
    """Remove the bundled binary (used for tests / clean slate)."""
    install_root: Path = install_dir().parent
    if install_root.exists():
        shutil.rmtree(install_root, ignore_errors=True)
