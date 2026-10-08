"""Embeddings (Bedrock, Nova Multimodal) and the S3 Vectors index that holds one text vector per wine.

Text and photo vectors live in separate indexes and are never compared with each other.
"""

import json
from typing import Any, Literal

from .models import Wine

EMBEDDING_MODEL_ID = "amazon.nova-2-multimodal-embeddings-v1:0"
EMBEDDING_DIMENSION = 1024

# Nova embedding purposes: GENERIC_INDEX for stored vectors, TEXT_RETRIEVAL for queries against a text-only index.
Purpose = Literal["GENERIC_INDEX", "TEXT_RETRIEVAL"]


class Embedder:
    def __init__(self, bedrock_runtime: Any, model_id: str = EMBEDDING_MODEL_ID) -> None:
        self._client = bedrock_runtime
        self._model_id = model_id

    def embed_text(self, text: str, purpose: Purpose) -> list[float]:
        body = {
            "taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {
                "embeddingPurpose": purpose,
                "embeddingDimension": EMBEDDING_DIMENSION,
                "text": {"truncationMode": "END", "value": text},
            },
        }
        resp = self._client.invoke_model(
            modelId=self._model_id, body=json.dumps(body), accept="application/json", contentType="application/json"
        )
        return json.loads(resp["body"].read())["embeddings"][0]["embedding"]


def wine_text(wine: Wine) -> str:
    """What a wine's text vector describes: the label plus everything the person said about it."""
    lines = [wine.label]
    for label, value in [
        ("Region", ", ".join(p for p in [wine.region, wine.country] if p)),
        ("Style", wine.style or ""),
        ("Grapes", ", ".join(wine.varietals)),
        ("Verdict", wine.verdict or ""),
        ("Notes", wine.notes),
    ]:
        if value:
            lines.append(f"{label}: {value}")
    lines.extend(f"Tasting: {t.notes}" for t in wine.tastings if t.notes and t.notes != wine.notes)
    return "\n".join(lines)


class WineTextIndex:
    """One vector per wine, keyed by wine_id, filterable by user_id."""

    def __init__(self, s3vectors: Any, embedder: Embedder, *, bucket: str, index: str) -> None:
        self._client = s3vectors
        self._embedder = embedder
        self._target = {"vectorBucketName": bucket, "indexName": index}

    def upsert(self, wine: Wine) -> None:
        vector = self._embedder.embed_text(wine_text(wine), "GENERIC_INDEX")
        self._client.put_vectors(
            **self._target,
            vectors=[{"key": wine.wine_id, "data": {"float32": vector}, "metadata": {"user_id": wine.user_id}}],
        )

    def delete(self, wine_id: str) -> None:
        self._client.delete_vectors(**self._target, keys=[wine_id])

    def query(self, user_id: str, text: str, top_k: int = 10) -> list[str]:
        """IDs of this user's wines whose text is closest in meaning to `text`, closest first."""
        vector = self._embedder.embed_text(text, "TEXT_RETRIEVAL")
        resp = self._client.query_vectors(
            **self._target, topK=top_k, queryVector={"float32": vector}, filter={"user_id": {"$eq": user_id}}
        )
        return [v["key"] for v in resp.get("vectors", [])]
