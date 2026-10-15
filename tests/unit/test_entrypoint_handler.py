"""Drives the real Lambda handler with API Gateway HTTP API (v2) events."""

from urllib.parse import parse_qs, unquote, urlparse

import pytest

from wine_cellar.bootstrap import build_mcp_handler
from wine_cellar.config import Settings
from wine_cellar.models import photo_id_of
from wine_cellar.photos import MAX_PHOTO_BYTES
from wine_cellar.users import UserStore

from . import agent
from .conftest import BUCKET, JPEG


def _bootstrapped(table, token: str):
    """The handler exactly as bootstrap builds it from settings. Without a vector bucket, photos are off."""
    return agent.client(build_mcp_handler(Settings(table_name=table.name, allowed_hosts=[agent.HOST]), table), token)


def test_rejects_missing_or_wrong_token(call):
    assert call(agent.rpc("tools/list"), token="")[0] == 401
    assert call(agent.rpc("tools/list"), token="nope")[0] == 401
    assert call(agent.rpc("tools/list"), token="wct_unknown")[0] == 401


def test_rejects_unknown_host(call):
    status, _ = call(agent.rpc("tools/list"), host="evil.example.com")
    assert status in (400, 421)


def test_initialize_and_list_tools(call):
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
    status, body = call(agent.rpc("initialize", init))
    assert status == 200, body
    assert body["result"]["serverInfo"]["name"] == "wine-cellar"

    status, body = call(agent.rpc("tools/list"))
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
    wine_id = agent.ok(call, "create_wine", {"name": "Ridge Monte Bello", "varietal": "Cab", "verdict": "like"})[
        "wine"
    ]["wine_id"]
    updated = agent.ok(call, "update_wine", {"wine_id": wine_id, "verdict": "love", "varietal": None})["wine"]
    assert (updated["verdict"], updated["varietal"], updated["name"]) == ("love", "", "Ridge Monte Bello")

    matches = agent.ok(call, "search_wines", {"query": "monte bello"})["matches"]
    assert matches[0]["wine_id"] == wine_id
    assert matches[0]["name"] == "Ridge Monte Bello"


def _key_of(url: str) -> str:
    return unquote(urlparse(url).path.removeprefix(f"/{BUCKET}").lstrip("/"))


def test_wine_photos_are_listed_with_download_links_and_can_be_deleted(call, service, s3):
    wine_id = agent.ok(call, "create_wine", {"name": "Ridge"})["wine"]["wine_id"]
    keys = []
    for _ in range(2):
        upload = agent.ok(call, "add_photo", {"wine_id": wine_id, "content_type": "image/jpeg"})
        assert (upload["upload_url"], upload["max_bytes"]) == (f"https://{BUCKET}.s3.amazonaws.com/", MAX_PHOTO_BYTES)
        # What happens after the agent POSTs the form: the file lands at the form's key, typed as the form says, and
        # the S3 event records it.
        keys.append(upload["fields"]["key"])
        s3.put_object(Bucket=BUCKET, Key=keys[-1], Body=JPEG, ContentType=upload["fields"]["Content-Type"])
        service.photo_uploaded(keys[-1])

    photos = agent.ok(call, "get_wine", {"wine_id": wine_id})["wine"]["photos"]
    assert [p["photo_id"] for p in photos] == [photo_id_of(k) for k in keys]
    assert all(p["added_at"] for p in photos)
    # Each photo links to its own image, and list_wines and search_wines return the same links.
    assert [_key_of(p["url"]) for p in photos] == keys
    assert all(parse_qs(urlparse(p["url"]).query)["X-Amz-Expires"] == ["3600"] for p in photos)
    assert agent.ok(call, "list_wines", {})["wines"][0]["photos"] == photos
    assert agent.ok(call, "search_wines", {"query": "ridge"})["matches"][0]["photos"] == photos

    deleted = agent.ok(call, "delete_photo", {"wine_id": wine_id, "photo_id": photo_id_of(keys[0])})["wine"]
    assert [p["photo_id"] for p in deleted["photos"]] == [photo_id_of(keys[1])]

    assert agent.ok(call, "delete_wine", {"wine_id": wine_id}) == {"deleted": wine_id}


def test_upload_search_photo_returns_a_link(call):
    upload = agent.ok(call, "upload_search_photo", {"content_type": "image/png"})
    assert upload["max_bytes"] == MAX_PHOTO_BYTES
    assert upload["photo_ref"].startswith("search/")
    assert (upload["fields"]["key"], upload["fields"]["Content-Type"]) == (upload["photo_ref"], "image/png")


def test_photo_uploads_only_take_image_types(call):
    _, body = call(agent.rpc("tools/list"))
    tools = {t["name"]: t for t in body["result"]["tools"]}
    for name in ("add_photo", "upload_search_photo"):
        assert "content_type" in tools[name]["inputSchema"]["required"]
    status, body = call(agent.tool_call("upload_search_photo", {"content_type": "image/svg+xml"}))
    assert status == 200
    assert body["result"]["isError"] is True


def test_tool_annotations_reach_the_client(call):
    _, body = call(agent.rpc("tools/list"))
    tools = {t["name"]: t for t in body["result"]["tools"]}
    assert tools["search_wines"]["annotations"]["readOnlyHint"] is True
    assert tools["delete_wine"]["annotations"]["destructiveHint"] is True


def test_tools_do_not_ask_for_a_user_id(call):
    _, body = call(agent.rpc("tools/list"))
    for tool in body["result"]["tools"]:
        props = tool["inputSchema"].get("properties", {})
        assert not any("user" in name or name == "ctx" for name in props), (tool["name"], list(props))


def test_token_decides_the_user(call, tokens):
    agent.ok(call, "create_wine", {"name": "Ridge Monte Bello"})
    assert len(agent.ok(call, "list_wines", {})["wines"]) == 1
    _, theirs = call(agent.tool_call("list_wines", {}), token=tokens["sam"])
    assert theirs["result"]["structuredContent"]["wines"] == []


def test_deactivated_user_is_rejected(table, tokens):
    sam = UserStore(table).find("sam")
    assert sam is not None
    UserStore(table).set_active(sam.user_id, False)
    call = _bootstrapped(table, tokens["andrew"])  # a fresh handler, so its token cache hasn't seen sam as active
    assert call(agent.rpc("tools/list"), token=tokens["sam"])[0] == 401
    assert call(agent.rpc("tools/list"))[0] == 200


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("create_wine", {"name": "  "}, "name"),
        ("get_wine", {"wine_id": "nope"}, "No wine"),
        ("update_wine", {"wine_id": "nope", "notes": "x"}, "No wine"),
        ("delete_wine", {"wine_id": "nope"}, "No wine"),
        ("add_photo", {"wine_id": "nope", "content_type": "image/jpeg"}, "No wine"),
        ("delete_photo", {"wine_id": "nope", "photo_id": "p"}, "No wine"),
        ("search_wines", {}, "photo_ref"),
        ("search_wines", {"photo_ref": "search/usr_other/01J"}, "upload again"),
        ("list_wines", {"next_token": "not-a-token!"}, "next_token"),
    ],
)
def test_agent_mistakes_are_tool_errors(call, tool, arguments, message):
    status, body = call(agent.tool_call(tool, arguments))
    assert status == 200
    assert body["result"]["isError"] is True
    assert message in body["result"]["content"][0]["text"]


def test_photos_are_off_without_a_vector_bucket(table, tokens):
    status, body = _bootstrapped(table, tokens["andrew"])(
        agent.tool_call("upload_search_photo", {"content_type": "image/jpeg"})
    )
    assert status == 200
    assert body["result"]["isError"] is True
    assert "enabled" in body["result"]["content"][0]["text"]
