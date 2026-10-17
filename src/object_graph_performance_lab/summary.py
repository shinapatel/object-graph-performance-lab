"""Inspect loaded rows and connection health. This does not time queries."""

from __future__ import annotations

from object_graph_performance_lab.db import Target, probe
from object_graph_performance_lab.ingest import read_state
from object_graph_performance_lab.verify import adult_token, decimal_token


def inspect_target(target: Target) -> None:
    print(probe(target), flush=True)
    titles = int(target.fetchone("SELECT COUNT(*) FROM titles")[0])
    ratings = int(target.fetchone("SELECT COUNT(*) FROM ratings")[0])
    crew = int(target.fetchone("SELECT COUNT(*) FROM title_crew")[0])
    print(f"  titles: {titles}", flush=True)
    print(f"  ratings: {ratings}", flush=True)
    print(f"  crew relationships: {crew}", flush=True)
    state = read_state(target)
    if state.get("subset_limit") is not None:
        print(f"  subset_limit: {state.get('subset_limit')}", flush=True)
        print(f"  ingest status: {state.get('status', '')}", flush=True)
    rows = target.fetchall(
        """
        SELECT t.title_id, t.name, t.type, t.release_year, t.is_adult,
               r.average_rating, r.vote_count
        FROM titles AS t
        LEFT JOIN ratings AS r ON r.title_id = t.title_id
        ORDER BY t.title_id
        LIMIT 5
        """
    )
    print("  examples:", flush=True)
    if not rows:
        print("    (no titles)", flush=True)
    for row in rows:
        average = "null" if row[5] is None else decimal_token(row[5])
        votes = "null" if row[6] is None else str(row[6])
        year = "null" if row[3] is None else str(row[3])
        adult = adult_token(row[4]) or "null"
        print(
            f"    {row[0]} | {row[1]} | {row[2]} | year {year} | "
            f"adult {adult} | rating {average} | votes {votes}",
            flush=True,
        )
    crew_rows = target.fetchall(
        """
        SELECT title_id, person_id, role
        FROM title_crew
        ORDER BY title_id, role, person_id
        LIMIT 8
        """
    )
    for title_id, person_id, role in crew_rows:
        print(f"    crew {title_id} {role} {person_id}", flush=True)
