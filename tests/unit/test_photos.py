import base64

import boto3
import pytest

from wine_cellar.photos import MAX_PHOTO_BYTES, InvalidPhotoError, PhotoStore, decode_photo
from wine_cellar.repository import WineRepository
from wine_cellar.service import PhotosDisabledError, WineService
from wine_cellar.vectors import Embedder, WinePhotoIndex, WineTextIndex

from .fakes import FakeBedrock, FakeS3Vectors

USER = "usr_1"
JPEG = b"\xff\xd8\xff\xe0" + b"label" * 100
BUCKET = "photos-test"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture
def bedrock():
    return FakeBedrock()


@pytest.fixture
def photo_vectors():
    return FakeS3Vectors()


@pytest.fixture
def s3(table):  # `table` keeps moto's mock_aws active
    client = boto3.client("s3", region_name="us-east-1")
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


def test_decode_accepts_common_formats_and_data_urls():
    assert decode_photo(b64(JPEG)).format == "jpeg"
    assert decode_photo("data:image/jpeg;base64," + b64(JPEG)).data == JPEG
    assert decode_photo(b64(b"\x89PNG\r\n\x1a\n" + b"x" * 10)).format == "png"
    assert decode_photo(b64(b"RIFF\x00\x00\x00\x00WEBPVP8 ")).format == "webp"


@pytest.mark.parametrize(
    ("photo", "message"),
    [
        ("not base64!!", "base64"),
        (b64(b"%PDF-1.7"), "JPEG, PNG"),
        (b64(b"\xff\xd8\xff" + b"x" * MAX_PHOTO_BYTES), "smaller size"),
    ],
)
def test_decode_rejects_bad_photos_with_a_useful_message(photo, message):
    with pytest.raises(InvalidPhotoError, match=message):
        decode_photo(photo)


def test_add_wine_photo_stores_and_indexes_each_photo(service, s3, photo_vectors, bedrock):
    wine, _ = service.save_wine(USER, producer="Ridge", name="Monte Bello")
    service.add_wine_photo(USER, wine.wine_id, decode_photo(b64(JPEG)))
    updated = service.add_wine_photo(USER, wine.wine_id, decode_photo(b64(JPEG)))

    assert len(updated.photo_keys) == 2
    assert service.get_wine(USER, wine.wine_id).photo_keys == updated.photo_keys
    key = updated.photo_keys[0]
    assert key.startswith(f"{USER}/{wine.wine_id}/") and key.endswith(".jpg")
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == JPEG
    assert photo_vectors.vectors[key]["metadata"] == {"user_id": USER, "wine_id": wine.wine_id}
    image = bedrock.bodies[-1]["singleEmbeddingParams"]["image"]
    assert image["format"] == "jpeg" and base64.b64decode(image["source"]["bytes"]) == JPEG


def test_save_wine_can_attach_a_photo(service):
    wine, created = service.save_wine(USER, producer="Ridge", photo=decode_photo(b64(JPEG)))
    assert created and len(wine.photo_keys) == 1
    again, created_again = service.save_wine(USER, producer="Ridge", photo=decode_photo(b64(JPEG)))
    assert not created_again and len(again.photo_keys) == 2


def test_delete_wine_removes_its_photos_and_vectors(service, s3, photo_vectors):
    wine, _ = service.save_wine(USER, producer="Ridge", photo=decode_photo(b64(JPEG)))
    service.delete_wine(USER, wine.wine_id)
    assert s3.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0
    assert photo_vectors.vectors == {}


def test_search_by_photo_returns_each_wine_once(service, photo_vectors, bedrock):
    ridge, _ = service.save_wine(USER, producer="Ridge", photo=decode_photo(b64(JPEG)))
    ridge = service.add_wine_photo(USER, ridge.wine_id, decode_photo(b64(JPEG)))
    other, _ = service.save_wine(USER, producer="Lopez de Heredia", photo=decode_photo(b64(JPEG)))
    photo_vectors.ranking = [ridge.photo_keys[1], ridge.photo_keys[0], *other.photo_keys]

    results = service.search_wines(USER, photo=decode_photo(b64(JPEG)))
    assert [w.wine_id for w in results] == [ridge.wine_id, other.wine_id]
    assert bedrock.bodies[-1]["singleEmbeddingParams"]["embeddingPurpose"] == "IMAGE_RETRIEVAL"
    assert photo_vectors.last_query["filter"] == {"user_id": {"$eq": USER}}


def test_search_with_photo_and_label_text_merges_both(service, photo_vectors):
    ridge, _ = service.save_wine(USER, producer="Ridge", name="Monte Bello", photo=decode_photo(b64(JPEG)))
    other, _ = service.save_wine(USER, producer="Lopez de Heredia", photo=decode_photo(b64(JPEG)))
    photo_vectors.ranking = [*other.photo_keys, *ridge.photo_keys]
    # The photo alone ranks the other wine first; the label text agrees with Ridge's photo match.
    results = service.search_wines(USER, "Ridge Monte Bello", photo=decode_photo(b64(JPEG)))
    assert results[0].wine_id == ridge.wine_id


def test_photo_vector_failures_do_not_lose_the_photo(table, s3, bedrock):
    service = WineService(
        WineRepository(table),
        photos=PhotoStore(s3, BUCKET),
        photo_index=WinePhotoIndex(FakeS3Vectors(fail=True), Embedder(bedrock), bucket="vb", index="wine-photos"),
    )
    wine, _ = service.save_wine(USER, producer="Ridge", photo=decode_photo(b64(JPEG)))
    assert len(service.get_wine(USER, wine.wine_id).photo_keys) == 1
    assert [w.wine_id for w in service.search_wines(USER, "ridge", photo=decode_photo(b64(JPEG)))] == [wine.wine_id]


def test_photos_need_a_bucket(table):
    service = WineService(WineRepository(table))
    wine, _ = service.save_wine(USER, producer="Ridge")
    with pytest.raises(PhotosDisabledError):
        service.add_wine_photo(USER, wine.wine_id, decode_photo(b64(JPEG)))
