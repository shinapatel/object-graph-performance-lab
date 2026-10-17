from object_graph_performance_lab.config import DatasetPaths
from object_graph_performance_lab.db import Target
from object_graph_performance_lab.ingest import ingest_files
from object_graph_performance_lab.queries import titles_by_year_and_type


def test_filter_titles_by_year_and_type(sqlite_target: Target, datasets: DatasetPaths):
    ingest_files(datasets, [sqlite_target], 3)
    rows = titles_by_year_and_type(sqlite_target, 1894, "short")
    assert rows == [("tt0000009", "Ninth", "short", 1894)]
    assert titles_by_year_and_type(sqlite_target, 1892, "movie") == []
