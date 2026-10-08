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
