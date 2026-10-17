"""Deterministic IMDb subset.

The subset is a prefix of title.basics in published file order, not a random
sample. ``limit`` is the number of distinct title ids to keep. ``limit`` 0 keeps
every title. Ratings and crew rows are kept only when their tconst is in that
same id set. Callers use one id set for every database so SQLite and Postgres
are not sampled separately.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from object_graph_performance_lab.errors import LabError
from object_graph_performance_lab.tsv import (
    CREW_COLUMNS,
    RATING_COLUMNS,
    TITLE_COLUMNS,
    RatingRow,
    TitleRow,
    iter_dicts,
    missing_to_none,
    parse_crew_links,
    parse_rating_row,
    parse_title_row,
)


def _check_limit(limit: int) -> None:
    if limit < 0:
        raise LabError("--limit must be >= 0. Use 0 to read every title.")


def iter_titles(
    path: Path,
    limit: int,
    ids: set[str] | None = None,
    on_scan: Callable[[int], None] | None = None,
) -> Iterator[TitleRow]:
    """Yield the first ``limit`` distinct titles. Fill ``ids`` with their tconsts.

    A repeated tconst keeps the first row and skips the later one. ``limit`` 0
    does not stop early.
    """
    _check_limit(limit)
    seen = ids if ids is not None else set()
    kept = 0
    for scanned, raw in enumerate(iter_dicts(path, TITLE_COLUMNS), start=1):
        if on_scan is not None and scanned % 250_000 == 0:
            on_scan(scanned)
        try:
            title = parse_title_row(raw)
        except ValueError as exc:
            raise LabError(f"{path} row {scanned}: {exc}") from exc
        if title is None or title.title_id in seen:
            continue
        seen.add(title.title_id)
        kept += 1
        yield title
        if limit and kept >= limit:
            return


def iter_ratings(
    path: Path,
    ids: set[str],
    on_scan: Callable[[int], None] | None = None,
) -> Iterator[RatingRow]:
    """Yield ratings whose title id is in ``ids``. The first row for an id wins."""
    if not ids:
        return
    seen: set[str] = set()
    for scanned, raw in enumerate(iter_dicts(path, RATING_COLUMNS), start=1):
        if on_scan is not None and scanned % 250_000 == 0:
            on_scan(scanned)
        try:
            rating = parse_rating_row(raw)
        except ValueError as exc:
            raise LabError(f"{path} row {scanned}: {exc}") from exc
        if rating is None or rating.title_id not in ids or rating.title_id in seen:
            continue
        seen.add(rating.title_id)
        yield rating
        if len(seen) == len(ids):
            return


def iter_crew(
    path: Path,
    ids: set[str],
    on_scan: Callable[[int], None] | None = None,
) -> Iterator[tuple[str, str, str]]:
    """Yield (title_id, person_id, role) links for titles in ``ids``."""
    if not ids:
        return
    seen_titles: set[str] = set()
    for scanned, raw in enumerate(iter_dicts(path, CREW_COLUMNS), start=1):
        if on_scan is not None and scanned % 250_000 == 0:
            on_scan(scanned)
        title_id = missing_to_none(raw.get("tconst"))
        if title_id is None or title_id not in ids or title_id in seen_titles:
            continue
        seen_titles.add(title_id)
        yield from parse_crew_links(raw)
        if len(seen_titles) == len(ids):
            return
