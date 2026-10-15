"""Adapters: the requests sent to Bedrock and S3 Vectors, against fakes of those APIs.

Service tests use fakes of our own TextIndex and PhotoIndex instead, so only these tests know the vendor request
shapes. e2e checks them against the real services after every deploy.
"""

import base64

import pytest
from botocore.exceptions import ClientError

from wine_cellar.models import InvalidPhotoError, Wine
from wine_cellar.vectors import EMBEDDING_DIMENSION, Embedder, WinePhotoIndex, WineTextIndex

from .conftest import USER
from .fakes import FakeBedrock, FakeS3Vectors


@pytest.fixture
def bedrock():
    return FakeBedrock()


@pytest.fixture
def s3vectors():
    return FakeS3Vectors()


def _text_index(s3vectors, bedrock) -> WineTextIndex:
    return WineTextIndex(s3vectors, Embedder(bedrock), bucket="vb", index="wine-text")


def _photo_index(s3vectors, bedrock) -> WinePhotoIndex:
    return WinePhotoIndex(s3vectors, Embedder(bedrock), bucket="vb", index="wine-photos")


def test_embedder_sends_nova_requests(bedrock):
    embedder = Embedder(bedrock)
    embedder.embed_text("bold red", "GENERIC_INDEX")
    embedder.embed_image(b"jpegbytes", "jpeg", "IMAGE_RETRIEVAL")
    text, image = (b["singleEmbeddingParams"] for b in bedrock.bodies)
    assert text["embeddingPurpose"] == "GENERIC_INDEX"
    assert text["embeddingDimension"] == EMBEDDING_DIMENSION
    assert text["text"]["value"] == "bold red"
    assert image["embeddingPurpose"] == "IMAGE_RETRIEVAL"
    assert image["image"]["format"] == "jpeg"
    assert base64.b64decode(image["image"]["source"]["bytes"]) == b"jpegbytes"


def test_embedder_turns_an_unreadable_image_into_invalid_photo(bedrock):
    bedrock.image_error = "ValidationException"
    with pytest.raises(InvalidPhotoError):
        Embedder(bedrock).embed_image(b"garbage", "jpeg", "GENERIC_INDEX")


def test_embedder_raises_other_bedrock_errors_as_they_are(bedrock):
    bedrock.image_error = "ThrottlingException"  # raised, so the caller can retry
    with pytest.raises(ClientError):
        Embedder(bedrock).embed_image(b"jpegbytes", "jpeg", "GENERIC_INDEX")


def test_text_index_stores_one_vector_per_wine_tagged_with_its_user(bedrock, s3vectors):
    index = _text_index(s3vectors, bedrock)
    wine = Wine(wine_id="w1", user_id=USER, name="Ridge", notes="Bold")
    index.upsert(wine)
    assert s3vectors.vectors["w1"]["metadata"] == {"user_id": USER}
    assert "Bold" in bedrock.bodies[-1]["singleEmbeddingParams"]["text"]["value"]
    s3vectors.ranking = ["w1"]
    assert index.query(USER, "bold red") == ["w1"]
    assert index.query("usr_other", "bold red") == []
    index.delete("w1")
    assert s3vectors.vectors == {}


def test_text_index_queries_this_users_vectors_for_text_retrieval(bedrock, s3vectors):
    _text_index(s3vectors, bedrock).query(USER, "bold red")
    assert bedrock.bodies[-1]["singleEmbeddingParams"]["embeddingPurpose"] == "TEXT_RETRIEVAL"
    assert s3vectors.last_query == {"topK": 10, "filter": {"user_id": {"$eq": USER}}}


def test_photo_index_stores_one_vector_per_photo_tagged_with_its_user_and_wine(bedrock, s3vectors):
    index = _photo_index(s3vectors, bedrock)
    index.upsert("wines/usr_1/w1/p1", b"jpegbytes", "jpeg", user_id=USER, wine_id="w1")
    assert s3vectors.vectors["wines/usr_1/w1/p1"]["metadata"] == {"user_id": USER, "wine_id": "w1"}
    s3vectors.ranking = ["wines/usr_1/w1/p1"]
    assert index.query(USER, b"jpegbytes", "jpeg") == ["wines/usr_1/w1/p1"]
    index.delete(["wines/usr_1/w1/p1"])
    index.delete([])  # nothing to send
    assert s3vectors.vectors == {}


def test_photo_index_queries_this_users_vectors_for_image_retrieval(bedrock, s3vectors):
    _photo_index(s3vectors, bedrock).query(USER, b"jpegbytes", "jpeg")
    assert bedrock.bodies[-1]["singleEmbeddingParams"]["embeddingPurpose"] == "IMAGE_RETRIEVAL"
    assert s3vectors.last_query["filter"] == {"user_id": {"$eq": USER}}
