"""High gear: recording, listing and finding wines through WineService, using primitives only."""

from datetime import UTC, datetime, timedelta

import pytest

from wine_cellar.models import Verdict
from wine_cellar.service import InvalidPageTokenError, InvalidWineError, WineNotFoundError

from .conftest import USER


def test_create_then_update_only_the_given_fields(service):
    wine = service.create_wine(USER, name=" Ridge Monte Bello ", varietal="Cab", verdict=Verdict.LIKE, notes="Bold")
    assert wine.name == "Ridge Monte Bello"

    updated = service.update_wine(USER, wine.wine_id, verdict=Verdict.LOVE, notes="Bold\nEven better with food")
    assert updated.verdict == Verdict.LOVE
    assert updated.notes == "Bold\nEven better with food"
    assert updated.varietal == "Cab"  # not cleared by an update that omitted it
    assert updated.created_at == wine.created_at
    assert updated.updated_at > wine.updated_at
    assert service.get_wine(USER, wine.wine_id) == updated


def test_update_empties_fields_sent_as_none(service):
    wine = service.create_wine(USER, name="Ridge", varietal="Zinfandel", verdict=Verdict.LIKE, notes="ok")
    updated = service.update_wine(USER, wine.wine_id, verdict=None, notes=None, varietal="Cabernet")
    assert (updated.verdict, updated.notes, updated.varietal) == (None, "", "Cabernet")
    assert updated.name == "Ridge"


def test_only_the_name_is_required(service):
    wine = service.create_wine(USER, name="Monte Bello")
    assert (wine.varietal, wine.verdict, wine.notes) == ("", None, "")


@pytest.mark.parametrize("name", ["", "   "])
def test_a_wine_needs_a_name(service, name):
    with pytest.raises(InvalidWineError):
        service.create_wine(USER, name=name)
    wine = service.create_wine(USER, name="Ridge")
    for bad in (name, None):
        with pytest.raises(InvalidWineError):
            service.update_wine(USER, wine.wine_id, name=bad)
    assert service.get_wine(USER, wine.wine_id).name == "Ridge"


def test_update_unknown_wine(service):
    with pytest.raises(WineNotFoundError):
        service.update_wine(USER, "nope", notes="x")


def test_users_are_isolated(service):
    wine = service.create_wine(USER, name="Ridge Monte Bello")
    assert service.list_wines("other")[0] == []
    with pytest.raises(WineNotFoundError):
        service.get_wine("other", wine.wine_id)


def test_search_ranks_closest_match_first(service):
    service.create_wine(USER, name="Ridge Monte Bello")
    service.create_wine(USER, name="Lopez de Heredia Viña Tondonia")
    results = service.search_wines(USER, "tondonia")
    assert results[0].name == "Lopez de Heredia Viña Tondonia"
    assert all(r.name != "Ridge Monte Bello" for r in results)


def test_search_returns_at_most_limit_wines(service):
    for i in range(4):
        service.create_wine(USER, name=f"Ridge {i}")
    assert len(service.search_wines(USER, "ridge", limit=2)) == 2


def test_list_filters_by_verdict_and_varietal(service):
    service.create_wine(USER, name="A", verdict=Verdict.LOVE, varietal="Syrah")
    service.create_wine(USER, name="B", verdict=Verdict.DISLIKE, varietal="Chardonnay")
    assert [w.name for w in service.list_wines(USER, verdict=Verdict.LOVE)[0]] == ["A"]
    assert [w.name for w in service.list_wines(USER, varietal="syrah")[0]] == ["A"]


def test_list_filters_by_dates(service):
    older = service.create_wine(USER, name="Older")
    newer = service.create_wine(USER, name="Newer")
    cutoff = older.created_at + (newer.created_at - older.created_at) / 2
    assert [w.name for w in service.list_wines(USER, created_after=cutoff)[0]] == ["Newer"]
    assert [w.name for w in service.list_wines(USER, created_before=cutoff)[0]] == ["Older"]

    service.update_wine(USER, older.wine_id, notes="changed my mind")
    assert [
        w.name for w in service.list_wines(USER, updated_after=newer.updated_at + timedelta(microseconds=1))[0]
    ] == ["Older"]
    # A naive datetime counts as UTC.
    naive = (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None)
    assert service.list_wines(USER, created_after=naive)[0] == []


def test_list_pages_through_every_wine_once(service):
    # Newest-first order needs real timestamps; e2e checks it. Wines made in the same millisecond can't show it.
    wines = [service.create_wine(USER, name=f"W{i}") for i in range(5)]

    first, token = service.list_wines(USER, limit=2)
    second, token2 = service.list_wines(USER, limit=2, next_token=token)
    third, token3 = service.list_wines(USER, limit=2, next_token=token2)
    assert sorted(w.wine_id for w in first + second + third) == sorted(w.wine_id for w in wines)
    assert (len(first), len(second), len(third)) == (2, 2, 1)
    assert token3 is None


def test_filtered_page_keeps_reading_until_full(service):
    for i in range(6):
        service.create_wine(USER, name=f"W{i}", verdict=Verdict.LOVE if i % 2 else Verdict.DISLIKE)
    page, token = service.list_wines(USER, verdict=Verdict.LOVE, limit=2)
    rest, last = service.list_wines(USER, verdict=Verdict.LOVE, limit=2, next_token=token)
    assert len(page) == 2 and len(rest) == 1
    assert {w.verdict for w in page + rest} == {Verdict.LOVE}
    assert last is None


@pytest.mark.parametrize("token", ["not-a-token!", "aGVsbG8="])
def test_list_rejects_a_bad_token(service, token):
    with pytest.raises(InvalidPageTokenError):
        service.list_wines(USER, next_token=token)


def test_delete(service):
    wine = service.create_wine(USER, name="Ridge")
    service.delete_wine(USER, wine.wine_id)
    with pytest.raises(WineNotFoundError):
        service.delete_wine(USER, wine.wine_id)


def test_wines_stored_before_versioning_can_still_be_changed(service, table):
    wine = service.create_wine(USER, name="Ridge")
    table.update_item(
        Key={"pk": f"USER#{USER}", "sk": f"WINE#{wine.wine_id}"}, UpdateExpression="REMOVE version"
    )  # as written before versioning
    assert service.update_wine(USER, wine.wine_id, notes="Bold").notes == "Bold"
    assert service.get_wine(USER, wine.wine_id).notes == "Bold"
