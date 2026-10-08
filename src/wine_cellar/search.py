"""Fuzzy text matching over a user's wines.

A personal list is small enough to score in memory, which avoids running a search cluster.
"""

from rapidfuzz import fuzz, utils

from .models import Wine


def score(query: str, wine: Wine) -> float:
    """0-100 similarity between free text and a wine's label fields."""
    text = wine.search_text
    return max(
        fuzz.token_set_ratio(query, text, processor=utils.default_process),
        fuzz.partial_ratio(query, text, processor=utils.default_process),
    )


def rank(query: str, wines: list[Wine], *, limit: int = 5, min_score: float = 60) -> list[tuple[Wine, float]]:
    scored = [(w, score(query, w)) for w in wines]
    scored = [s for s in scored if s[1] >= min_score]
    scored.sort(key=lambda s: s[1], reverse=True)
    return scored[:limit]


def fuse(rankings: list[list[str]], *, k: int = 60) -> list[str]:
    """Reciprocal rank fusion: merge ranked ID lists, favouring IDs ranked high in any of them.

    Each ID earns 1 / (k + rank) per list it appears in; k = 60 is the usual constant.
    Ranks are compared rather than scores, because fuzzy and vector scores aren't on the same scale.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1 / (k + rank)
    return sorted(scores, key=lambda item: scores[item], reverse=True)


def find_same_wine(candidate: Wine, wines: list[Wine]) -> Wine | None:
    """The existing record for the same producer, name and vintage, if any."""
    target = " ".join(p for p in [candidate.producer, candidate.name] if p)
    for wine in wines:
        if wine.vintage != candidate.vintage:
            continue
        existing = " ".join(p for p in [wine.producer, wine.name] if p)
        if fuzz.token_sort_ratio(target, existing, processor=utils.default_process) >= 90:
            return wine
    return None
