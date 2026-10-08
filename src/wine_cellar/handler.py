"""Lambda entry point."""

import boto3

from .config import Settings
from .lambda_adapter import make_handler
from .repository import WineRepository
from .server import build_app
from .service import WineService
from .tokens import TokenStore
from .vectors import Embedder, WineTextIndex

settings = Settings.from_env()
_table = boto3.resource("dynamodb").Table(settings.table_name)
_text_index = (
    WineTextIndex(
        boto3.client("s3vectors"),
        Embedder(boto3.client("bedrock-runtime")),
        bucket=settings.vector_bucket,
        index=settings.text_index,
    )
    if settings.vector_bucket
    else None
)
_service = WineService(WineRepository(_table), _text_index)
_tokens = TokenStore(_table)
handler = make_handler(*build_app(_service, _tokens.resolve, settings.allowed_hosts))
