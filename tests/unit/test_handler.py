"""Drives the real Lambda handler with API Gateway HTTP API (v2) events."""

import importlib
import json

import boto3
import pytest

TOKEN = "test-token"
HOST = "mcp.wine-dev.redswimmer.com"


@pytest.fixture
def handler(table, monkeypatch):
    secret = boto3.client("secretsmanager", region_name="us-east-1").create_secret(
        Name="wine-token", SecretString=TOKEN
    )
    monkeypatch.setenv("TABLE_NAME", table.name)
    monkeypatch.setenv("TOKEN_SECRET_ARN", secret["ARN"])
    monkeypatch.setenv("ALLOWED_HOSTS", HOST)
    import wine_cellar.handler as mod

    return importlib.reload(mod).handler


def _event(body: dict, *, token: str | None = TOKEN, host: str = HOST) -> dict:
    headers = {
        "host": host,
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-06-18",
    }
    if token is not None:
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
    assert _call(handler, _rpc("tools/list"), token=None)[0] == 401
    assert _call(handler, _rpc("tools/list"), token="nope")[0] == 401


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
    args = {"telegram_user_id": "42", "producer": "Ridge", "name": "Monte Bello", "vintage": 2019, "verdict": "love"}
    status, body = _call(handler, _rpc("tools/call", {"name": "save_wine", "arguments": args}))
    assert status == 200, body
    assert not body["result"].get("isError"), body
    assert body["result"]["structuredContent"]["created"] is True

    status, body = _call(
        handler,
        _rpc("tools/call", {"name": "search_wines", "arguments": {"telegram_user_id": "42", "query": "monte bello"}}),
    )
    matches = body["result"]["structuredContent"]["matches"]
    assert matches[0]["verdict"] == "love"
    assert matches[0]["label"] == "Ridge Monte Bello 2019"


def test_unknown_wine_is_a_tool_error(handler):
    status, body = _call(
        handler,
        _rpc("tools/call", {"name": "get_wine", "arguments": {"telegram_user_id": "42", "wine_id": "missing"}}),
    )
    assert status == 200
    assert body["result"]["isError"] is True


def test_tool_annotations_reach_the_client(handler):
    _, body = _call(handler, _rpc("tools/list"))
    tools = {t["name"]: t for t in body["result"]["tools"]}
    assert tools["search_wines"]["annotations"]["readOnlyHint"] is True
    assert tools["delete_wine"]["annotations"]["destructiveHint"] is True
