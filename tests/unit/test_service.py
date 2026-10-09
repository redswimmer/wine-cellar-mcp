from datetime import UTC, datetime, timedelta

import pytest

from wine_cellar.models import Verdict, Wine
from wine_cellar.service import WineNotFoundError

USER = "123456"


def test_create_then_update_only_the_given_fields(service):
    wine = service.create_wine(USER, name=" Ridge Monte Bello ", vintage=2019, verdict=Verdict.LIKE, notes="Bold")
    assert wine.name == "Ridge Monte Bello"

    updated = service.update_wine(USER, wine.wine_id, verdict=Verdict.LOVE, notes="Bold\nEven better with food")
    assert updated.verdict == Verdict.LOVE
    assert updated.notes == "Bold\nEven better with food"
    assert updated.vintage == 2019  # not cleared by an update that omitted it
    assert updated.created_at == wine.created_at
    assert updated.updated_at > wine.updated_at
    assert service.get_wine(USER, wine.wine_id) == updated


def test_update_empties_fields_sent_as_none(service):
    wine = service.create_wine(USER, name="Ridge", vintage=2019, varietal="Zinfandel", verdict=Verdict.LIKE, notes="ok")
    updated = service.update_wine(USER, wine.wine_id, vintage=None, verdict=None, notes=None, varietal="Cabernet")
    assert (updated.vintage, updated.verdict, updated.notes, updated.varietal) == (None, None, "", "Cabernet")
    assert updated.name == "Ridge"


def test_only_the_name_is_required(service):
    wine = service.create_wine(USER, name="Monte Bello")
    assert (wine.vintage, wine.varietal, wine.verdict, wine.notes) == (None, "", None, "")


def test_update_unknown_wine(service):
    with pytest.raises(WineNotFoundError):
        service.update_wine(USER, "nope", notes="x")


def test_users_are_isolated(service):
    wine = service.create_wine(USER, name="Ridge Monte Bello")
    assert service.list_wines("other") == []
    with pytest.raises(WineNotFoundError):
        service.get_wine("other", wine.wine_id)


def test_search_ranks_closest_match_first(service):
    service.create_wine(USER, name="Ridge Monte Bello", vintage=2019)
    service.create_wine(USER, name="Lopez de Heredia Viña Tondonia", vintage=2010)
    results = service.search_wines(USER, "tondonia")
    assert results[0].name == "Lopez de Heredia Viña Tondonia"
    assert all(r.name != "Ridge Monte Bello" for r in results)


def test_list_filters_by_verdict_and_varietal(service):
    service.create_wine(USER, name="A", verdict=Verdict.LOVE, varietal="Syrah")
    service.create_wine(USER, name="B", verdict=Verdict.DISLIKE, varietal="Chardonnay")
    assert [w.name for w in service.list_wines(USER, verdict=Verdict.LOVE)] == ["A"]
    assert [w.name for w in service.list_wines(USER, varietal="syrah")] == ["A"]


def test_list_filters_by_dates(service):
    older = service.create_wine(USER, name="Older")
    newer = service.create_wine(USER, name="Newer")
    cutoff = older.created_at + (newer.created_at - older.created_at) / 2
    assert [w.name for w in service.list_wines(USER, created_after=cutoff)] == ["Newer"]
    assert [w.name for w in service.list_wines(USER, created_before=cutoff)] == ["Older"]

    service.update_wine(USER, older.wine_id, notes="changed my mind")
    assert [w.name for w in service.list_wines(USER, updated_after=newer.updated_at + timedelta(microseconds=1))] == [
        "Older"
    ]
    # A naive datetime counts as UTC.
    naive = (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None)
    assert service.list_wines(USER, created_after=naive) == []


def test_reads_wines_saved_before_the_model_was_simplified():
    wine = Wine.model_validate(
        {
            "wine_id": "w",
            "user_id": USER,
            "producer": "Ridge",
            "name": "Monte Bello",
            "varietals": ["Cab", "Merlot"],
            "region": "Santa Cruz",
            "tastings": [],
            "rating": 5,
        }
    )
    assert (wine.name, wine.varietal) == ("Ridge Monte Bello", "Cab, Merlot")


def test_delete(service):
    wine = service.create_wine(USER, name="Ridge")
    service.delete_wine(USER, wine.wine_id)
    with pytest.raises(WineNotFoundError):
        service.delete_wine(USER, wine.wine_id)
