"""Lambda entry point for the S3 event sent when a photo is uploaded to a wine's link."""

from typing import Any

import boto3

from .bootstrap import build_service
from .config import Settings
from .photos import uploaded_keys

settings = Settings.from_env()
_service = build_service(settings, boto3.resource("dynamodb").Table(settings.table_name))


def handler(event: dict[str, Any], _context: Any) -> None:
    for key in uploaded_keys(event):
        _service.photo_uploaded(key)
