"""Users of the service. IDs are generated here and opaque; names are only labels for people."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

USER_ID_PREFIX = "usr_"
PROFILE_SK = "PROFILE"


def _key(user_id: str) -> dict[str, str]:
    # Same partition as the user's wines (USER#<id>), which are keyed WINE#..., so a profile never
    # shows up in wine queries.
    return {"pk": f"USER#{user_id}", "sk": PROFILE_SK}


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    created_at: str


class UserStore:
    def __init__(self, table: Any) -> None:
        self._table = table

    def create(self, name: str) -> User:
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        user = User(
            user_id=f"{USER_ID_PREFIX}{str(ULID()).lower()}",
            name=name,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self._table.put_item(
            Item={**_key(user.user_id), "name": user.name, "created_at": user.created_at},
            ConditionExpression="attribute_not_exists(pk)",
        )
        return user

    def get(self, user_id: str) -> User | None:
        item = self._table.get_item(Key=_key(user_id)).get("Item")
        return User(user_id, item["name"], item.get("created_at", "")) if item else None

    def list(self) -> list[User]:
        users: list[User] = []
        kwargs: dict[str, Any] = {
            "FilterExpression": "begins_with(pk, :p) AND sk = :s",
            "ExpressionAttributeValues": {":p": "USER#", ":s": PROFILE_SK},
        }
        while True:
            resp = self._table.scan(**kwargs)
            users += [
                User(i["pk"].removeprefix("USER#"), i["name"], i.get("created_at", "")) for i in resp.get("Items", [])
            ]
            if "LastEvaluatedKey" not in resp:
                return sorted(users, key=lambda u: u.created_at)
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
