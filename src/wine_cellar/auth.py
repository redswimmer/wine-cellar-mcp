"""Bearer-token check in front of the MCP app.

The token decides which user the request acts for. The resolved user ID is placed in the ASGI
scope's `state`, where tools read it through the request (see `server.current_user`).
"""

import json
from collections.abc import Callable

from starlette.types import ASGIApp, Receive, Scope, Send

USER_ID_STATE_KEY = "user_id"


class BearerAuthMiddleware:
    """Rejects any HTTP request without a token that resolves to a user."""

    def __init__(self, app: ASGIApp, resolve_user: Callable[[str], str | None]) -> None:
        self._app = app
        self._resolve_user = resolve_user

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        scheme, _, token = headers.get("authorization", "").partition(" ")
        user_id = self._resolve_user(token) if scheme.lower() == "bearer" and token else None
        if user_id is None:
            body = json.dumps({"error": "unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})[USER_ID_STATE_KEY] = user_id
        await self._app(scope, receive, send)
