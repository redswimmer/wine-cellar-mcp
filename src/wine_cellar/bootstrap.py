"""Composition root: builds the service and the MCP handler from settings. The only place AWS clients are made."""

import boto3
from botocore.config import Config
from mangum import Mangum

from .config import Settings
from .lambda_adapter import make_handler
from .photos import PhotoStore
from .repository import WineRepository
from .server import build_app
from .service import WineService
from .tokens import TokenStore
from .vectors import Embedder, WinePhotoIndex, WineTextIndex


def build_service(settings: Settings, table, region: str | None = None) -> WineService:
    """The service with every store the settings enable. Photos need vectors too, so no vector bucket means neither."""
    if not settings.vector_bucket:
        return WineService(WineRepository(table))
    s3vectors = boto3.client("s3vectors", region_name=region)
    embedder = Embedder(boto3.client("bedrock-runtime", region_name=region))
    s3 = boto3.client("s3", region_name=region, config=Config(signature_version="s3v4"))
    return WineService(
        WineRepository(table),
        WineTextIndex(s3vectors, embedder, bucket=settings.vector_bucket, index=settings.text_index),
        photos=PhotoStore(s3, settings.photo_bucket) if settings.photo_bucket else None,
        photo_index=WinePhotoIndex(s3vectors, embedder, bucket=settings.vector_bucket, index=settings.photo_index),
    )


def build_mcp_handler(settings: Settings, table) -> Mangum:
    """The Lambda handler for the MCP server."""
    app, session_manager = build_app(build_service(settings, table), TokenStore(table).resolve, settings.allowed_hosts)
    return make_handler(app, session_manager)
