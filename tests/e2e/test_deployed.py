"""Smoke tests for a deployed endpoint: auth, the tool list, and a save/search/get/delete round trip."""

import json
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {"save_wine", "add_tasting", "search_wines", "list_wines", "get_wine", "delete_wine"}
PRODUCER = "Smoke Test Cellars"
TEST_WINE = {"producer": PRODUCER, "name": "Smoke Test Red", "vintage": 2020, "verdict": "like"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}},
}


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


async def search_ids(s: ClientSession, query: str) -> list[str]:
    return [m["wine_id"] for m in (await call(s, "search_wines", {"query": query}))["matches"]]


@pytest.mark.parametrize("auth", [None, "Bearer wct_bogus"], ids=["no-token", "bogus-token"])
async def test_rejects_requests_without_a_valid_token(url, auth):
    headers = {"Accept": "application/json, text/event-stream"}
    if auth:
        headers["Authorization"] = auth
    async with create_mcp_http_client(headers=headers) as http:
        resp = await http.post(url, json=INITIALIZE)
    assert resp.status_code == 401


async def test_lists_the_tools_with_no_user_parameter(url, token):
    async with session(url, token) as s:
        tools = (await s.list_tools()).tools
    assert {t.name for t in tools} == EXPECTED_TOOLS
    assert not [t.name for t in tools if any("user" in p for p in t.input_schema.get("properties", {}))]


async def test_save_search_get_delete_round_trip(url, token):
    async with session(url, token) as s:
        wine_id = (await call(s, "save_wine", TEST_WINE))["wine"]["wine_id"]
        try:
            assert wine_id in await search_ids(s, PRODUCER)
            assert (await call(s, "get_wine", {"wine_id": wine_id}))["wine"]["wine_id"] == wine_id
        finally:
            await call(s, "delete_wine", {"wine_id": wine_id})
        assert wine_id not in await search_ids(s, PRODUCER)
