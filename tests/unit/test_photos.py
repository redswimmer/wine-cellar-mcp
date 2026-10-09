import base64

import boto3
import pytest
from botocore.config import Config

from wine_cellar.photos import MAX_PHOTO_BYTES, InvalidPhotoError, PhotoStore, sniff
from wine_cellar.repository import WineRepository
from wine_cellar.service import PhotosDisabledError, WineService
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


def upload(service, s3, data: bytes = JPEG, user: str = USER) -> str:
    """What the agent does: get an upload link, then PUT the file to it."""
    ref = service.create_photo_upload(user).photo_ref
    s3.put_object(Bucket=BUCKET, Key=ref, Body=data)
    return ref


def test_sniff_recognises_common_formats():
    assert sniff(JPEG).format == "jpeg"
    assert sniff(b"\x89PNG\r\n\x1a\n" + b"x" * 10).format == "png"
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ").format == "webp"
    assert sniff(b"GIF89a...").format == "gif"


def test_upload_link_is_a_presigned_put_for_this_users_upload(service):
    upload_ = service.create_photo_upload(USER)
    assert upload_.photo_ref.startswith(f"uploads/{USER}/")
    assert BUCKET in upload_.upload_url and upload_.photo_ref in upload_.upload_url
    assert "X-Amz-Signature=" in upload_.upload_url


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"%PDF-1.7", "JPEG, PNG"),
        (b"\xff\xd8\xff" + b"x" * MAX_PHOTO_BYTES, "smaller size"),
    ],
)
def test_bad_uploads_are_rejected_with_a_useful_message(service, s3, data, message):
    ref = upload(service, s3, data)
    with pytest.raises(InvalidPhotoError, match=message):
        service.search_wines(USER, photo_ref=ref)


def test_missing_or_expired_upload_says_to_upload_again(service):
    ref = service.create_photo_upload(USER).photo_ref  # link created, but nothing was uploaded
    with pytest.raises(InvalidPhotoError, match="upload again"):
        service.search_wines(USER, photo_ref=ref)


def test_another_users_upload_cannot_be_used(service, s3):
    ref = upload(service, s3, user="usr_other")
    with pytest.raises(InvalidPhotoError, match="upload again"):
        service.search_wines(USER, photo_ref=ref)


def test_a_bad_upload_does_not_half_save_the_wine(service, s3):
    ref = service.create_photo_upload(USER).photo_ref
    with pytest.raises(InvalidPhotoError):
        service.save_wine(USER, producer="Ridge", photo_ref=ref)
    assert service.list_wines(USER) == []


def test_add_wine_photo_stores_indexes_and_clears_the_upload(service, s3, photo_vectors, bedrock):
    wine, _ = service.save_wine(USER, producer="Ridge", name="Monte Bello")
    service.add_wine_photo(USER, wine.wine_id, upload(service, s3))
    ref = upload(service, s3)
    updated = service.add_wine_photo(USER, wine.wine_id, ref)

    assert len(updated.photo_keys) == 2
    assert service.get_wine(USER, wine.wine_id).photo_keys == updated.photo_keys
    key = updated.photo_keys[0]
    assert key.startswith(f"{USER}/{wine.wine_id}/") and key.endswith(".jpg")
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == JPEG
    assert s3.list_objects_v2(Bucket=BUCKET, Prefix="uploads/").get("KeyCount") == 0
    assert photo_vectors.vectors[key]["metadata"] == {"user_id": USER, "wine_id": wine.wine_id}
    image = bedrock.bodies[-1]["singleEmbeddingParams"]["image"]
    assert image["format"] == "jpeg" and base64.b64decode(image["source"]["bytes"]) == JPEG


def test_search_then_save_with_the_same_photo_ref(service, s3):
    ref = upload(service, s3)
    assert service.search_wines(USER, "ridge", photo_ref=ref) == []
    wine, created = service.save_wine(USER, producer="Ridge", photo_ref=ref)
    assert created and len(wine.photo_keys) == 1
    again, created_again = service.save_wine(USER, producer="Ridge", photo_ref=upload(service, s3))
    assert not created_again and len(again.photo_keys) == 2


def test_delete_wine_removes_its_photos_and_vectors(service, s3, photo_vectors):
    wine, _ = service.save_wine(USER, producer="Ridge", photo_ref=upload(service, s3))
    service.delete_wine(USER, wine.wine_id)
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    assert photo_vectors.vectors == {}


def test_search_by_photo_returns_each_wine_once(service, s3, photo_vectors, bedrock):
    ridge, _ = service.save_wine(USER, producer="Ridge", photo_ref=upload(service, s3))
    ridge = service.add_wine_photo(USER, ridge.wine_id, upload(service, s3))
    other, _ = service.save_wine(USER, producer="Lopez de Heredia", photo_ref=upload(service, s3))
    photo_vectors.ranking = [ridge.photo_keys[1], ridge.photo_keys[0], *other.photo_keys]

    results = service.search_wines(USER, photo_ref=upload(service, s3))
    assert [w.wine_id for w in results] == [ridge.wine_id, other.wine_id]
    assert bedrock.bodies[-1]["singleEmbeddingParams"]["embeddingPurpose"] == "IMAGE_RETRIEVAL"
    assert photo_vectors.last_query["filter"] == {"user_id": {"$eq": USER}}


def test_search_with_photo_and_label_text_merges_both(service, s3, photo_vectors):
    ridge, _ = service.save_wine(USER, producer="Ridge", name="Monte Bello", photo_ref=upload(service, s3))
    other, _ = service.save_wine(USER, producer="Lopez de Heredia", photo_ref=upload(service, s3))
    photo_vectors.ranking = [*other.photo_keys, *ridge.photo_keys]
    # The photo alone ranks the other wine first; the label text agrees with Ridge's photo match.
    results = service.search_wines(USER, "Ridge Monte Bello", photo_ref=upload(service, s3))
    assert results[0].wine_id == ridge.wine_id


def test_photo_vector_failures_do_not_lose_the_photo(table, s3, bedrock):
    service = WineService(
        WineRepository(table),
        photos=PhotoStore(s3, BUCKET),
        photo_index=WinePhotoIndex(FakeS3Vectors(fail=True), Embedder(bedrock), bucket="vb", index="wine-photos"),
    )
    wine, _ = service.save_wine(USER, producer="Ridge", photo_ref=upload(service, s3))
    assert len(service.get_wine(USER, wine.wine_id).photo_keys) == 1
    assert [w.wine_id for w in service.search_wines(USER, "ridge", photo_ref=upload(service, s3))] == [wine.wine_id]


def test_photos_need_a_bucket(table):
    service = WineService(WineRepository(table))
    wine, _ = service.save_wine(USER, producer="Ridge")
    with pytest.raises(PhotosDisabledError):
        service.create_photo_upload(USER)
    with pytest.raises(PhotosDisabledError):
        service.add_wine_photo(USER, wine.wine_id, "uploads/usr_1/x")
