"""Lambda entry point."""

import boto3

from .config import Settings
from .lambda_adapter import make_handler
from .photos import PhotoStore
from .repository import WineRepository
from .server import build_app
from .service import WineService
from .tokens import TokenStore
from .vectors import Embedder, WinePhotoIndex, WineTextIndex


def _service(settings: Settings, table) -> WineService:
    if not settings.vector_bucket:
        return WineService(WineRepository(table))
    s3vectors, embedder = boto3.client("s3vectors"), Embedder(boto3.client("bedrock-runtime"))
    return WineService(
        WineRepository(table),
        WineTextIndex(s3vectors, embedder, bucket=settings.vector_bucket, index=settings.text_index),
        photos=PhotoStore(boto3.client("s3"), settings.photo_bucket) if settings.photo_bucket else None,
        photo_index=WinePhotoIndex(s3vectors, embedder, bucket=settings.vector_bucket, index=settings.photo_index),
    )


settings = Settings.from_env()
_table = boto3.resource("dynamodb").Table(settings.table_name)
_tokens = TokenStore(_table)
handler = make_handler(*build_app(_service(settings, _table), _tokens.resolve, settings.allowed_hosts))
