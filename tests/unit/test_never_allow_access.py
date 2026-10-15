"""What the server must never allow around access: each test tries it, as an agent would, and expects a refusal.

Two users, andrew (A) and sam (B), share one wired handler. A owns a wine with a label photo and an unused search
photo. B then tries to reach A's data through every tool, and the tests check that B gets nothing of A's back and that
A's wines, photos and vectors are unchanged. The vector store here ignores its user filter, so search isolation is
proven by the server rather than by S3 Vectors.

"""

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, cast

import pytest
from ulid import ULID

from wine_cellar.lambda_adapter import make_handler
from wine_cellar.models import photo_id_of
from wine_cellar.photos import PhotoStore
from wine_cellar.repository import WineRepository, user_pk
from wine_cellar.server import build_app
from wine_cellar.service import WineService
from wine_cellar.tokens import TokenStore, hash_token
from wine_cellar.users import UserStore
from wine_cellar.vectors import Embedder, WinePhotoIndex, WineTextIndex

from .conftest import BUCKET, JPEG
from .fakes import FakeS3Vectors
from .test_entrypoint_handler import HOST, _client, _event, _ok, _rpc, _tool

JPEG_TYPE = "image/jpeg"
# In A's wine name and notes, and in no attempt's arguments, so any of A's wine content in a response is caught.
A_MARKER = "zqxa"


class LeakyS3Vectors(FakeS3Vectors):
    """A vector store whose user filter is broken: every query returns every vector."""

    def query_vectors(self, *, vectorBucketName, indexName, topK, queryVector, filter, returnMetadata=False):
        hits = list(self.vectors.values())[:topK]
        return {"vectors": [{"key": v["key"], **({"metadata": v["metadata"]} if returnMetadata else {})} for v in hits]}


@dataclass
class World:
    call: Any
    service: WineService
    table: Any
    s3: Any
    vectors: list[LeakyS3Vectors]
    tokens: dict[str, str]
    a_user: str
    b_user: str
    a_wine: str
    a_photo_key: str
    a_search_ref: str
    b_wine: str
    b_photo_key: str

    def snapshot(self, user_id: str) -> dict[str, Any]:
        """Everything stored for a user: table items, S3 objects and vectors."""
        items = self.table.scan()["Items"]
        objects = self.s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])
        return {
            "items": sorted((i["sk"], i.get("doc", "")) for i in items if i["pk"] == user_pk(user_id)),
            "objects": sorted((o["Key"], o["ETag"]) for o in objects if f"/{user_id}/" in o["Key"]),
            "vectors": [
                sorted(k for k, v in index.vectors.items() if v["metadata"]["user_id"] == user_id)
                for index in self.vectors
            ],
        }

    def a_secrets(self) -> list[str]:
        """Strings that would show A's data leaked into a response: A's ids, photo keys and search photo."""
        return [self.a_user, self.a_wine, self.a_photo_key, photo_id_of(self.a_photo_key), self.a_search_ref, A_MARKER]


def _upload_photo(world_call, service, s3, wine_id: str, token: str | None = None) -> str:
    """What the agent and S3 do with an add_photo link: PUT the file, then the upload event records it."""
    status, body = world_call(_tool("add_photo", {"wine_id": wine_id, "content_type": JPEG_TYPE}), token=token)
    key = body["result"]["structuredContent"]["fields"]["key"]
    s3.put_object(Bucket=BUCKET, Key=key, Body=JPEG, ContentType=JPEG_TYPE)
    service.photo_uploaded(key)
    return key


@pytest.fixture
def world(table, s3, bedrock) -> World:
    users, tokens = UserStore(table), TokenStore(table)
    a, b = users.create("andrew"), users.create("sam")
    issued = {"andrew": tokens.create(a.user_id, "a-agent"), "sam": tokens.create(b.user_id, "b-agent")}
    text_vectors, photo_vectors = LeakyS3Vectors(), LeakyS3Vectors()
    embedder = Embedder(bedrock)
    service = WineService(
        WineRepository(table),
        WineTextIndex(text_vectors, embedder, bucket="vb", index="wine-text"),
        photos=PhotoStore(s3, BUCKET),
        photo_index=WinePhotoIndex(photo_vectors, embedder, bucket="vb", index="wine-photos"),
    )
    call = _client(make_handler(*build_app(service, TokenStore(table).resolve, [HOST])), issued["andrew"])

    a_wine = _ok(
        call, "create_wine", {"name": f"Ridge Monte Bello {A_MARKER}", "notes": f"huge, structured {A_MARKER}"}
    )["wine"]["wine_id"]
    a_photo_key = _upload_photo(call, service, s3, a_wine)
    a_search_ref = _ok(call, "upload_search_photo", {"content_type": JPEG_TYPE})["photo_ref"]
    s3.put_object(Bucket=BUCKET, Key=a_search_ref, Body=JPEG, ContentType=JPEG_TYPE)
    b_call = _as(call, issued["sam"])
    b_wine = _ok(b_call, "create_wine", {"name": "Lopez de Heredia Tondonia"})["wine"]["wine_id"]
    b_photo_key = _upload_photo(call, service, s3, b_wine, token=issued["sam"])
    return World(
        call=call,
        service=service,
        table=table,
        s3=s3,
        vectors=[text_vectors, photo_vectors],
        tokens=issued,
        a_user=a.user_id,
        b_user=b.user_id,
        a_wine=a_wine,
        a_photo_key=a_photo_key,
        a_search_ref=a_search_ref,
        b_wine=b_wine,
        b_photo_key=b_photo_key,
    )


def _as(call, token: str):
    return lambda body, **kw: call(body, token=token, **kw)


# B's attempts on A's data, at least one per tool. Every call also passes A's user_id, which must be ignored.
ATTEMPTS: dict[str, Any] = {
    "get A's wine": lambda w: ("get_wine", {"wine_id": w.a_wine}),
    "list wines": lambda w: ("list_wines", {}),
    "list with a next_token made from A's wine": lambda w: (
        "list_wines",
        {"next_token": base64.urlsafe_b64encode(w.a_wine.encode()).decode()},
    ),
    "search by A's wine name": lambda w: ("search_wines", {"query": "Ridge Monte Bello"}),
    "search by description": lambda w: ("search_wines", {"query": "huge structured red"}),
    "search with A's search photo": lambda w: ("search_wines", {"photo_ref": w.a_search_ref}),
    "update A's wine": lambda w: ("update_wine", {"wine_id": w.a_wine, "notes": "changed by B"}),
    "slip A's photo key onto B's wine": lambda w: ("update_wine", {"wine_id": w.b_wine, "photo_keys": [w.a_photo_key]}),
    "delete A's wine": lambda w: ("delete_wine", {"wine_id": w.a_wine}),
    "delete B's own wine": lambda w: ("delete_wine", {"wine_id": w.b_wine}),
    "add a photo to A's wine": lambda w: ("add_photo", {"wine_id": w.a_wine, "content_type": JPEG_TYPE}),
    "delete A's photo": lambda w: ("delete_photo", {"wine_id": w.a_wine, "photo_id": photo_id_of(w.a_photo_key)}),
    "delete A's photo via B's wine": lambda w: (
        "delete_photo",
        {"wine_id": w.b_wine, "photo_id": photo_id_of(w.a_photo_key)},
    ),
    "delete B's own photo": lambda w: ("delete_photo", {"wine_id": w.b_wine, "photo_id": photo_id_of(w.b_photo_key)}),
    "create a wine with A's wine_id": lambda w: ("create_wine", {"name": "Planted", "wine_id": w.a_wine}),
    "get a search upload link": lambda w: ("upload_search_photo", {"content_type": JPEG_TYPE}),
}


@pytest.mark.parametrize("attempt", ATTEMPTS)
def test_one_user_never_reaches_another_users_wines_or_photos(world, attempt):
    """B can't see, change or delete A's wines or photos, or act as A by naming A's user_id."""
    tool, arguments = ATTEMPTS[attempt](world)
    before = world.snapshot(world.a_user)

    status, body = world.call(_tool(tool, {**arguments, "user_id": world.a_user}), token=world.tokens["sam"])

    assert status == 200, body
    # An error may echo back an id B sent; anything else of A's in the response is a leak.
    sent = json.dumps(arguments)
    leaked = [s for s in world.a_secrets() if s not in sent and s in json.dumps(body)]
    assert not leaked, f"{tool} showed B A's data: {leaked}"
    assert world.snapshot(world.a_user) == before, f"{tool} as B changed A's data"


def test_attempts_cover_every_tool(world):
    """Guard: a new tool fails here until it has a cross-user attempt above."""
    _, body = world.call(_rpc("tools/list"))
    assert {t["name"] for t in body["result"]["tools"]} == {make(world)[0] for make in ATTEMPTS.values()}


def test_another_users_wine_looks_the_same_as_no_wine(world):
    """B can't tell whether a wine_id belongs to someone else: the error is the same as for an id nobody has."""

    def error(wine_id: str) -> str:
        _, body = world.call(_tool("get_wine", {"wine_id": wine_id}), token=world.tokens["sam"])
        return body["result"]["content"][0]["text"].replace(wine_id, "<id>")

    assert error(world.a_wine) == error(str(ULID()))


def test_a_wine_never_links_to_a_photo_outside_its_own_folder(world):
    """Even if a wine record held another user's photo key, its details never give out a link to that photo."""
    repo = WineRepository(world.table)
    b_wine = repo.get(world.b_user, world.b_wine)
    assert b_wine is not None
    repo.put(b_wine.model_copy(update={"photo_keys": [*b_wine.photo_keys, world.a_photo_key]}))

    body = json.dumps(_ok(_as(world.call, world.tokens["sam"]), "get_wine", {"wine_id": world.b_wine}))

    assert world.a_user not in body
    assert photo_id_of(world.b_photo_key) in body


def test_search_with_a_broken_vector_filter_still_finds_the_wines_for_their_owner(world):
    """Makes sure the leak checks above aren't passing just because search returns nothing."""
    names = [m["name"] for m in _ok(world.call, "search_wines", {"query": "huge structured red"})["matches"]]
    assert names == [f"Ridge Monte Bello {A_MARKER}"]


def test_photo_search_never_matches_another_users_label(world):
    """B's photo search, with a broken vector filter, returns only B's wines."""
    b_call = _as(world.call, world.tokens["sam"])
    ref = _ok(b_call, "upload_search_photo", {"content_type": JPEG_TYPE})["photo_ref"]
    world.s3.put_object(Bucket=BUCKET, Key=ref, Body=JPEG, ContentType=JPEG_TYPE)

    matches = _ok(b_call, "search_wines", {"photo_ref": ref})["matches"]

    assert [m["wine_id"] for m in matches] == [world.b_wine]


def test_acting_as_another_user_by_name_creates_nothing_for_them(world):
    """create_wine with A's user_id records the wine for B, the token's user."""
    b_call = _as(world.call, world.tokens["sam"])
    _ok(b_call, "create_wine", {"name": "Planted for A", "user_id": world.a_user})
    assert "Planted for A" not in json.dumps(_ok(world.call, "list_wines", {}))
    assert "Planted for A" in json.dumps(_ok(b_call, "list_wines", {}))


@pytest.mark.parametrize(
    "ref",
    [
        "search/{b}/../{a}/{ulid}",
        "search/{b}/%2E%2E/{a}/{ulid}",
        "search/{b}/{ulid}/../../{a}/{ulid}",
        "search/{b}/{ulid}/extra",
        "search/{a}/{ulid}",
        "wines/{b}/{wine}/{ulid}",
    ],
)
def test_search_photo_refs_outside_the_callers_own_never_reach_s3(world, ref):
    """a photo_ref must be exactly one of the caller's own search photos; anything else is refused unread."""
    ref = ref.format(a=world.a_user, b=world.b_user, ulid=ULID(), wine=world.b_wine)
    s3_keys: list[str] = []
    world.s3.meta.events.register("before-parameter-build.s3", lambda params, **_: s3_keys.append(params.get("Key")))

    status, body = world.call(_tool("search_wines", {"photo_ref": ref}), token=world.tokens["sam"])

    assert status == 200 and body["result"]["isError"] is True
    assert "upload again" in body["result"]["content"][0]["text"]
    assert ref not in s3_keys


@pytest.mark.parametrize("owner", ["a", "b"])
def test_an_upload_under_one_user_never_lands_on_another_users_wine(world, owner):
    """a file whose key pairs one user with the other user's wine is discarded, and both wines are unchanged."""
    user, wine = (world.a_user, world.b_wine) if owner == "a" else (world.b_user, world.a_wine)
    key = f"wines/{user}/{wine}/{ULID()}"
    before = (world.snapshot(world.a_user), world.snapshot(world.b_user))
    world.s3.put_object(Bucket=BUCKET, Key=key, Body=JPEG, ContentType=JPEG_TYPE)

    world.service.photo_uploaded(key)

    assert (world.snapshot(world.a_user), world.snapshot(world.b_user)) == before
    assert world.s3.list_objects_v2(Bucket=BUCKET, Prefix=key).get("KeyCount") == 0


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_other_methods_never_skip_the_token_check(world, method):
    """API Gateway routes GET and DELETE /mcp to the same handler, and they need a token too."""
    event = _event(_rpc("tools/list"), token="", host=HOST)
    event["requestContext"]["http"]["method"] = method
    handler = make_handler(*build_app(world.service, TokenStore(world.table).resolve, [HOST]))
    assert handler(event, cast(Any, None))["statusCode"] == 401


def test_a_revoked_token_is_refused(world):
    """once revoked (and past the token cache), a token gets 401."""
    TokenStore(world.table).revoke(hash_token(world.tokens["sam"]))
    fresh = _client(make_handler(*build_app(world.service, TokenStore(world.table).resolve, [HOST])), "")
    assert fresh(_rpc("tools/list"), token=world.tokens["sam"])[0] == 401


def test_tokens_are_never_stored_or_logged(world, caplog):
    """After real calls, no table item, log line or response holds a token; the table keeps only its hash."""
    caplog.set_level(logging.DEBUG)
    responses = []
    for token in world.tokens.values():
        responses.append(world.call(_tool("list_wines", {}), token=token))
        responses.append(world.call(_tool("get_wine", {"wine_id": "nope"}), token=token))

    stored = json.dumps(world.table.scan()["Items"], default=str)
    for token in world.tokens.values():
        assert token not in stored
        assert hash_token(token) in stored
        assert token not in caplog.text
        assert token not in json.dumps(responses)
