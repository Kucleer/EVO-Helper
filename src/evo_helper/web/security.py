"""Loopback-only request security: same-origin CSRF protection or a local token.

Mutating requests must either come from the same origin (browser forms) or carry
an ``X-Evo-Helper-Token`` header matching the locally configured token.  The
service never binds beyond ``127.0.0.1``.
"""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin_allowed(origin: bytes | None, host: bytes | None) -> bool:
    if origin is None or host is None:
        return False
    host_text = host.decode("latin-1")
    origin_text = origin.decode("latin-1")
    return origin_text in {f"http://{host_text}", f"https://{host_text}"}


class LocalSecurityMiddleware:
    """ASGI middleware that enforces origin or local-token checks."""

    def __init__(self, app: ASGIApp, local_token: str) -> None:
        self.app = app
        self._local_token = local_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {key: value for key, value in scope.get("headers", [])}
            method = scope.get("method", "GET")
            if method in MUTATING_METHODS:
                origin = headers.get(b"origin")
                host = headers.get(b"host")
                token = headers.get(b"x-evo-helper-token", b"").decode("utf-8", "ignore")
                if token != self._local_token and not _origin_allowed(origin, host):
                    response = JSONResponse(
                        {"detail": "missing or invalid local token / origin"},
                        status_code=403,
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


__all__ = ["LocalSecurityMiddleware", "MUTATING_METHODS"]
