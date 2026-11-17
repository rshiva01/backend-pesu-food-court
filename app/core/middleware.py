from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Payment provider webhooks are server-to-server: no browser headers, verified by
# HMAC signature instead (milestone 5).
CSRF_EXEMPT_PREFIXES = ("/api/v1/payments/",)

Call = Callable[[Request], Awaitable[Response]]


class CsrfMiddleware(BaseHTTPMiddleware):
    """Cookies are SameSite=Strict; on top of that, every state-changing request
    must carry X-Requested-With (a header cross-site forms can't set) and, when
    the browser sends Origin, it must be one of ours."""

    async def dispatch(self, request: Request, call_next: Call) -> Response:
        if request.method not in SAFE_METHODS and not request.url.path.startswith(
            CSRF_EXEMPT_PREFIXES
        ):
            if request.headers.get("x-requested-with") != "fetch":
                return JSONResponse({"detail": "Forbidden"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin not in get_settings().allowed_origins:
                return JSONResponse({"detail": "Forbidden"}, status_code=403)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """API responses are JSON only; Caddy adds HSTS/CSP for the whole site."""

    async def dispatch(self, request: Request, call_next: Call) -> Response:
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Cache-Control", "no-store")
        h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        return resp
