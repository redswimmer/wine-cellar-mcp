"""Embeddings (Bedrock, Nova Multimodal) and the S3 Vectors indexes for wine text and label photos.

Text and photo vectors live in separate indexes and are never compared with each other.
"""

import base64
import json
from typing import Any, Literal

from botocore.exceptions import ClientError

from .models import InvalidPhotoError, Wine

EMBEDDING_MODEL_ID = "amazon.nova-2-multimodal-embeddings-v1:0"
EMBEDDING_DIMENSION = 1024

# Nova embedding purposes: GENERIC_INDEX for stored vectors; TEXT_RETRIEVAL and IMAGE_RETRIEVAL for queries
# against a text-only or image-only index.
Purpose = Literal["GENERIC_INDEX", "TEXT_RETRIEVAL", "IMAGE_RETRIEVAL"]


class Embedder:
    def __init__(self, bedrock_runtime: Any, model_id: str = EMBEDDING_MODEL_ID) -> None:
        self._client = bedrock_runtime
        self._model_id = model_id

    def embed_text(self, text: str, purpose: Purpose) -> list[float]:
        return self._embed(purpose, text={"truncationMode": "END", "value": text})

    def embed_image(self, image: bytes, image_format: str, purpose: Purpose) -> list[float]:
        """The image's embedding. InvalidPhotoError if Bedrock can't read it (e.g. a valid header over garbage)."""
        try:
            return self._embed(
                purpose,
                image={
                    "format": image_format,
                    "detailLevel": "STANDARD_IMAGE",
                    "source": {"bytes": base64.b64encode(image).decode()},
                },
            )
        except ClientError as e:
            # Throttling and server errors are raised as they are, so the caller can retry.
            if e.response.get("Error", {}).get("Code") == "ValidationException":
                raise InvalidPhotoError("The photo couldn't be read as an image. Upload the photo itself.") from e
            raise

    def _embed(self, purpose: Purpose, **content: dict[str, Any]) -> list[float]:
        body = {
            "taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {
                "embeddingPurpose": purpose,
                "embeddingDimension": EMBEDDING_DIMENSION,
                **content,
            },
        }
        resp = self._client.invoke_model(
            modelId=self._model_id, body=json.dumps(body), accept="application/json", contentType="application/json"
        )
        return json.loads(resp["body"].read())["embeddings"][0]["embedding"]


def wine_text(wine: Wine) -> str:
    """What a wine's text vector describes: the name plus everything the person said about it."""
    lines = [wine.name]
    lines.extend(
        f"{label}: {value}"
        for label, value in [
            ("Varietal", wine.varietal),
            ("Verdict", wine.verdict or ""),
            ("Notes", wine.notes),
        ]
        if value
    )
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


class WinePhotoIndex:
    """One vector per label photo, keyed by its S3 key, filterable by user_id, with the wine_id it belongs to."""

    def __init__(self, s3vectors: Any, embedder: Embedder, *, bucket: str, index: str) -> None:
        self._client = s3vectors
        self._embedder = embedder
        self._target = {"vectorBucketName": bucket, "indexName": index}

    def upsert(self, photo_key: str, image: bytes, image_format: str, *, user_id: str, wine_id: str) -> None:
        vector = self._embedder.embed_image(image, image_format, "GENERIC_INDEX")
        self._client.put_vectors(
            **self._target,
            vectors=[
                {
                    "key": photo_key,
                    "data": {"float32": vector},
                    "metadata": {"user_id": user_id, "wine_id": wine_id},
                }
            ],
        )

    def delete(self, photo_keys: list[str]) -> None:
        if photo_keys:
            self._client.delete_vectors(**self._target, keys=photo_keys)

    def query(self, user_id: str, image: bytes, image_format: str, top_k: int = 10) -> list[str]:
        """Keys of this user's label photos most similar to `image`, closest first."""
        vector = self._embedder.embed_image(image, image_format, "IMAGE_RETRIEVAL")
        resp = self._client.query_vectors(
            **self._target, topK=top_k, queryVector={"float32": vector}, filter={"user_id": {"$eq": user_id}}
        )
        return [v["key"] for v in resp.get("vectors", [])]
