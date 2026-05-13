"""Auto-install spotifyd binary from GitHub releases.

We don't ship a binary in the package (Pantry packages are pip-only). Instead,
we detect the host platform on first use and download the official spotifyd
release for that platform into a managed directory.

Linux variants use the "full" build because it includes the PulseAudio backend
that BluetoothAudio.playback_env() relies on. macOS uses CoreAudio so the
default build is fine.

The spotifyd v0.4.x Linux binaries are dynamically linked against
``libssl.so.1.1``/``libcrypto.so.1.1``, which Debian 12+/Ubuntu 22.04+ no
longer ship by default (they have ``libssl.so.3``). To stay zero-touch we
also fetch the libssl1.1 .deb from a Debian snapshot and extract the .so
files into the same managed bin dir; the daemon manager points
``LD_LIBRARY_PATH`` at it when launching spotifyd.
"""

from __future__ import annotations

import platform
import shutil
import stat
import subprocess
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


SPOTIFYD_VERSION: str = "v0.4.2"
RELEASE_URL_TEMPLATE: str = (
    "https://github.com/Spotifyd/spotifyd/releases/download/"
    f"{SPOTIFYD_VERSION}/{{asset}}"
)


@dataclass
class _PlatformAsset:
    asset: str        # tarball filename in the release
    archive_member: str  # path inside the tarball pointing at the binary


def _detect_asset() -> _PlatformAsset | None:
    """Map (system, machine) → spotifyd release asset.

    Linux uses "full" builds (PulseAudio + ALSA + Rodio) so BluetoothAudio's
    PULSE_SINK env routing works. macOS uses "default" (CoreAudio).
    """
    system: str = platform.system().lower()
    machine: str = platform.machine().lower()

    if system == "linux":
        if machine in ("x86_64", "amd64"):
            return _PlatformAsset(
                "spotifyd-linux-x86_64-full.tar.gz", "spotifyd"
            )
        if machine in ("aarch64", "arm64"):
            return _PlatformAsset(
                "spotifyd-linux-aarch64-full.tar.gz", "spotifyd"
            )
        if machine.startswith("armv7") or machine == "armhf":
            return _PlatformAsset(
                "spotifyd-linux-armv7-full.tar.gz", "spotifyd"
            )
        # Pi Zero (armv6) has no prebuilt binary in v0.4.x — would need to
        # build from source. Most modern Pi nodes are armv7 or aarch64.

    if system == "darwin":
        if machine in ("arm64", "aarch64"):
            return _PlatformAsset(
                "spotifyd-macos-aarch64-default.tar.gz", "spotifyd"
            )
        if machine in ("x86_64", "amd64"):
            return _PlatformAsset(
                "spotifyd-macos-x86_64-default.tar.gz", "spotifyd"
            )

    return None


def install_dir() -> Path:
    """Where the spotifyd binary lives once installed."""
    return Path.home() / ".jarvis" / "spotify" / "bin"


def binary_path() -> Path:
    return install_dir() / "spotifyd"


def is_installed() -> bool:
    p: Path = binary_path()
    return p.exists() and p.is_file()


# --- libssl1.1 sidecar (Linux only) -----------------------------------------

_LIBSSL_DEB_URL_TEMPLATE: str = (
    "http://snapshot.debian.org/archive/debian/20240210T084752Z/"
    "pool/main/o/openssl/libssl1.1_1.1.1w-0+deb11u1_{arch}.deb"
)
_LIBCRYPTO_NEEDED: tuple[str, ...] = ("libssl.so.1.1", "libcrypto.so.1.1")


def _libssl_arch() -> str | None:
    """Return the Debian arch suffix for the libssl .deb, or None if N/A."""
    if platform.system().lower() != "linux":
        return None
    machine: str = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine.startswith("armv7") or machine == "armhf":
        return "armhf"
    return None


def _system_has_libssl11() -> bool:
    """Best-effort check whether the system already provides libssl.so.1.1."""
    try:
        result = subprocess.run(
            ["ldconfig", "-p"],
            capture_output=True, text=True, timeout=5.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return all(name in result.stdout for name in _LIBCRYPTO_NEEDED)


def libssl_dir() -> Path:
    """Where the bundled libssl1.1 .so files live (if installed)."""
    return install_dir() / "lib"


def needs_libssl_sidecar() -> bool:
    """Whether we need to download libssl1.1 alongside spotifyd."""
    if _libssl_arch() is None:
        return False  # macOS / unsupported — no sidecar needed
    if _system_has_libssl11():
        return False
    return True


def has_bundled_libssl() -> bool:
    bundled: Path = libssl_dir()
    return all((bundled / name).exists() for name in _LIBCRYPTO_NEEDED)


def ensure_libssl_sidecar() -> None:
    """Fetch libssl1.1 .deb from Debian snapshot and extract .so files.

    Idempotent — no-op if libs are already on disk or system already has them.
    Raises RuntimeError if the platform isn't supported.
    """
    if not needs_libssl_sidecar() or has_bundled_libssl():
        return

    arch: str | None = _libssl_arch()
    if arch is None:
        raise RuntimeError("libssl1.1 sidecar unavailable for this platform")

    deb_url: str = _LIBSSL_DEB_URL_TEMPLATE.format(arch=arch)
    logger.info("Downloading libssl1.1", url=deb_url)
    try:
        with urllib.request.urlopen(deb_url, timeout=120) as resp:
            deb_bytes: bytes = resp.read()
    except Exception as e:
        raise RuntimeError(f"Failed to download libssl1.1 .deb: {e}") from e

    libssl_dir().mkdir(parents=True, exist_ok=True)
    _extract_libs_from_deb(deb_bytes, libssl_dir())
    logger.info("libssl1.1 installed", path=str(libssl_dir()))


def _extract_libs_from_deb(deb_bytes: bytes, dest: Path) -> None:
    """Extract libssl.so.1.1 and libcrypto.so.1.1 from a .deb archive.

    A .deb is an ar archive containing data.tar.{gz,xz,zst} which holds the
    package files. We rely on ``ar`` being installed (it is on Debian/Ubuntu
    by default).
    """
    work_dir: Path = dest.parent / "_libssl_extract"
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    deb_path: Path = work_dir / "libssl.deb"
    deb_path.write_bytes(deb_bytes)

    try:
        # Extract data.tar.* from the .deb
        result = subprocess.run(
            ["ar", "x", str(deb_path)],
            cwd=work_dir, capture_output=True, text=True, timeout=30.0,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ar failed extracting .deb: {result.stderr or result.stdout}"
            )

        # Find the data tarball and extract the .so files we need
        data_tar: Path | None = None
        for candidate in ("data.tar.zst", "data.tar.xz", "data.tar.gz"):
            p: Path = work_dir / candidate
            if p.exists():
                data_tar = p
                break
        if data_tar is None:
            raise RuntimeError(".deb did not contain a data.tar.{zst,xz,gz}")

        # tarfile handles xz/gz; zstd needs an extra step
        if data_tar.suffix == ".zst":
            decompressed: Path = work_dir / "data.tar"
            zstd_result = subprocess.run(
                ["zstd", "-d", "-f", str(data_tar), "-o", str(decompressed)],
                capture_output=True, text=True, timeout=30.0,
            )
            if zstd_result.returncode != 0:
                raise RuntimeError(f"zstd decompress failed: {zstd_result.stderr}")
            data_tar = decompressed

        with tarfile.open(data_tar, "r:*") as tar:
            for member in tar.getmembers():
                base: str = Path(member.name).name
                if base in _LIBCRYPTO_NEEDED and member.isfile():
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        continue
                    (dest / base).write_bytes(extracted.read())
                # Also extract the symlinks pointing at the .so.1.1 if present
                # (for example libssl.so.1.1.0 → libssl.so.1.1) — not needed
                # for our case since we only need the runtime .so.1.1 files.
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def ensure_installed() -> Path:
    """Return the path to spotifyd, downloading + installing if missing.

    Also ensures libssl1.1 is available — bundling a sidecar copy if the
    system only has libssl.so.3 (Debian 12+, Ubuntu 22.04+).

    Raises RuntimeError on unsupported platforms or download failures.
    """
    # Make sure libssl1.1 is available before/after spotifyd download — order
    # doesn't matter, both run on first install.
    if needs_libssl_sidecar() and not has_bundled_libssl():
        ensure_libssl_sidecar()

    if is_installed():
        return binary_path()

    asset_info: _PlatformAsset | None = _detect_asset()
    if asset_info is None:
        raise RuntimeError(
            f"No spotifyd build available for {platform.system()} "
            f"{platform.machine()}. Please install spotifyd manually."
        )

    install_dir().mkdir(parents=True, exist_ok=True)
    download_url: str = RELEASE_URL_TEMPLATE.format(asset=asset_info.asset)
    archive_path: Path = install_dir() / asset_info.asset

    logger.info("Downloading spotifyd", url=download_url)
    try:
        with urllib.request.urlopen(download_url, timeout=120) as resp:
            archive_path.write_bytes(resp.read())
    except Exception as e:
        raise RuntimeError(f"Failed to download spotifyd: {e}") from e

    logger.info("Extracting spotifyd archive", path=str(archive_path))
    try:
        with tarfile.open(archive_path, "r:*") as tar:
            member = _find_binary_member(tar, asset_info.archive_member)
            if member is None:
                raise RuntimeError(
                    f"spotifyd binary not found inside {asset_info.asset}"
                )
            extracted_obj = tar.extractfile(member)
            if extracted_obj is None:
                raise RuntimeError("spotifyd archive member is not a regular file")
            binary_path().write_bytes(extracted_obj.read())
    finally:
        try:
            archive_path.unlink()
        except OSError:
            pass

    # chmod +x
    current_mode: int = binary_path().stat().st_mode
    binary_path().chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    logger.info("spotifyd installed", path=str(binary_path()))
    return binary_path()


def _find_binary_member(tar: tarfile.TarFile, expected_name: str) -> tarfile.TarInfo | None:
    """Find the spotifyd binary inside the tarball.

    Some releases flatten "spotifyd" at the top level; others nest it under a
    versioned dir. Walk the members and pick the first that ends with the
    expected basename and is a regular file.
    """
    for m in tar.getmembers():
        if not m.isfile():
            continue
        name: str = Path(m.name).name
        if name == expected_name:
            return m
    return None


def uninstall() -> None:
    """Remove the installed binary (used for tests / clean slate)."""
    install_root: Path = install_dir().parent
    if install_root.exists():
        shutil.rmtree(install_root, ignore_errors=True)
