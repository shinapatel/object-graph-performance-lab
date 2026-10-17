"""Equivalent title queries for the cached skeleton and for SQLite.

Both paths return ``TitleRecord`` values. The Python path reuses skeleton
objects. The SQLite path filters in SQL and constructs only the matching records.
"""

from __future__ import annotations

from dataclasses import dataclass

from object_graph_performance_lab.db import Target
from object_graph_performance_lab.benchmarks.skeleton import (
    TitleRecord,
    group_crew,
    record_from_row,
)

TITLE_COLUMNS = """
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
"""

CREW_COLUMNS = """
SELECT c.title_id, c.person_id, c.role
FROM title_crew AS c
INNER JOIN titles AS t ON t.title_id = c.title_id
LEFT JOIN ratings AS r ON r.title_id = t.title_id
"""

CREW_ALL = """
SELECT title_id, person_id, role
FROM title_crew
ORDER BY title_id, role, person_id
"""


@dataclass(frozen=True, slots=True)
class Filter:
    """Predicates that exist on the stored IMDb columns.

    ``require_rating`` matches a ratings row, including a row whose average
    and vote count are NULL. A NULL ``release_year`` fails ``release_year_at_least``.
    """

    title_type: str | None = None
    release_year: int | None = None
    release_year_at_least: int | None = None
    require_rating: bool = False

    def is_open(self) -> bool:
        return (
            self.title_type is None
            and self.release_year is None
            and self.release_year_at_least is None
            and not self.require_rating
        )


@dataclass(frozen=True, slots=True)
class GraphResult:
    records: tuple[TitleRecord, ...]
    records_examined: int
    objects_materialized: int
    title_rows_fetched: int
    crew_rows_fetched: int
    access: str


def query_python(
    titles: tuple[TitleRecord, ...],
    by_id: dict[str, TitleRecord],
    *,
    kind: str,
    filt: Filter,
    scoped_ids: tuple[str, ...] | None,
) -> GraphResult:
    """Filter the cached skeleton without copying its records."""
    if kind == "full":
        return GraphResult(
            records=titles,
            records_examined=0,
            objects_materialized=0,
            title_rows_fetched=0,
            crew_rows_fetched=0,
            access="reused_sequence",
        )
    if kind == "scoped":
        if not scoped_ids:
            raise ValueError("scoped lookup requires title ids")
        found: list[TitleRecord] = []
        for title_id in scoped_ids:
            record = by_id.get(title_id)
            if record is not None:
                found.append(record)
        if len(found) > 1 and any(
            found[index].title_id > found[index + 1].title_id
            for index in range(len(found) - 1)
        ):
            found.sort(key=lambda record: record.title_id)
        return GraphResult(
            records=tuple(found),
            records_examined=len(scoped_ids),
            objects_materialized=0,
            title_rows_fetched=0,
            crew_rows_fetched=0,
            access="dict_probe",
        )
    if kind != "filter":
        raise ValueError(f"unknown graph query kind {kind}")
    matched = tuple(record for record in titles if _matches(record, filt))
    return GraphResult(
        records=matched,
        records_examined=len(titles),
        objects_materialized=0,
        title_rows_fetched=0,
        crew_rows_fetched=0,
        access="linear_scan",
    )


def query_sqlite(
    target: Target,
    *,
    kind: str,
    filt: Filter,
    scoped_ids: tuple[str, ...] | None,
) -> GraphResult:
    """Apply the same predicate in SQL and build records for the matches."""
    title_sql, title_params, crew_sql, crew_params = statement_pair(kind, filt, scoped_ids)
    title_rows = list(target.fetchall(title_sql, title_params))
    crew_rows = list(target.fetchall(crew_sql, crew_params))
    crew_by_title = group_crew(crew_rows)
    records = tuple(
        record_from_row(row, crew_by_title.get(row[0], ())) for row in title_rows
    )
    return GraphResult(
        records=records,
        records_examined=len(title_rows),
        objects_materialized=len(records),
        title_rows_fetched=len(title_rows),
        crew_rows_fetched=len(crew_rows),
        access="sql",
    )


def statement_pair(
    kind: str,
    filt: Filter,
    scoped_ids: tuple[str, ...] | None,
) -> tuple[str, tuple, str, tuple]:
    """Title query and crew query. Parameters stay bound, never interpolated."""
    if kind == "full":
        title_sql = TITLE_COLUMNS + " ORDER BY t.title_id"
        return title_sql, (), CREW_ALL, ()
    if kind == "scoped":
        if not scoped_ids:
            raise ValueError("scoped lookup requires title ids")
        placeholders = ", ".join("?" * len(scoped_ids))
        where = f" WHERE t.title_id IN ({placeholders})"
        title_sql = TITLE_COLUMNS + where + " ORDER BY t.title_id"
        crew_sql = (
            CREW_COLUMNS
            + where
            + " ORDER BY c.title_id, c.role, c.person_id"
        )
        return title_sql, scoped_ids, crew_sql, scoped_ids
    if kind != "filter":
        raise ValueError(f"unknown graph query kind {kind}")
    clause, params = _filter_clause(filt)
    title_sql = TITLE_COLUMNS + clause + " ORDER BY t.title_id"
    crew_sql = CREW_COLUMNS + clause + " ORDER BY c.title_id, c.role, c.person_id"
    return title_sql, params, crew_sql, params


def explain_plans(
    target: Target,
    kind: str,
    filt: Filter,
    scoped_ids: tuple[str, ...] | None,
) -> dict[str, str]:
    title_sql, title_params, crew_sql, crew_params = statement_pair(kind, filt, scoped_ids)
    return {
        "titles": _explain(target, title_sql, title_params),
        "crew": _explain(target, crew_sql, crew_params),
    }


def select_scoped_ids(target: Target, *, step: int = 200, count: int = 50) -> tuple[str, ...]:
    """Every ``step``-th title id, in title id order, capped at ``count``.

    On the 10,000-title prefix, step 200 yields exactly 50 ids.
    """
    rows = target.fetchall("SELECT title_id FROM titles ORDER BY title_id")
    return tuple(row[0] for row in rows[::step][:count])


def _matches(record: TitleRecord, filt: Filter) -> bool:
    if filt.title_type is not None and record.type != filt.title_type:
        return False
    if filt.release_year is not None and record.release_year != filt.release_year:
        return False
    if filt.release_year_at_least is not None:
        if record.release_year is None or record.release_year < filt.release_year_at_least:
            return False
    if filt.require_rating and not record.has_rating:
        return False
    return True


def _filter_clause(filt: Filter) -> tuple[str, tuple]:
    clauses: list[str] = []
    params: list[object] = []
    if filt.title_type is not None:
        clauses.append("t.type = ?")
        params.append(filt.title_type)
    if filt.release_year is not None:
        clauses.append("t.release_year = ?")
        params.append(filt.release_year)
    if filt.release_year_at_least is not None:
        clauses.append("t.release_year >= ?")
        params.append(filt.release_year_at_least)
    if filt.require_rating:
        clauses.append("r.title_id IS NOT NULL")
    if not clauses:
        return "", ()
    return " WHERE " + " AND ".join(clauses), tuple(params)


def _explain(target: Target, sql: str, params: tuple) -> str:
    rows = target.fetchall(f"EXPLAIN QUERY PLAN {sql}", params)
    return "\n".join(" | ".join(str(value) for value in row) for row in rows)
