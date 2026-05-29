"""OAuth token refresh for Spotify.

The mobile app handles the initial OAuth code exchange via AuthenticationConfig
(PKCE flow) and posts back the access_token + refresh_token, which the
command's store_auth_values() persists. After that, access tokens expire
hourly — this helper exchanges a refresh_token for a fresh access_token via
the Spotify token endpoint.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

try:
    import httpx
except ImportError:
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


logger = JarvisLogger(service="cmd.spotify.auth")


TOKEN_URL: str = "https://accounts.spotify.com/api/token"


# Module-level httpx.Client singleton — reused across token refresh calls.
# Each httpx.post() at module-level created a new Client with its own
# connection pool that wasn't released cleanly, accumulating ~5-25 MB per
# few minutes on the long-running keepalive-agent loop. Reusing one Client
# (lazy-init below) pools connections and lets GC reclaim response objects
# normally.
_client: "httpx.Client | None" = None


def _get_client() -> "httpx.Client":
    global _client
    if _client is None:
        if httpx is None:
            raise RuntimeError("httpx not available")
        _client = httpx.Client(timeout=15.0)
    return _client


def refresh_access_token(*, client_id: str, refresh_token: str) -> dict[str, Any] | None:
    """Exchange a refresh_token for a new access_token.

    Returns the raw token response dict on success, or None on failure.
    Spotify usually rotates the refresh_token; if the response has a new one,
    the caller should persist it.
    """
    if httpx is None:
        logger.error("httpx not installed — cannot refresh Spotify token")
        return None

    body: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    try:
        resp = _get_client().post(
            TOKEN_URL,
            content=urlencode(body),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except Exception as e:
        logger.error("Spotify token refresh network error", error=str(e))
        return None

    if not (200 <= resp.status_code < 300):
        logger.error(
            "Spotify token refresh failed",
            status=resp.status_code, body=resp.text[:200],
        )
        return None

    data = resp.json()
    if not isinstance(data, dict):
        return None
    return data
