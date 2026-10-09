"""Low gear: pure domain functions with fiddly edge cases. Most behaviour is tested through the service instead."""

import pytest

from wine_cellar.models import Verdict, Wine
from wine_cellar.photos import sniff, uploaded_keys, wine_photo_owner
from wine_cellar.search import fuse
from wine_cellar.vectors import wine_text

from .conftest import JPEG, USER


def test_reads_wines_saved_before_the_model_was_simplified():
    wine = Wine.model_validate(
        {
            "wine_id": "w",
            "user_id": USER,
            "producer": "Ridge",
            "name": "Monte Bello",
            "varietals": ["Cab", "Merlot"],
            "region": "Santa Cruz",
            "tastings": [],
            "rating": 5,
        }
    )
    assert (wine.name, wine.varietal) == ("Ridge Monte Bello", "Cab, Merlot")


def test_wine_text_includes_label_details_and_what_the_person_said():
    wine = Wine(
        wine_id="w1",
        user_id=USER,
        name="Ridge Monte Bello",
        vintage=2019,
        varietal="Cabernet Sauvignon",
        verdict=Verdict.LOVE,
        notes="Huge and structured",
    )
    text = wine_text(wine)
    for part in ["Ridge Monte Bello 2019", "Cabernet", "love", "Huge"]:
        assert part in text


def test_fuse_rewards_agreement():
    assert fuse([["a", "b"], ["b", "c"]]) == ["b", "a", "c"]
    assert fuse([[], ["x"]]) == ["x"]


def test_sniff_recognises_common_formats():
    assert sniff(JPEG).format == "jpeg"
    assert sniff(b"\x89PNG\r\n\x1a\n" + b"x" * 10).format == "png"
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ").format == "webp"
    assert sniff(b"GIF89a...").format == "gif"


def test_upload_event_keys_are_decoded():
    event = {"Records": [{"s3": {"object": {"key": "wines/usr_1/w1/01J+label%2Bshot.jpg"}}}]}
    assert uploaded_keys(event) == ["wines/usr_1/w1/01J label+shot.jpg"]
    assert uploaded_keys({}) == []


@pytest.mark.parametrize(
    ("key", "owner"),
    [
        ("wines/usr_1/w1/01J", ("usr_1", "w1")),
        ("search/usr_1/01J", None),
        ("wines/usr_1/01J", None),
        ("wines/usr_1/w1/01J/extra", None),
        ("wines//w1/01J", None),
    ],
)
def test_wine_photo_owner_accepts_only_wine_photo_keys(key, owner):
    assert wine_photo_owner(key) == owner
