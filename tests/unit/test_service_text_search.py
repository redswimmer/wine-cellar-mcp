"""High gear: text search through WineService, with a fake text index."""

from wine_cellar.service import WineService

from .conftest import USER


def test_updates_refresh_the_vector(service, text_index):
    wine = service.create_wine(USER, name="Ridge Monte Bello")
    service.update_wine(USER, wine.wine_id, notes="Opened up after an hour")
    assert text_index.indexed[wine.wine_id].notes == "Opened up after an hour"


def test_delete_removes_the_vector(service, text_index):
    wine = service.create_wine(USER, name="Ridge")
    service.delete_wine(USER, wine.wine_id)
    assert text_index.indexed == {}


def test_vector_failures_do_not_lose_the_wine(repo, text_index):
    text_index.fail = True
    service = WineService(repo, text_index)
    wine = service.create_wine(USER, name="Ridge")
    assert service.get_wine(USER, wine.wine_id).name == "Ridge"
    service.delete_wine(USER, wine.wine_id)
    assert service.list_wines(USER)[0] == []


def test_search_returns_vector_hits_whose_names_do_not_match(service, text_index):
    ridge = service.create_wine(USER, name="Ridge Monte Bello", notes="huge, structured")
    service.create_wine(USER, name="Lopez de Heredia Tondonia")
    text_index.ranking = [ridge.wine_id]
    results = service.search_wines(USER, "that bold red I loved")
    assert [w.wine_id for w in results] == [ridge.wine_id]
    assert service.search_wines("other", "that bold red I loved") == []


def test_search_ranks_wines_found_both_ways_first(service, text_index):
    a = service.create_wine(USER, name="Ridge Monte Bello")
    b = service.create_wine(USER, name="Lopez de Heredia Tondonia")
    text_index.ranking = [b.wine_id, a.wine_id]
    # Only fuzzy matching finds Monte Bello; vectors rank Tondonia first but find Monte Bello too.
    ids = [w.wine_id for w in service.search_wines(USER, "ridge monte bello")]
    assert ids[0] == a.wine_id
    assert b.wine_id in ids  # found only by its vector


def test_search_ignores_vectors_of_deleted_wines(service, text_index):
    kept = service.create_wine(USER, name="Ridge")
    gone = service.create_wine(USER, name="Tondonia")
    text_index.fail = True  # the delete's vector cleanup fails, so its vector is left behind
    service.delete_wine(USER, gone.wine_id)
    text_index.fail = False
    text_index.ranking = [gone.wine_id, kept.wine_id]
    assert [w.wine_id for w in service.search_wines(USER, "anything")] == [kept.wine_id]


def test_search_falls_back_to_names_when_vectors_fail(service, text_index):
    service.create_wine(USER, name="Lopez de Heredia Tondonia")
    text_index.fail = True
    assert [w.name for w in service.search_wines(USER, "tondonia")] == ["Lopez de Heredia Tondonia"]


def test_reindex_rewrites_every_wine_vector(repo, service, text_index):
    unindexed = WineService(repo)  # e.g. wines saved before the index existed
    wines = [unindexed.create_wine(USER, name=name) for name in ["Ridge", "Tondonia"]]
    assert service.reindex(USER) == (2, 0)
    assert sorted(text_index.indexed) == sorted(w.wine_id for w in wines)
