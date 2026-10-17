import os
from pathlib import Path

import pytest

from object_graph_performance_lab.config import DatasetPaths
from object_graph_performance_lab.db import connect_postgres, probe
from object_graph_performance_lab.ingest import ingest_files
from object_graph_performance_lab.schema import ensure_schema
from object_graph_performance_lab.verify import verify_target

pytestmark = pytest.mark.integration

_FIXTURES = Path(__file__).parent / "fixtures"
FIXTURES = DatasetPaths(
    basics=_FIXTURES / "title.basics.tsv",
    ratings=_FIXTURES / "title.ratings.tsv",
    crew=_FIXTURES / "title.crew.tsv",
)


@pytest.mark.integration
def test_postgres_fixture_roundtrip():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        pytest.skip("DATABASE_URL not set")
    target = connect_postgres(url)
    schema = "ogpl_phase1_test"
    try:
        assert "SELECT 1 ok" in probe(target)
        target.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        target.execute(f"CREATE SCHEMA {schema}")
        target.execute(f"SET search_path TO {schema}")
        target.commit()
        ensure_schema(target)
        columns = [
            row[0]
            for row in target.fetchall(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = ? AND table_name = 'titles'
                ORDER BY ordinal_position
                """,
                (schema,),
            )
        ]
        assert columns == [
            "title_id",
            "name",
            "type",
            "release_year",
            "runtime_minutes",
            "is_adult",
        ]
        ingest_files(FIXTURES, [target], 3)
        assert verify_target(target, FIXTURES, 3) == []
        ingest_files(FIXTURES, [target], 3)
        assert target.fetchone("SELECT COUNT(*) FROM titles")[0] == 3
    finally:
        target.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        target.commit()
        target.close()
