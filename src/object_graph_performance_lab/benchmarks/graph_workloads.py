"""Workloads for the cached Python skeleton versus SQLite pushdown.

Counts are the measured sizes on the ingested 10,000-title prefix:

- full graph: 10,000 titles (100%)
- scoped graph: 50 title ids (0.50%)
- release year 1900: 85 titles (0.85%)
- release year >= 1916 and a ratings row: 1,012 titles (10.12%)
- type movie: 4,200 titles (42%)

A ratings row is rating availability. ``average_rating`` may still be NULL.
"""

from __future__ import annotations

from dataclasses import dataclass

from object_graph_performance_lab.benchmarks.graph_query import Filter
from object_graph_performance_lab.errors import LabError

PREFIX_TITLE_COUNT = 10000

INDEX_NOTE = (
    "The Python skeleton keeps titles in title-id order and indexes title_id "
    "with a dictionary. Scoped requests probe that dictionary. Contextual "
    "filters walk the cached title tuple once; type and year are not indexed "
    "again in Python. SQLite uses the Phase 1 indexes already on disk: the "
    "titles primary key, idx_titles_type, idx_titles_release_year, the ratings "
    "primary key, and the title_crew primary key. No index was added for this "
    "experiment. NULL release_year fails a release_year >= comparison on both "
    "sides. Rating availability is the presence of a ratings row."
)


@dataclass(frozen=True, slots=True)
class GraphWorkload:
    name: str
    summary: str
    kind: str
    filt: Filter
    expected_rows: int | None
    selectivity_label: str


GRAPH_WORKLOADS: tuple[GraphWorkload, ...] = (
    GraphWorkload(
        name="full_graph",
        summary="Every stored title, with rating fields and crew, and no selective predicate.",
        kind="full",
        filt=Filter(),
        expected_rows=10000,
        selectivity_label="100% (10000/10000)",
    ),
    GraphWorkload(
        name="scoped_graph",
        summary="Fifty title ids: every 200th id in title_id order.",
        kind="scoped",
        filt=Filter(),
        expected_rows=50,
        selectivity_label="0.50% (50/10000)",
    ),
    GraphWorkload(
        name="filter_year_1900",
        summary="Titles whose release_year equals 1900.",
        kind="filter",
        filt=Filter(release_year=1900),
        expected_rows=85,
        selectivity_label="0.85% (85/10000)",
    ),
    GraphWorkload(
        name="filter_rated_since_1916",
        summary="Titles with release_year >= 1916 and a ratings row.",
        kind="filter",
        filt=Filter(release_year_at_least=1916, require_rating=True),
        expected_rows=1012,
        selectivity_label="10.12% (1012/10000)",
    ),
    GraphWorkload(
        name="filter_type_movie",
        summary="Titles whose type is movie.",
        kind="filter",
        filt=Filter(title_type="movie"),
        expected_rows=4200,
        selectivity_label="42% (4200/10000)",
    ),
)


def select_graph_workloads(names: list[str] | None) -> tuple[GraphWorkload, ...]:
    """Return the default sequence, or the named workloads in catalog order."""
    if not names:
        return GRAPH_WORKLOADS
    catalog = {workload.name: workload for workload in GRAPH_WORKLOADS}
    unknown = [name for name in names if name not in catalog]
    if unknown:
        known = ", ".join(workload.name for workload in GRAPH_WORKLOADS)
        raise LabError(f"Unknown workload {unknown[0]!r}. Choose: {known}")
    if len(names) != len(set(names)):
        raise LabError("The same workload was requested more than once.")
    chosen = set(names)
    return tuple(workload for workload in GRAPH_WORKLOADS if workload.name in chosen)
