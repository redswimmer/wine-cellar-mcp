import pytest

from wine_cellar.repository import WineRepository
from wine_cellar.service import WineService
from wine_cellar.users import USER_ID_PREFIX, UserStore


def test_ids_are_generated_and_unique_even_for_the_same_name(table):
    store = UserStore(table)
    a, b = store.create("Andrew"), store.create("Andrew")
    assert a.user_id != b.user_id
    assert a.user_id.startswith(USER_ID_PREFIX)
    assert len(a.user_id) == len(USER_ID_PREFIX) + 26
    assert {u.user_id for u in store.list()} == {a.user_id, b.user_id}
    fetched = store.get(a.user_id)
    assert fetched is not None and fetched.name == "Andrew"
    assert store.get("usr_missing") is None


def test_blank_name_rejected(table):
    with pytest.raises(ValueError):
        UserStore(table).create("  ")


def test_profile_does_not_appear_as_a_wine(table):
    user = UserStore(table).create("Andrew")
    service = WineService(WineRepository(table))
    service.save_wine(user.user_id, producer="Ridge")
    assert [w.producer for w in service.list_wines(user.user_id)] == ["Ridge"]
