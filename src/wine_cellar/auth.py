"""Bearer-token check in front of the MCP app."""

import hmac
import json
from collections.abc import Callable

from starlette.types import ASGIApp, Receive, Scope, Send


class BearerAuthMiddleware:
    """Rejects any HTTP request whose Authorization header doesn't carry the expected token."""

    def __init__(self, app: ASGIApp, token_provider: Callable[[], str]) -> None:
        self._app = app
        self._token_provider = token_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        supplied = headers.get("authorization", "")
        expected = f"Bearer {self._token_provider()}"
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
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
        await self._app(scope, receive, send)
