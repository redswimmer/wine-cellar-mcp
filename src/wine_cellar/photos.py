"""Label photos: uploads by pre-signed URL, validation and S3 storage.

Images never travel inside an MCP request; agents PUT them straight to S3 with a pre-signed URL.

- A wine's photos live at `wines/<user_id>/<wine_id>/<ulid>`. Uploading to a link from `add_photo` saves the photo
  to that wine; an S3 event then checks it, records it on the wine and indexes it.
- A photo to search with lives at `search/<user_id>/<ulid>` and is passed to `search_wines` as a `photo_ref`. It is
  deleted after the search and never saved.
"""

from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError
from ulid import ULID

MAX_PHOTO_BYTES = 5_000_000
WINE_PREFIX = "wines/"
SEARCH_PREFIX = "search/"
UPLOAD_URL_SECONDS = 900  # how long the agent has to start the upload
SEARCH_RETENTION_DAYS = 1  # search photos that were never searched with are deleted by a bucket lifecycle rule

# Leading bytes of each format Nova accepts.
_SIGNATURES = {
    b"\xff\xd8\xff": "jpeg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
}


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


def wine_photo_owner(key: str) -> tuple[str, str] | None:
    """The (user_id, wine_id) a wine photo key belongs to, or None for any other key."""
    parts = key.removeprefix(WINE_PREFIX).split("/")
    if not key.startswith(WINE_PREFIX) or len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1]


class PhotoStore:
    def __init__(self, s3: Any, bucket: str) -> None:
        self._s3 = s3
        self._bucket = bucket

    def _upload_link(self, key: str) -> Upload:
        url = self._s3.generate_presigned_url(
            "put_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=UPLOAD_URL_SECONDS
        )
        return Upload(key, url)

    def create_wine_upload(self, user_id: str, wine_id: str) -> Upload:
        return self._upload_link(f"{WINE_PREFIX}{user_id}/{wine_id}/{ULID()}")

    def create_search_upload(self, user_id: str) -> Upload:
        return self._upload_link(f"{SEARCH_PREFIX}{user_id}/{ULID()}")

    def load_search_photo(self, user_id: str, photo_ref: str) -> Photo:
        """The checked photo behind a search photo_ref. Refs only resolve for the user who created them."""
        missing = InvalidPhotoError(
            "No photo has been uploaded for this photo_ref: the upload failed, hasn't finished, expired, or was "
            "already used for a search. Call upload_search_photo and upload again."
        )
        if not photo_ref.startswith(f"{SEARCH_PREFIX}{user_id}/"):
            raise missing
        try:
            return self.get(photo_ref)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise missing from e
            raise

    def get(self, key: str) -> Photo:
        """A stored photo, checked for size and format."""
        _check_size(self._s3.head_object(Bucket=self._bucket, Key=key)["ContentLength"])
        return sniff(self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read())

    def delete_wine_photos(self, user_id: str, wine_id: str) -> None:
        pages = self._s3.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix=f"{WINE_PREFIX}{user_id}/{wine_id}/"
        )
        for page in pages:
            self.delete([o["Key"] for o in page.get("Contents", [])])

    def delete(self, keys: list[str]) -> None:
        if keys:
            self._s3.delete_objects(Bucket=self._bucket, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True})
