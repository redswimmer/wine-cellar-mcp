"""High gear: text vectors and search through WineService, with fake Bedrock and S3 Vectors."""

from wine_cellar.repository import WineRepository
from wine_cellar.service import WineService
from wine_cellar.vectors import Embedder, WineTextIndex

from .conftest import USER
from .fakes import FakeS3Vectors


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
