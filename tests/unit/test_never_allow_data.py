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
- never bring back a deleted wine that was stored before versioning:
  test_adapter_repository.py::test_a_wine_stored_before_versioning_is_not_written_back_after_a_delete
"""

import pytest

from wine_cellar.models import ImageType, photo_id_of
from wine_cellar.photos import MAX_PHOTOS_PER_WINE
from wine_cellar.service import WineNotFoundError

from . import agent
from .agent import add_photo, search_photo, upload
from .conftest import BUCKET, JPEG, PDF, USER
from .fakes import RacingRepository


@pytest.fixture
def repo(table):
    """Every service and handler in this file writes through a repository whose writes can be raced."""
    repo = RacingRepository(table)
    yield repo
    assert repo.pending is None, "a race was set up but no write ran it"


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
def test_never_accepts_input_it_cannot_handle(call, tool, arguments, message):
    status, body = call(agent.tool_call(tool, arguments))
    assert status == 200
    assert body["result"]["isError"] is True
    assert message in body["result"]["content"][0]["text"]
    assert agent.ok(call, "list_wines", {})["wines"] == []


@pytest.mark.parametrize("field", ["name", "varietal", "notes"])
def test_never_crashes_when_an_update_is_too_long(call, field):
    wine = agent.ok(call, "create_wine", {"name": "Ridge", "notes": "ok"})["wine"]
    status, body = call(agent.tool_call("update_wine", {"wine_id": wine["wine_id"], field: "x" * 500_000}))
    assert (status, body["result"]["isError"]) == (200, True)
    assert "at most" in body["result"]["content"][0]["text"]
    assert agent.ok(call, "get_wine", {"wine_id": wine["wine_id"]})["wine"] == wine


def test_never_accepts_a_request_over_1_mb(call):
    status, _ = call(agent.tool_call("create_wine", {"name": "Ridge", "notes": "n" * 1_100_000}))
    assert status == 413
    assert agent.ok(call, "list_wines", {})["wines"] == []


# Writes that happen at the same time


def uploaded(service, s3, wine_id: str, data: bytes = JPEG) -> str:
    """A photo the agent has uploaded to add_photo's link, whose S3 event hasn't run yet."""
    link = service.add_photo(USER, wine_id, ImageType.JPEG)
    upload(s3, link, data)
    return link.photo_ref


def test_never_loses_a_photo_saved_during_an_update(service, s3, repo):
    wine = service.create_wine(USER, name="Ridge")
    key = uploaded(service, s3, wine.wine_id)
    repo.race(lambda: service.photo_uploaded(key))
    service.update_wine(USER, wine.wine_id, notes="Loved it")
    saved = service.get_wine(USER, wine.wine_id)
    assert (saved.photo_keys, saved.notes) == ([key], "Loved it")


def test_never_loses_an_update_made_while_a_photo_is_saved(service, s3, repo):
    wine = service.create_wine(USER, name="Ridge")
    key = uploaded(service, s3, wine.wine_id)
    repo.race(lambda: service.update_wine(USER, wine.wine_id, notes="Loved it"))
    service.photo_uploaded(key)
    saved = service.get_wine(USER, wine.wine_id)
    assert (saved.photo_keys, saved.notes) == ([key], "Loved it")


def test_never_loses_a_photo_saved_at_the_same_time_as_another(service, s3, repo):
    wine = service.create_wine(USER, name="Ridge")
    first, second = uploaded(service, s3, wine.wine_id), uploaded(service, s3, wine.wine_id)
    repo.race(lambda: service.photo_uploaded(second))
    service.photo_uploaded(first)
    assert sorted(service.get_wine(USER, wine.wine_id).photo_keys) == sorted([first, second])


def test_never_brings_back_a_wine_deleted_during_an_update(service, repo):
    wine = service.create_wine(USER, name="Ridge")
    repo.race(lambda: service.delete_wine(USER, wine.wine_id))
    with pytest.raises(WineNotFoundError):
        service.update_wine(USER, wine.wine_id, notes="Loved it")
    assert service.list_wines(USER) == ([], None)


def test_never_brings_back_a_wine_deleted_while_its_photo_is_saved(service, s3, repo, photo_index):
    wine = service.create_wine(USER, name="Ridge")
    key = uploaded(service, s3, wine.wine_id)
    repo.race(lambda: service.delete_wine(USER, wine.wine_id))
    service.photo_uploaded(key)
    assert service.list_wines(USER) == ([], None)
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    assert photo_index.indexed == []


def test_never_gives_the_agent_a_raw_error_when_a_wine_stays_busy(call, repo):
    wine_id = agent.ok(call, "create_wine", {"name": "Ridge"})["wine"]["wine_id"]
    repo.always_conflict = True
    status, body = call(agent.tool_call("update_wine", {"wine_id": wine_id, "notes": "Loved it"}))
    assert (status, body["result"]["isError"]) == (200, True)
    assert "try again" in body["result"]["content"][0]["text"]


# Photos


def test_never_keeps_a_photo_whose_file_was_replaced_with_a_non_image(service, s3, photo_index):
    wine = service.create_wine(USER, name="Ridge")
    key = add_photo(service, s3, wine.wine_id)
    # The same upload link, used again within its 15 minutes: S3 checks the declared type, not the bytes.
    s3.put_object(Bucket=BUCKET, Key=key, Body=PDF, ContentType="image/jpeg")
    service.photo_uploaded(key)
    assert service.get_wine(USER, wine.wine_id).photo_keys == []
    assert photo_index.indexed == []
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


def test_never_saves_a_photo_it_cannot_search_with(service, s3, photo_index):
    wine = service.create_wine(USER, name="Ridge")
    photo_index.unreadable = True  # e.g. a JPEG header followed by garbage
    key = uploaded(service, s3, wine.wine_id)
    service.photo_uploaded(key)
    assert service.get_wine(USER, wine.wine_id).photo_keys == []
    assert photo_index.indexed == []
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


def test_never_drops_a_photo_because_indexing_was_busy(service, s3, photo_index):
    wine = service.create_wine(USER, name="Ridge")
    key = uploaded(service, s3, wine.wine_id)
    photo_index.fail = True  # e.g. Bedrock throttling
    with pytest.raises(RuntimeError):  # raised, so S3 runs the event again
        service.photo_uploaded(key)
    photo_index.fail = False
    service.photo_uploaded(key)
    assert service.get_wine(USER, wine.wine_id).photo_keys == [key]


def test_never_fails_on_an_upload_event_for_a_photo_already_deleted(service, s3, photo_index):
    wine = service.create_wine(USER, name="Ridge")
    key = add_photo(service, s3, wine.wine_id)
    service.delete_photo(USER, wine.wine_id, photo_id_of(key))
    service.photo_uploaded(key)  # S3 delivers events at least once, so it can come again
    assert service.get_wine(USER, wine.wine_id).photo_keys == []
    assert photo_index.indexed == []


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


def test_never_matches_a_photo_search_against_a_deleted_photo(service, s3, photo_index):
    wine = service.create_wine(USER, name="Ridge")
    key = add_photo(service, s3, wine.wine_id)
    photo_index.fail = True  # the delete's vector cleanup fails
    service.delete_photo(USER, wine.wine_id, key.rsplit("/", 1)[-1])
    photo_index.fail = False
    photo_index.ranking = [key]  # the deleted photo's vector is still there and is the closest match
    assert service.search_wines(USER, photo_ref=search_photo(service, s3)) == []


def test_never_returns_a_deleted_wine_from_photo_search(service, s3, photo_index):
    wine = service.create_wine(USER, name="Ridge")
    key = add_photo(service, s3, wine.wine_id)
    photo_index.fail = True
    service.delete_wine(USER, wine.wine_id)
    photo_index.fail = False
    photo_index.ranking = [key]
    assert service.search_wines(USER, photo_ref=search_photo(service, s3)) == []


def test_never_lets_a_search_delete_a_saved_photo(service, s3):
    wine = service.create_wine(USER, name="Ridge")
    key = add_photo(service, s3, wine.wine_id)
    with pytest.raises(ValueError, match="upload again"):
        service.search_wines(USER, photo_ref=key)
    assert s3.head_object(Bucket=BUCKET, Key=key)
