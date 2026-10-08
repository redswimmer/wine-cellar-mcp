"""End-to-end tests against a deployed MCP endpoint. Not collected by default; run with `uv run pytest tests/e2e`.

The URL comes from $WINE_MCP_URL (default: dev). The token comes from $WINE_MCP_TOKEN, or from
$WINE_MCP_TOKEN_FILE (default ~/.config/wine-cellar/dev-token). Tests skip when no token is available.
"""

import os
from pathlib import Path

import pytest

DEFAULT_URL = "https://mcp.wine-dev.redswimmer.com/mcp"
DEFAULT_TOKEN_FILE = "~/.config/wine-cellar/dev-token"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def url() -> str:
    return os.environ.get("WINE_MCP_URL", DEFAULT_URL)


@pytest.fixture(scope="session")
def token() -> str:
    value = os.environ.get("WINE_MCP_TOKEN")
    if not value:
        path = Path(os.environ.get("WINE_MCP_TOKEN_FILE", DEFAULT_TOKEN_FILE)).expanduser()
        if not path.exists():
            pytest.skip(f"No token: set WINE_MCP_TOKEN or create {path}")
        value = path.read_text().strip()
    if not value.startswith("wct_"):
        pytest.fail("The token should start with wct_; check that the token file holds the token, not the user ID")
    return value
