"""Shared fixtures: moto for DynamoDB and S3, in-memory fakes for Bedrock and S3 Vectors.

`service` is the fully wired WineService that most tests drive. Tests of a deliberately degraded setup (no photos,
a failing index) build their own WineService from these fixtures.
"""

import boto3
import pytest
from botocore.config import Config
from moto import mock_aws

from wine_cellar.photos import PhotoStore
from wine_cellar.repository import WineRepository
from wine_cellar.service import WineService
from wine_cellar.vectors import Embedder, WinePhotoIndex, WineTextIndex

from .fakes import FakeBedrock, FakeS3Vectors

TABLE = "wines-test"
BUCKET = "photos-test"
USER = "usr_1"
JPEG = b"\xff\xd8\xff\xe0" + b"label" * 100


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
def bedrock():
    return FakeBedrock()


@pytest.fixture
def text_vectors():
    return FakeS3Vectors()


@pytest.fixture
def photo_vectors():
    return FakeS3Vectors()


@pytest.fixture
def service(table, s3, bedrock, text_vectors, photo_vectors):
    embedder = Embedder(bedrock)
    return WineService(
        WineRepository(table),
        WineTextIndex(text_vectors, embedder, bucket="vb", index="wine-text"),
        photos=PhotoStore(s3, BUCKET),
        photo_index=WinePhotoIndex(photo_vectors, embedder, bucket="vb", index="wine-photos"),
    )
