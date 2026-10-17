"""Read-only workloads for the 10,000-title IMDb prefix.

Parameters are fixed so a later run hits the same rows. Counts below were
observed on that prefix (first 10,000 titles in title.basics file order):

- ``tt0000001`` is one primary-key row (Carmencita).
- ``release_year = 1900`` matches 85 titles. ``idx_titles_release_year`` exists
  on both engines.
- ``tt0005408`` has 21 crew rows. The crew primary key starts with ``title_id``
  on both engines, so the lookup can use that index.
- ``type = 'movie'`` matches 4,200 titles. ``idx_titles_type`` exists on both.

No benchmark-only indexes are added. The statement text is the same for both
engines. PostgreSQL receives ``%s`` placeholders because the connection layer
rewrites ``?``. ``ORDER BY`` makes the row sequence comparable. ``is_adult`` is
INTEGER in SQLite and BOOLEAN in PostgreSQL; comparison maps both to 0/1.
"""

from __future__ import annotations

from dataclasses import dataclass

from object_graph_performance_lab.errors import LabError

PRIMARY_KEY_ID = "tt0000001"
SELECTIVE_YEAR = 1900
RELATIONSHIP_TITLE_ID = "tt0005408"
LARGE_TYPE = "movie"

_TITLE_COLUMNS = (
    "title_id",
    "name",
    "type",
    "release_year",
    "runtime_minutes",
    "is_adult",
)

_TITLE_SQL = (
    "SELECT title_id, name, type, release_year, runtime_minutes, is_adult FROM titles"
)


@dataclass(frozen=True)
class Workload:
    name: str
    summary: str
    sql: str
    params: tuple
    columns: tuple[str, ...]
    min_rows: int
    max_rows: int
    establishes_connection: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "summary": self.summary,
            "sql": self.sql,
            "params": list(self.params),
            "min_rows": self.min_rows,
            "max_rows": self.max_rows,
            "establishes_connection": self.establishes_connection,
        }


QUERY_WORKLOADS: tuple[Workload, ...] = (
    Workload(
        name="primary_key_lookup",
        summary="One title by primary key. Highly selective metadata lookup.",
        sql=f"{_TITLE_SQL} WHERE title_id = ?",
        params=(PRIMARY_KEY_ID,),
        columns=_TITLE_COLUMNS,
        min_rows=1,
        max_rows=1,
    ),
    Workload(
        name="selective_query",
        summary="Titles from one indexed release year. Scoped metadata lookup.",
        sql=f"{_TITLE_SQL} WHERE release_year = ? ORDER BY title_id",
        params=(SELECTIVE_YEAR,),
        columns=_TITLE_COLUMNS,
        min_rows=10,
        max_rows=100,
    ),
    Workload(
        name="relationship_lookup",
        summary="Crew rows for one title, joined back to the title.",
        sql=(
            "SELECT c.person_id, c.role, t.title_id, t.name "
            "FROM title_crew AS c "
            "INNER JOIN titles AS t ON t.title_id = c.title_id "
            "WHERE c.title_id = ? "
            "ORDER BY c.role, c.person_id"
        ),
        params=(RELATIONSHIP_TITLE_ID,),
        columns=("person_id", "role", "title_id", "name"),
        min_rows=1,
        max_rows=100,
    ),
    Workload(
        name="larger_result_set",
        summary="Every movie title in the loaded subset. Broader metadata retrieval.",
        sql=f"{_TITLE_SQL} WHERE type = ? ORDER BY title_id",
        params=(LARGE_TYPE,),
        columns=_TITLE_COLUMNS,
        min_rows=1000,
        max_rows=5000,
    ),
)

CONNECTION_WORKLOAD = Workload(
    name="connection_establishment",
    summary=(
        "Open a new connection and fetch SELECT 1. "
        "Steady-state only; the first connection of the process is reported separately."
    ),
    sql="SELECT 1",
    params=(),
    columns=("one",),
    min_rows=1,
    max_rows=1,
    establishes_connection=True,
)

ALL_WORKLOADS: tuple[Workload, ...] = QUERY_WORKLOADS + (CONNECTION_WORKLOAD,)
_BY_NAME = {workload.name: workload for workload in ALL_WORKLOADS}


def select_workloads(names: list[str] | None) -> tuple[Workload, ...]:
    """Return workloads in the requested order, or the canonical order when omitted."""
    if not names:
        return ALL_WORKLOADS
    chosen: list[Workload] = []
    seen: set[str] = set()
    for name in names:
        workload = _BY_NAME.get(name)
        if workload is None:
            known = ", ".join(workload_name for workload_name in _BY_NAME)
            raise LabError(f"Unknown workload {name!r}. Choose from: {known}")
        if name in seen:
            raise LabError(f"Workload {name!r} was selected more than once.")
        seen.add(name)
        chosen.append(workload)
    return tuple(chosen)


def normalize_value(column: str, value: object) -> object:
    if column != "is_adult":
        return value
    if value is None:
        return None
    if value is True or value == 1:
        return 1
    if value is False or value == 0:
        return 0
    raise LabError(f"unexpected is_adult value {value!r}")


def normalize_rows(columns: tuple[str, ...], rows: list[tuple]) -> list[tuple]:
    normalized: list[tuple] = []
    for row in rows:
        if len(row) != len(columns):
            raise LabError(
                f"expected {len(columns)} columns, got {len(row)}"
            )
        normalized.append(
            tuple(normalize_value(column, value) for column, value in zip(columns, row))
        )
    return normalized


def describe_mismatch(left: list[tuple], right: list[tuple]) -> str:
    if len(left) != len(right):
        return f"row count {len(left)} != {len(right)}"
    for index, (left_row, right_row) in enumerate(zip(left, right)):
        if left_row != right_row:
            return f"row {index} differs: {left_row!r} != {right_row!r}"
    return "rows match"
