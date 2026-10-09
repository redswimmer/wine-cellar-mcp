"""Smoke tests for a deployed endpoint: auth, the tool list, list paging, and save/search/get/delete round trips (text and photo)."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "create_wine",
    "update_wine",
    "add_photo",
    "delete_photo",
    "upload_search_photo",
    "search_wines",
    "list_wines",
    "get_wine",
    "delete_wine",
}
LABEL = Path(__file__).with_name("label.jpg")
NAME = "Smoke Test Cellars Red"
TEST_WINE = {"name": NAME, "vintage": 2020, "verdict": "like"}
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


async def test_create_update_search_delete_round_trip(url, token):
    async with session(url, token) as s:
        wine_id = (await call(s, "create_wine", TEST_WINE))["wine"]["wine_id"]
        try:
            assert wine_id in await search_ids(s, NAME)
            await call(s, "update_wine", {"wine_id": wine_id, "verdict": "love"})
            assert (await call(s, "get_wine", {"wine_id": wine_id}))["wine"]["verdict"] == "love"
        finally:
            await call(s, "delete_wine", {"wine_id": wine_id})
        assert wine_id not in await search_ids(s, NAME)


async def test_list_pages_with_next_token(url, token):
    async with session(url, token) as s:
        ids = [(await call(s, "create_wine", {"name": f"{NAME} {i}"}))["wine"]["wine_id"] for i in range(3)]
        try:
            seen, next_token = [], None
            for _ in ids:
                page = await call(s, "list_wines", {"limit": 1} | ({"next_token": next_token} if next_token else {}))
                seen += [w["wine_id"] for w in page["wines"]]
                next_token = page["next_token"]
            assert seen == ids[::-1]  # newest first
        finally:
            for wine_id in ids:
                await call(s, "delete_wine", {"wine_id": wine_id})


async def put_label(upload_url: str) -> None:
    async with create_mcp_http_client() as http:
        resp = await http.put(upload_url, content=LABEL.read_bytes())
    assert resp.status_code == 200, resp.text


async def test_add_photo_then_search_by_photo(url, token):
    async with session(url, token) as s:
        wine_id = (await call(s, "create_wine", TEST_WINE))["wine"]["wine_id"]
        try:
            await put_label((await call(s, "add_photo", {"wine_id": wine_id}))["upload_url"])
            for _ in range(20):  # the upload event records the photo a few seconds later
                if (await call(s, "get_wine", {"wine_id": wine_id}))["wine"]["photos"]:
                    break
                await asyncio.sleep(1)
            else:
                pytest.fail("photo was never saved to the wine")

            search = await call(s, "upload_search_photo", {})
            await put_label(search["upload_url"])
            matches = (await call(s, "search_wines", {"photo_ref": search["photo_ref"]}))["matches"]
            assert matches and matches[0]["wine_id"] == wine_id
        finally:
            await call(s, "delete_wine", {"wine_id": wine_id})


async def test_unuploaded_photo_ref_is_a_clear_error(url, token):
    async with session(url, token) as s:
        ref = (await call(s, "upload_search_photo", {}))["photo_ref"]
        result = await s.call_tool("search_wines", {"photo_ref": ref})
    assert result.is_error
    assert "upload again" in "".join(getattr(c, "text", "") for c in result.content)
