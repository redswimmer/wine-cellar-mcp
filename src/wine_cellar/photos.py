"""Label photos: validation and S3 storage."""

import base64
import binascii
from dataclasses import dataclass
from typing import Any

from ulid import ULID

# NemoClaw caps an MCP request body at 131,072 bytes; base64 adds a third, leaving room for the rest of the call.
MAX_PHOTO_BYTES = 95_000

# Leading bytes of each format Nova accepts.
_SIGNATURES = {
    b"\xff\xd8\xff": "jpeg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
}
_EXTENSIONS = {"jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp"}


class InvalidPhotoError(ValueError):
    pass


@dataclass(frozen=True)
class Photo:
    data: bytes
    format: str  # jpeg, png, gif or webp


def decode_photo(photo_base64: str) -> Photo:
    """Decode and check a base64 label photo, with or without a data: URL prefix."""
    if photo_base64.startswith("data:"):
        photo_base64 = photo_base64.partition(",")[2]
    try:
        data = base64.b64decode(photo_base64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise InvalidPhotoError("The photo isn't valid base64.") from e
    if len(data) > MAX_PHOTO_BYTES:
        raise InvalidPhotoError(
            f"The photo is {len(data) // 1000} KB; the limit is {MAX_PHOTO_BYTES // 1000} KB. "
            "Send a smaller size (about 800 px on the long side is enough to read a label)."
        )
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return Photo(data, "webp")
    for signature, fmt in _SIGNATURES.items():
        if data.startswith(signature):
            return Photo(data, fmt)
    raise InvalidPhotoError("The photo must be a JPEG, PNG, WebP or GIF image.")


class PhotoStore:
    """Photos at s3://<bucket>/<user_id>/<wine_id>/<ulid>.<ext>."""

    def __init__(self, s3: Any, bucket: str) -> None:
        self._s3 = s3
        self._bucket = bucket

    def put(self, user_id: str, wine_id: str, photo: Photo) -> str:
        key = f"{user_id}/{wine_id}/{ULID()}.{_EXTENSIONS[photo.format]}"
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=photo.data, ContentType=f"image/{photo.format}")
        return key

    def get(self, key: str) -> Photo:
        data = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        return Photo(data, _format_from_key(key))

    def delete(self, keys: list[str]) -> None:
        if keys:
            self._s3.delete_objects(Bucket=self._bucket, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True})


def _format_from_key(key: str) -> str:
    ext = key.rsplit(".", 1)[-1]
    return {v: k for k, v in _EXTENSIONS.items()}[ext]
