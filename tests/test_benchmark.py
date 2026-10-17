import csv
import json
import statistics
from pathlib import Path

import pytest

from object_graph_performance_lab.benchmarks.report import format_summary, write_csv, write_json
from object_graph_performance_lab.benchmarks.runner import (
    Engine,
    open_measured_engine,
    run_comparison,
)
from object_graph_performance_lab.benchmarks.stats import (
    median_ratio,
    percentile_linear,
    summarize_ns,
)
from object_graph_performance_lab.benchmarks.workloads import (
    ALL_WORKLOADS,
    LARGE_TYPE,
    PRIMARY_KEY_ID,
    RELATIONSHIP_TITLE_ID,
    SELECTIVE_YEAR,
    Workload,
    normalize_rows,
    select_workloads,
)
from object_graph_performance_lab.cli import main
from object_graph_performance_lab.config import DatasetPaths, sqlite_path
from object_graph_performance_lab.db import Target, connect_sqlite
from object_graph_performance_lab.errors import LabError
from object_graph_performance_lab.ingest import ingest_files

FIXTURES = Path(__file__).parent / "fixtures"
DATASETS = DatasetPaths(
    basics=FIXTURES / "title.basics.tsv",
    ratings=FIXTURES / "title.ratings.tsv",
    crew=FIXTURES / "title.crew.tsv",
)

FIXTURE_LOOKUP = Workload(
    name="primary_key_lookup",
    summary="Fixture primary key.",
    sql=(
        "SELECT title_id, name, type, release_year, runtime_minutes, is_adult "
        "FROM titles WHERE title_id = ?"
    ),
    params=("tt0000001",),
    columns=("title_id", "name", "type", "release_year", "runtime_minutes", "is_adult"),
    min_rows=1,
    max_rows=1,
)


def test_percentile_and_summary_match_hand_calculation():
    ordered = [0.0, 10.0, 20.0, 30.0]
    assert percentile_linear(ordered, 95) == pytest.approx(28.5)
    assert percentile_linear(ordered, 50) == pytest.approx(statistics.median(ordered))
    summary = summarize_ns([0, 10_000_000, 20_000_000, 30_000_000])
    assert summary["count"] == 4
    assert summary["min_ms"] == 0
    assert summary["max_ms"] == 30
    assert summary["median_ms"] == pytest.approx(15)
    assert summary["p95_ms"] == pytest.approx(28.5)
    assert summary["stdev_ms"] == pytest.approx(statistics.stdev([0, 10, 20, 30]))
    assert summary["samples_ms"] == [0, 10, 20, 30]


def test_summary_rejects_one_sample_and_zero_ratio():
    with pytest.raises(ValueError, match="two"):
        summarize_ns([100])
    with pytest.raises(ValueError, match="zero"):
        median_ratio(5, 0)


def test_adult_flag_normalizes_across_dialects():
    columns = ("title_id", "is_adult")
    sqlite_rows = [("tt1", 0), ("tt2", 1), ("tt3", None)]
    postgres_rows = [("tt1", False), ("tt2", True), ("tt3", None)]
    assert normalize_rows(columns, sqlite_rows) == normalize_rows(columns, postgres_rows)


def test_workload_selection_is_deterministic():
    assert select_workloads(None) == ALL_WORKLOADS
    assert select_workloads(None)[0].params == (PRIMARY_KEY_ID,)
    assert select_workloads(None)[1].params == (SELECTIVE_YEAR,)
    assert select_workloads(None)[2].params == (RELATIONSHIP_TITLE_ID,)
    assert select_workloads(None)[3].params == (LARGE_TYPE,)
    chosen = select_workloads(["larger_result_set", "primary_key_lookup"])
    assert [workload.name for workload in chosen] == [
        "larger_result_set",
        "primary_key_lookup",
    ]
    again = select_workloads(["larger_result_set", "primary_key_lookup"])
    assert [(item.sql, item.params) for item in chosen] == [
        (item.sql, item.params) for item in again
    ]
    with pytest.raises(LabError, match="Unknown workload"):
        select_workloads(["not-a-workload"])
    with pytest.raises(LabError, match="more than once"):
        select_workloads(["primary_key_lookup", "primary_key_lookup"])


def test_cli_rejects_bad_benchmark_arguments(capsys):
    assert main(["benchmark", "--experiment", "python-vs-sql"]) == 1
    assert "local-vs-remote" in capsys.readouterr().err
    assert main(["benchmark", "--experiment", "local-vs-remote", "--iterations", "1"]) == 1
    assert "at least 2" in capsys.readouterr().err
    assert main(["benchmark", "--experiment", "local-vs-remote", "--warmup", "-1"]) == 1
    assert "warmup" in capsys.readouterr().err
    assert (
        main(
            [
                "benchmark",
                "--experiment",
                "local-vs-remote",
                "--workload",
                "missing",
                "--iterations",
                "2",
            ]
        )
        == 1
    )
    assert "Unknown workload" in capsys.readouterr().err


def test_cli_benchmark_requires_postgres(monkeypatch, capsys):
    monkeypatch.setattr(
        "object_graph_performance_lab.cli.database_url",
        lambda: None,
    )
    code = main(
        ["benchmark", "--experiment", "local-vs-remote", "--iterations", "2", "--warmup", "0"]
    )
    assert code == 1
    assert "DATABASE_URL" in capsys.readouterr().err


def _engines(tmp_path: Path) -> tuple[Engine, Engine]:
    first_path = tmp_path / "sqlite.sqlite"
    second_path = tmp_path / "postgres.sqlite"
    first = connect_sqlite(first_path)
    second = connect_sqlite(second_path)
    ingest_files(DATASETS, [first, second], 0)
    first.close()
    second.close()
    sqlite_engine = Engine(
        name="sqlite",
        target=connect_sqlite(first_path, readonly=True),
        connect=lambda: connect_sqlite(first_path, readonly=True),
    )
    postgres_engine = Engine(
        name="postgres",
        target=connect_sqlite(second_path, readonly=True),
        connect=lambda: connect_sqlite(second_path, readonly=True),
    )
    return sqlite_engine, postgres_engine


def test_runner_checks_parity_and_serializes(tmp_path: Path):
    sqlite_engine, postgres_engine = _engines(tmp_path)
    try:
        report = run_comparison(
            experiment="local-vs-remote",
            workloads=(FIXTURE_LOOKUP,),
            engines=[sqlite_engine, postgres_engine],
            iterations=4,
            warmup=1,
        )
    finally:
        sqlite_engine.target.close()
        postgres_engine.target.close()
    assert report["parity_passed"] is True
    assert report["dataset"]["titles"] == 5
    workload = report["workloads"][0]
    assert workload["rows_returned"] == 1
    assert workload["engines"]["sqlite"]["count"] == 4
    assert len(workload["engines"]["postgres"]["samples_ms"]) == 4
    assert "fewer than 30" in " ".join(report["limitations"])
    text = format_summary(report)
    assert "primary_key_lookup" in text
    assert "PG/SQLite" in text
    json_path = tmp_path / "report.json"
    csv_path = tmp_path / "samples.csv"
    write_json(report, json_path)
    write_csv(report, csv_path)
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["workloads"][0]["parity_passed"] is True
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 8
    assert {row["engine"] for row in rows} == {"sqlite", "postgres"}
    assert rows[0]["workload"] == "primary_key_lookup"


def test_runner_fails_closed_when_rows_differ(tmp_path: Path):
    sqlite_engine, postgres_engine = _engines(tmp_path)

    class Tampered(Target):
        def fetchall(self, statement: str, params: tuple = ()):
            rows = [tuple(row) for row in super().fetchall(statement, params)]
            if rows and "EXPLAIN" not in statement.upper() and "COUNT" not in statement.upper():
                changed = list(rows[0])
                changed[1] = "tampered"
                rows[0] = tuple(changed)
            return rows

    tampered = Tampered(
        kind=postgres_engine.target.kind,
        conn=postgres_engine.target.conn,
        label=postgres_engine.target.label,
    )
    postgres_engine.target = tampered
    try:
        with pytest.raises(LabError, match="No speedup"):
            run_comparison(
                experiment="local-vs-remote",
                workloads=(FIXTURE_LOOKUP,),
                engines=[sqlite_engine, postgres_engine],
                iterations=2,
                warmup=0,
            )
    finally:
        sqlite_engine.target.close()
        postgres_engine.target.close()


def test_runner_does_not_drop_a_failed_iteration(tmp_path: Path):
    sqlite_engine, postgres_engine = _engines(tmp_path)

    class Flaky(Target):
        def __init__(self, inner: Target):
            super().__init__(kind=inner.kind, conn=inner.conn, label=inner.label)
            self.calls = 0

        def fetchall(self, statement: str, params: tuple = ()):
            if "EXPLAIN" not in statement.upper():
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("network down")
            return super().fetchall(statement, params)

    postgres_engine.target = Flaky(postgres_engine.target)
    try:
        with pytest.raises(LabError, match="not dropped"):
            run_comparison(
                experiment="local-vs-remote",
                workloads=(FIXTURE_LOOKUP,),
                engines=[sqlite_engine, postgres_engine],
                iterations=2,
                warmup=0,
            )
    finally:
        sqlite_engine.target.close()
        postgres_engine.target.close()


def test_sqlite_session_stays_read_only(tmp_path: Path):
    path = tmp_path / "imdb.sqlite"
    target = connect_sqlite(path)
    ingest_files(DATASETS, [target], 0)
    target.close()
    engine, elapsed_ms = open_measured_engine(
        "sqlite",
        lambda: connect_sqlite(path, readonly=True),
    )
    try:
        assert elapsed_ms >= 0
        with pytest.raises(Exception):
            engine.target.execute(
                "INSERT INTO titles (title_id) VALUES (?)",
                ("tt-new",),
            )
    finally:
        engine.target.close()


def test_loaded_prefix_matches_workload_bands():
    path = sqlite_path()
    if not path.is_file():
        pytest.skip("local IMDb sqlite is not loaded")
    target = connect_sqlite(path, readonly=True)
    try:
        for workload in select_workloads(None):
            if workload.establishes_connection:
                row = target.fetchone(workload.sql)
                assert row == (1,)
                continue
            rows = target.fetchall(workload.sql, workload.params)
            assert workload.min_rows <= len(rows) <= workload.max_rows
            if workload.name == "primary_key_lookup":
                assert rows[0][0] == "tt0000001"
                assert rows[0][1] == "Carmencita"
    finally:
        target.close()
