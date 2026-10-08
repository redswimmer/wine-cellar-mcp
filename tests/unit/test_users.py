import pytest

from wine_cellar.repository import WineRepository
from wine_cellar.service import WineService
from wine_cellar.users import USER_ID_PREFIX, UsernameTakenError, UserStore


def test_ids_are_generated_and_opaque(table):
    store = UserStore(table)
    a, b = store.create("andrew"), store.create("sam")
    assert a.user_id != b.user_id
    assert a.user_id.startswith(USER_ID_PREFIX)
    assert len(a.user_id) == len(USER_ID_PREFIX) + 26
    assert {(u.user_id, u.username) for u in store.list()} == {(a.user_id, "andrew"), (b.user_id, "sam")}


def test_usernames_are_unique_ignoring_case(table):
    store = UserStore(table)
    store.create("andrew")
    with pytest.raises(UsernameTakenError):
        store.create(" Andrew ")
    assert len(store.list()) == 1


@pytest.mark.parametrize("bad", ["", "a", "Andrew Smith", "-andrew", "x" * 33, "andrew!"])
def test_rejects_bad_usernames(table, bad):
    with pytest.raises(ValueError):
        UserStore(table).create(bad)


def test_find_by_username_or_id(table):
    store = UserStore(table)
    user = store.create("andrew")
    assert store.find("andrew") == user
    assert store.find("ANDREW") == user
    assert store.find(user.user_id) == user
    assert store.find("sam") is None
    assert store.find("usr_missing") is None
    assert store.find("not a username") is None


def test_profile_does_not_appear_as_a_wine(table):
    user = UserStore(table).create("andrew")
    service = WineService(WineRepository(table))
    service.save_wine(user.user_id, producer="Ridge")
    assert [w.producer for w in service.list_wines(user.user_id)] == ["Ridge"]
