"""API tokens that identify which user a caller acts for.

Each agent sandbox gets its own token, and the token alone decides the user, so the agent never
supplies a user ID. Only a SHA-256 hash of each token is stored; tokens are 256-bit random values,
so a plain hash is enough to make the stored form useless to someone reading the table.
"""

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .users import UserStore

TOKEN_PREFIX = "wct_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _key(token_hash: str) -> dict[str, str]:
    return {"pk": f"TOKEN#{token_hash}", "sk": "TOKEN"}


@dataclass(frozen=True)
class TokenRecord:
    token_hash: str
    user_id: str
    label: str
    created_at: str


class TokenStore:
    def __init__(self, table: Any, *, cache_seconds: float = 60, clock=time.monotonic) -> None:
        self._table = table
        self._users = UserStore(table)
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._cache: dict[str, tuple[str | None, float]] = {}

    def resolve(self, token: str) -> str | None:
        """The user ID a token acts for, or None if it is unknown, revoked, or its user is inactive.

        Results are cached briefly per Lambda environment, so a revoked token or a deactivated
        user stops working within `cache_seconds`.
        """
        if not token.startswith(TOKEN_PREFIX):
            return None
        token_hash = hash_token(token)
        now = self._clock()
        cached = self._cache.get(token_hash)
        if cached and cached[1] > now:
            return cached[0]
        item = self._table.get_item(Key=_key(token_hash)).get("Item")
        user = self._users.get(item["user_id"]) if item else None
        user_id = user.user_id if user and user.active else None
        self._cache[token_hash] = (user_id, now + self._cache_seconds)
        return user_id

    def create(self, user_id: str, label: str) -> str:
        """Issue a new token for an existing user. The token is returned once and never stored."""
        user = self._users.get(user_id)
        if user is None:
            raise LookupError(f"No user {user_id!r}; create one with `wine-tokens add-user`")
        if not user.active:
            raise ValueError(f"User {user.username!r} is deactivated; reactivate them first")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self._table.put_item(
            Item={
                **_key(hash_token(token)),
                "user_id": user_id,
                "label": label,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
        return token

    def list(self) -> list[TokenRecord]:
        records: list[TokenRecord] = []
        kwargs: dict[str, Any] = {
            "FilterExpression": "begins_with(pk, :p) AND sk = :s",
            "ExpressionAttributeValues": {":p": "TOKEN#", ":s": "TOKEN"},
        }
        while True:
            resp = self._table.scan(**kwargs)
            for item in resp.get("Items", []):
                records.append(
                    TokenRecord(
                        token_hash=item["pk"].removeprefix("TOKEN#"),
                        user_id=item["user_id"],
                        label=item.get("label", ""),
                        created_at=item.get("created_at", ""),
                    )
                )
            if "LastEvaluatedKey" not in resp:
                return sorted(records, key=lambda r: r.created_at)
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def revoke(self, token_hash_prefix: str) -> TokenRecord:
        """Delete the one token whose hash starts with the given prefix (as shown by `list`)."""
        matches = [r for r in self.list() if r.token_hash.startswith(token_hash_prefix)]
        if len(matches) != 1:
            raise LookupError(f"{len(matches)} tokens match {token_hash_prefix!r}; give a longer prefix")
        self._table.delete_item(Key=_key(matches[0].token_hash))
        self._cache.pop(matches[0].token_hash, None)
        return matches[0]
