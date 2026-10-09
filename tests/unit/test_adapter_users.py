"""Adapter: the user store over DynamoDB (moto), and that profiles share the table with wines safely."""

import pytest

from wine_cellar.repository import WineRepository, user_pk
from wine_cellar.service import WineService
from wine_cellar.users import PROFILE_SK, USER_ID_PREFIX, UsernameTakenError, UserStore


def test_ids_are_generated_and_opaque(table):
    store = UserStore(table)
    a, b = store.create("andrew"), store.create("sam")
    assert a.user_id != b.user_id
    assert a.user_id.startswith(USER_ID_PREFIX)
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
    service.create_wine(user.user_id, name="Ridge")
    assert [w.name for w in service.list_wines(user.user_id)[0]] == ["Ridge"]


def test_deactivate_and_reactivate_keep_wines(table):
    store = UserStore(table)
    user = store.create("andrew")
    service = WineService(WineRepository(table))
    service.create_wine(user.user_id, name="Ridge")

    assert store.set_active(user.user_id, False).active is False
    assert store.find("andrew") == store.get(user.user_id)
    assert [u.active for u in store.list()] == [False]
    assert [w.name for w in service.list_wines(user.user_id)[0]] == ["Ridge"]

    assert store.set_active(user.user_id, True).active is True


def test_set_active_unknown_user(table):
    with pytest.raises(LookupError):
        UserStore(table).set_active("usr_missing", False)


def test_profiles_without_active_flag_count_as_active(table):
    user = UserStore(table).create("andrew")
    table.update_item(Key={"pk": user_pk(user.user_id), "sk": PROFILE_SK}, UpdateExpression="REMOVE active")
    fetched = UserStore(table).get(user.user_id)
    assert fetched is not None and fetched.active
