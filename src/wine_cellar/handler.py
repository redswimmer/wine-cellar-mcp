"""Lambda entry point for the MCP server. Built once per cold start; see bootstrap for the wiring."""

from typing import Any

import boto3
from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.logging.utils import copy_config_to_registered_loggers

from .bootstrap import build_mcp_handler
from .config import Settings

logger = Logger()
copy_config_to_registered_loggers(source_logger=logger)
tracer = Tracer()

settings = Settings.from_env()
_mcp = build_mcp_handler(settings, boto3.resource("dynamodb").Table(settings.table_name))


@logger.inject_lambda_context
@tracer.capture_lambda_handler(capture_response=False)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return _mcp(event, context)
