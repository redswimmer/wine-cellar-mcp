import base64

import boto3
import pytest
from botocore.config import Config

from wine_cellar.photos import MAX_PHOTO_BYTES, InvalidPhotoError, PhotoStore, sniff
from wine_cellar.repository import WineRepository
from wine_cellar.service import PhotosDisabledError, WineNotFoundError, WineService
from wine_cellar.vectors import Embedder, WinePhotoIndex, WineTextIndex

from .fakes import FakeBedrock, FakeS3Vectors

USER = "usr_1"
JPEG = b"\xff\xd8\xff\xe0" + b"label" * 100
BUCKET = "photos-test"


@pytest.fixture
def bedrock():
    return FakeBedrock()


@pytest.fixture
def photo_vectors():
    return FakeS3Vectors()


@pytest.fixture
def s3(table):  # `table` keeps moto's mock_aws active
    client = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
    client.create_bucket(Bucket=BUCKET)
    return client


@pytest.fixture
def service(table, s3, bedrock, photo_vectors):
    embedder = Embedder(bedrock)
    return WineService(
        WineRepository(table),
        WineTextIndex(FakeS3Vectors(), embedder, bucket="vb", index="wine-text"),
        photos=PhotoStore(s3, BUCKET),
        photo_index=WinePhotoIndex(photo_vectors, embedder, bucket="vb", index="wine-photos"),
    )


def search_photo(service, s3, data: bytes = JPEG, user: str = USER) -> str:
    """What the agent does to search by photo: get an upload link, PUT the file, keep the photo_ref."""
    ref = service.upload_search_photo(user).photo_ref
    s3.put_object(Bucket=BUCKET, Key=ref, Body=data)
    return ref


def add_photo(service, s3, wine_id: str, data: bytes = JPEG) -> str:
    """The agent PUTs a photo to add_photo's link; S3 then sends the upload event."""
    key = service.add_photo(USER, wine_id).photo_ref
    s3.put_object(Bucket=BUCKET, Key=key, Body=data)
    service.photo_uploaded(key)
    return key


def test_sniff_recognises_common_formats():
    assert sniff(JPEG).format == "jpeg"
    assert sniff(b"\x89PNG\r\n\x1a\n" + b"x" * 10).format == "png"
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ").format == "webp"
    assert sniff(b"GIF89a...").format == "gif"


def test_add_photo_link_uploads_straight_to_the_wine(service):
    wine, _ = service.save_wine(USER, producer="Ridge")
    link = service.add_photo(USER, wine.wine_id)
    assert link.photo_ref.startswith(f"wines/{USER}/{wine.wine_id}/")
    assert BUCKET in link.upload_url and link.photo_ref in link.upload_url
    assert "X-Amz-Signature=" in link.upload_url


def test_add_photo_needs_an_existing_wine(service):
    with pytest.raises(WineNotFoundError):
        service.add_photo(USER, "nope")


def test_uploaded_photo_is_saved_to_the_wine_and_indexed(service, s3, photo_vectors, bedrock):
    wine, _ = service.save_wine(USER, producer="Ridge", name="Monte Bello")
    first = add_photo(service, s3, wine.wine_id)
    second = add_photo(service, s3, wine.wine_id)
    service.photo_uploaded(second)  # S3 can deliver an event twice

    assert service.get_wine(USER, wine.wine_id).photo_keys == [first, second]
    assert photo_vectors.vectors[first]["metadata"] == {"user_id": USER, "wine_id": wine.wine_id}
    image = bedrock.bodies[-1]["singleEmbeddingParams"]["image"]
    assert image["format"] == "jpeg" and base64.b64decode(image["source"]["bytes"]) == JPEG


@pytest.mark.parametrize("data", [b"%PDF-1.7", b"\xff\xd8\xff" + b"x" * MAX_PHOTO_BYTES])
def test_unusable_uploads_to_a_wine_are_discarded(service, s3, photo_vectors, data):
    wine, _ = service.save_wine(USER, producer="Ridge")
    add_photo(service, s3, wine.wine_id, data)
    assert service.get_wine(USER, wine.wine_id).photo_keys == []
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    assert photo_vectors.vectors == {}


def test_photo_for_a_deleted_wine_is_discarded(service, s3):
    wine, _ = service.save_wine(USER, producer="Ridge")
    key = service.add_photo(USER, wine.wine_id).photo_ref
    service.delete_wine(USER, wine.wine_id)
    s3.put_object(Bucket=BUCKET, Key=key, Body=JPEG)
    service.photo_uploaded(key)
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


def test_delete_wine_removes_its_photos_and_vectors(service, s3, photo_vectors):
    wine, _ = service.save_wine(USER, producer="Ridge")
    add_photo(service, s3, wine.wine_id)
    # Uploaded, but its event hasn't run yet.
    s3.put_object(Bucket=BUCKET, Key=service.add_photo(USER, wine.wine_id).photo_ref, Body=JPEG)
    service.delete_wine(USER, wine.wine_id)
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    assert photo_vectors.vectors == {}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"%PDF-1.7", "JPEG, PNG"),
        (b"\xff\xd8\xff" + b"x" * MAX_PHOTO_BYTES, "smaller size"),
    ],
)
def test_bad_search_photos_are_rejected_with_a_useful_message(service, s3, data, message):
    with pytest.raises(InvalidPhotoError, match=message):
        service.search_wines(USER, photo_ref=search_photo(service, s3, data))


def test_missing_search_photo_says_to_upload_again(service):
    ref = service.upload_search_photo(USER).photo_ref  # link created, but nothing was uploaded
    with pytest.raises(InvalidPhotoError, match="upload again"):
        service.search_wines(USER, photo_ref=ref)


def test_another_users_search_photo_cannot_be_used(service, s3):
    ref = search_photo(service, s3, user="usr_other")
    with pytest.raises(InvalidPhotoError, match="upload again"):
        service.search_wines(USER, photo_ref=ref)


def test_search_photo_is_used_once_and_never_saved(service, s3):
    ref = search_photo(service, s3)
    assert service.search_wines(USER, "ridge", photo_ref=ref) == []
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    with pytest.raises(InvalidPhotoError, match="upload again"):
        service.search_wines(USER, photo_ref=ref)


def test_search_by_photo_returns_each_wine_once(service, s3, photo_vectors, bedrock):
    ridge, _ = service.save_wine(USER, producer="Ridge")
    ridge_photos = [add_photo(service, s3, ridge.wine_id), add_photo(service, s3, ridge.wine_id)]
    other, _ = service.save_wine(USER, producer="Lopez de Heredia")
    other_photo = add_photo(service, s3, other.wine_id)
    photo_vectors.ranking = [ridge_photos[1], ridge_photos[0], other_photo]

    results = service.search_wines(USER, photo_ref=search_photo(service, s3))
    assert [w.wine_id for w in results] == [ridge.wine_id, other.wine_id]
    assert bedrock.bodies[-1]["singleEmbeddingParams"]["embeddingPurpose"] == "IMAGE_RETRIEVAL"
    assert photo_vectors.last_query["filter"] == {"user_id": {"$eq": USER}}


def test_search_with_photo_and_label_text_merges_both(service, s3, photo_vectors):
    ridge, _ = service.save_wine(USER, producer="Ridge", name="Monte Bello")
    other, _ = service.save_wine(USER, producer="Lopez de Heredia")
    photo_vectors.ranking = [add_photo(service, s3, other.wine_id), add_photo(service, s3, ridge.wine_id)]
    # The photo alone ranks the other wine first; the label text agrees with Ridge's photo match.
    results = service.search_wines(USER, "Ridge Monte Bello", photo_ref=search_photo(service, s3))
    assert results[0].wine_id == ridge.wine_id


def test_photo_search_failures_fall_back_to_text(table, s3, bedrock):
    service = WineService(
        WineRepository(table),
        photos=PhotoStore(s3, BUCKET),
        photo_index=WinePhotoIndex(FakeS3Vectors(fail=True), Embedder(bedrock), bucket="vb", index="wine-photos"),
    )
    wine, _ = service.save_wine(USER, producer="Ridge")
    assert [w.wine_id for w in service.search_wines(USER, "ridge", photo_ref=search_photo(service, s3))] == [
        wine.wine_id
    ]


def test_photos_need_a_bucket(table):
    service = WineService(WineRepository(table))
    wine, _ = service.save_wine(USER, producer="Ridge")
    with pytest.raises(PhotosDisabledError):
        service.upload_search_photo(USER)
    with pytest.raises(PhotosDisabledError):
        service.add_photo(USER, wine.wine_id)
