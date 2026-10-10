"""Lambda entry point for the S3 event sent when a photo is uploaded to a wine's link."""

from typing import Any

import boto3
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.logging.utils import copy_config_to_registered_loggers

from .bootstrap import build_service
from .config import Settings
from .photos import uploaded_keys

logger = Logger()
copy_config_to_registered_loggers(source_logger=logger)
tracer = Tracer()

settings = Settings.from_env()
_service = build_service(settings, boto3.resource("dynamodb").Table(settings.table_name))


@logger.inject_lambda_context
@tracer.capture_lambda_handler(capture_response=False)
def handler(event: dict[str, Any], context: Any) -> None:
    for key in uploaded_keys(event):
        _service.photo_uploaded(key)
