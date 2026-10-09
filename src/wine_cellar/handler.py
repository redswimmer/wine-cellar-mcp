"""Lambda entry point for the MCP server. Built once per cold start; see bootstrap for the wiring."""

import boto3

from .bootstrap import build_mcp_handler
from .config import Settings

settings = Settings.from_env()
handler = build_mcp_handler(settings, boto3.resource("dynamodb").Table(settings.table_name))
