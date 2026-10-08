from datetime import date

import pytest

from wine_cellar.models import Style, Verdict
from wine_cellar.service import WineNotFoundError

USER = "123456"


def test_save_creates_then_updates_same_wine(service):
    wine, created = service.save_wine(USER, producer="Ridge", name="Monte Bello", vintage=2019, verdict=Verdict.LOVE)
    assert created

    again, created_again = service.save_wine(
        USER, producer="ridge", name="Monte-Bello", vintage=2019, notes="Even better with food"
    )
    assert not created_again
    assert again.wine_id == wine.wine_id
    assert again.verdict == Verdict.LOVE  # not cleared by an update that omitted it
    assert again.notes == "Even better with food"
    assert len(service.list_wines(USER)) == 1


def test_different_vintage_is_a_different_wine(service):
    service.save_wine(USER, producer="Ridge", name="Monte Bello", vintage=2019)
    _, created = service.save_wine(USER, producer="Ridge", name="Monte Bello", vintage=2018)
    assert created


def test_users_are_isolated(service):
    wine, _ = service.save_wine(USER, producer="Ridge", name="Monte Bello")
    assert service.list_wines("other") == []
    with pytest.raises(WineNotFoundError):
        service.get_wine("other", wine.wine_id)


def test_search_ranks_closest_match_first(service):
    service.save_wine(USER, producer="Ridge", name="Monte Bello", vintage=2019, region="Santa Cruz Mountains")
    service.save_wine(USER, producer="Lopez de Heredia", name="Viña Tondonia", vintage=2010, region="Rioja")
    results = service.search_wines(USER, "tondonia rioja")
    assert results[0][0].producer == "Lopez de Heredia"
    assert all(r[0].producer != "Ridge" for r in results)


def test_add_tasting_updates_verdict_and_history(service):
    wine, _ = service.save_wine(USER, producer="Ridge", name="Monte Bello", verdict=Verdict.LIKE)
    updated = service.add_tasting(
        USER, wine.wine_id, notes="Opened up after an hour", verdict=Verdict.LOVE, tasted_on=date(2026, 10, 1)
    )
    assert updated.verdict == Verdict.LOVE
    assert updated.tastings[0].tasted_on == date(2026, 10, 1)
    assert service.get_wine(USER, wine.wine_id).tastings == updated.tastings


def test_list_filters(service):
    service.save_wine(USER, producer="A", style=Style.RED, verdict=Verdict.LOVE, varietals=["Syrah"], region="Rhône")
    service.save_wine(USER, producer="B", style=Style.WHITE, verdict=Verdict.DISLIKE, varietals=["Chardonnay"])
    assert [w.producer for w in service.list_wines(USER, verdict=Verdict.LOVE)] == ["A"]
    assert [w.producer for w in service.list_wines(USER, style=Style.WHITE)] == ["B"]
    assert [w.producer for w in service.list_wines(USER, varietal="syrah")] == ["A"]
    assert [w.producer for w in service.list_wines(USER, region="rhône")] == ["A"]


def test_delete(service):
    wine, _ = service.save_wine(USER, producer="Ridge")
    service.delete_wine(USER, wine.wine_id)
    with pytest.raises(WineNotFoundError):
        service.delete_wine(USER, wine.wine_id)
