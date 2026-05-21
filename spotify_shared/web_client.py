"""Thin wrapper around the Spotify Web API for the operations we need.

All calls use the user's OAuth access token (Authorization: Bearer ...).
On 401 the caller should refresh the token via auth.refresh_access_token()
and retry once.

Only the surface we actually use is implemented — search, play, pause, skip,
prev, volume, shuffle, repeat, list devices, transfer playback.
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


logger = JarvisLogger(service="cmd.spotify.web")


API_BASE: str = "https://api.spotify.com/v1"


class SpotifyAuthError(RuntimeError):
    """Raised on 401 — caller should refresh the access token and retry once."""


class SpotifyAPIError(RuntimeError):
    """Generic API error (non-2xx, non-401)."""


@dataclass
class SpotifyDevice:
    id: str
    name: str
    type: str
    is_active: bool
    volume_percent: int | None


@dataclass
class CurrentTrack:
    name: str
    artists: list[str]
    album: str
    uri: str
    is_playing: bool


@dataclass
class SearchHit:
    """A search result that can be turned into a play request.

    `uri` is the track/album/playlist/artist Spotify URI. `kind` says which.
    `display` is a human-readable label for the response message.
    """
    uri: str
    kind: str  # "track" | "album" | "playlist" | "artist"
    display: str


class SpotifyClient:
    def __init__(self, access_token: str) -> None:
        if httpx is None:
            raise RuntimeError("httpx is required — declared in jarvis_package.yaml")
        self._token: str = access_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    # 5xx retry policy for Spotify Web API. 502/503 are typically transient
    # backend hiccups that a single retry recovers from. 504 is a gateway
    # timeout — Spotify's gateway already waited (and timed out) for its own
    # backend, so retrying just stacks 10-15s of latency on top with little
    # chance of success; treat 504 as a hard fail. 2 attempts (not 3) keeps
    # worst-case wait under ~25s for a fully-broken endpoint.
    _RETRIABLE_STATUSES: frozenset[int] = frozenset({500, 502, 503})
    _MAX_ATTEMPTS: int = 2
    _RETRY_BACKOFF_BASE: float = 0.4  # 0.4s before retry 2
    _REQUEST_TIMEOUT_SECONDS: float = 8.0

    def _request(
        self, method: str, path: str, *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        import time as _time

        url: str = f"{API_BASE}{path}"
        last_5xx_status: int | None = None
        last_5xx_text: str = ""

        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                resp = httpx.request(  # type: ignore[union-attr]
                    method, url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    timeout=self._REQUEST_TIMEOUT_SECONDS,
                )
            except Exception as e:
                # Network errors get the same retry treatment as 5xx — Spotify's
                # CDN occasionally resets connections.
                if attempt < self._MAX_ATTEMPTS:
                    _time.sleep(self._RETRY_BACKOFF_BASE * attempt)
                    continue
                raise SpotifyAPIError(f"network error: {e}") from e

            if resp.status_code == 401:
                raise SpotifyAuthError("access token rejected (401)")
            if resp.status_code == 204:
                return None
            if resp.status_code in self._RETRIABLE_STATUSES:
                last_5xx_status = resp.status_code
                last_5xx_text = resp.text[:200]
                if attempt < self._MAX_ATTEMPTS:
                    logger.warning(
                        "Spotify API transient 5xx; retrying",
                        method=method, path=path, attempt=attempt,
                        status=resp.status_code,
                    )
                    _time.sleep(self._RETRY_BACKOFF_BASE * attempt)
                    continue
                # Exhausted: fall through to error raise below
            if not (200 <= resp.status_code < 300):
                raise SpotifyAPIError(
                    f"Spotify API {resp.status_code}: {resp.text[:200]}"
                )
            # Endpoints like /pause and /play return 2xx with empty (or
            # whitespace-only) bodies. Don't trip on those — only attempt
            # to decode when there's actual content beyond whitespace.
            body: str = (resp.text or "").strip()
            if not body:
                return None
            try:
                return resp.json()
            except ValueError:
                # 2xx with non-JSON body: treat as success-with-no-payload
                # rather than raising, so callers like pause()/play() that
                # don't need a response don't see spurious "failed" errors.
                logger.warning(
                    "Spotify 2xx with non-JSON body — treating as success",
                    method=method, path=path, status=resp.status_code,
                    body_preview=body[:80],
                )
                return None

        # All attempts exhausted with retriable 5xx
        raise SpotifyAPIError(
            f"Spotify API {last_5xx_status} after {self._MAX_ATTEMPTS} attempts: {last_5xx_text}"
        )

    # -- User --

    def me(self) -> dict[str, Any]:
        result = self._request("GET", "/me")
        return result if isinstance(result, dict) else {}

    # -- User playlists --

    def list_user_playlists(self, *, max_pages: int = 4, page_size: int = 50) -> list[dict[str, str]]:
        """Return the authenticated user's playlists.

        Pagination: ``page_size`` items per request, capped at ``max_pages``
        (default 4 → up to 200 playlists). Spotify's API returns the user's
        own playlists plus playlists they follow.
        """
        out: list[dict[str, str]] = []
        path: str | None = f"/me/playlists?limit={page_size}"
        for _ in range(max_pages):
            if not path:
                break
            data = self._request("GET", path)
            if not isinstance(data, dict):
                break
            for item in data.get("items") or []:
                out.append({
                    "name": item.get("name", ""),
                    "uri": item.get("uri", ""),
                    "id": item.get("id", ""),
                })
            next_url: str | None = data.get("next")
            if next_url and next_url.startswith(API_BASE):
                path = next_url[len(API_BASE):]
            else:
                path = None
        return out

    def find_user_playlist(self, query: str) -> SearchHit | None:
        """Match a query against the user's own playlists by name.

        Resolution: exact match (case-insensitive) → prefix match. Substring
        matching is intentionally skipped because it's too greedy — would
        match playlists like "Coffee Shop Vibes" when the user wants the song
        "Coffee".
        """
        q: str = query.lower().strip()
        if not q:
            return None
        playlists: list[dict[str, str]] = self.list_user_playlists()

        for p in playlists:
            if p["name"].lower() == q:
                return SearchHit(uri=p["uri"], kind="playlist", display=p["name"])

        for p in playlists:
            if p["name"].lower().startswith(q):
                return SearchHit(uri=p["uri"], kind="playlist", display=p["name"])

        return None

    # -- Devices --

    def list_devices(self) -> list[SpotifyDevice]:
        data = self._request("GET", "/me/player/devices")
        if not isinstance(data, dict):
            return []
        out: list[SpotifyDevice] = []
        for d in data.get("devices", []):
            out.append(SpotifyDevice(
                id=d.get("id") or "",
                name=d.get("name") or "",
                type=d.get("type") or "",
                is_active=bool(d.get("is_active")),
                volume_percent=d.get("volume_percent"),
            ))
        return out

    def find_device(self, name: str) -> SpotifyDevice | None:
        target: str = name.lower()
        for d in self.list_devices():
            if d.name.lower() == target:
                return d
        return None

    def transfer_playback(self, device_id: str, *, play: bool = False) -> None:
        self._request(
            "PUT", "/me/player",
            json_body={"device_ids": [device_id], "play": play},
        )

    # -- Search --

    def search(self, query: str, *, limit: int = 5) -> SearchHit | None:
        """Search for a query and return the best hit.

        Resolution order:
          1. Artist (catalog)
          2. Track (catalog)
          3. Album (catalog)
          4. User's own playlist by name (exact / prefix match)

        Public/editorial playlists are intentionally not searched — they were
        noisy and rarely what the user wanted. Personal playlists are checked
        last so that common phrases like "play coffee" still find the song
        rather than a playlist named "Coffee Shop Vibes".
        """
        data = self._request(
            "GET", "/search",
            params={
                "q": query,
                "type": "artist,track,album",
                "limit": limit,
            },
        )
        if not isinstance(data, dict):
            return None

        artists = (data.get("artists") or {}).get("items") or []
        if artists:
            top = artists[0]
            return SearchHit(
                uri=top["uri"], kind="artist",
                display=top.get("name", "artist"),
            )

        tracks = (data.get("tracks") or {}).get("items") or []
        if tracks:
            top = tracks[0]
            artist_name: str = ""
            top_artists = top.get("artists") or []
            if top_artists:
                artist_name = top_artists[0].get("name", "")
            label: str = top.get("name", "track")
            if artist_name:
                label = f"{label} by {artist_name}"
            return SearchHit(uri=top["uri"], kind="track", display=label)

        albums = (data.get("albums") or {}).get("items") or []
        if albums:
            top = albums[0]
            artist_name = ""
            top_artists = top.get("artists") or []
            if top_artists:
                artist_name = top_artists[0].get("name", "")
            label = top.get("name", "album")
            if artist_name:
                label = f"{label} by {artist_name}"
            return SearchHit(uri=top["uri"], kind="album", display=label)

        return self.find_user_playlist(query)

    # -- Playback control --

    def play(self, *, device_id: str | None = None, hit: SearchHit | None = None) -> None:
        """Start or resume playback.

        With `hit`: starts playing that artist/album/playlist/track.
        Without `hit`: resumes whatever was playing (or no-op if nothing was).
        """
        params: dict[str, Any] = {}
        if device_id:
            params["device_id"] = device_id

        body: dict[str, Any] = {}
        if hit is not None:
            if hit.kind == "track":
                body["uris"] = [hit.uri]
            else:
                # artist/album/playlist all use context_uri
                body["context_uri"] = hit.uri

        self._request(
            "PUT", "/me/player/play",
            params=params or None,
            json_body=body or None,
        )

    def pause(self, *, device_id: str | None = None) -> None:
        self._request(
            "PUT", "/me/player/pause",
            params={"device_id": device_id} if device_id else None,
        )

    def next_track(self, *, device_id: str | None = None) -> None:
        self._request(
            "POST", "/me/player/next",
            params={"device_id": device_id} if device_id else None,
        )

    def previous_track(self, *, device_id: str | None = None) -> None:
        self._request(
            "POST", "/me/player/previous",
            params={"device_id": device_id} if device_id else None,
        )

    def set_volume(self, percent: int, *, device_id: str | None = None) -> None:
        # Spotify clamps to 0-100 anyway, but be explicit
        clamped: int = max(0, min(100, percent))
        params: dict[str, Any] = {"volume_percent": clamped}
        if device_id:
            params["device_id"] = device_id
        self._request("PUT", "/me/player/volume", params=params)

    def set_shuffle(self, on: bool, *, device_id: str | None = None) -> None:
        params: dict[str, Any] = {"state": "true" if on else "false"}
        if device_id:
            params["device_id"] = device_id
        self._request("PUT", "/me/player/shuffle", params=params)

    def set_repeat(self, mode: str, *, device_id: str | None = None) -> None:
        """mode: 'off' | 'track' | 'context'."""
        if mode not in ("off", "track", "context"):
            raise ValueError(f"invalid repeat mode: {mode}")
        params: dict[str, Any] = {"state": mode}
        if device_id:
            params["device_id"] = device_id
        self._request("PUT", "/me/player/repeat", params=params)

    def now_playing(self) -> CurrentTrack | None:
        data = self._request("GET", "/me/player/currently-playing")
        if not isinstance(data, dict) or "item" not in data:
            return None
        item = data.get("item") or {}
        artists: list[str] = [a.get("name", "") for a in (item.get("artists") or [])]
        album: str = (item.get("album") or {}).get("name", "")
        return CurrentTrack(
            name=item.get("name", ""),
            artists=artists,
            album=album,
            uri=item.get("uri", ""),
            is_playing=bool(data.get("is_playing")),
        )
