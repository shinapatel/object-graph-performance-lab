import sqlite3

import pytest

from object_graph_performance_lab.config import DatasetPaths
from object_graph_performance_lab.db import Target, connect_sqlite
from object_graph_performance_lab.errors import LabError
from object_graph_performance_lab.ingest import ingest_files
from object_graph_performance_lab.schema import ensure_schema


def _ids(target: Target) -> list[str]:
    return [row[0] for row in target.fetchall("SELECT title_id FROM titles ORDER BY rowid")]


def _counts(target: Target) -> tuple[int, int, int]:
    return (
        target.fetchone("SELECT COUNT(*) FROM titles")[0],
        target.fetchone("SELECT COUNT(*) FROM ratings")[0],
        target.fetchone("SELECT COUNT(*) FROM title_crew")[0],
    )


def test_two_targets_get_the_same_subset(tmp_path, datasets: DatasetPaths):
    first = connect_sqlite(tmp_path / "a.sqlite")
    second = connect_sqlite(tmp_path / "b.sqlite")
    try:
        ingest_files(datasets, [first, second], 3)
        expected = ["tt0000009", "tt0000001", "tt0000003"]
        assert _ids(first) == expected
        assert _ids(second) == expected
        assert _counts(first) == (3, 3, 4)
        assert _counts(second) == _counts(first)
        adult = first.fetchone("SELECT is_adult FROM titles WHERE title_id = 'tt0000003'")[0]
        year = first.fetchone(
            "SELECT release_year, runtime_minutes FROM titles WHERE title_id = 'tt0000003'"
        )
        rating = first.fetchone(
            "SELECT average_rating, vote_count FROM ratings WHERE title_id = 'tt0000003'"
        )
        assert adult is None
        assert year == (None, None)
        assert rating == (None, None)
        name = first.fetchone("SELECT name FROM titles WHERE title_id = 'tt0000001'")[0]
        assert name == "Café, The"
    finally:
        first.close()
        second.close()


def test_duplicate_ingest_does_not_change_rows(sqlite_target: Target, datasets: DatasetPaths):
    ingest_files(datasets, [sqlite_target], 3)
    before = _counts(sqlite_target)
    ingest_files(datasets, [sqlite_target], 3)
    assert _counts(sqlite_target) == before == (3, 3, 4)
    assert sqlite_target.fetchone(
        "SELECT state_value FROM ingest_state WHERE state_key = 'status'"
    )[0] == "complete"


def test_different_limit_is_refused_without_deleting(sqlite_target: Target, datasets: DatasetPaths):
    ingest_files(datasets, [sqlite_target], 3)
    with pytest.raises(LabError, match="Refusing to ingest"):
        ingest_files(datasets, [sqlite_target], 2)
    assert _counts(sqlite_target) == (3, 3, 4)


def test_resume_after_a_failed_batch(tmp_path, datasets: DatasetPaths):
    healthy = connect_sqlite(tmp_path / "healthy.sqlite")
    partial = connect_sqlite(tmp_path / "partial.sqlite")

    class Flaky(Target):
        def write_many(self, statement: str, rows: list[tuple]) -> None:
            if "average_rating" in statement:
                raise RuntimeError("network down")
            super().write_many(statement, rows)

    flaky = Flaky(kind=partial.kind, conn=partial.conn, label=partial.label)
    try:
        with pytest.raises(LabError, match="reset --yes"):
            ingest_files(datasets, [healthy, flaky], 3, batch_size=10)
        assert _counts(healthy) == (3, 3, 0)
        assert _counts(partial) == (3, 0, 0)
        ingest_files(datasets, [healthy, partial], 3, batch_size=10)
        assert _counts(healthy) == (3, 3, 4)
        assert _counts(partial) == (3, 3, 4)
    finally:
        healthy.close()
        partial.close()


def test_reopen_without_rebuild(tmp_path, datasets: DatasetPaths):
    path = tmp_path / "imdb.sqlite"
    target = connect_sqlite(path)
    try:
        ingest_files(datasets, [target], 3)
    finally:
        target.close()
    again = connect_sqlite(path)
    try:
        assert again.fetchone("PRAGMA foreign_keys")[0] == 1
        assert again.fetchone("SELECT COUNT(*) FROM titles")[0] == 3
        with pytest.raises(sqlite3.IntegrityError):
            again.execute(
                "INSERT INTO title_crew (title_id, person_id, role) VALUES (?, ?, ?)",
                ("tt-missing", "nm1", "director"),
            )
    finally:
        again.close()
