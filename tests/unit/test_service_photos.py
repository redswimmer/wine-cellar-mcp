"""High gear: label photos through WineService, with S3 on moto and a fake photo index."""

import base64
import json

import pytest

from wine_cellar.models import ImageType, InvalidPhotoError, photo_id_of
from wine_cellar.photos import MAX_PHOTO_BYTES, PhotoStore
from wine_cellar.service import PhotoNotFoundError, PhotosDisabledError, WineNotFoundError, WineService

from .agent import add_photo, search_photo, upload
from .conftest import BUCKET, JPEG, PDF, USER

TOO_BIG = b"\xff\xd8\xff" + b"x" * MAX_PHOTO_BYTES


def test_add_photo_link_uploads_straight_to_the_wine(service):
    wine = service.create_wine(USER, name="Ridge")
    link = service.add_photo(USER, wine.wine_id, ImageType.PNG)
    assert link.photo_ref.startswith(f"wines/{USER}/{wine.wine_id}/")  # the S3 upload event filters on this
    assert (link.fields["key"], link.fields["Content-Type"]) == (link.photo_ref, "image/png")
    # S3 enforces the policy: only this type, and nothing empty or over the size limit, gets stored.
    conditions = json.loads(base64.b64decode(link.fields["policy"]))["conditions"]
    assert {"Content-Type": "image/png"} in conditions
    assert ["content-length-range", 1, MAX_PHOTO_BYTES] in conditions


def test_add_photo_needs_an_existing_wine(service):
    with pytest.raises(WineNotFoundError):
        service.add_photo(USER, "nope", ImageType.JPEG)


def test_uploaded_photo_is_saved_to_the_wine_and_indexed(service, s3, photo_index):
    wine = service.create_wine(USER, name="Ridge Monte Bello")
    first = add_photo(service, s3, wine.wine_id)
    second = add_photo(service, s3, wine.wine_id)
    service.photo_uploaded(second)  # S3 can deliver an event twice

    saved = service.get_wine(USER, wine.wine_id)
    assert saved.photo_keys == [first, second]
    assert saved.updated_at > wine.updated_at
    assert photo_index.indexed == sorted([first, second])
    assert s3.head_object(Bucket=BUCKET, Key=first)["ContentType"] == "image/jpeg"


def test_uploads_outside_wines_are_ignored(service, s3):
    s3.put_object(Bucket=BUCKET, Key="search/usr_1/01J", Body=JPEG)
    service.photo_uploaded("search/usr_1/01J")
    assert s3.list_objects_v2(Bucket=BUCKET)["KeyCount"] == 1


@pytest.mark.parametrize("data", [PDF, TOO_BIG], ids=["pdf", "too-big"])
def test_unusable_uploads_to_a_wine_are_discarded(service, s3, photo_index, data):
    wine = service.create_wine(USER, name="Ridge")
    add_photo(service, s3, wine.wine_id, data)
    assert service.get_wine(USER, wine.wine_id).photo_keys == []
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    assert photo_index.indexed == []


def test_photo_that_isnt_its_declared_type_is_discarded(service, s3):
    wine = service.create_wine(USER, name="Ridge")
    add_photo(service, s3, wine.wine_id, JPEG, ImageType.PNG)  # JPEG bytes uploaded as image/png
    assert service.get_wine(USER, wine.wine_id).photo_keys == []
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


def test_search_photo_of_the_wrong_type_says_which_type_to_use(service, s3):
    with pytest.raises(InvalidPhotoError, match="content_type image/jpeg"):
        service.search_wines(USER, photo_ref=search_photo(service, s3, JPEG, image_type=ImageType.GIF))


def test_delete_photo_removes_just_that_photo(service, s3, photo_index):
    wine = service.create_wine(USER, name="Ridge")
    keep = add_photo(service, s3, wine.wine_id)
    wrong = add_photo(service, s3, wine.wine_id)
    before = service.get_wine(USER, wine.wine_id)
    updated = service.delete_photo(USER, wine.wine_id, photo_id_of(wrong))
    assert updated.photo_keys == [keep] == service.get_wine(USER, wine.wine_id).photo_keys
    assert updated.updated_at > before.updated_at
    assert [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]] == [keep]
    assert photo_index.indexed == [keep]
    with pytest.raises(PhotoNotFoundError):
        service.delete_photo(USER, wine.wine_id, photo_id_of(wrong))


def test_delete_photo_survives_storage_failures(repo, s3, photo_index, service):
    wine = service.create_wine(USER, name="Ridge")
    key = add_photo(service, s3, wine.wine_id)
    photo_index.fail = True
    broken = WineService(repo, photos=PhotoStore(s3, "no-such-bucket"), photo_index=photo_index)
    assert broken.delete_photo(USER, wine.wine_id, photo_id_of(key)).photo_keys == []
    assert service.get_wine(USER, wine.wine_id).photo_keys == []


def test_photo_for_a_deleted_wine_is_discarded(service, s3):
    wine = service.create_wine(USER, name="Ridge")
    link = service.add_photo(USER, wine.wine_id, ImageType.JPEG)
    service.delete_wine(USER, wine.wine_id)
    upload(s3, link, JPEG)
    service.photo_uploaded(link.photo_ref)
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


def test_delete_wine_removes_its_photos_and_vectors(service, s3, photo_index):
    wine = service.create_wine(USER, name="Ridge")
    add_photo(service, s3, wine.wine_id)
    # Uploaded, but its event hasn't run yet.
    upload(s3, service.add_photo(USER, wine.wine_id, ImageType.JPEG), JPEG)
    service.delete_wine(USER, wine.wine_id)
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    assert photo_index.indexed == []


@pytest.mark.parametrize(
    ("data", "message"),
    [(PDF, "JPEG, PNG"), (TOO_BIG, "smaller size")],
    ids=["pdf", "too-big"],
)
def test_bad_search_photos_are_rejected_with_a_useful_message(service, s3, data, message):
    with pytest.raises(InvalidPhotoError, match=message):
        service.search_wines(USER, photo_ref=search_photo(service, s3, data))


def test_missing_search_photo_says_to_upload_again(service):
    ref = service.upload_search_photo(USER, ImageType.JPEG).photo_ref  # link created, but nothing was uploaded
    with pytest.raises(InvalidPhotoError, match="upload again"):
        service.search_wines(USER, photo_ref=ref)


def test_another_users_search_photo_cannot_be_used(service, s3):
    ref = search_photo(service, s3, user="usr_other")
    with pytest.raises(InvalidPhotoError, match="upload again"):
        service.search_wines(USER, photo_ref=ref)


def test_search_photo_is_used_once_and_never_saved(service, s3):
    ref = search_photo(service, s3)
    service.search_wines(USER, "ridge", photo_ref=ref)
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    with pytest.raises(InvalidPhotoError, match="upload again"):
        service.search_wines(USER, photo_ref=ref)


def test_search_by_photo_returns_each_wine_once(service, s3, photo_index):
    ridge = service.create_wine(USER, name="Ridge")
    ridge_photos = [add_photo(service, s3, ridge.wine_id), add_photo(service, s3, ridge.wine_id)]
    other = service.create_wine(USER, name="Lopez de Heredia")
    other_photo = add_photo(service, s3, other.wine_id)
    photo_index.ranking = [ridge_photos[1], ridge_photos[0], other_photo]

    results = service.search_wines(USER, photo_ref=search_photo(service, s3))
    assert [w.wine_id for w in results] == [ridge.wine_id, other.wine_id]


def test_search_with_photo_and_label_text_merges_both(service, s3, photo_index):
    ridge = service.create_wine(USER, name="Ridge Monte Bello")
    other = service.create_wine(USER, name="Lopez de Heredia")
    photo_index.ranking = [add_photo(service, s3, other.wine_id), add_photo(service, s3, ridge.wine_id)]
    # The photo alone ranks the other wine first; the label text agrees with Ridge's photo match.
    ids = [w.wine_id for w in service.search_wines(USER, "Ridge Monte Bello", photo_ref=search_photo(service, s3))]
    assert ids[0] == ridge.wine_id
    assert other.wine_id in ids  # found only by its photo


def test_photo_search_failures_fall_back_to_text(repo, s3, photo_index):
    photo_index.fail = True
    service = WineService(repo, photos=PhotoStore(s3, BUCKET), photo_index=photo_index)
    wine = service.create_wine(USER, name="Ridge")
    assert [w.wine_id for w in service.search_wines(USER, "ridge", photo_ref=search_photo(service, s3))] == [
        wine.wine_id
    ]


def test_photos_need_a_bucket(repo):
    service = WineService(repo)
    wine = service.create_wine(USER, name="Ridge")
    with pytest.raises(PhotosDisabledError):
        service.upload_search_photo(USER, ImageType.JPEG)
    with pytest.raises(PhotosDisabledError):
        service.add_photo(USER, wine.wine_id, ImageType.JPEG)


def test_reindex_skips_photos_it_cannot_read(service, s3, photo_index):
    wine = service.create_wine(USER, name="Ridge")
    good = add_photo(service, s3, wine.wine_id)
    gone = add_photo(service, s3, wine.wine_id)
    s3.delete_object(Bucket=BUCKET, Key=gone)
    photo_index.owners.clear()
    assert service.reindex(USER) == (1, 1)
    assert photo_index.indexed == [good]


def test_reindex_skips_photos_the_model_cannot_read(service, s3, photo_index):
    wine = service.create_wine(USER, name="Ridge")
    add_photo(service, s3, wine.wine_id)
    photo_index.owners.clear()
    photo_index.unreadable = True
    assert service.reindex(USER) == (1, 0)
