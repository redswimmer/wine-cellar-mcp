"""DynamoDB storage for wines. One item per wine, partitioned by user."""

import json
from typing import Any

from botocore.exceptions import ClientError

from .models import Wine

USER_PREFIX = "USER#"
WINE_PREFIX = "WINE#"


def user_pk(user_id: str) -> str:
    """The partition holding a user's wines and profile (users.py)."""
    return f"{USER_PREFIX}{user_id}"


class WriteConflictError(Exception):
    """The wine was changed or deleted after it was read, so the write was refused. Read it again and retry."""


def _wine(item: dict[str, Any]) -> Wine:
    # Wines stored before versioning have no version attribute.
    return Wine.model_validate({**json.loads(item["doc"]), "version": int(item.get("version", 0))})


class WineRepository:
    def __init__(self, table: Any) -> None:
        self._table = table

    def put(self, wine: Wine, *, new: bool = False) -> Wine:
        """Store a wine and return it with its new version.

        A new wine is only created, never written over another. Any other write only succeeds if the stored wine still
        has the version this one was read with (none, for one stored before versioning), so concurrent writes can't
        silently undo each other and a deleted wine can't come back. Otherwise WriteConflictError is raised.
        """
        condition: dict[str, Any] = {"ExpressionAttributeNames": {"#v": "version"}}
        if new:
            condition = {"ConditionExpression": "attribute_not_exists(pk)"}
        elif wine.version:
            condition |= {"ConditionExpression": "#v = :v", "ExpressionAttributeValues": {":v": wine.version}}
        else:
            condition |= {"ConditionExpression": "attribute_exists(pk) AND attribute_not_exists(#v)"}
        stored = wine.model_copy(update={"version": wine.version + 1})
        try:
            # Stored as a JSON document plus keys, so Decimal conversion and schema drift stay out of the way.
            self._table.put_item(
                Item={
                    "pk": user_pk(wine.user_id),
                    "sk": f"{WINE_PREFIX}{wine.wine_id}",
                    "search_text": wine.search_text,
                    "doc": stored.model_dump_json(exclude={"version"}),
                    "version": stored.version,
                },
                **condition,
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise WriteConflictError(f"Wine {wine.wine_id} changed since it was read") from e
            raise
        return stored

    def get(self, user_id: str, wine_id: str) -> Wine | None:
        resp = self._table.get_item(Key={"pk": user_pk(user_id), "sk": f"{WINE_PREFIX}{wine_id}"})
        item = resp.get("Item")
        return _wine(item) if item else None

    def delete(self, user_id: str, wine_id: str) -> bool:
        resp = self._table.delete_item(
            Key={"pk": user_pk(user_id), "sk": f"{WINE_PREFIX}{wine_id}"},
            ReturnValues="ALL_OLD",
        )
        return "Attributes" in resp

    def page(self, user_id: str, limit: int, after: str | None = None) -> tuple[list[Wine], str | None]:
        """Up to `limit` wines, newest first (wine ids are ULIDs), starting after wine `after`.

        Returns the wines and the id to pass as `after` for the next page, or None on the last page.
        """
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :sk)",
            "ExpressionAttributeValues": {":pk": user_pk(user_id), ":sk": WINE_PREFIX},
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if after:
            kwargs["ExclusiveStartKey"] = {"pk": user_pk(user_id), "sk": f"{WINE_PREFIX}{after}"}
        resp = self._table.query(**kwargs)
        wines = [_wine(i) for i in resp.get("Items", [])]
        last = resp.get("LastEvaluatedKey")
        return wines, last["sk"].removeprefix(WINE_PREFIX) if last else None

    def list_for_user(self, user_id: str) -> list[Wine]:
        wines: list[Wine] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :sk)",
            "ExpressionAttributeValues": {":pk": user_pk(user_id), ":sk": WINE_PREFIX},
        }
        while True:
            resp = self._table.query(**kwargs)
            wines.extend(_wine(i) for i in resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return wines
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
