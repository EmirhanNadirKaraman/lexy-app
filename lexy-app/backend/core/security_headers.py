"""HTTP security-headers middleware (S5).

Adds a conservative baseline of security headers to every response — including
error responses (4xx/5xx) and the SPA served from `frontend/dist`.

CSP rationale (tuned against the built SPA + the real YouTube embed, not guesses):
  * `dist/index.html` loads only EXTERNAL, same-origin app JS/CSS (no inline
    <script>) → `'self'` covers the app bundle.
  * `YoutubeEmbed.tsx` injects `https://www.youtube.com/iframe_api` at runtime,
    which in turn loads YouTube's widget player JS from `www.youtube.com` /
    `s.ytimg.com`. Those origins are in `script-src` or the player won't load.
  * The React app sets inline `style=` attributes everywhere (no CSS
    framework), so `style-src 'unsafe-inline'` is required or the UI breaks.
  * The YouTube player runs in an iframe → `frame-src` allows the two YouTube
    origins. Thumbnails come from i.ytimg.com over https → `img-src https:`.
  * The frontend only ever calls its own origin (`apiUrl` resolves to a
    same-origin `/api/...` path in the web/PWA build), so `connect-src 'self'`
    covers fetch + EventSource (SSE notifications) without opening exfil paths.
    (The YT player talks to its iframe via postMessage, which CSP doesn't gate.)
  * `dist/sw.js` registers a service worker; `worker-src` falls back to
    `script-src`, so the SW is allowed without an explicit directive.

HSTS is opt-in via `ENABLE_HSTS` (off by default so local HTTP dev isn't
pinned to HTTPS). Flip it on behind TLS in production.
"""
from __future__ import annotations

import os

from starlette.middleware.base import BaseHTTPMiddleware

# Single-line CSP. See module docstring for the rationale behind each source.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' https://www.youtube.com https://s.ytimg.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-src https://www.youtube.com https://www.youtube-nocookie.com; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

# Headers added to every response regardless of environment.
_STATIC_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}

HSTS_VALUE = "max-age=31536000; includeSubDomains"


def _hsts_enabled() -> bool:
    return os.getenv("ENABLE_HSTS", "").lower() in ("1", "true", "yes")


def build_security_headers(*, hsts_enabled: bool | None = None) -> dict[str, str]:
    """Return the security-header set. HSTS is included only when enabled.

    `hsts_enabled=None` (the default) reads the `ENABLE_HSTS` env var at call
    time, so production can flip it without a code change and tests can toggle
    it via monkeypatch / explicit argument.
    """
    if hsts_enabled is None:
        hsts_enabled = _hsts_enabled()
    headers = dict(_STATIC_HEADERS)
    if hsts_enabled:
        headers["Strict-Transport-Security"] = HSTS_VALUE
    return headers


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Set baseline security headers on every outgoing response.

    Uses `setdefault` so a route that deliberately sets one of these headers
    (e.g. a custom CSP for a specific response) is not overridden.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for key, value in build_security_headers().items():
            response.headers.setdefault(key, value)
        return response
