"""Cached title skeleton for Experiment 2.

The skeleton is built once from the local SQLite artifact. Later requests reuse
these objects. Initialization time and traced allocation are recorded here and
are not warm-request samples.
"""

from __future__ import annotations

import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path

from object_graph_performance_lab.db import Target

TITLE_SQL = """
SELECT
    t.title_id,
    t.name,
    t.type,
    t.release_year,
    t.runtime_minutes,
    t.is_adult,
    r.average_rating,
    r.vote_count,
    CASE WHEN r.title_id IS NULL THEN 0 ELSE 1 END
FROM titles AS t
LEFT JOIN ratings AS r ON r.title_id = t.title_id
ORDER BY t.title_id
"""

CREW_SQL = """
SELECT title_id, person_id, role
FROM title_crew
ORDER BY title_id, role, person_id
"""


@dataclass(frozen=True, slots=True)
class TitleRecord:
    """One title plus its rating fields and ordered crew links.

    ``has_rating`` is true when a ratings row exists. A ratings row can still
    store NULL for ``average_rating`` and ``vote_count``. ``crew`` is ordered
    by role, then person id.
    """

    title_id: str
    name: str | None
    type: str | None
    release_year: int | None
    runtime_minutes: int | None
    is_adult: int | None
    average_rating: float | None
    vote_count: int | None
    has_rating: bool
    crew: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Skeleton:
    """Titles sorted by title id, plus a dictionary for id lookups."""

    titles: tuple[TitleRecord, ...]
    by_id: dict[str, TitleRecord]
    title_count: int
    rating_count: int
    crew_count: int


@dataclass(frozen=True, slots=True)
class SkeletonLoad:
    skeleton: Skeleton
    initialization_ns: int
    traced_peak_bytes: int
    traced_retained_bytes: int
    rss_before_bytes: int | None
    rss_after_bytes: int | None


def load_skeleton(target: Target) -> SkeletonLoad:
    """Read the artifact and retain the skeleton.

    The clock includes the SQL reads and object construction. tracemalloc is
    running during that interval, so the time includes tracer overhead.
    ``traced_peak_bytes`` includes temporary row lists. ``traced_retained_bytes``
    is what remains referenced by the skeleton after those lists are released.
    """
    tracemalloc.start()
    try:
        rss_before = _rss_bytes()
        started = time.perf_counter_ns()
        skeleton = _build(target)
        elapsed = time.perf_counter_ns() - started
        _current, peak = tracemalloc.get_traced_memory()
        retained = _retained_bytes(skeleton)
        rss_after = _rss_bytes()
        return SkeletonLoad(
            skeleton=skeleton,
            initialization_ns=elapsed,
            traced_peak_bytes=peak,
            traced_retained_bytes=retained,
            rss_before_bytes=rss_before,
            rss_after_bytes=rss_after,
        )
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()


def _build(target: Target) -> Skeleton:
    title_rows = target.fetchall(TITLE_SQL)
    crew_by_title = group_crew(target.fetchall(CREW_SQL))
    records = tuple(
        record_from_row(row, crew_by_title.get(row[0], ())) for row in title_rows
    )
    by_id = {record.title_id: record for record in records}
    if len(by_id) != len(records):
        raise RuntimeError("duplicate title_id while building the skeleton")
    return Skeleton(
        titles=records,
        by_id=by_id,
        title_count=len(records),
        rating_count=sum(1 for record in records if record.has_rating),
        crew_count=sum(len(record.crew) for record in records),
    )


def _retained_bytes(skeleton: Skeleton) -> int:
    """Bytes still traced after temporary row lists have been released."""
    if not tracemalloc.is_tracing():
        return 0
    current, _peak = tracemalloc.get_traced_memory()
    # Touch the skeleton so a later change cannot treat it as unused.
    _ = skeleton.title_count
    return current


def group_crew(rows: list[tuple]) -> dict[str, tuple[tuple[str, str], ...]]:
    grouped: dict[str, list[tuple[str, str]]] = {}
    for title_id, person_id, role in rows:
        grouped.setdefault(title_id, []).append((person_id, role))
    return {title_id: tuple(links) for title_id, links in grouped.items()}


def record_from_row(row: tuple, crew: tuple[tuple[str, str], ...]) -> TitleRecord:
    average = row[6]
    votes = row[7]
    adult = row[5]
    return TitleRecord(
        title_id=row[0],
        name=row[1],
        type=row[2],
        release_year=None if row[3] is None else int(row[3]),
        runtime_minutes=None if row[4] is None else int(row[4]),
        is_adult=None if adult is None else int(adult),
        average_rating=None if average is None else float(average),
        vote_count=None if votes is None else int(votes),
        has_rating=row[8] == 1,
        crew=crew,
    )


def _rss_bytes() -> int | None:
    """Current resident set size, in bytes, from the Linux process status."""
    status = Path("/proc/self/status")
    if not status.is_file():
        return None
    prefix = "VmRSS:"
    for line in status.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            kib = int(line.split()[1])
            return kib * 1024
    return None
