"""Shared fixtures: moto for DynamoDB and S3, in-memory fakes of our search indexes.

`service` is the fully wired WineService that most tests drive, and `call` drives it through the real MCP handler.
Tests of a deliberately degraded setup (no photos, no index) build their own WineService from these fixtures.
"""

import boto3
import pytest
from botocore.config import Config
from moto import mock_aws

from wine_cellar.lambda_adapter import make_handler
from wine_cellar.photos import PhotoStore
from wine_cellar.repository import WineRepository
from wine_cellar.server import build_app
from wine_cellar.service import WineService
from wine_cellar.tokens import TokenStore
from wine_cellar.users import UserStore

from .fakes import FakePhotoIndex, FakeTextIndex

TABLE = "wines-test"
BUCKET = "photos-test"
USER = "usr_1"
JPEG = b"\xff\xd8\xff\xe0" + b"label" * 100
PDF = b"%PDF-1.7"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def aws():
    with mock_aws():
        yield


@pytest.fixture
def table(aws):
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    return ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def s3(aws):
    client = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
    client.create_bucket(Bucket=BUCKET)
    return client


@pytest.fixture
def repo(table):
    return WineRepository(table)


@pytest.fixture
def text_index():
    return FakeTextIndex()


@pytest.fixture
def photo_index():
    return FakePhotoIndex()


@pytest.fixture
def service(repo, s3, text_index, photo_index):
    return WineService(repo, text_index, photos=PhotoStore(s3, BUCKET), photo_index=photo_index)


@pytest.fixture
def tokens(table):
    users, store = UserStore(table), TokenStore(table)
    return {name: store.create(users.create(name).user_id, f"{name}-agent") for name in ("andrew", "sam")}


@pytest.fixture
def call(service, table, tokens):
    """Calls the MCP handler over the fully wired test service, as andrew unless another token is given."""
    from .agent import HOST, client  # agent imports this module's constants

    return client(make_handler(*build_app(service, TokenStore(table).resolve, [HOST])), tokens["andrew"])
