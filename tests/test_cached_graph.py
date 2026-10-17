"""Experiment 2: cached Python skeleton versus SQLite pushdown."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from object_graph_performance_lab.benchmarks.cached_experiment import (
    describe_record_mismatch,
    prepare_sqlite,
    run_cached_experiment,
)
from object_graph_performance_lab.benchmarks.graph_query import (
    Filter,
    query_python,
    select_scoped_ids,
)
from object_graph_performance_lab.benchmarks.graph_workloads import (
    GraphWorkload,
    select_graph_workloads,
)
from object_graph_performance_lab.benchmarks.report import format_cached_summary, write_csv, write_json
from object_graph_performance_lab.benchmarks.skeleton import Skeleton, SkeletonLoad, TitleRecord, load_skeleton
from object_graph_performance_lab.cli import main
from object_graph_performance_lab.config import DatasetPaths
from object_graph_performance_lab.db import connect_sqlite
from object_graph_performance_lab.errors import LabError
from object_graph_performance_lab.ingest import ingest_files
from object_graph_performance_lab.schema import ensure_schema

FIXTURES = Path(__file__).parent / "fixtures"
DATASETS = DatasetPaths(
    basics=FIXTURES / "title.basics.tsv",
    ratings=FIXTURES / "title.ratings.tsv",
    crew=FIXTURES / "title.crew.tsv",
)


def _open_fixture(tmp_path: Path):
    path = tmp_path / "imdb.sqlite"
    target = connect_sqlite(path)
    ingest_files(DATASETS, [target], 0)
    target.close()
    target = connect_sqlite(path, readonly=True)
    prepare_sqlite(target)
    return target


def _workload(name: str, kind: str, filt: Filter | None = None) -> GraphWorkload:
    return GraphWorkload(
        name=name,
        summary=name,
        kind=kind,
        filt=filt or Filter(),
        expected_rows=None,
        selectivity_label="fixture",
    )


def test_skeleton_keeps_null_rating_row_and_ordered_crew(tmp_path: Path):
    target = _open_fixture(tmp_path)
    try:
        loaded = load_skeleton(target)
    finally:
        target.close()
    skeleton = loaded.skeleton
    assert skeleton.title_count == 5
    assert skeleton.rating_count == 5
    assert skeleton.crew_count == 9
    assert loaded.initialization_ns > 0
    assert loaded.traced_peak_bytes >= loaded.traced_retained_bytes > 0
    null_rating = skeleton.by_id["tt0000003"]
    assert null_rating.has_rating is True
    assert null_rating.average_rating is None
    assert null_rating.vote_count is None
    assert null_rating.release_year is None
    assert null_rating.is_adult is None
    assert skeleton.by_id["tt0000001"].crew == (
        ("nm0000002", "director"),
        ("nm0000002", "writer"),
        ("nm0000003", "writer"),
    )
    assert skeleton.by_id["tt0000009"].crew == (("nm0000001", "director"),)
    assert tuple(record.title_id for record in skeleton.titles) == (
        "tt0000001",
        "tt0000002",
        "tt0000003",
        "tt0000008",
        "tt0000009",
    )


def test_python_reuses_records_and_indexes_ids(tmp_path: Path):
    target = _open_fixture(tmp_path)
    try:
        skeleton = load_skeleton(target).skeleton
    finally:
        target.close()
    full = query_python(
        skeleton.titles,
        skeleton.by_id,
        kind="full",
        filt=Filter(),
        scoped_ids=None,
    )
    assert full.records is skeleton.titles
    assert full.records_examined == 0
    assert full.objects_materialized == 0
    assert full.access == "reused_sequence"

    scoped = query_python(
        skeleton.titles,
        skeleton.by_id,
        kind="scoped",
        filt=Filter(),
        scoped_ids=("tt0000008", "tt0000001", "tt-missing"),
    )
    assert scoped.records_examined == 3
    assert scoped.objects_materialized == 0
    assert scoped.access == "dict_probe"
    assert tuple(record.title_id for record in scoped.records) == ("tt0000001", "tt0000008")
    assert scoped.records[0] is skeleton.by_id["tt0000001"]

    filtered = query_python(
        skeleton.titles,
        skeleton.by_id,
        kind="filter",
        filt=Filter(release_year_at_least=1894),
        scoped_ids=None,
    )
    assert filtered.records_examined == 5
    assert filtered.objects_materialized == 0
    assert filtered.access == "linear_scan"
    assert tuple(record.title_id for record in filtered.records) == (
        "tt0000002",
        "tt0000008",
        "tt0000009",
    )
    assert filtered.records[0] is skeleton.by_id["tt0000002"]


def test_python_and_sqlite_match_on_the_fixture(tmp_path: Path):
    target = _open_fixture(tmp_path)
    workloads = (
        _workload("full_graph", "full"),
        _workload("scoped_graph", "scoped"),
        _workload("filter_year_1894", "filter", Filter(release_year=1894)),
        _workload("filter_type_short", "filter", Filter(title_type="short")),
        _workload(
            "filter_rated_since_1894",
            "filter",
            Filter(release_year_at_least=1894, require_rating=True),
        ),
    )
    try:
        report = run_cached_experiment(
            experiment="cached-python-vs-sqlite",
            workloads=workloads,
            target=target,
            iterations=4,
            warmup=1,
            scoped_ids=("tt0000009", "tt0000002"),
        )
    finally:
        target.close()
    assert report["parity_passed"] is True
    assert report["dataset"]["titles"] == 5
    by_name = {workload["name"]: workload for workload in report["workloads"]}
    assert by_name["full_graph"]["rows_returned"] == 5
    assert by_name["full_graph"]["python_access"] == "reused_sequence"
    assert by_name["full_graph"]["python_objects_materialized"] == 0
    assert by_name["full_graph"]["sqlite_objects_materialized"] == 5
    assert by_name["scoped_graph"]["rows_returned"] == 2
    assert by_name["scoped_graph"]["python_records_examined"] == 2
    assert by_name["scoped_graph"]["python_access"] == "dict_probe"
    assert by_name["filter_year_1894"]["rows_returned"] == 2
    assert by_name["filter_year_1894"]["python_records_examined"] == 5
    assert by_name["filter_type_short"]["rows_returned"] == 3
    assert by_name["filter_rated_since_1894"]["rows_returned"] == 3
    assert by_name["full_graph"]["engines"]["cached_python"]["count"] == 4
    assert by_name["full_graph"]["median_ratio_python_over_sqlite"] > 0
    text = format_cached_summary(report)
    assert "Python/SQLite" in text
    assert "Parity validation passed: True" in text


def test_runner_serializes_samples(tmp_path: Path):
    target = _open_fixture(tmp_path)
    try:
        report = run_cached_experiment(
            experiment="cached-python-vs-sqlite",
            workloads=(_workload("full_graph", "full"),),
            target=target,
            iterations=2,
            warmup=1,
        )
    finally:
        target.close()
    json_path = tmp_path / "report.json"
    csv_path = tmp_path / "report.csv"
    write_json(report, json_path)
    write_csv(report, csv_path)
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["skeleton"]["title_count"] == 5
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4
    assert {row["engine"] for row in rows} == {"cached_python", "sqlite"}
    assert all(row["parity_passed"] == "True" for row in rows)


def test_prefix_ids_on_ten_thousand_rows_are_fifty(tmp_path: Path):
    target = connect_sqlite(tmp_path / "ids.sqlite")
    try:
        ensure_schema(target)
        target.write_many(
            "INSERT INTO titles (title_id, name, type, release_year, runtime_minutes, is_adult) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(f"tt{index:07d}", "N", "movie", 1900, None, 0) for index in range(10000)],
        )
        ids = select_scoped_ids(target)
    finally:
        target.close()
    assert len(ids) == 50
    assert ids[0] == "tt0000000"
    assert ids[1] == "tt0000200"
    assert ids[-1] == "tt0009800"


def test_null_year_and_null_average_match_across_engines(tmp_path: Path):
    target = connect_sqlite(tmp_path / "edges.sqlite")
    try:
        ensure_schema(target)
        target.write_many(
            "INSERT INTO titles (title_id, name, type, release_year, runtime_minutes, is_adult) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("ttA", "Rated", "movie", 1916, 10, 0),
                ("ttB", "No rating", "movie", 1916, 10, 0),
                ("ttC", "Null year", "movie", None, None, None),
            ],
        )
        target.write_many(
            "INSERT INTO ratings (title_id, average_rating, vote_count) VALUES (?, ?, ?)",
            [("ttA", None, None), ("ttC", 1.5, 4)],
        )
        target.write_many(
            "INSERT INTO title_crew (title_id, person_id, role) VALUES (?, ?, ?)",
            [("ttA", "nm1", "writer"), ("ttA", "nm1", "director")],
        )
        prepare_sqlite(target)
        loaded = load_skeleton(target)
        rated = loaded.skeleton.by_id["ttA"]
        assert rated.has_rating is True
        assert rated.average_rating is None
        assert rated.crew == (("nm1", "director"), ("nm1", "writer"))
        assert loaded.skeleton.by_id["ttB"].has_rating is False
        report = run_cached_experiment(
            experiment="cached-python-vs-sqlite",
            workloads=(
                _workload(
                    "filter_rated_since_1916",
                    "filter",
                    Filter(release_year_at_least=1916, require_rating=True),
                ),
            ),
            target=target,
            iterations=2,
            warmup=0,
            skeleton_load=loaded,
        )
    finally:
        target.close()
    assert report["workloads"][0]["rows_returned"] == 1
    assert report["parity_passed"] is True


def test_mismatch_reports_no_speedup(tmp_path: Path):
    target = _open_fixture(tmp_path)
    try:
        loaded = load_skeleton(target)
        original = loaded.skeleton.titles[0]
        changed = TitleRecord(
            title_id=original.title_id,
            name="Changed",
            type=original.type,
            release_year=original.release_year,
            runtime_minutes=original.runtime_minutes,
            is_adult=original.is_adult,
            average_rating=original.average_rating,
            vote_count=original.vote_count,
            has_rating=original.has_rating,
            crew=original.crew,
        )
        titles = (changed,) + loaded.skeleton.titles[1:]
        by_id = dict(loaded.skeleton.by_id)
        by_id[changed.title_id] = changed
        bad = Skeleton(
            titles=titles,
            by_id=by_id,
            title_count=loaded.skeleton.title_count,
            rating_count=loaded.skeleton.rating_count,
            crew_count=loaded.skeleton.crew_count,
        )
        bad_load = SkeletonLoad(
            skeleton=bad,
            initialization_ns=loaded.initialization_ns,
            traced_peak_bytes=loaded.traced_peak_bytes,
            traced_retained_bytes=loaded.traced_retained_bytes,
            rss_before_bytes=loaded.rss_before_bytes,
            rss_after_bytes=loaded.rss_after_bytes,
        )
        with pytest.raises(LabError, match="No speedup") as caught:
            run_cached_experiment(
                experiment="cached-python-vs-sqlite",
                workloads=(_workload("full_graph", "full"),),
                target=target,
                iterations=2,
                warmup=0,
                skeleton_load=bad_load,
            )
    finally:
        target.close()
    assert "name" in str(caught.value)
    assert describe_record_mismatch((changed,), (original,)) != "rows match"


def test_failed_iteration_is_not_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = _open_fixture(tmp_path)
    calls = {"count": 0}
    real = query_sqlite_identity()

    def flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] > 1:
            raise RuntimeError("read failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(
        "object_graph_performance_lab.benchmarks.cached_experiment.query_sqlite",
        flaky,
    )
    try:
        with pytest.raises(LabError, match="not dropped"):
            run_cached_experiment(
                experiment="cached-python-vs-sqlite",
                workloads=(_workload("full_graph", "full"),),
                target=target,
                iterations=2,
                warmup=0,
            )
    finally:
        target.close()


def query_sqlite_identity():
    from object_graph_performance_lab.benchmarks.cached_experiment import query_sqlite

    return query_sqlite


def test_cli_cached_experiment_skips_postgres(monkeypatch, tmp_path: Path, capsys):
    path = tmp_path / "imdb.sqlite"
    target = connect_sqlite(path)
    ingest_files(DATASETS, [target], 0)
    target.close()
    monkeypatch.setattr("object_graph_performance_lab.cli.sqlite_path", lambda: path)
    monkeypatch.setattr(
        "object_graph_performance_lab.cli.database_url",
        lambda: (_ for _ in ()).throw(AssertionError("postgres was consulted")),
    )
    monkeypatch.setattr(
        "object_graph_performance_lab.benchmarks.graph_workloads.GRAPH_WORKLOADS",
        (_workload("full_graph", "full"),),
    )
    json_path = tmp_path / "out.json"
    csv_path = tmp_path / "out.csv"
    code = main(
        [
            "benchmark",
            "--experiment",
            "cached-python-vs-sqlite",
            "--iterations",
            "2",
            "--warmup",
            "1",
            "--json",
            str(json_path),
            "--csv",
            str(csv_path),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "DATABASE_URL" not in captured.err
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["parity_passed"] is True
    assert loaded["workloads"][0]["rows_returned"] == 5


def test_cli_refuses_default_workloads_on_a_small_database(monkeypatch, tmp_path: Path, capsys):
    path = tmp_path / "imdb.sqlite"
    target = connect_sqlite(path)
    ingest_files(DATASETS, [target], 0)
    target.close()
    monkeypatch.setattr("object_graph_performance_lab.cli.sqlite_path", lambda: path)
    code = main(
        [
            "benchmark",
            "--experiment",
            "cached-python-vs-sqlite",
            "--iterations",
            "2",
            "--warmup",
            "0",
        ]
    )
    assert code == 1
    assert "10000" in capsys.readouterr().err


def test_cli_rejects_unknown_cached_workload(capsys):
    code = main(
        [
            "benchmark",
            "--experiment",
            "cached-python-vs-sqlite",
            "--workload",
            "missing",
            "--iterations",
            "2",
        ]
    )
    assert code == 1
    assert "Unknown workload" in capsys.readouterr().err


def test_cli_does_not_write_when_the_run_fails(monkeypatch, tmp_path: Path):
    path = tmp_path / "imdb.sqlite"
    target = connect_sqlite(path)
    ingest_files(DATASETS, [target], 0)
    target.close()
    monkeypatch.setattr("object_graph_performance_lab.cli.sqlite_path", lambda: path)
    monkeypatch.setattr(
        "object_graph_performance_lab.benchmarks.graph_workloads.GRAPH_WORKLOADS",
        (_workload("full_graph", "full"),),
    )

    def explode(**_kwargs):
        raise LabError("results differ. No speedup is reported.")

    monkeypatch.setattr(
        "object_graph_performance_lab.cli.run_cached_experiment",
        explode,
    )
    json_path = tmp_path / "out.json"
    csv_path = tmp_path / "out.csv"
    code = main(
        [
            "benchmark",
            "--experiment",
            "cached-python-vs-sqlite",
            "--iterations",
            "2",
            "--warmup",
            "0",
            "--json",
            str(json_path),
            "--csv",
            str(csv_path),
        ]
    )
    assert code == 1
    assert not json_path.exists()
    assert not csv_path.exists()


def test_catalog_names_are_stable():
    names = [workload.name for workload in select_graph_workloads(None)]
    assert names == [
        "full_graph",
        "scoped_graph",
        "filter_year_1900",
        "filter_rated_since_1916",
        "filter_type_movie",
    ]
    chosen = select_graph_workloads(["filter_type_movie", "full_graph"])
    assert [workload.name for workload in chosen] == ["full_graph", "filter_type_movie"]
    with pytest.raises(LabError, match="more than once"):
        select_graph_workloads(["full_graph", "full_graph"])
