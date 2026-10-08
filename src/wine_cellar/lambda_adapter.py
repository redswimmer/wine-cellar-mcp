"""Runs the MCP ASGI app inside Lambda.

The MCP session manager's run() may only be entered once, so it can't use Mangum's per-invocation
lifespan. Instead it is started once per execution environment on a persistent event loop; Mangum
then serves each invocation on that same loop, and the loop is frozen between invocations.
"""

import asyncio
from typing import Any, cast

from mangum import Mangum
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.types import ASGIApp

# Strong references so the background tasks aren't garbage collected.
_background: set[asyncio.Task[None]] = set()


def make_handler(app: ASGIApp, session_manager: StreamableHTTPSessionManager) -> Mangum:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    started = asyncio.Event()

    async def _serve_forever() -> None:
        async with session_manager.run():
            started.set()
            await asyncio.Event().wait()

    _background.add(loop.create_task(_serve_forever()))
    loop.run_until_complete(started.wait())
    return Mangum(cast(Any, app), lifespan="off")
