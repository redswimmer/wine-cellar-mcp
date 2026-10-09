"""Adapters: the requests sent to Bedrock and S3 Vectors. Service tests check behaviour only."""

import base64

from wine_cellar.vectors import EMBEDDING_DIMENSION, Embedder, WinePhotoIndex, WineTextIndex

from .conftest import USER


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


def test_text_index_queries_this_users_vectors_for_text_retrieval(bedrock, text_vectors):
    WineTextIndex(text_vectors, Embedder(bedrock), bucket="vb", index="wine-text").query(USER, "bold red")
    assert bedrock.bodies[-1]["singleEmbeddingParams"]["embeddingPurpose"] == "TEXT_RETRIEVAL"
    assert text_vectors.last_query == {"topK": 10, "filter": {"user_id": {"$eq": USER}}}


def test_photo_index_queries_this_users_vectors_for_image_retrieval(bedrock, text_vectors):
    WinePhotoIndex(text_vectors, Embedder(bedrock), bucket="vb", index="wine-photos").query(USER, b"jpegbytes", "jpeg")
    assert bedrock.bodies[-1]["singleEmbeddingParams"]["embeddingPurpose"] == "IMAGE_RETRIEVAL"
    assert text_vectors.last_query["filter"] == {"user_id": {"$eq": USER}}
