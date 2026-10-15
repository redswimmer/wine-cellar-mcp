"""What an agent does: MCP calls to the Lambda handler, and photo uploads to the links the tools hand out."""

import json

from wine_cellar.models import ImageType, Upload

from .conftest import BUCKET, JPEG, USER

HOST = "mcp.wine-dev.example.com"


def client(handler, default_token: str):
    def call(body: dict, *, token: str | None = None, host: str = HOST) -> tuple[int, dict]:
        resp = handler(event(body, token=default_token if token is None else token, host=host), None)
        try:
            return resp["statusCode"], json.loads(resp["body"])
        except (KeyError, ValueError):
            return resp["statusCode"], resp.get("body")

    return call


def event(body: dict, *, token: str, host: str) -> dict:
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


def rpc(method: str, params: dict | None = None, id_: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def tool_call(name: str, arguments: dict) -> dict:
    return rpc("tools/call", {"name": name, "arguments": arguments})


def ok(call, name: str, arguments: dict) -> dict:
    status, body = call(tool_call(name, arguments))
    assert status == 200, body
    assert not body["result"].get("isError"), body
    return body["result"]["structuredContent"]


def upload(s3, link: Upload, data: bytes) -> None:
    """What S3 stores when the agent POSTs to the link: the file, with the Content-Type the link requires."""
    s3.put_object(Bucket=BUCKET, Key=link.photo_ref, Body=data, ContentType=link.fields["Content-Type"])


def search_photo(service, s3, data: bytes = JPEG, user: str = USER, image_type: ImageType = ImageType.JPEG) -> str:
    """What the agent does to search by photo: get an upload link, upload the file, keep the photo_ref."""
    link = service.upload_search_photo(user, image_type)
    upload(s3, link, data)
    return link.photo_ref


def add_photo(service, s3, wine_id: str, data: bytes = JPEG, image_type: ImageType = ImageType.JPEG) -> str:
    """The agent uploads a photo to add_photo's link; S3 then sends the upload event."""
    link = service.add_photo(USER, wine_id, image_type)
    upload(s3, link, data)
    service.photo_uploaded(link.photo_ref)
    return link.photo_ref
