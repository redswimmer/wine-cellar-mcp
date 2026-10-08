import pytest

from wine_cellar.tokens import TOKEN_PREFIX, TokenStore, hash_token


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_create_resolves_to_user_and_stores_only_a_hash(table):
    store = TokenStore(table)
    token = store.create("andrew", "andrew-claw")
    assert token.startswith(TOKEN_PREFIX)
    assert store.resolve(token) == "andrew"

    items = table.scan()["Items"]
    assert len(items) == 1
    assert token not in str(items)
    assert items[0]["pk"] == f"TOKEN#{hash_token(token)}"


def test_unknown_and_malformed_tokens_resolve_to_none(table):
    store = TokenStore(table)
    assert store.resolve("wct_nope") is None
    assert store.resolve("not-a-wct-token") is None


@pytest.mark.parametrize("bad", ["", "Andrew", "has space", "-leading", "x" * 65])
def test_rejects_bad_user_ids(table, bad):
    with pytest.raises(ValueError):
        TokenStore(table).create(bad, "label")


def test_revoke_takes_effect_after_cache_expiry_elsewhere(table):
    clock = FakeClock()
    admin = TokenStore(table)
    lambda_view = TokenStore(table, cache_seconds=60, clock=clock)
    token = admin.create("andrew", "andrew-claw")
    assert lambda_view.resolve(token) == "andrew"

    admin.revoke(hash_token(token)[:8])
    assert lambda_view.resolve(token) == "andrew"  # still cached in this environment
    clock.now = 61
    assert lambda_view.resolve(token) is None


def test_list_and_revoke_by_prefix(table):
    store = TokenStore(table)
    store.create("andrew", "a")
    store.create("sam", "b")
    records = store.list()
    assert {r.user_id for r in records} == {"andrew", "sam"}
    with pytest.raises(LookupError):
        store.revoke("")  # matches both
    revoked = store.revoke(records[0].token_hash[:12])
    assert [r.user_id for r in store.list()] == [r.user_id for r in records[1:]]
    assert revoked.token_hash == records[0].token_hash
