import pytest

from wine_cellar.models import Verdict, Wine
from wine_cellar.repository import WineRepository
from wine_cellar.search import fuse
from wine_cellar.service import WineService
from wine_cellar.vectors import EMBEDDING_DIMENSION, Embedder, WineTextIndex, wine_text

from .fakes import FakeBedrock, FakeS3Vectors

USER = "usr_1"


@pytest.fixture
def bedrock():
    return FakeBedrock()


@pytest.fixture
def s3v():
    return FakeS3Vectors()


@pytest.fixture
def indexed_service(table, bedrock, s3v):
    return WineService(WineRepository(table), WineTextIndex(s3v, Embedder(bedrock), bucket="vb", index="wine-text"))


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


def test_save_writes_a_text_vector_scoped_to_the_user(indexed_service, bedrock, s3v):
    wine = indexed_service.create_wine(USER, name="Ridge Monte Bello", notes="bold")
    vector = s3v.vectors[wine.wine_id]
    assert vector["metadata"] == {"user_id": USER}
    assert len(vector["data"]["float32"]) == EMBEDDING_DIMENSION
    params = bedrock.bodies[0]["singleEmbeddingParams"]
    assert params["embeddingPurpose"] == "GENERIC_INDEX"
    assert params["embeddingDimension"] == EMBEDDING_DIMENSION
    assert "bold" in params["text"]["value"]


def test_updates_refresh_the_vector(indexed_service, bedrock):
    wine = indexed_service.create_wine(USER, name="Ridge Monte Bello")
    indexed_service.update_wine(USER, wine.wine_id, notes="Opened up after an hour")
    assert len(bedrock.bodies) == 2
    assert "Opened up" in bedrock.bodies[-1]["singleEmbeddingParams"]["text"]["value"]


def test_delete_removes_the_vector(indexed_service, s3v):
    wine = indexed_service.create_wine(USER, name="Ridge")
    indexed_service.delete_wine(USER, wine.wine_id)
    assert s3v.vectors == {}


def test_vector_failures_do_not_lose_the_wine(table, bedrock):
    service = WineService(
        WineRepository(table),
        WineTextIndex(FakeS3Vectors(fail=True), Embedder(bedrock), bucket="vb", index="wine-text"),
    )
    wine = service.create_wine(USER, name="Ridge")
    assert service.get_wine(USER, wine.wine_id).name == "Ridge"
    service.delete_wine(USER, wine.wine_id)
    assert service.list_wines(USER)[0] == []


def test_search_finds_wines_by_meaning_when_names_do_not_match(indexed_service, s3v, bedrock):
    ridge = indexed_service.create_wine(USER, name="Ridge Monte Bello", notes="huge, structured")
    indexed_service.create_wine(USER, name="Lopez de Heredia Tondonia")
    s3v.ranking = [ridge.wine_id]
    results = indexed_service.search_wines(USER, "that bold red I loved")
    assert [w.wine_id for w in results] == [ridge.wine_id]
    assert bedrock.bodies[-1]["singleEmbeddingParams"]["embeddingPurpose"] == "TEXT_RETRIEVAL"
    assert s3v.last_query == {"topK": 10, "filter": {"user_id": {"$eq": USER}}}


def test_search_ranks_wines_found_both_ways_first(indexed_service, s3v):
    a = indexed_service.create_wine(USER, name="Ridge Monte Bello")
    b = indexed_service.create_wine(USER, name="Lopez de Heredia Tondonia")
    s3v.ranking = [b.wine_id, a.wine_id]
    # Only fuzzy matching finds Monte Bello; vectors rank Tondonia first but find Monte Bello too.
    results = indexed_service.search_wines(USER, "ridge monte bello")
    assert results[0].wine_id == a.wine_id


def test_search_ignores_vectors_of_deleted_wines(indexed_service, s3v):
    wine = indexed_service.create_wine(USER, name="Ridge")
    s3v.vectors["gone"] = {"key": "gone", "metadata": {"user_id": USER}}
    s3v.ranking = ["gone", wine.wine_id]
    assert [w.wine_id for w in indexed_service.search_wines(USER, "anything")] == [wine.wine_id]


def test_search_falls_back_to_names_when_vectors_fail(table, bedrock):
    s3v = FakeS3Vectors()
    service = WineService(WineRepository(table), WineTextIndex(s3v, Embedder(bedrock), bucket="vb", index="wine-text"))
    service.create_wine(USER, name="Lopez de Heredia Tondonia")
    s3v.fail = True
    assert [w.name for w in service.search_wines(USER, "tondonia")] == ["Lopez de Heredia Tondonia"]


def test_fuse_rewards_agreement():
    assert fuse([["a", "b"], ["b", "c"]]) == ["b", "a", "c"]
    assert fuse([[], ["x"]]) == ["x"]
