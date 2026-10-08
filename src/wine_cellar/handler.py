"""Lambda entry point."""

import functools

import boto3

from .config import Settings
from .lambda_adapter import make_handler
from .repository import WineRepository
from .server import build_app
from .service import WineService

settings = Settings.from_env()
_secrets = boto3.client("secretsmanager")


@functools.cache
def _token() -> str:
    # Cached for the life of the execution environment; rotating the secret takes effect on new cold starts.
    return _secrets.get_secret_value(SecretId=settings.token_secret_arn)["SecretString"]


_service = WineService(WineRepository(boto3.resource("dynamodb").Table(settings.table_name)))
handler = make_handler(*build_app(_service, _token, settings.allowed_hosts))
