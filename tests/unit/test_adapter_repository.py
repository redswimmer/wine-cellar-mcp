"""Adapter: the wine repository over DynamoDB (moto). Its write rules are what the never-lose tests rely on."""

import pytest
from ulid import ULID

from wine_cellar.models import Wine
from wine_cellar.repository import WINE_PREFIX, WriteConflictError, user_pk

from .conftest import USER


def _wine(name: str = "Ridge") -> Wine:
    return Wine(wine_id=str(ULID()), user_id=USER, name=name)


def _stored_before_versioning(table, wine: Wine) -> None:
    """Make a stored wine look as it did before versioning: no version attribute."""
    key = {"pk": user_pk(wine.user_id), "sk": f"{WINE_PREFIX}{wine.wine_id}"}
    # The condition makes a wrong key fail here, instead of quietly creating a stray item.
    table.update_item(Key=key, UpdateExpression="REMOVE version", ConditionExpression="attribute_exists(pk)")
    assert "version" not in table.get_item(Key=key)["Item"]


def test_put_returns_the_wine_with_its_new_version(repo):
    stored = repo.put(_wine(), new=True)
    assert stored.version == 1
    assert repo.get(USER, stored.wine_id) == stored
    assert repo.put(stored.model_copy(update={"notes": "Bold"})).version == 2


def test_a_new_wine_never_overwrites_a_stored_one(repo):
    stored = repo.put(_wine(), new=True)
    with pytest.raises(WriteConflictError):
        repo.put(stored.model_copy(update={"name": "Other", "version": 0}), new=True)
    assert repo.get(USER, stored.wine_id) == stored


def test_a_write_from_a_stale_copy_is_refused(repo):
    stale = repo.put(_wine(), new=True)
    current = repo.put(stale.model_copy(update={"notes": "first"}))
    with pytest.raises(WriteConflictError):
        repo.put(stale.model_copy(update={"notes": "second"}))
    assert repo.get(USER, stale.wine_id) == current


def test_a_deleted_wine_is_not_written_back(repo):
    stored = repo.put(_wine(), new=True)
    assert repo.delete(USER, stored.wine_id)
    with pytest.raises(WriteConflictError):
        repo.put(stored.model_copy(update={"notes": "Bold"}))
    assert repo.get(USER, stored.wine_id) is None
    assert not repo.delete(USER, stored.wine_id)


def test_a_wine_stored_before_versioning_can_still_be_changed(repo, table):
    stored = repo.put(_wine(), new=True)
    _stored_before_versioning(table, stored)
    legacy = repo.get(USER, stored.wine_id)
    assert legacy is not None and legacy.version == 0
    assert repo.put(legacy.model_copy(update={"notes": "Bold"})).version == 1
    with pytest.raises(WriteConflictError):  # once versioned, the unversioned copy is stale
        repo.put(legacy.model_copy(update={"notes": "Stale"}))


def test_a_wine_stored_before_versioning_is_not_written_back_after_a_delete(repo, table):
    stored = repo.put(_wine(), new=True)
    _stored_before_versioning(table, stored)
    legacy = repo.get(USER, stored.wine_id)
    assert legacy is not None
    repo.delete(USER, stored.wine_id)
    with pytest.raises(WriteConflictError):
        repo.put(legacy.model_copy(update={"notes": "Bold"}))
    assert repo.get(USER, stored.wine_id) is None


def test_wines_are_kept_per_user(repo):
    stored = repo.put(_wine(), new=True)
    assert repo.get("usr_other", stored.wine_id) is None
    assert repo.list_for_user("usr_other") == []
    assert not repo.delete("usr_other", stored.wine_id)


def test_pages_run_newest_first_and_end_with_no_cursor(repo):
    ids = sorted(str(ULID()) for _ in range(3))  # ULIDs sort by creation time
    wines = [repo.put(Wine(wine_id=i, user_id=USER, name=n), new=True) for i, n in zip(ids, "ABC", strict=True)]
    first, after = repo.page(USER, 2)
    assert [w.name for w in first] == ["C", "B"]
    rest, after = repo.page(USER, 2, after)
    assert ([w.name for w in rest], after) == (["A"], None)
    assert sorted(w.wine_id for w in repo.list_for_user(USER)) == sorted(w.wine_id for w in wines)
