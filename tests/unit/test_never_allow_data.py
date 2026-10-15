"""What the server should never do with the data it's given: one test per rule, each trying to make it happen.

Tool input goes through the real MCP handler; photos go through `photo_uploaded`, the S3 upload event's contract.
Rules already covered elsewhere:

- never save a wine without a name: test_service_wines.py::test_a_wine_needs_a_name
- never keep a non-image as a wine photo: test_service_photos.py::test_unusable_uploads_to_a_wine_are_discarded
- never store a photo over 5 MB: test_service_photos.py::test_unusable_uploads_to_a_wine_are_discarded
- never add a photo to a deleted wine: test_service_photos.py::test_photo_for_a_deleted_wine_is_discarded
- never keep or reuse a search photo: test_service_photos.py::test_search_photo_is_used_once_and_never_saved
- never return a deleted wine from text search:
  test_service_text_search.py::test_search_ignores_vectors_of_deleted_wines
- never accept a forged page token: test_service_wines.py::test_list_rejects_a_bad_token
"""

from collections.abc import Callable

import pytest

from wine_cellar.models import ImageType
from wine_cellar.photos import MAX_PHOTOS_PER_WINE
from wine_cellar.repository import WineRepository
from wine_cellar.service import WineNotFoundError

from .conftest import BUCKET, JPEG, USER
from .test_entrypoint_handler import _ok, _tool, call, tokens  # noqa: F401  (fixtures)
from .test_service_photos import PDF, add_photo, search_photo, upload

# Tool input


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        # never crash when notes, a name or a varietal are too long to store
        ("create_wine", {"name": "Ridge", "notes": "n" * 500_000}, "at most"),
        ("create_wine", {"name": "R" * 1_000}, "at most"),
        ("create_wine", {"name": "Ridge", "varietal": "v" * 1_000}, "at most"),
        # never run a search query too big to handle
        ("search_wines", {"query": "q" * 10_000}, "at most"),
        # never store a verdict other than love, like, meh or dislike
        ("create_wine", {"name": "Ridge", "verdict": "great"}, "verdict"),
        # never return more than 20 search results or 100 list results
        ("search_wines", {"query": "ridge", "limit": 21}, "limit"),
        ("list_wines", {"limit": 101}, "limit"),
    ],
)
def test_never_accepts_input_it_cannot_handle(call, tool, arguments, message):  # noqa: F811
    status, body = call(_tool(tool, arguments))
    assert status == 200
    assert body["result"]["isError"] is True
    assert message in body["result"]["content"][0]["text"]
    assert _ok(call, "list_wines", {})["wines"] == []


@pytest.mark.parametrize("field", ["name", "varietal", "notes"])
def test_never_crashes_when_an_update_is_too_long(call, field):  # noqa: F811
    wine = _ok(call, "create_wine", {"name": "Ridge", "notes": "ok"})["wine"]
    status, body = call(_tool("update_wine", {"wine_id": wine["wine_id"], field: "x" * 500_000}))
    assert (status, body["result"]["isError"]) == (200, True)
    assert "at most" in body["result"]["content"][0]["text"]
    assert _ok(call, "get_wine", {"wine_id": wine["wine_id"]})["wine"] == wine


def test_never_accepts_a_request_over_1_mb(call):  # noqa: F811
    status, _ = call(_tool("create_wine", {"name": "Ridge", "notes": "n" * 1_100_000}))
    assert status == 413
    assert _ok(call, "list_wines", {})["wines"] == []


# Writes that happen at the same time


def race(monkeypatch, competitor: Callable[[], object]) -> None:
    """Run `competitor` right after the next wine read, so the write that follows starts from a stale copy."""
    real_get = WineRepository.get
    pending = [competitor]

    def get(self, user_id, wine_id):
        wine = real_get(self, user_id, wine_id)
        if pending:
            pending.pop()()
        return wine

    monkeypatch.setattr(WineRepository, "get", get)


def uploaded(service, s3, wine_id: str, data: bytes = JPEG) -> str:
    """A photo the agent has uploaded to add_photo's link, whose S3 event hasn't run yet."""
    link = service.add_photo(USER, wine_id, ImageType.JPEG)
    upload(s3, link, data)
    return link.photo_ref


def test_never_loses_a_photo_saved_during_an_update(service, s3, monkeypatch):
    wine = service.create_wine(USER, name="Ridge")
    key = uploaded(service, s3, wine.wine_id)
    race(monkeypatch, lambda: service.photo_uploaded(key))
    service.update_wine(USER, wine.wine_id, notes="Loved it")
    saved = service.get_wine(USER, wine.wine_id)
    assert (saved.photo_keys, saved.notes) == ([key], "Loved it")


def test_never_loses_an_update_made_while_a_photo_is_saved(service, s3, monkeypatch):
    wine = service.create_wine(USER, name="Ridge")
    key = uploaded(service, s3, wine.wine_id)
    race(monkeypatch, lambda: service.update_wine(USER, wine.wine_id, notes="Loved it"))
    service.photo_uploaded(key)
    saved = service.get_wine(USER, wine.wine_id)
    assert (saved.photo_keys, saved.notes) == ([key], "Loved it")


def test_never_loses_a_photo_saved_at_the_same_time_as_another(service, s3, monkeypatch):
    wine = service.create_wine(USER, name="Ridge")
    first, second = uploaded(service, s3, wine.wine_id), uploaded(service, s3, wine.wine_id)
    race(monkeypatch, lambda: service.photo_uploaded(second))
    service.photo_uploaded(first)
    assert sorted(service.get_wine(USER, wine.wine_id).photo_keys) == sorted([first, second])


def test_never_brings_back_a_wine_deleted_during_an_update(service, monkeypatch):
    wine = service.create_wine(USER, name="Ridge")
    race(monkeypatch, lambda: service.delete_wine(USER, wine.wine_id))
    with pytest.raises(WineNotFoundError):
        service.update_wine(USER, wine.wine_id, notes="Loved it")
    assert service.list_wines(USER) == ([], None)


def test_never_brings_back_a_wine_deleted_while_its_photo_is_saved(service, s3, monkeypatch, photo_vectors):
    wine = service.create_wine(USER, name="Ridge")
    key = uploaded(service, s3, wine.wine_id)
    race(monkeypatch, lambda: service.delete_wine(USER, wine.wine_id))
    service.photo_uploaded(key)
    assert service.list_wines(USER) == ([], None)
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    assert photo_vectors.vectors == {}


# Photos


def test_never_keeps_a_photo_whose_file_was_replaced_with_a_non_image(service, s3, photo_vectors):
    wine = service.create_wine(USER, name="Ridge")
    key = add_photo(service, s3, wine.wine_id)
    # The same upload link, used again within its 15 minutes: S3 checks the declared type, not the bytes.
    s3.put_object(Bucket=BUCKET, Key=key, Body=PDF, ContentType="image/jpeg")
    service.photo_uploaded(key)
    assert service.get_wine(USER, wine.wine_id).photo_keys == []
    assert photo_vectors.vectors == {}
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


def test_never_saves_a_photo_it_cannot_search_with(service, s3, bedrock, photo_vectors):
    wine = service.create_wine(USER, name="Ridge")
    bedrock.reject_images = True  # e.g. a JPEG header followed by garbage
    key = uploaded(service, s3, wine.wine_id)
    service.photo_uploaded(key)
    assert service.get_wine(USER, wine.wine_id).photo_keys == []
    assert photo_vectors.vectors == {}
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


def test_never_accepts_unlimited_photos_on_one_wine(service, s3):
    wine = service.create_wine(USER, name="Ridge")
    # Links handed out before the wine filled up can still be used.
    keys = [uploaded(service, s3, wine.wine_id) for _ in range(MAX_PHOTOS_PER_WINE + 1)]
    for key in keys:
        service.photo_uploaded(key)
    assert len(service.get_wine(USER, wine.wine_id).photo_keys) == MAX_PHOTOS_PER_WINE
    assert s3.list_objects_v2(Bucket=BUCKET)["KeyCount"] == MAX_PHOTOS_PER_WINE
    with pytest.raises(ValueError, match="delete one"):
        service.add_photo(USER, wine.wine_id, ImageType.JPEG)


def test_never_matches_a_photo_search_against_a_deleted_photo(service, s3, photo_vectors, monkeypatch):
    wine = service.create_wine(USER, name="Ridge")
    key = add_photo(service, s3, wine.wine_id)
    monkeypatch.setattr(photo_vectors, "delete_vectors", _fail)
    service.delete_photo(USER, wine.wine_id, key.rsplit("/", 1)[-1])
    photo_vectors.ranking = [key]  # the deleted photo's vector is still there and is the closest match
    assert service.search_wines(USER, photo_ref=search_photo(service, s3)) == []


def test_never_returns_a_deleted_wine_from_photo_search(service, s3, photo_vectors, monkeypatch):
    wine = service.create_wine(USER, name="Ridge")
    key = add_photo(service, s3, wine.wine_id)
    monkeypatch.setattr(photo_vectors, "delete_vectors", _fail)
    service.delete_wine(USER, wine.wine_id)
    photo_vectors.ranking = [key]
    assert service.search_wines(USER, photo_ref=search_photo(service, s3)) == []


def test_never_lets_a_search_delete_a_saved_photo(service, s3):
    wine = service.create_wine(USER, name="Ridge")
    key = add_photo(service, s3, wine.wine_id)
    with pytest.raises(ValueError, match="upload again"):
        service.search_wines(USER, photo_ref=key)
    assert s3.head_object(Bucket=BUCKET, Key=key)


def _fail(**_):
    raise RuntimeError("s3vectors down")
