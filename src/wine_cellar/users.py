"""Users of the service. IDs are generated here and opaque; each user also has a unique username.

Users are deactivated rather than deleted: a deactivated user's tokens stop working, but their
wines are kept and reactivating them restores access with the same tokens.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from .repository import USER_PREFIX, user_pk

USER_ID_PREFIX = "usr_"
PROFILE_SK = "PROFILE"
USERNAME_SK = "USERNAME"
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


class UsernameTakenError(ValueError):
    pass


def _key(user_id: str) -> dict[str, str]:
    # Same partition as the user's wines (USER#<id>), which are keyed WINE#..., so a profile never
    # shows up in wine queries.
    return {"pk": user_pk(user_id), "sk": PROFILE_SK}


def _username_key(username: str) -> dict[str, str]:
    # A separate item reserves each username, so a conditional write can enforce uniqueness.
    return {"pk": f"USERNAME#{username}", "sk": USERNAME_SK}


def normalize_username(username: str) -> str:
    username = username.strip().lower()
    if not USERNAME_PATTERN.match(username):
        raise ValueError("username must be 2-32 characters: a-z, 0-9, _ or -, starting with a letter or digit")
    return username


@dataclass(frozen=True)
class User:
    user_id: str
    username: str
    created_at: str
    active: bool = True


def _from_item(user_id: str, item: dict[str, Any]) -> User:
    # Profiles written before deactivation existed have no `active` attribute and count as active.
    return User(user_id, item["username"], item.get("created_at", ""), bool(item.get("active", True)))


class UserStore:
    def __init__(self, table: Any) -> None:
        self._table = table

    def create(self, username: str) -> User:
        username = normalize_username(username)
        user = User(
            user_id=f"{USER_ID_PREFIX}{str(ULID()).lower()}",
            username=username,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        client = self._table.meta.client
        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table.name,
                            "Item": {**_username_key(username), "user_id": user.user_id},
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table.name,
                            "Item": {
                                **_key(user.user_id),
                                "username": username,
                                "created_at": user.created_at,
                                "active": True,
                            },
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except client.exceptions.TransactionCanceledException as e:
            raise UsernameTakenError(f"Username {username!r} is taken") from e
        return user

    def get(self, user_id: str) -> User | None:
        item = self._table.get_item(Key=_key(user_id)).get("Item")
        return _from_item(user_id, item) if item else None

    def find(self, user: str) -> User | None:
        """Look up a user by ID (usr_...) or by username."""
        if user.startswith(USER_ID_PREFIX):
            return self.get(user)
        try:
            username = normalize_username(user)
        except ValueError:
            return None
        item = self._table.get_item(Key=_username_key(username)).get("Item")
        return self.get(item["user_id"]) if item else None

    def list(self) -> list[User]:
        users: list[User] = []
        kwargs: dict[str, Any] = {
            "FilterExpression": "begins_with(pk, :p) AND sk = :s",
            "ExpressionAttributeValues": {":p": USER_PREFIX, ":s": PROFILE_SK},
        }
        while True:
            resp = self._table.scan(**kwargs)
            users += [_from_item(i["pk"].removeprefix(USER_PREFIX), i) for i in resp.get("Items", [])]
            if "LastEvaluatedKey" not in resp:
                return sorted(users, key=lambda u: u.created_at)
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def set_active(self, user_id: str, active: bool) -> User:
        """Deactivate or reactivate a user. Their wines and tokens are left in place."""
        client = self._table.meta.client
        try:
            item = self._table.update_item(
                Key=_key(user_id),
                UpdateExpression="SET active = :a, status_changed_at = :t",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeValues={":a": active, ":t": datetime.now(UTC).isoformat(timespec="seconds")},
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except client.exceptions.ConditionalCheckFailedException as e:
            raise LookupError(f"No user {user_id!r}") from e
        return _from_item(user_id, item)
