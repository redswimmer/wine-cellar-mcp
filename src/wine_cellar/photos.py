"""Label photos: uploads by pre-signed URL, validation and S3 storage.

Agents upload a photo straight to S3 with a pre-signed PUT URL and then pass its `photo_ref` to a tool, so images
never travel inside an MCP request.
"""

from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError
from ulid import ULID

MAX_PHOTO_BYTES = 5_000_000
UPLOAD_PREFIX = "uploads/"
UPLOAD_URL_SECONDS = 900  # how long the agent has to start the upload
UPLOAD_RETENTION_DAYS = 1  # unused uploads are deleted by a bucket lifecycle rule

# Leading bytes of each format Nova accepts.
_SIGNATURES = {
    b"\xff\xd8\xff": "jpeg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
}
_EXTENSIONS = {"jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp"}


class InvalidPhotoError(ValueError):
    """The photo can't be used; the message tells the agent what to do instead."""


@dataclass(frozen=True)
class Photo:
    data: bytes
    format: str  # jpeg, png, gif or webp


@dataclass(frozen=True)
class Upload:
    photo_ref: str
    upload_url: str


def _check_size(size: int) -> None:
    if size > MAX_PHOTO_BYTES:
        raise InvalidPhotoError(
            f"The photo is {size / 1_000_000:.1f} MB; the limit is {MAX_PHOTO_BYTES // 1_000_000} MB. "
            "Upload a smaller size (about 1000 px on the long side is plenty to read a label)."
        )


def sniff(data: bytes) -> Photo:
    """Check a photo's size and format from its bytes."""
    _check_size(len(data))
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return Photo(data, "webp")
    for signature, fmt in _SIGNATURES.items():
        if data.startswith(signature):
            return Photo(data, fmt)
    raise InvalidPhotoError("The upload isn't a JPEG, PNG, WebP or GIF image. Upload the photo itself.")


class PhotoStore:
    """Wine photos at s3://<bucket>/<user_id>/<wine_id>/<ulid>.<ext>; pending uploads under uploads/<user_id>/."""

    def __init__(self, s3: Any, bucket: str) -> None:
        self._s3 = s3
        self._bucket = bucket

    def create_upload(self, user_id: str) -> Upload:
        ref = f"{UPLOAD_PREFIX}{user_id}/{ULID()}"
        url = self._s3.generate_presigned_url(
            "put_object", Params={"Bucket": self._bucket, "Key": ref}, ExpiresIn=UPLOAD_URL_SECONDS
        )
        return Upload(ref, url)

    def load_upload(self, user_id: str, photo_ref: str) -> Photo:
        """The uploaded photo behind a photo_ref, checked. Refs only resolve for the user who created them."""
        missing = InvalidPhotoError(
            "No photo has been uploaded for this photo_ref: the upload failed, hasn't finished, or expired "
            f"(uploads are kept for {UPLOAD_RETENTION_DAYS} day). Call create_photo_upload and upload again."
        )
        if not photo_ref.startswith(f"{UPLOAD_PREFIX}{user_id}/"):
            raise missing
        try:
            _check_size(self._s3.head_object(Bucket=self._bucket, Key=photo_ref)["ContentLength"])
            data = self._s3.get_object(Bucket=self._bucket, Key=photo_ref)["Body"].read()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise missing from e
            raise
        return sniff(data)

    def delete_upload(self, photo_ref: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=photo_ref)

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
