import pytest

from wine_cellar.tokens import TOKEN_PREFIX, TokenStore, hash_token
from wine_cellar.users import UserStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def andrew(table):
    return UserStore(table).create("andrew").user_id


def test_create_resolves_to_user_and_stores_only_a_hash(table, andrew):
    store = TokenStore(table)
    token = store.create(andrew, "andrew-claw")
    assert token.startswith(TOKEN_PREFIX)
    assert store.resolve(token) == andrew

    items = [i for i in table.scan()["Items"] if i["pk"].startswith("TOKEN#")]
    assert len(items) == 1
    assert token not in str(table.scan()["Items"])
    assert items[0]["pk"] == f"TOKEN#{hash_token(token)}"


def test_unknown_and_malformed_tokens_resolve_to_none(table):
    store = TokenStore(table)
    assert store.resolve("wct_nope") is None
    assert store.resolve("not-a-wct-token") is None


def test_token_requires_an_existing_user(table):
    with pytest.raises(LookupError):
        TokenStore(table).create("usr_doesnotexist", "label")


def test_revoke_takes_effect_after_cache_expiry_elsewhere(table, andrew):
    clock = FakeClock()
    admin = TokenStore(table)
    lambda_view = TokenStore(table, cache_seconds=60, clock=clock)
    token = admin.create(andrew, "andrew-claw")
    assert lambda_view.resolve(token) == andrew

    admin.revoke(hash_token(token)[:8])
    assert lambda_view.resolve(token) == andrew  # still cached in this environment
    clock.now = 61
    assert lambda_view.resolve(token) is None


def test_list_and_revoke_by_prefix(table, andrew):
    sam = UserStore(table).create("sam").user_id
    store = TokenStore(table)
    store.create(andrew, "a")
    store.create(sam, "b")
    records = store.list()
    assert {r.user_id for r in records} == {andrew, sam}
    with pytest.raises(LookupError):
        store.revoke("")  # matches both
    revoked = store.revoke(records[0].token_hash[:12])
    assert [r.user_id for r in store.list()] == [r.user_id for r in records[1:]]
    assert revoked.token_hash == records[0].token_hash


def test_deactivated_user_tokens_stop_working_and_come_back(table, andrew):
    clock = FakeClock()
    users = UserStore(table)
    lambda_view = TokenStore(table, cache_seconds=60, clock=clock)
    token = TokenStore(table).create(andrew, "andrew-claw")
    assert lambda_view.resolve(token) == andrew

    users.set_active(andrew, False)
    clock.now = 61
    assert lambda_view.resolve(token) is None
    assert [r.user_id for r in TokenStore(table).list()] == [andrew]  # token kept

    users.set_active(andrew, True)
    clock.now = 122
    assert lambda_view.resolve(token) == andrew


def test_no_new_tokens_for_deactivated_user(table, andrew):
    UserStore(table).set_active(andrew, False)
    with pytest.raises(ValueError):
        TokenStore(table).create(andrew, "label")
