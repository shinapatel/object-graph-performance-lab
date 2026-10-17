from object_graph_performance_lab.config import DatasetPaths
from object_graph_performance_lab.db import Target
from object_graph_performance_lab.ingest import ingest_files
from object_graph_performance_lab.verify import RowChecksum, verify_target


def test_checksum_is_order_independent_and_counts_duplicates():
    left = RowChecksum()
    right = RowChecksum()
    for line in ("b", "a"):
        left.add(line)
    for line in ("a", "b"):
        right.add(line)
    assert left.hex() == right.hex()
    left.add("a")
    assert left.hex() != right.hex()


def test_verify_passes_then_fails_when_a_row_changes(sqlite_target: Target, datasets: DatasetPaths):
    ingest_files(datasets, [sqlite_target], 3)
    assert verify_target(sqlite_target, datasets, 3) == []
    sqlite_target.execute(
        "UPDATE titles SET name = ? WHERE title_id = ?",
        ("Changed", "tt0000009"),
    )
    sqlite_target.commit()
    errors = verify_target(sqlite_target, datasets, 3)
    assert errors
    assert any("tt0000009" in error for error in errors)


def test_large_mode_checksum_catches_a_change(sqlite_target: Target, datasets: DatasetPaths, monkeypatch):
    monkeypatch.setattr("object_graph_performance_lab.verify.EXHAUSTIVE_MAX", 0)
    ingest_files(datasets, [sqlite_target], 3)
    assert verify_target(sqlite_target, datasets, 3) == []
    sqlite_target.execute(
        "UPDATE ratings SET vote_count = ? WHERE title_id = ?",
        (999, "tt0000001"),
    )
    sqlite_target.commit()
    errors = verify_target(sqlite_target, datasets, 3)
    assert any("checksum" in error for error in errors)


def test_orphan_is_reported_when_foreign_keys_were_bypassed(sqlite_target: Target, datasets: DatasetPaths):
    ingest_files(datasets, [sqlite_target], 3)
    sqlite_target.execute("PRAGMA foreign_keys = OFF")
    sqlite_target.execute(
        "INSERT INTO title_crew (title_id, person_id, role) VALUES (?, ?, ?)",
        ("tt-missing", "nm9", "writer"),
    )
    sqlite_target.commit()
    sqlite_target.execute("PRAGMA foreign_keys = ON")
    errors = verify_target(sqlite_target, datasets, 3)
    assert any("crew" in error and "no matching title" in error for error in errors)
