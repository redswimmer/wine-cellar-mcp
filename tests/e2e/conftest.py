"""End-to-end tests against a deployed MCP endpoint. Not collected by default; run with `uv run pytest tests/e2e`.

The URL comes from $WINE_MCP_URL (default: dev). The token comes from $WINE_MCP_TOKEN, or from
$WINE_MCP_TOKEN_FILE (default ~/.config/wine-cellar/dev-token). Tests skip when no token is available.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

DEFAULT_URL = "https://mcp.wine-dev.redswimmer.com/mcp"
DEFAULT_TOKEN_FILE = "~/.config/wine-cellar/dev-token"
NAME = "Smoke Test Cellars Red"  # every test wine's name starts with this


@asynccontextmanager
async def session(url: str, token: str):
    async with (
        create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http,
        streamable_http_client(url, http_client=http) as (read, write, *_),
        ClientSession(read, write) as s,
    ):
        await s.initialize()
        yield s


async def call(s: ClientSession, tool: str, args: dict) -> dict:
    result = await s.call_tool(tool, args)
    text = "".join(getattr(c, "text", "") for c in result.content)
    assert not result.is_error, f"{tool} failed: {text}"
    return result.structured_content or json.loads(text)


async def delete_leftover_test_wines(url: str, token: str) -> None:
    """Wines left by an earlier run that was cancelled before its cleanup, which would crowd search results."""
    async with session(url, token) as s:
        leftovers, next_token = [], None
        while True:
            page = await call(s, "list_wines", {"limit": 100} | ({"next_token": next_token} if next_token else {}))
            leftovers += [w["wine_id"] for w in page["wines"] if w["name"].startswith(NAME)]
            if not (next_token := page["next_token"]):
                break
        for wine_id in leftovers:
            await call(s, "delete_wine", {"wine_id": wine_id})


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def url() -> str:
    return os.environ.get("WINE_MCP_URL", DEFAULT_URL)


@pytest.fixture(scope="session")
def token(url: str) -> str:
    """The test user's token. Leftover test wines are deleted first."""
    value = os.environ.get("WINE_MCP_TOKEN")
    if not value:
        path = Path(os.environ.get("WINE_MCP_TOKEN_FILE", DEFAULT_TOKEN_FILE)).expanduser()
        if not path.exists():
            pytest.skip(f"No token: set WINE_MCP_TOKEN or create {path}")
        value = path.read_text().strip()
    if not value.startswith("wct_"):
        pytest.fail("The token should start with wct_; check that the token file holds the token, not the user ID")
    asyncio.run(delete_leftover_test_wines(url, value))
    return value
