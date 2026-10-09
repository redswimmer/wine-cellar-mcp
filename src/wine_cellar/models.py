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
    vintage: int | None = Field(default=None, description="Year on the label, or null for non-vintage")
    varietal: str = Field(default="", description="Grape or blend, e.g. 'Cabernet Sauvignon'")
    verdict: Verdict | None = None
    notes: str = ""
    photo_keys: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def _from_old_fields(cls, data: Any) -> Any:
        """Read wines stored before the model was simplified (producer + name, a varietals list)."""
        if isinstance(data, dict) and "producer" in data:
            data = dict(data)
            data["name"] = " ".join(p for p in [data.pop("producer"), data.get("name", "")] if p)
            if varietals := data.pop("varietals", None):
                data.setdefault("varietal", ", ".join(varietals))
        return data

    @property
    def search_text(self) -> str:
        return " ".join(p for p in [self.name, str(self.vintage or ""), self.varietal] if p).lower()

    @property
    def label(self) -> str:
        return " ".join(p for p in [self.name, str(self.vintage) if self.vintage else ""] if p)


class InvalidPhotoError(ValueError):
    """The photo can't be used; the message tells the agent what to do instead."""


@dataclass(frozen=True)
class Upload:
    """A link the agent PUTs a photo to, and the limits that apply to it."""

    photo_ref: str
    upload_url: str
    max_bytes: int
    expires_in_seconds: int


def photo_id_of(key: str) -> str:
    """A photo's id: the ULID at the end of its key, which also records when it was added."""
    return key.rsplit("/", 1)[-1].split(".", 1)[0]


def photo_added_at(key: str) -> datetime:
    return ULID.from_str(photo_id_of(key)).datetime
