"""Business logic behind the MCP tools."""

import logging
from datetime import UTC, date, datetime

from ulid import ULID

from . import search
from .models import Style, Tasting, Verdict, Wine
from .repository import WineRepository
from .vectors import WineTextIndex

log = logging.getLogger(__name__)


class WineNotFoundError(LookupError):
    pass


class WineService:
    def __init__(self, repo: WineRepository, text_index: WineTextIndex | None = None) -> None:
        self._repo = repo
        self._text_index = text_index

    def _save(self, wine: Wine) -> None:
        """Store a wine, then refresh its text vector.

        DynamoDB is the source of truth, so a failed vector write is logged rather than failing the save;
        `wine-tokens reindex` repairs the index.
        """
        self._repo.put(wine)
        if self._text_index is None:
            return
        try:
            self._text_index.upsert(wine)
        except Exception:
            log.exception("Text vector upsert failed for wine %s", wine.wine_id)

    def save_wine(
        self,
        user_id: str,
        *,
        producer: str,
        name: str = "",
        vintage: int | None = None,
        region: str = "",
        country: str = "",
        varietals: list[str] | None = None,
        style: Style | None = None,
        verdict: Verdict | None = None,
        rating: int | None = None,
        notes: str = "",
    ) -> tuple[Wine, bool]:
        """Create a wine, or update the matching one. Returns the wine and whether it was new."""
        candidate = Wine(
            wine_id=str(ULID()),
            user_id=user_id,
            producer=producer.strip(),
            name=name.strip(),
            vintage=vintage,
            region=region.strip(),
            country=country.strip(),
            varietals=varietals or [],
            style=style,
            verdict=verdict,
            rating=rating,
            notes=notes.strip(),
        )
        existing = search.find_same_wine(candidate, self._repo.list_for_user(user_id))
        if existing is None:
            self._save(candidate)
            return candidate, True

        # Only overwrite fields the caller actually supplied.
        updates = {
            k: v
            for k, v in {
                "region": candidate.region,
                "country": candidate.country,
                "varietals": candidate.varietals,
                "style": style,
                "verdict": verdict,
                "rating": rating,
                "notes": candidate.notes,
            }.items()
            if v not in (None, "", [])
        }
        updated = existing.model_copy(update={**updates, "updated_at": datetime.now(UTC)})
        self._save(updated)
        return updated, False

    def add_tasting(
        self,
        user_id: str,
        wine_id: str,
        *,
        notes: str = "",
        verdict: Verdict | None = None,
        tasted_on: date | None = None,
        where: str = "",
    ) -> Wine:
        wine = self.get_wine(user_id, wine_id)
        tasting = Tasting(tasted_on=tasted_on or date.today(), verdict=verdict, notes=notes.strip(), where=where)
        update: dict[str, object] = {"tastings": [*wine.tastings, tasting], "updated_at": datetime.now(UTC)}
        if verdict is not None:
            update["verdict"] = verdict
        if notes.strip():
            update["notes"] = notes.strip()
        updated = wine.model_copy(update=update)
        self._save(updated)
        return updated

    def get_wine(self, user_id: str, wine_id: str) -> Wine:
        wine = self._repo.get(user_id, wine_id)
        if wine is None:
            raise WineNotFoundError(f"No wine {wine_id} for this user")
        return wine

    def delete_wine(self, user_id: str, wine_id: str) -> None:
        if not self._repo.delete(user_id, wine_id):
            raise WineNotFoundError(f"No wine {wine_id} for this user")
        if self._text_index is None:
            return
        try:
            self._text_index.delete(wine_id)
        except Exception:
            log.exception("Text vector delete failed for wine %s", wine_id)

    def search_wines(self, user_id: str, query: str, limit: int = 5) -> list[tuple[Wine, float]]:
        return search.rank(query, self._repo.list_for_user(user_id), limit=limit)

    def list_wines(
        self,
        user_id: str,
        *,
        verdict: Verdict | None = None,
        style: Style | None = None,
        region: str | None = None,
        varietal: str | None = None,
    ) -> list[Wine]:
        wines = self._repo.list_for_user(user_id)
        if verdict:
            wines = [w for w in wines if w.verdict == verdict]
        if style:
            wines = [w for w in wines if w.style == style]
        if region:
            wines = [w for w in wines if region.lower() in f"{w.region} {w.country}".lower()]
        if varietal:
            wines = [w for w in wines if any(varietal.lower() in v.lower() for v in w.varietals)]
        return sorted(wines, key=lambda w: w.updated_at, reverse=True)
