import base64

from wine_cellar.models import Verdict, Wine
from wine_cellar.repository import WineRepository
from wine_cellar.search import fuse
from wine_cellar.service import WineService
from wine_cellar.vectors import EMBEDDING_DIMENSION, Embedder, WinePhotoIndex, WineTextIndex, wine_text

from .conftest import USER
from .fakes import FakeS3Vectors


def test_wine_text_includes_label_details_and_what_the_person_said():
    wine = Wine(
        wine_id="w1",
        user_id=USER,
        name="Ridge Monte Bello",
        vintage=2019,
        varietal="Cabernet Sauvignon",
        verdict=Verdict.LOVE,
        notes="Huge and structured",
    )
    text = wine_text(wine)
    for part in ["Ridge Monte Bello 2019", "Cabernet", "love", "Huge"]:
        assert part in text


def test_save_writes_a_text_vector_scoped_to_the_user(service, text_vectors):
    wine = service.create_wine(USER, name="Ridge Monte Bello", notes="bold")
    assert text_vectors.vectors[wine.wine_id]["metadata"] == {"user_id": USER}


def test_updates_refresh_the_vector(service, text_vectors):
    wine = service.create_wine(USER, name="Ridge Monte Bello")
    before = text_vectors.vectors[wine.wine_id]["data"]
    service.update_wine(USER, wine.wine_id, notes="Opened up after an hour")
    assert text_vectors.vectors[wine.wine_id]["data"] != before


def test_delete_removes_the_vector(service, text_vectors):
    wine = service.create_wine(USER, name="Ridge")
    service.delete_wine(USER, wine.wine_id)
    assert text_vectors.vectors == {}


def test_vector_failures_do_not_lose_the_wine(table, bedrock):
    service = WineService(
        WineRepository(table),
        WineTextIndex(FakeS3Vectors(fail=True), Embedder(bedrock), bucket="vb", index="wine-text"),
    )
    wine = service.create_wine(USER, name="Ridge")
    assert service.get_wine(USER, wine.wine_id).name == "Ridge"
    service.delete_wine(USER, wine.wine_id)
    assert service.list_wines(USER)[0] == []


def test_search_returns_vector_hits_whose_names_do_not_match(service, text_vectors):
    ridge = service.create_wine(USER, name="Ridge Monte Bello", notes="huge, structured")
    service.create_wine(USER, name="Lopez de Heredia Tondonia")
    text_vectors.ranking = [ridge.wine_id]
    results = service.search_wines(USER, "that bold red I loved")
    assert [w.wine_id for w in results] == [ridge.wine_id]


def test_search_ranks_wines_found_both_ways_first(service, text_vectors):
    a = service.create_wine(USER, name="Ridge Monte Bello")
    b = service.create_wine(USER, name="Lopez de Heredia Tondonia")
    text_vectors.ranking = [b.wine_id, a.wine_id]
    # Only fuzzy matching finds Monte Bello; vectors rank Tondonia first but find Monte Bello too.
    ids = [w.wine_id for w in service.search_wines(USER, "ridge monte bello")]
    assert ids[0] == a.wine_id
    assert b.wine_id in ids  # found only by its vector


def test_search_ignores_vectors_of_deleted_wines(service, text_vectors):
    wine = service.create_wine(USER, name="Ridge")
    text_vectors.vectors["gone"] = {"key": "gone", "metadata": {"user_id": USER}}
    text_vectors.ranking = ["gone", wine.wine_id]
    assert [w.wine_id for w in service.search_wines(USER, "anything")] == [wine.wine_id]


def test_search_falls_back_to_names_when_vectors_fail(service, text_vectors):
    service.create_wine(USER, name="Lopez de Heredia Tondonia")
    text_vectors.fail = True
    assert [w.name for w in service.search_wines(USER, "tondonia")] == ["Lopez de Heredia Tondonia"]


def test_reindex_rewrites_every_wine_vector(table, service, text_vectors):
    unindexed = WineService(WineRepository(table))  # e.g. wines saved before the index existed
    wines = [unindexed.create_wine(USER, name=name) for name in ["Ridge", "Tondonia"]]
    assert service.reindex(USER) == (2, 0)
    assert sorted(text_vectors.vectors) == sorted(w.wine_id for w in wines)


def test_fuse_rewards_agreement():
    assert fuse([["a", "b"], ["b", "c"]]) == ["b", "a", "c"]
    assert fuse([[], ["x"]]) == ["x"]


# Adapter tests: the requests sent to Bedrock and S3 Vectors. Service tests above check behaviour only.


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
