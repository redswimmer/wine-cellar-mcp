import io
import json
from datetime import date

import pytest

from wine_cellar.models import Style, Tasting, Verdict, Wine
from wine_cellar.repository import WineRepository
from wine_cellar.service import WineService
from wine_cellar.vectors import EMBEDDING_DIMENSION, EMBEDDING_MODEL_ID, Embedder, WineTextIndex, wine_text

USER = "usr_1"


class FakeBedrock:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    def invoke_model(self, *, modelId, body, accept, contentType):
        assert modelId == EMBEDDING_MODEL_ID
        self.bodies.append(json.loads(body))
        out = {"embeddings": [{"embeddingType": "TEXT", "embedding": [0.1] * EMBEDDING_DIMENSION}]}
        return {"body": io.BytesIO(json.dumps(out).encode())}


class FakeS3Vectors:
    def __init__(self, *, fail: bool = False) -> None:
        self.vectors: dict[str, dict] = {}
        self.fail = fail

    def put_vectors(self, *, vectorBucketName, indexName, vectors):
        if self.fail:
            raise RuntimeError("s3vectors down")
        assert (vectorBucketName, indexName) == ("vb", "wine-text")
        self.vectors.update({v["key"]: v for v in vectors})

    def delete_vectors(self, *, vectorBucketName, indexName, keys):
        if self.fail:
            raise RuntimeError("s3vectors down")
        for k in keys:
            self.vectors.pop(k, None)


@pytest.fixture
def bedrock():
    return FakeBedrock()


@pytest.fixture
def s3v():
    return FakeS3Vectors()


@pytest.fixture
def indexed_service(table, bedrock, s3v):
    return WineService(WineRepository(table), WineTextIndex(s3v, Embedder(bedrock), bucket="vb", index="wine-text"))


def test_wine_text_includes_label_details_and_what_the_person_said():
    wine = Wine(
        wine_id="w1",
        user_id=USER,
        producer="Ridge",
        name="Monte Bello",
        vintage=2019,
        region="Santa Cruz Mountains",
        country="USA",
        varietals=["Cabernet Sauvignon"],
        style=Style.RED,
        verdict=Verdict.LOVE,
        notes="Huge and structured",
        tastings=[Tasting(tasted_on=date(2026, 1, 1), notes="Needs a decant")],
    )
    text = wine_text(wine)
    for part in ["Ridge Monte Bello 2019", "Santa Cruz Mountains, USA", "red", "Cabernet", "love", "Huge", "decant"]:
        assert part in text


def test_save_writes_a_text_vector_scoped_to_the_user(indexed_service, bedrock, s3v):
    wine, _ = indexed_service.save_wine(USER, producer="Ridge", name="Monte Bello", notes="bold")
    vector = s3v.vectors[wine.wine_id]
    assert vector["metadata"] == {"user_id": USER}
    assert len(vector["data"]["float32"]) == EMBEDDING_DIMENSION
    params = bedrock.bodies[0]["singleEmbeddingParams"]
    assert params["embeddingPurpose"] == "GENERIC_INDEX"
    assert params["embeddingDimension"] == EMBEDDING_DIMENSION
    assert "bold" in params["text"]["value"]


def test_updates_and_tastings_refresh_the_vector(indexed_service, bedrock):
    wine, _ = indexed_service.save_wine(USER, producer="Ridge", name="Monte Bello")
    indexed_service.save_wine(USER, producer="Ridge", name="Monte Bello", notes="Even better with food")
    indexed_service.add_tasting(USER, wine.wine_id, notes="Opened up after an hour")
    assert len(bedrock.bodies) == 3
    assert "Opened up" in bedrock.bodies[-1]["singleEmbeddingParams"]["text"]["value"]


def test_delete_removes_the_vector(indexed_service, s3v):
    wine, _ = indexed_service.save_wine(USER, producer="Ridge")
    indexed_service.delete_wine(USER, wine.wine_id)
    assert s3v.vectors == {}


def test_vector_failures_do_not_lose_the_wine(table, bedrock):
    service = WineService(
        WineRepository(table),
        WineTextIndex(FakeS3Vectors(fail=True), Embedder(bedrock), bucket="vb", index="wine-text"),
    )
    wine, _ = service.save_wine(USER, producer="Ridge")
    assert service.get_wine(USER, wine.wine_id).producer == "Ridge"
    service.delete_wine(USER, wine.wine_id)
    assert service.list_wines(USER) == []
