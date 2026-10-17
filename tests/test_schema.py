import sqlite3

import pytest

from object_graph_performance_lab.db import Target
from object_graph_performance_lab.schema import ensure_schema


def test_schema_is_idempotent_and_enforces_foreign_keys(sqlite_target: Target):
    ensure_schema(sqlite_target)
    columns = [
        row[1]
        for row in sqlite_target.fetchall("PRAGMA table_info(titles)")
    ]
    assert columns == [
        "title_id",
        "name",
        "type",
        "release_year",
        "runtime_minutes",
        "is_adult",
    ]
    indexes = {
        row[0]
        for row in sqlite_target.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    assert {
        "idx_titles_type",
        "idx_titles_release_year",
        "idx_ratings_vote_count",
        "idx_title_crew_person_id",
        "idx_title_crew_role",
    } <= indexes
    assert sqlite_target.fetchone("PRAGMA foreign_keys")[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_target.execute(
            "INSERT INTO ratings (title_id, average_rating, vote_count) VALUES (?, ?, ?)",
            ("tt-missing", 1.0, 1),
        )
    sqlite_target.rollback()
