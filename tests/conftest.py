from __future__ import annotations

from pathlib import Path

import pytest

from object_graph_performance_lab.config import DatasetPaths
from object_graph_performance_lab.db import Target, connect_sqlite
from object_graph_performance_lab.schema import ensure_schema

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def datasets() -> DatasetPaths:
    return DatasetPaths(
        basics=FIXTURES / "title.basics.tsv",
        ratings=FIXTURES / "title.ratings.tsv",
        crew=FIXTURES / "title.crew.tsv",
    )


@pytest.fixture
def sqlite_target(tmp_path: Path) -> Target:
    target = connect_sqlite(tmp_path / "imdb.sqlite")
    ensure_schema(target)
    try:
        yield target
    finally:
        target.close()
