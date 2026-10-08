"""Smoke-test a deployed MCP endpoint: auth, tool list, and a save/search/get/delete round trip.

    uv run python scripts/smoke_test.py [--url URL] [--token-file PATH]

The token comes from $WINE_MCP_TOKEN, or from the token file (default ~/.config/wine-cellar/dev-token).
The test wine is deleted at the end, so no data is left behind. Exits non-zero if any check fails.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

EXPECTED_TOOLS = {"save_wine", "add_tasting", "search_wines", "list_wines", "get_wine", "delete_wine"}
TEST_WINE = {"producer": "Smoke Test Cellars", "name": "Smoke Test Red", "vintage": 2020, "verdict": "like"}

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}")
    if not ok:
        failures.append(name)


def payload(result) -> dict:
    if result.structured_content:
        return result.structured_content
    text = "".join(getattr(c, "text", "") for c in result.content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


async def round_trip(url: str, token: str) -> None:
    async with (
        create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http,
        streamable_http_client(url, http_client=http) as (read, write, *_),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        check("initialize", True, init.server_info.name)

        tools = (await session.list_tools()).tools
        names = {t.name for t in tools}
        user_params = [t.name for t in tools if any("user" in p for p in t.input_schema.get("properties", {}))]
        check("tools/list", names == EXPECTED_TOOLS and not user_params, ", ".join(sorted(names)))

        saved = await session.call_tool("save_wine", TEST_WINE)
        wine_id = payload(saved).get("wine", {}).get("wine_id")
        check("save_wine", not saved.is_error and bool(wine_id), wine_id or "")
        if not wine_id:
            return

        try:
            found = await session.call_tool("search_wines", {"query": TEST_WINE["producer"]})
            ids = [m.get("wine_id") for m in payload(found).get("matches", [])]
            check("search_wines", not found.is_error and wine_id in ids)

            got = await session.call_tool("get_wine", {"wine_id": wine_id})
            check("get_wine", not got.is_error and payload(got).get("wine", {}).get("wine_id") == wine_id)
        finally:
            deleted = await session.call_tool("delete_wine", {"wine_id": wine_id})
            check("delete_wine", not deleted.is_error)

        after = await session.call_tool("search_wines", {"query": TEST_WINE["producer"]})
        check("no test data left", wine_id not in [m.get("wine_id") for m in payload(after).get("matches", [])])


def unauthorized(url: str) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    for name, extra in [("no token -> 401", {}), ("bogus token -> 401", {"Authorization": "Bearer wct_bogus"})]:
        status = httpx2.post(url, json=body, headers={**headers, **extra}).status_code
        check(name, status == 401, str(status))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="https://mcp.wine-dev.redswimmer.com/mcp")
    parser.add_argument("--token-file", type=Path, default=Path("~/.config/wine-cellar/dev-token").expanduser())
    args = parser.parse_args()

    token = os.environ.get("WINE_MCP_TOKEN") or args.token_file.read_text().strip()
    if not token.startswith("wct_"):
        sys.exit("The token should start with wct_; check that the token file holds the token, not the user ID.")

    asyncio.run(round_trip(args.url, token))
    unauthorized(args.url)
    if failures:
        sys.exit(f"{len(failures)} check(s) failed: {', '.join(failures)}")
    print("All checks passed.")


if __name__ == "__main__":
    main()
