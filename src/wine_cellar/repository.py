"""DynamoDB storage for wines. One item per wine, partitioned by user."""

import json
from typing import Any

from .models import Wine

WINE_PREFIX = "WINE#"


def _pk(user_id: str) -> str:
    return f"USER#{user_id}"


class WineRepository:
    def __init__(self, table: Any) -> None:
        self._table = table

    def put(self, wine: Wine) -> None:
        # Stored as a JSON document plus keys, so Decimal conversion and schema drift stay out of the way.
        self._table.put_item(
            Item={
                "pk": _pk(wine.user_id),
                "sk": f"{WINE_PREFIX}{wine.wine_id}",
                "search_text": wine.search_text,
                "doc": wine.model_dump_json(),
            }
        )

    def get(self, user_id: str, wine_id: str) -> Wine | None:
        resp = self._table.get_item(Key={"pk": _pk(user_id), "sk": f"{WINE_PREFIX}{wine_id}"})
        item = resp.get("Item")
        return Wine.model_validate(json.loads(item["doc"])) if item else None

    def delete(self, user_id: str, wine_id: str) -> bool:
        resp = self._table.delete_item(
            Key={"pk": _pk(user_id), "sk": f"{WINE_PREFIX}{wine_id}"},
            ReturnValues="ALL_OLD",
        )
        return "Attributes" in resp

    def page(self, user_id: str, limit: int, after: str | None = None) -> tuple[list[Wine], str | None]:
        """Up to `limit` wines, newest first (wine ids are ULIDs), starting after wine `after`.

        Returns the wines and the id to pass as `after` for the next page, or None on the last page.
        """
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :sk)",
            "ExpressionAttributeValues": {":pk": _pk(user_id), ":sk": WINE_PREFIX},
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if after:
            kwargs["ExclusiveStartKey"] = {"pk": _pk(user_id), "sk": f"{WINE_PREFIX}{after}"}
        resp = self._table.query(**kwargs)
        wines = [Wine.model_validate(json.loads(i["doc"])) for i in resp.get("Items", [])]
        last = resp.get("LastEvaluatedKey")
        return wines, last["sk"].removeprefix(WINE_PREFIX) if last else None

    def list_for_user(self, user_id: str) -> list[Wine]:
        wines: list[Wine] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :sk)",
            "ExpressionAttributeValues": {":pk": _pk(user_id), ":sk": WINE_PREFIX},
        }
        while True:
            resp = self._table.query(**kwargs)
            wines.extend(Wine.model_validate(json.loads(i["doc"])) for i in resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return wines
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
