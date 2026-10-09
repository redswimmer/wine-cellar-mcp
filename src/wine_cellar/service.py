"""Business logic behind the MCP tools."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from . import search
from .models import Verdict, Wine
from .photos import InvalidPhotoError, PhotoStore, Upload, photo_id_of, wine_photo_owner
from .repository import WineRepository
from .vectors import WinePhotoIndex, WineTextIndex

log = logging.getLogger(__name__)


class WineNotFoundError(LookupError):
    pass


def _best_effort[T](action: Callable[[], T], what: str) -> T | None:
    """Run a vector or photo-storage step that must never fail the request. DynamoDB is the source of truth."""
    try:
        return action()
    except Exception:
        log.exception("%s failed", what)
        return None


class PhotosDisabledError(RuntimeError):
    pass


class PhotoNotFoundError(LookupError):
    pass


class WineService:
    def __init__(
        self,
        repo: WineRepository,
        text_index: WineTextIndex | None = None,
        *,
        photos: PhotoStore | None = None,
        photo_index: WinePhotoIndex | None = None,
    ) -> None:
        self._repo = repo
        self._text_index = text_index
        self._photos = photos
        self._photo_index = photo_index

    def _save(self, wine: Wine) -> None:
        """Store a wine, then refresh its text vector.

        DynamoDB is the source of truth, so a failed vector write is logged rather than failing the save;
        `wine-tokens reindex` repairs the index.
        """
        self._repo.put(wine)
        text_index = self._text_index
        if text_index is not None:
            _best_effort(lambda: text_index.upsert(wine), f"Text vector upsert for wine {wine.wine_id}")

    def create_wine(
        self,
        user_id: str,
        *,
        name: str,
        vintage: int | None = None,
        varietal: str = "",
        verdict: Verdict | None = None,
        notes: str = "",
    ) -> Wine:
        wine = Wine(
            wine_id=str(ULID()),
            user_id=user_id,
            name=name.strip(),
            vintage=vintage,
            varietal=varietal.strip(),
            verdict=verdict,
            notes=notes.strip(),
        )
        self._save(wine)
        return wine

    def update_wine(self, user_id: str, wine_id: str, **changes: Any) -> Wine:
        """Change the given fields; fields left out (None) keep their value."""
        wine = self.get_wine(user_id, wine_id)
        changes = {k: v.strip() if type(v) is str else v for k, v in changes.items() if v is not None}
        updated = Wine.model_validate({**wine.model_dump(), **changes, "updated_at": datetime.now(UTC)})
        self._save(updated)
        return updated

    def add_photo(self, user_id: str, wine_id: str) -> Upload:
        """An upload link that saves a photo to this wine. `photo_uploaded` finishes the job once the file lands."""
        self.get_wine(user_id, wine_id)
        return self._photo_store().create_wine_upload(user_id, wine_id)

    def upload_search_photo(self, user_id: str) -> Upload:
        return self._photo_store().create_search_upload(user_id)

    def _photo_store(self) -> PhotoStore:
        if self._photos is None:
            raise PhotosDisabledError("Photos aren't enabled on this server")
        return self._photos

    def photo_uploaded(self, key: str) -> None:
        """Record and index a photo uploaded to a wine's link (run from the S3 upload event).

        A file that isn't a usable image, or whose wine was deleted meanwhile, is removed. Errors are raised, not
        logged, so the event is retried; every step is safe to repeat.
        """
        owner = wine_photo_owner(key)
        if owner is None:
            log.warning("Ignoring upload outside wines/: %s", key)
            return
        user_id, wine_id = owner
        photos = self._photo_store()
        wine = self._repo.get(user_id, wine_id)
        try:
            photo = photos.get(key)
        except InvalidPhotoError as e:
            log.warning("Removing unusable photo %s: %s", key, e)
            wine = None
        if wine is None:
            photos.delete([key])
            return
        if key not in wine.photo_keys:
            wine = wine.model_copy(update={"photo_keys": [*wine.photo_keys, key], "updated_at": datetime.now(UTC)})
            self._repo.put(wine)  # the text vector doesn't describe photos, so it needs no refresh
        if self._photo_index is not None:
            self._photo_index.upsert(key, photo.data, photo.format, user_id=user_id, wine_id=wine_id)

    def delete_photo(self, user_id: str, wine_id: str, photo_id: str) -> Wine:
        wine = self.get_wine(user_id, wine_id)
        key = next((k for k in wine.photo_keys if photo_id_of(k) == photo_id), None)
        if key is None:
            raise PhotoNotFoundError(f"Wine {wine_id} has no photo {photo_id}")
        updated = wine.model_copy(
            update={"photo_keys": [k for k in wine.photo_keys if k != key], "updated_at": datetime.now(UTC)}
        )
        self._repo.put(updated)
        photo_index, photos = self._photo_index, self._photos
        if photo_index is not None:
            _best_effort(lambda: photo_index.delete([key]), f"Photo vector delete for {key}")
        if photos is not None:
            _best_effort(lambda: photos.delete([key]), f"Photo delete for {key}")
        return updated

    def get_wine(self, user_id: str, wine_id: str) -> Wine:
        wine = self._repo.get(user_id, wine_id)
        if wine is None:
            raise WineNotFoundError(f"No wine {wine_id} for this user")
        return wine

    def delete_wine(self, user_id: str, wine_id: str) -> None:
        wine = self.get_wine(user_id, wine_id)
        if not self._repo.delete(user_id, wine_id):
            raise WineNotFoundError(f"No wine {wine_id} for this user")
        # The wine is gone either way; anything left behind is only storage, and search ignores it.
        text_index, photo_index, photos = self._text_index, self._photo_index, self._photos
        if text_index is not None:
            _best_effort(lambda: text_index.delete(wine_id), f"Text vector delete for wine {wine_id}")
        if photo_index is not None:
            _best_effort(lambda: photo_index.delete(wine.photo_keys), f"Photo vector delete for wine {wine_id}")
        if photos is not None:
            # By prefix, so a photo uploaded but not yet recorded goes too.
            _best_effort(lambda: photos.delete_wine_photos(user_id, wine_id), f"Photo delete for wine {wine_id}")

    def search_wines(self, user_id: str, query: str = "", photo_ref: str | None = None, limit: int = 5) -> list[Wine]:
        """Best matches first, merging every search that applies by rank (reciprocal rank fusion).

        Text is fuzzy-matched by name and matched by meaning against the text vectors; a photo is matched against
        the label photo vectors. Text and photos are never compared with each other. The search photo is deleted
        afterwards; it is never saved.
        """
        photo = None
        if photo_ref:
            store = self._photo_store()
            photo = store.load_search_photo(user_id, photo_ref)
            _best_effort(lambda: store.delete([photo_ref]), f"Search photo cleanup for {photo_ref}")
        wines = {w.wine_id: w for w in self._repo.list_for_user(user_id)}
        text_index, photo_index = self._text_index, self._photo_index
        rankings: list[list[str]] = []
        if query.strip():
            rankings.append([w.wine_id for w, _ in search.rank(query, list(wines.values()), limit=10)])
            if text_index is not None:
                rankings.append(_best_effort(lambda: text_index.query(user_id, query), "Text vector query") or [])
        if photo is not None and photo_index is not None:
            ranking = _best_effort(lambda: photo_index.query(user_id, photo.data, photo.format), "Photo vector query")
            rankings.append(ranking or [])
        # Vectors can outlive a wine if a delete's cleanup failed, so keep only wines that exist.
        rankings = [[wine_id for wine_id in r if wine_id in wines] for r in rankings]
        return [wines[wine_id] for wine_id in search.fuse(rankings)[:limit]]

    def list_wines(
        self,
        user_id: str,
        *,
        verdict: Verdict | None = None,
        varietal: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        updated_after: datetime | None = None,
        updated_before: datetime | None = None,
    ) -> list[Wine]:
        """The user's wines, most recently updated first. Date bounds are inclusive; naive datetimes are UTC."""
        wines = self._repo.list_for_user(user_id)
        if verdict:
            wines = [w for w in wines if w.verdict == verdict]
        if varietal:
            wines = [w for w in wines if varietal.lower() in w.varietal.lower()]
        for field, after, before in [
            ("created_at", created_after, created_before),
            ("updated_at", updated_after, updated_before),
        ]:
            if after:
                wines = [w for w in wines if getattr(w, field) >= _utc(after)]
            if before:
                wines = [w for w in wines if getattr(w, field) <= _utc(before)]
        return sorted(wines, key=lambda w: w.updated_at, reverse=True)


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
