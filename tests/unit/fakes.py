"""In-memory stand-ins for Bedrock and S3 Vectors, which moto doesn't cover."""

import io
import json
import zlib

from botocore.exceptions import ClientError

from wine_cellar.vectors import EMBEDDING_DIMENSION, EMBEDDING_MODEL_ID


class FakeBedrock:
    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.reject_images = False  # answer images the way Bedrock answers one it can't read

    def invoke_model(self, *, modelId, body, accept, contentType):
        assert modelId == EMBEDDING_MODEL_ID
        self.bodies.append(json.loads(body))
        kind = "IMAGE" if "image" in self.bodies[-1]["singleEmbeddingParams"] else "TEXT"
        if kind == "IMAGE" and self.reject_images:
            raise ClientError({"Error": {"Code": "ValidationException", "Message": "Invalid image"}}, "InvokeModel")
        # Different input, different vector, so tests can tell when a vector was rewritten.
        value = zlib.crc32(body.encode()) / 2**32
        out = {"embeddings": [{"embeddingType": kind, "embedding": [value] * EMBEDDING_DIMENSION}]}
        return {"body": io.BytesIO(json.dumps(out).encode())}


class FakeS3Vectors:
    def __init__(self, *, fail: bool = False) -> None:
        self.vectors: dict[str, dict] = {}
        self.ranking: list[str] = []
        self.fail = fail

    def put_vectors(self, *, vectorBucketName, indexName, vectors):
        if self.fail:
            raise RuntimeError("s3vectors down")
        self.vectors.update({v["key"]: v for v in vectors})

    def delete_vectors(self, *, vectorBucketName, indexName, keys):
        if self.fail:
            raise RuntimeError("s3vectors down")
        for k in keys:
            self.vectors.pop(k, None)

    def query_vectors(self, *, vectorBucketName, indexName, topK, queryVector, filter, returnMetadata=False):
        if self.fail:
            raise RuntimeError("s3vectors down")
        self.last_query = {"topK": topK, "filter": filter}
        # Pretend the closest vectors are whatever `ranking` says, restricted by the user filter.
        user = filter["user_id"]["$eq"]
        hits = [self.vectors[k] for k in self.ranking if k in self.vectors]
        hits = [v for v in hits if v["metadata"]["user_id"] == user][:topK]
        return {"vectors": [{"key": v["key"], **({"metadata": v["metadata"]} if returnMetadata else {})} for v in hits]}
