"""Domain model: a wine the person has tried and what they think of it, and its label photos."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator
from ulid import ULID


class Verdict(StrEnum):
    LOVE = "love"
    LIKE = "like"
    MEH = "meh"
    DISLIKE = "dislike"


class Wine(BaseModel):
    wine_id: str
    user_id: str
    name: str = Field(description="What the label says, e.g. 'Ridge Monte Bello Cabernet'")
    varietal: str = Field(default="", description="Grape or blend, e.g. 'Cabernet Sauvignon'")
    verdict: Verdict | None = None
    notes: str = ""
    photo_keys: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def _from_old_fields(cls, data: Any) -> Any:
        """Read wines stored before the model was simplified (producer + name, a varietals list).

        Unknown fields, such as a vintage stored before it was dropped, are ignored.
        """
        if isinstance(data, dict) and "producer" in data:
            data = dict(data)
            data["name"] = " ".join(p for p in [data.pop("producer"), data.get("name", "")] if p)
            if varietals := data.pop("varietals", None):
                data.setdefault("varietal", ", ".join(varietals))
        return data

    @property
    def search_text(self) -> str:
        return " ".join(p for p in [self.name, self.varietal] if p).lower()

    @property
    def photos(self) -> list["PhotoInfo"]:
        """The wine's photos as the agent sees them: an id and when it was added, not the storage key."""
        return [PhotoInfo(photo_id_of(k), photo_added_at(k)) for k in self.photo_keys]


class ImageType(StrEnum):
    """The photo formats accepted, as the Content-Type an upload declares."""

    JPEG = "image/jpeg"
    PNG = "image/png"
    WEBP = "image/webp"
    GIF = "image/gif"


class InvalidPhotoError(ValueError):
    """The photo can't be used; the message tells the agent what to do instead."""


@dataclass(frozen=True)
class Upload:
    """A pre-signed POST the agent uploads a photo with: the form fields to send with the file, and its limits."""

    photo_ref: str
    upload_url: str
    fields: dict[str, str]
    max_bytes: int
    expires_in_seconds: int


@dataclass(frozen=True)
class PhotoInfo:
    photo_id: str
    added_at: datetime


def photo_id_of(key: str) -> str:
    """A photo's id: the ULID at the end of its key, which also records when it was added."""
    return key.rsplit("/", 1)[-1].split(".", 1)[0]


def photo_added_at(key: str) -> datetime:
    return ULID.from_str(photo_id_of(key)).datetime
