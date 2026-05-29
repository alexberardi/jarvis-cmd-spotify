"""Spotify Web API client — search and user-metadata only.

All playback control (play/pause/next/prev/volume/shuffle/repeat) now goes
through the go-librespot localhost API in ``local_client.py``. Spotify's
Web API stays in the picture for two read-only jobs: searching the catalog
to resolve a voice query to a URI, and reading the authenticated user's own
playlists. Both of those endpoints are reliable; the playback-control
endpoints were the source of the 5xx pain.

All calls use the user's OAuth access token (Authorization: Bearer ...).
On 401 the caller should refresh the token via ``auth.refresh_access_token()``
and retry once.
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


# Module-level httpx.Client singleton — reused across all Web API calls.
# Per-call httpx.request() was creating a new Client + connection pool on
# every search/playlist call, accumulating memory across the long-running
# keepalive-agent + per-voice-command paths. One pooled client is the
# canonical pattern.
_client: "httpx.Client | None" = None


def _get_client() -> "httpx.Client":
    global _client
    if _client is None:
        if httpx is None:
            raise RuntimeError("httpx not available")
        _client = httpx.Client(timeout=10.0)
    return _client


API_BASE: str = "https://api.spotify.com/v1"


class SpotifyAuthError(RuntimeError):
    """Raised on 401 — caller should refresh the access token and retry once."""


class SpotifyAPIError(RuntimeError):
    """Generic API error (non-2xx, non-401)."""


@dataclass
class SearchHit:
    """A search result that can be turned into a play request.

    ``uri`` is the track/album/playlist/artist Spotify URI. ``kind`` says
    which. ``display`` is a human-readable label for the response message.
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

    # 5xx retry policy. Search endpoints rarely hiccup in practice but 502/503
    # do happen occasionally. 504 = gateway already gave up, retrying just
    # stacks latency — treat as a hard fail. 2 attempts caps worst-case wait
    # under ~25s when fully broken.
    _RETRIABLE_STATUSES: frozenset[int] = frozenset({500, 502, 503})
    _MAX_ATTEMPTS: int = 2
    _RETRY_BACKOFF_BASE: float = 0.4
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
                resp = _get_client().request(
                    method, url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    timeout=self._REQUEST_TIMEOUT_SECONDS,
                )
            except Exception as e:
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
            if not (200 <= resp.status_code < 300):
                raise SpotifyAPIError(
                    f"Spotify API {resp.status_code}: {resp.text[:200]}"
                )
            body: str = (resp.text or "").strip()
            if not body:
                return None
            try:
                return resp.json()
            except ValueError:
                logger.warning(
                    "Spotify 2xx with non-JSON body — treating as success",
                    method=method, path=path, status=resp.status_code,
                    body_preview=body[:80],
                )
                return None

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
        (default 4 → up to 200 playlists). Spotify returns the user's own
        playlists plus playlists they follow.
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
        match playlists like "Coffee Shop Vibes" when the user wants the
        song "Coffee".
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

    # -- Catalog search --

    def search(self, query: str, *, limit: int = 5) -> SearchHit | None:
        """Search the Spotify catalog and return the best hit.

        Resolution order:
          1. Artist (catalog)
          2. Track (catalog)
          3. Album (catalog)
          4. User's own playlist by name (exact / prefix match)

        Public/editorial playlists are intentionally not searched — they
        were noisy and rarely what the user wanted. Personal playlists are
        checked last so that common phrases like "play coffee" still find
        the song rather than a playlist named "Coffee Shop Vibes".
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
