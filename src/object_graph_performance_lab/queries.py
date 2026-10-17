"""Small read queries used by tests. Not a benchmark harness."""

from __future__ import annotations

from object_graph_performance_lab.db import Target


def titles_by_year_and_type(target: Target, year: int, title_type: str) -> list[tuple]:
    return target.fetchall(
        """
        SELECT title_id, name, type, release_year
        FROM titles
        WHERE release_year = ? AND type = ?
        ORDER BY title_id
        """,
        (year, title_type),
    )
