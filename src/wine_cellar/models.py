"""Domain model: a wine the person has tried and what they think of it."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


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
