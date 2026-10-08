"""Drives the real Lambda handler with API Gateway HTTP API (v2) events."""

import importlib
import json

import pytest

from wine_cellar.tokens import TokenStore
from wine_cellar.users import UserStore

HOST = "mcp.wine-dev.redswimmer.com"
TOKEN = ""  # set per test by the handler fixture


@pytest.fixture
def tokens(table):
    users, store = UserStore(table), TokenStore(table)
    return {name: store.create(users.create(name).user_id, f"{name}-claw") for name in ("andrew", "sam")}


@pytest.fixture
def handler(table, tokens, monkeypatch):
    global TOKEN
    TOKEN = tokens["andrew"]
    monkeypatch.setenv("TABLE_NAME", table.name)
    monkeypatch.setenv("ALLOWED_HOSTS", HOST)
    import wine_cellar.handler as mod

    return importlib.reload(mod).handler


def _event(body: dict, *, token: str | None = None, host: str = HOST) -> dict:
    token = TOKEN if token is None else token
    headers = {
        "host": host,
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-06-18",
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": "/mcp",
        "rawQueryString": "",
        "headers": headers,
        "requestContext": {
            "http": {"method": "POST", "path": "/mcp", "protocol": "HTTP/1.1", "sourceIp": "1.2.3.4", "userAgent": "t"},
            "domainName": host,
            "stage": "$default",
            "requestId": "r",
            "routeKey": "$default",
            "accountId": "1",
            "apiId": "a",
            "time": "",
            "timeEpoch": 0,
        },
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }


def _rpc(method: str, params: dict | None = None, id_: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def _call(handler, body, **kw):
    resp = handler(_event(body, **kw), None)
    try:
        return resp["statusCode"], json.loads(resp["body"])
    except (KeyError, ValueError):
        return resp["statusCode"], resp.get("body")


def test_rejects_missing_or_wrong_token(handler):
    assert _call(handler, _rpc("tools/list"), token="")[0] == 401
    assert _call(handler, _rpc("tools/list"), token="nope")[0] == 401
    assert _call(handler, _rpc("tools/list"), token="wct_unknown")[0] == 401


def test_rejects_unknown_host(handler):
    status, _ = _call(handler, _rpc("tools/list"), host="evil.example.com")
    assert status in (400, 421)


def test_initialize_and_list_tools(handler):
    status, body = _call(
        handler,
        _rpc(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
        ),
    )
    assert status == 200, body
    assert body["result"]["serverInfo"]["name"] == "wine-cellar"

    status, body = _call(handler, _rpc("tools/list"))
    assert status == 200, body
    names = {t["name"] for t in body["result"]["tools"]}
    assert names == {"save_wine", "add_tasting", "search_wines", "list_wines", "get_wine", "delete_wine"}


def test_save_then_search_over_http(handler):
    args = {"producer": "Ridge", "name": "Monte Bello", "vintage": 2019, "verdict": "love"}
    status, body = _call(handler, _rpc("tools/call", {"name": "save_wine", "arguments": args}))
    assert status == 200, body
    assert not body["result"].get("isError"), body
    assert body["result"]["structuredContent"]["created"] is True

    status, body = _call(
        handler,
        _rpc("tools/call", {"name": "search_wines", "arguments": {"query": "monte bello"}}),
    )
    matches = body["result"]["structuredContent"]["matches"]
    assert matches[0]["verdict"] == "love"
    assert matches[0]["label"] == "Ridge Monte Bello 2019"


def test_unknown_wine_is_a_tool_error(handler):
    status, body = _call(
        handler,
        _rpc("tools/call", {"name": "get_wine", "arguments": {"wine_id": "missing"}}),
    )
    assert status == 200
    assert body["result"]["isError"] is True


def test_tool_annotations_reach_the_client(handler):
    _, body = _call(handler, _rpc("tools/list"))
    tools = {t["name"]: t for t in body["result"]["tools"]}
    assert tools["search_wines"]["annotations"]["readOnlyHint"] is True
    assert tools["delete_wine"]["annotations"]["destructiveHint"] is True


def test_tools_do_not_ask_for_a_user_id(handler):
    _, body = _call(handler, _rpc("tools/list"))
    for tool in body["result"]["tools"]:
        props = tool["inputSchema"].get("properties", {})
        assert not any("user" in name or name == "ctx" for name in props), (tool["name"], list(props))


def test_token_decides_the_user(handler, tokens):
    save = _rpc("tools/call", {"name": "save_wine", "arguments": {"producer": "Ridge", "name": "Monte Bello"}})
    assert _call(handler, save)[0] == 200

    listing = _rpc("tools/call", {"name": "list_wines", "arguments": {}})
    _, mine = _call(handler, listing)
    assert mine["result"]["structuredContent"]["total"] == 1
    _, theirs = _call(handler, listing, token=tokens["sam"])
    assert theirs["result"]["structuredContent"]["total"] == 0


def test_deactivated_user_is_rejected(table, tokens, monkeypatch):
    sam = UserStore(table).find("sam")
    assert sam is not None
    UserStore(table).set_active(sam.user_id, False)
    monkeypatch.setenv("TABLE_NAME", table.name)
    monkeypatch.setenv("ALLOWED_HOSTS", HOST)
    import wine_cellar.handler as mod

    handler = importlib.reload(mod).handler
    assert _call(handler, _rpc("tools/list"), token=tokens["sam"])[0] == 401
    assert _call(handler, _rpc("tools/list"), token=tokens["andrew"])[0] == 200
