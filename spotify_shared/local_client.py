"""HTTP client for go-librespot's localhost API.

go-librespot exposes a REST API on ``localhost:3678`` while it runs. Every
playback action — play, pause, next, prev, volume, shuffle, repeat — is a
single HTTP round-trip to the local daemon, no Spotify cloud involved. This
is the whole architectural reason we moved off Spotify's Web API for control:
``localhost`` doesn't 502.

Search and metadata still go through the Spotify Web API (see ``web_client.py``)
because go-librespot doesn't expose those.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    import httpx
except ImportError:  # httpx is declared in jarvis_package.yaml; available at runtime
    httpx = None  # type: ignore[assignment]

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


logger = JarvisLogger(service="cmd.spotify.local")


# Module-level httpx.Client singleton — reused across every localhost call.
# Per-call httpx.request() was creating a new Client + connection pool each
# time, so heavy command usage (play/pause/next, etc.) compounded with the
# keepalive ticks built up unreleased state. One pooled client is the
# canonical pattern.
_client: "httpx.Client | None" = None


def _get_client() -> "httpx.Client":
    global _client
    if _client is None:
        if httpx is None:
            raise RuntimeError("httpx not available")
        # Per-call timeout overrides this default when supplied.
        _client = httpx.Client(timeout=5.0)
    return _client


class LocalAPIError(RuntimeError):
    """Non-2xx response from the go-librespot local API."""


class LocalAPIUnavailable(RuntimeError):
    """Daemon isn't reachable on localhost (not running / still starting)."""


@dataclass
class LocalStatus:
    """Subset of GET /status that we expose to callers."""

    stopped: bool
    paused: bool
    buffering: bool
    volume: int
    volume_steps: int
    repeat_track: bool
    repeat_context: bool
    shuffle_context: bool
    track_uri: str
    track_name: str
    track_artists: list[str]
    track_album: str


class LocalClient:
    """Thin wrapper over go-librespot's REST API."""

    # Fast endpoints (status / volume / shuffle / repeat / pause / resume) get
    # a short timeout — they're cheap. Track-transition endpoints (play / next
    # / prev) need a long timeout because the HTTP POST doesn't return until
    # go-librespot has fetched + decoded the next track, which on a Pi Zero
    # over the network can legitimately take 5-10s on a cold first track.
    _FAST_TIMEOUT_SECONDS: float = 3.0
    _SLOW_TIMEOUT_SECONDS: float = 15.0

    def __init__(self, base_url: str) -> None:
        if httpx is None:
            raise RuntimeError("httpx is required — declared in jarvis_package.yaml")
        self._base: str = base_url.rstrip("/")

    # -- Plumbing -----------------------------------------------------------

    def _request(
        self, method: str, path: str, *,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url: str = f"{self._base}{path}"
        try:
            resp = _get_client().request(
                method, url, json=json_body,
                timeout=timeout if timeout is not None else self._FAST_TIMEOUT_SECONDS,
            )
        except Exception as e:
            raise LocalAPIUnavailable(f"go-librespot unreachable at {url}: {e}") from e

        if not (200 <= resp.status_code < 300):
            raise LocalAPIError(
                f"go-librespot API {resp.status_code} on {method} {path}: "
                f"{resp.text[:200]}"
            )
        body: str = (resp.text or "").strip()
        if not body:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    # -- Readiness / status -------------------------------------------------

    def is_ready(self) -> bool:
        """Whether the daemon is up AND has post-pairing credentials.

        ``GET /`` returns ``playback_ready: true`` once go-librespot has a
        session attached (i.e. pairing has happened). Until then it's
        reachable but ``playback_ready: false`` — the user still needs to
        pick the device in their phone's Spotify app.
        """
        try:
            data = self._request("GET", "/")
        except (LocalAPIUnavailable, LocalAPIError):
            return False
        return bool(isinstance(data, dict) and data.get("playback_ready"))

    def status(self) -> LocalStatus:
        data = self._request("GET", "/status")
        if not isinstance(data, dict):
            raise LocalAPIError("GET /status returned non-object")
        track = data.get("track") or {}
        return LocalStatus(
            stopped=bool(data.get("stopped")),
            paused=bool(data.get("paused")),
            buffering=bool(data.get("buffering")),
            volume=int(data.get("volume") or 0),
            volume_steps=int(data.get("volume_steps") or 100),
            repeat_track=bool(data.get("repeat_track")),
            repeat_context=bool(data.get("repeat_context")),
            shuffle_context=bool(data.get("shuffle_context")),
            track_uri=str(track.get("uri") or ""),
            track_name=str(track.get("name") or ""),
            track_artists=[str(a) for a in (track.get("artist_names") or [])],
            track_album=str(track.get("album_name") or ""),
        )

    # -- Playback ----------------------------------------------------------

    def play(self, uri: str, *, skip_to_uri: str | None = None) -> None:
        """Start playing the given Spotify URI.

        ``uri`` can be any context-or-track URI: ``spotify:track:...``,
        ``spotify:album:...``, ``spotify:artist:...``, ``spotify:playlist:...``.
        ``skip_to_uri`` jumps to a specific track inside the context.

        Uses the slow timeout — ``POST /player/play`` blocks until go-librespot
        has loaded the first track, which on a Pi over the network can take
        5-10s on a cold start.
        """
        body: dict[str, Any] = {"uri": uri, "paused": False}
        if skip_to_uri:
            body["skip_to_uri"] = skip_to_uri
        self._request(
            "POST", "/player/play",
            json_body=body, timeout=self._SLOW_TIMEOUT_SECONDS,
        )

    def resume(self) -> None:
        self._request("POST", "/player/resume")

    def pause(self) -> None:
        self._request("POST", "/player/pause")

    def next(self) -> None:
        # Track-transition: needs the slow timeout, same reasoning as play().
        self._request(
            "POST", "/player/next",
            timeout=self._SLOW_TIMEOUT_SECONDS,
        )

    def prev(self) -> None:
        self._request(
            "POST", "/player/prev",
            timeout=self._SLOW_TIMEOUT_SECONDS,
        )

    # -- Volume ------------------------------------------------------------

    def set_volume(self, percent: int) -> None:
        """Set absolute volume. With ``volume_steps: 100`` in config.yml the
        value passed here IS the percent — no remapping needed."""
        clamped: int = max(0, min(100, percent))
        self._request("POST", "/player/volume", json_body={"volume": clamped})

    # -- Modes -------------------------------------------------------------

    def set_shuffle(self, on: bool) -> None:
        self._request(
            "POST", "/player/shuffle_context",
            json_body={"shuffle_context": bool(on)},
        )

    def set_repeat(self, mode: str) -> None:
        """Set repeat mode: 'off' | 'track' | 'context'.

        go-librespot has separate ``repeat_track`` and ``repeat_context``
        booleans rather than a single tri-state. Map them as a unit so
        callers don't have to know about the split.
        """
        if mode not in ("off", "track", "context"):
            raise ValueError(f"invalid repeat mode: {mode}")
        repeat_track: bool = mode == "track"
        repeat_context: bool = mode == "context"
        self._request(
            "POST", "/player/repeat_track",
            json_body={"repeat_track": repeat_track},
        )
        self._request(
            "POST", "/player/repeat_context",
            json_body={"repeat_context": repeat_context},
        )
