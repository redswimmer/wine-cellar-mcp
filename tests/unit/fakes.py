"""In-memory fakes.

FakeTextIndex, FakePhotoIndex and RacingRepository stand in for our own ports in service tests. FakeBedrock and
FakeS3Vectors imitate the AWS APIs moto doesn't cover, and are only for the adapter tests in test_adapter_vectors.py.
"""

import io
import json
import zlib
from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError

from wine_cellar.models import InvalidPhotoError, Wine
from wine_cellar.repository import WineRepository, WriteConflictError
from wine_cellar.vectors import EMBEDDING_DIMENSION, EMBEDDING_MODEL_ID


class _FakeIndex:
    """What a vector index does for the service: stores entries by key and returns the closest of a user's keys.

    `ranking` scripts which keys are closest, since similarity isn't our logic. `fail` makes every call raise, as an
    outage would. A `leaky` index ignores the user on query, as a broken filter would.
    """

    def __init__(self, *, leaky: bool = False) -> None:
        self.owners: dict[str, str] = {}  # key -> user_id
        self.ranking: list[str] = []
        self.fail = False
        self.leaky = leaky

    def keys_of(self, user_id: str) -> list[str]:
        return sorted(k for k, owner in self.owners.items() if owner == user_id)

    def _check(self) -> None:
        if self.fail:
            raise RuntimeError("index down")

    def _closest(self, user_id: str, top_k: int) -> list[str]:
        self._check()
        if self.leaky:
            return list(self.owners)[:top_k]
        return [k for k in self.ranking if self.owners.get(k) == user_id][:top_k]


class FakeTextIndex(_FakeIndex):
    def __init__(self, *, leaky: bool = False) -> None:
        super().__init__(leaky=leaky)
        self.indexed: dict[str, Wine] = {}  # wine_id -> the wine as last indexed

    def upsert(self, wine: Wine) -> None:
        self._check()
        self.indexed[wine.wine_id] = wine
        self.owners[wine.wine_id] = wine.user_id

    def delete(self, wine_id: str) -> None:
        self._check()
        self.indexed.pop(wine_id, None)
        self.owners.pop(wine_id, None)

    def query(self, user_id: str, text: str, top_k: int = 10) -> list[str]:
        return self._closest(user_id, top_k)


class FakePhotoIndex(_FakeIndex):
    def __init__(self, *, leaky: bool = False) -> None:
        super().__init__(leaky=leaky)
        self.unreadable = False  # every image is one the embedding model can't read

    @property
    def indexed(self) -> list[str]:
        return sorted(self.owners)

    def upsert(self, photo_key: str, image: bytes, image_format: str, *, user_id: str, wine_id: str) -> None:
        self._check_image()
        self.owners[photo_key] = user_id

    def delete(self, photo_keys: list[str]) -> None:
        self._check()
        for key in photo_keys:
            self.owners.pop(key, None)

    def query(self, user_id: str, image: bytes, image_format: str, top_k: int = 10) -> list[str]:
        self._check_image()
        return self._closest(user_id, top_k)

    def _check_image(self) -> None:
        self._check()
        if self.unreadable:
            raise InvalidPhotoError("The photo couldn't be read as an image. Upload the photo itself.")


class RacingRepository(WineRepository):
    """The real repository, plus two ways to lose the race for a write.

    `race(competitor)` runs `competitor` just before the next write, so that write starts from a stale copy, whatever
    the service read. `always_conflict` makes every write to an existing wine find it changed.
    """

    def __init__(self, table: Any) -> None:
        super().__init__(table)
        self.pending: Callable[[], object] | None = None
        self.always_conflict = False

    def race(self, competitor: Callable[[], object]) -> None:
        self.pending = competitor

    def put(self, wine: Wine, *, new: bool = False) -> Wine:
        if self.always_conflict and not new:
            raise WriteConflictError("another write got there first")
        competitor, self.pending = self.pending, None
        if competitor is not None:
            competitor()
        return super().put(wine, new=new)


class FakeBedrock:
    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.image_error: str | None = None  # an error code to answer images with, e.g. ValidationException

    def invoke_model(self, *, modelId, body, accept, contentType):
        assert modelId == EMBEDDING_MODEL_ID
        self.bodies.append(json.loads(body))
        kind = "IMAGE" if "image" in self.bodies[-1]["singleEmbeddingParams"] else "TEXT"
        if kind == "IMAGE" and self.image_error:
            raise ClientError({"Error": {"Code": self.image_error, "Message": "Image refused"}}, "InvokeModel")
        # Different input, different vector, so tests can tell when a vector was rewritten.
        value = zlib.crc32(body.encode()) / 2**32
        out = {"embeddings": [{"embeddingType": kind, "embedding": [value] * EMBEDDING_DIMENSION}]}
        return {"body": io.BytesIO(json.dumps(out).encode())}


class FakeS3Vectors:
    def __init__(self) -> None:
        self.vectors: dict[str, dict] = {}
        self.ranking: list[str] = []

    def put_vectors(self, *, vectorBucketName, indexName, vectors):
        self.vectors.update({v["key"]: v for v in vectors})

    def delete_vectors(self, *, vectorBucketName, indexName, keys):
        for k in keys:
            self.vectors.pop(k, None)

    def query_vectors(self, *, vectorBucketName, indexName, topK, queryVector, filter, returnMetadata=False):
        self.last_query = {"topK": topK, "filter": filter}
        # Pretend the closest vectors are whatever `ranking` says, restricted by the user filter.
        user = filter["user_id"]["$eq"]
        hits = [self.vectors[k] for k in self.ranking if k in self.vectors]
        hits = [v for v in hits if v["metadata"]["user_id"] == user][:topK]
        return {"vectors": [{"key": v["key"], **({"metadata": v["metadata"]} if returnMetadata else {})} for v in hits]}
