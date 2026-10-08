"""Domain models for wines and tastings."""

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Verdict(StrEnum):
    LOVE = "love"
    LIKE = "like"
    MEH = "meh"
    DISLIKE = "dislike"


class Style(StrEnum):
    RED = "red"
    WHITE = "white"
    ROSE = "rose"
    SPARKLING = "sparkling"
    DESSERT = "dessert"
    FORTIFIED = "fortified"
    ORANGE = "orange"


class Tasting(BaseModel):
    tasted_on: date
    verdict: Verdict | None = None
    notes: str = ""
    where: str = ""


class Wine(BaseModel):
    wine_id: str
    user_id: str
    producer: str
    name: str = ""
    vintage: int | None = Field(default=None, description="Year, or null for non-vintage")
    region: str = ""
    country: str = ""
    varietals: list[str] = Field(default_factory=list)
    style: Style | None = None
    verdict: Verdict | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    notes: str = ""
    tastings: list[Tasting] = Field(default_factory=list)
    photo_keys: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def search_text(self) -> str:
        parts = [self.producer, self.name, str(self.vintage or ""), self.region, self.country, *self.varietals]
        return " ".join(p for p in parts if p).lower()

    @property
    def label(self) -> str:
        vintage = str(self.vintage) if self.vintage else "NV"
        return " ".join(p for p in [self.producer, self.name, vintage] if p)
