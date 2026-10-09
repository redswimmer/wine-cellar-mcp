"""Drives the real Lambda handler with API Gateway HTTP API (v2) events."""

import json
from urllib.parse import unquote, urlparse

import pytest

from wine_cellar.bootstrap import build_mcp_handler
from wine_cellar.config import Settings
from wine_cellar.lambda_adapter import make_handler
from wine_cellar.models import photo_id_of
from wine_cellar.photos import MAX_PHOTO_BYTES
from wine_cellar.server import build_app
from wine_cellar.tokens import TokenStore
from wine_cellar.users import UserStore

from .conftest import BUCKET, JPEG

HOST = "mcp.wine-dev.redswimmer.com"


@pytest.fixture
def tokens(table):
    users, store = UserStore(table), TokenStore(table)
    return {name: store.create(users.create(name).user_id, f"{name}-claw") for name in ("andrew", "sam")}


@pytest.fixture
def call(service, table, tokens):
    """Calls the MCP handler over the fully wired test service, as andrew unless another token is given."""
    return _client(make_handler(*build_app(service, TokenStore(table).resolve, [HOST])), tokens["andrew"])


def _bootstrapped(table, token: str):
    """The handler exactly as bootstrap builds it from settings. Without a vector bucket, photos are off."""
    return _client(build_mcp_handler(Settings(table_name=table.name, allowed_hosts=[HOST]), table), token)


def _client(handler, default_token: str):
    def call(body: dict, *, token: str | None = None, host: str = HOST) -> tuple[int, dict]:
        resp = handler(_event(body, token=default_token if token is None else token, host=host), None)
        try:
            return resp["statusCode"], json.loads(resp["body"])
        except (KeyError, ValueError):
            return resp["statusCode"], resp.get("body")

    return call


def _event(body: dict, *, token: str, host: str) -> dict:
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


def _tool(name: str, arguments: dict) -> dict:
    return _rpc("tools/call", {"name": name, "arguments": arguments})


def _ok(call, name: str, arguments: dict) -> dict:
    status, body = call(_tool(name, arguments))
    assert status == 200, body
    assert not body["result"].get("isError"), body
    return body["result"]["structuredContent"]


def test_rejects_missing_or_wrong_token(call):
    assert call(_rpc("tools/list"), token="")[0] == 401
    assert call(_rpc("tools/list"), token="nope")[0] == 401
    assert call(_rpc("tools/list"), token="wct_unknown")[0] == 401


def test_rejects_unknown_host(call):
    status, _ = call(_rpc("tools/list"), host="evil.example.com")
    assert status in (400, 421)


def test_initialize_and_list_tools(call):
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
    status, body = call(_rpc("initialize", init))
    assert status == 200, body
    assert body["result"]["serverInfo"]["name"] == "wine-cellar"

    status, body = call(_rpc("tools/list"))
    assert status == 200, body
    names = {t["name"] for t in body["result"]["tools"]}
    assert names == {
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


def test_create_update_then_search_over_http(call):
    wine_id = _ok(call, "create_wine", {"name": "Ridge Monte Bello", "vintage": 2019, "verdict": "like"})["wine"][
        "wine_id"
    ]
    updated = _ok(call, "update_wine", {"wine_id": wine_id, "verdict": "love", "vintage": None})["wine"]
    assert (updated["verdict"], updated["vintage"], updated["name"]) == ("love", None, "Ridge Monte Bello")

    matches = _ok(call, "search_wines", {"query": "monte bello"})["matches"]
    assert matches[0]["wine_id"] == wine_id
    assert matches[0]["label"] == "Ridge Monte Bello"


def test_wine_photos_are_listed_and_can_be_deleted(call, service, s3):
    wine_id = _ok(call, "create_wine", {"name": "Ridge"})["wine"]["wine_id"]
    upload = _ok(call, "add_photo", {"wine_id": wine_id})
    assert (upload["method"], upload["max_bytes"]) == ("PUT", MAX_PHOTO_BYTES)
    # What happens after the agent PUTs to the link: the file lands at the link's key and the S3 event records it.
    key = unquote(urlparse(upload["upload_url"]).path.removeprefix(f"/{BUCKET}").lstrip("/"))
    s3.put_object(Bucket=BUCKET, Key=key, Body=JPEG)
    service.photo_uploaded(key)

    photos = _ok(call, "get_wine", {"wine_id": wine_id})["wine"]["photos"]
    assert [p["photo_id"] for p in photos] == [photo_id_of(key)]
    assert photos[0]["added_at"]
    deleted = _ok(call, "delete_photo", {"wine_id": wine_id, "photo_id": photo_id_of(key)})["wine"]
    assert deleted["photos"] == []

    assert _ok(call, "delete_wine", {"wine_id": wine_id}) == {"deleted": wine_id}


def test_upload_search_photo_returns_a_link(call):
    upload = _ok(call, "upload_search_photo", {})
    assert (upload["method"], upload["max_bytes"]) == ("PUT", MAX_PHOTO_BYTES)
    assert upload["photo_ref"].startswith("search/")


def test_tool_annotations_reach_the_client(call):
    _, body = call(_rpc("tools/list"))
    tools = {t["name"]: t for t in body["result"]["tools"]}
    assert tools["search_wines"]["annotations"]["readOnlyHint"] is True
    assert tools["delete_wine"]["annotations"]["destructiveHint"] is True


def test_tools_do_not_ask_for_a_user_id(call):
    _, body = call(_rpc("tools/list"))
    for tool in body["result"]["tools"]:
        props = tool["inputSchema"].get("properties", {})
        assert not any("user" in name or name == "ctx" for name in props), (tool["name"], list(props))


def test_token_decides_the_user(call, tokens):
    _ok(call, "create_wine", {"name": "Ridge Monte Bello"})
    assert len(_ok(call, "list_wines", {})["wines"]) == 1
    _, theirs = call(_tool("list_wines", {}), token=tokens["sam"])
    assert theirs["result"]["structuredContent"]["wines"] == []


def test_deactivated_user_is_rejected(table, tokens):
    sam = UserStore(table).find("sam")
    assert sam is not None
    UserStore(table).set_active(sam.user_id, False)
    call = _bootstrapped(table, tokens["andrew"])  # a fresh handler, so its token cache hasn't seen sam as active
    assert call(_rpc("tools/list"), token=tokens["sam"])[0] == 401
    assert call(_rpc("tools/list"))[0] == 200


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("create_wine", {"name": "  "}, "name"),
        ("get_wine", {"wine_id": "nope"}, "No wine"),
        ("update_wine", {"wine_id": "nope", "notes": "x"}, "No wine"),
        ("delete_wine", {"wine_id": "nope"}, "No wine"),
        ("add_photo", {"wine_id": "nope"}, "No wine"),
        ("delete_photo", {"wine_id": "nope", "photo_id": "p"}, "No wine"),
        ("search_wines", {}, "photo_ref"),
        ("search_wines", {"photo_ref": "search/usr_other/01J"}, "upload again"),
        ("list_wines", {"next_token": "not-a-token!"}, "next_token"),
    ],
)
def test_agent_mistakes_are_tool_errors(call, tool, arguments, message):
    status, body = call(_tool(tool, arguments))
    assert status == 200
    assert body["result"]["isError"] is True
    assert message in body["result"]["content"][0]["text"]


def test_photos_are_off_without_a_vector_bucket(table, tokens):
    status, body = _bootstrapped(table, tokens["andrew"])(_tool("upload_search_photo", {}))
    assert status == 200
    assert body["result"]["isError"] is True
    assert "enabled" in body["result"]["content"][0]["text"]
