"""Warm cached Python skeleton versus warm SQLite query pushdown.

Skeleton construction and SQLite session setup run before warmup. A measured
sample is one complete request: filtering plus result construction. Warmup
iterations and parity checks stay outside the samples. A mismatch or a failed
iteration aborts the run.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from object_graph_performance_lab.benchmarks.graph_query import (
    GraphResult,
    explain_plans,
    query_python,
    query_sqlite,
    select_scoped_ids,
)
from object_graph_performance_lab.benchmarks.graph_workloads import (
    INDEX_NOTE,
    PREFIX_TITLE_COUNT,
    GraphWorkload,
)
from object_graph_performance_lab.benchmarks.skeleton import (
    Skeleton,
    SkeletonLoad,
    TitleRecord,
    load_skeleton,
)
from object_graph_performance_lab.benchmarks.stats import median_ratio, summarize_ns
from object_graph_performance_lab.db import Target
from object_graph_performance_lab.errors import LabError


def prepare_sqlite(target: Target) -> None:
    """Read-only session. This is outside the timed request samples."""
    target.execute("PRAGMA query_only = ON")


def run_cached_experiment(
    *,
    experiment: str,
    workloads: tuple[GraphWorkload, ...],
    target: Target,
    iterations: int,
    warmup: int,
    skeleton_load: SkeletonLoad | None = None,
    scoped_ids: tuple[str, ...] | None = None,
) -> dict:
    if iterations < 2:
        raise LabError("iterations must be at least 2 so the standard deviation is defined")
    if warmup < 0:
        raise LabError("warmup must be at least 0")
    if target.kind != "sqlite":
        raise LabError("cached-python-vs-sqlite reads the local SQLite artifact")

    if skeleton_load is None:
        skeleton_load = load_skeleton(target)
    skeleton = skeleton_load.skeleton
    counts = _read_counts(target)
    _require_same_counts(skeleton, counts)

    needs_scope = any(workload.kind == "scoped" for workload in workloads)
    if needs_scope and scoped_ids is None:
        scoped_ids = select_scoped_ids(target)
    if needs_scope and not scoped_ids:
        raise LabError("scoped_graph found no title ids")

    workload_reports = [
        _run_workload(
            workload,
            skeleton,
            target,
            iterations=iterations,
            warmup=warmup,
            scoped_ids=scoped_ids,
        )
        for workload in workloads
    ]
    limitations = [
        "Both sides read the same local SQLite artifact. The timed samples do not include PostgreSQL.",
        "Python requests reuse skeleton objects. SQLite requests construct a new record for each match.",
        "The Python title_id dictionary serves scoped lookups. Contextual filters scan the cached title tuple. SQLite can use the Phase 1 indexes.",
        "Skeleton initialization is reported separately and is not compared with building the SQLite file.",
        "Initialization time includes tracemalloc overhead. Traced bytes are Python allocations, and RSS is the whole process.",
        "Loading the skeleton warms the SQLite page cache before warmup.",
        "SQLite rows fetched are the title and crew rows returned to Python. The engine may visit additional index entries; the query plan records the access path.",
        "This 10,000-title prefix is not a large metadata catalog. A bigger skeleton can change which side wins.",
        "This is an independent reconstruction on public IMDb data. It is not Rippling's Object Graph and it is not a historical measurement.",
    ]
    if iterations < 30:
        limitations.append(
            "This run used fewer than 30 measured iterations. The documented procedure uses at least 30."
        )
    return {
        "experiment": experiment,
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "dataset": counts,
        "iterations": iterations,
        "warmup": warmup,
        "parity_passed": True,
        "errors": [],
        "skeleton": {
            "initialization_ms": skeleton_load.initialization_ns / 1_000_000,
            "traced_peak_bytes": skeleton_load.traced_peak_bytes,
            "traced_retained_bytes": skeleton_load.traced_retained_bytes,
            "rss_before_bytes": skeleton_load.rss_before_bytes,
            "rss_after_bytes": skeleton_load.rss_after_bytes,
            "title_count": skeleton.title_count,
            "rating_count": skeleton.rating_count,
            "crew_count": skeleton.crew_count,
            "index": INDEX_NOTE,
        },
        "scoped_title_ids": list(scoped_ids) if scoped_ids is not None else [],
        "measurement": {
            "clock": "time.perf_counter_ns",
            "timed_region": (
                "one request: Python filtering or SQLite execution, fetch, and "
                "TitleRecord construction"
            ),
            "excluded": [
                "skeleton initialization",
                "SQLite connection setup",
                "scoped id selection",
                "warmup iterations",
                "parity comparison",
                "EXPLAIN QUERY PLAN",
            ],
            "iteration_order": (
                "even iterations run cached_python then sqlite; odd iterations reverse that"
            ),
            "stdev": "sample standard deviation, divisor n - 1",
            "p95": "linear interpolation between closest ranks, rank = 0.95 * (n - 1)",
            "ratio": "cached_python median_ms / sqlite median_ms",
            "output": "both sides return TitleRecord values with the same fields and crew order",
        },
        "limitations": limitations,
        "workloads": workload_reports,
    }


def _run_workload(
    workload: GraphWorkload,
    skeleton: Skeleton,
    target: Target,
    *,
    iterations: int,
    warmup: int,
    scoped_ids: tuple[str, ...] | None,
) -> dict:
    python_reference = _call_python(skeleton, workload, scoped_ids)
    sqlite_reference = _call_sqlite(target, workload, scoped_ids)
    _require_expected(workload, python_reference)
    _check_same(
        workload,
        python_reference.records,
        sqlite_reference.records,
        "parity check before timing",
    )
    _check_accounting(workload, python_reference, sqlite_reference)
    plans = explain_plans(target, workload.kind, workload.filt, _ids_for(workload, scoped_ids))

    for index in range(warmup):
        for engine in _order(index):
            result = _dispatch(engine, skeleton, target, workload, scoped_ids)
            _check_same(
                workload,
                python_reference.records,
                result.records,
                f"warmup {index} on {engine}",
            )

    samples: dict[str, list[int]] = {"cached_python": [], "sqlite": []}
    for index in range(iterations):
        for engine in _order(index):
            try:
                started = time.perf_counter_ns()
                result = _dispatch(engine, skeleton, target, workload, scoped_ids)
                elapsed = time.perf_counter_ns() - started
            except LabError:
                raise
            except Exception as exc:
                raise LabError(
                    f"{workload.name} failed on {engine} at measured iteration {index}. "
                    f"That iteration was not dropped. Error: {exc}"
                ) from exc
            _check_same(
                workload,
                python_reference.records,
                result.records,
                f"measured iteration {index} on {engine}",
            )
            if (
                result.records_examined != _reference_for(engine, python_reference, sqlite_reference).records_examined
                or result.objects_materialized
                != _reference_for(engine, python_reference, sqlite_reference).objects_materialized
            ):
                raise LabError(
                    f"{workload.name} accounting changed on {engine} at measured iteration {index}. "
                    "No speedup is reported."
                )
            samples[engine].append(elapsed)

    engines_report = {
        name: summarize_ns(values) for name, values in samples.items()
    }
    try:
        ratio = median_ratio(
            float(engines_report["cached_python"]["median_ms"]),
            float(engines_report["sqlite"]["median_ms"]),
        )
    except ValueError as exc:
        raise LabError(f"{workload.name}: {exc}. No speedup is reported.") from exc

    returned = len(python_reference.records)
    title_count = skeleton.title_count
    return {
        "name": workload.name,
        "summary": workload.summary,
        "kind": workload.kind,
        "selectivity_label": workload.selectivity_label,
        "selectivity": None if title_count == 0 else returned / title_count,
        "rows_returned": returned,
        "python_records_examined": python_reference.records_examined,
        "python_objects_materialized": python_reference.objects_materialized,
        "python_access": python_reference.access,
        "sqlite_records_fetched": sqlite_reference.records_examined,
        "sqlite_title_rows_fetched": sqlite_reference.title_rows_fetched,
        "sqlite_crew_rows_fetched": sqlite_reference.crew_rows_fetched,
        "sqlite_objects_materialized": sqlite_reference.objects_materialized,
        "parity_passed": True,
        "query_plans": plans,
        "engines": engines_report,
        "median_ratio_python_over_sqlite": ratio,
    }


def _dispatch(
    engine: str,
    skeleton: Skeleton,
    target: Target,
    workload: GraphWorkload,
    scoped_ids: tuple[str, ...] | None,
) -> GraphResult:
    if engine == "cached_python":
        return _call_python(skeleton, workload, scoped_ids)
    if engine == "sqlite":
        return _call_sqlite(target, workload, scoped_ids)
    raise LabError(f"unknown engine {engine}")


def _call_python(
    skeleton: Skeleton,
    workload: GraphWorkload,
    scoped_ids: tuple[str, ...] | None,
) -> GraphResult:
    return query_python(
        skeleton.titles,
        skeleton.by_id,
        kind=workload.kind,
        filt=workload.filt,
        scoped_ids=_ids_for(workload, scoped_ids),
    )


def _call_sqlite(
    target: Target,
    workload: GraphWorkload,
    scoped_ids: tuple[str, ...] | None,
) -> GraphResult:
    return query_sqlite(
        target,
        kind=workload.kind,
        filt=workload.filt,
        scoped_ids=_ids_for(workload, scoped_ids),
    )


def _ids_for(workload: GraphWorkload, scoped_ids: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if workload.kind == "scoped":
        return scoped_ids
    return None


def _order(iteration: int) -> tuple[str, str]:
    if iteration % 2 == 0:
        return ("cached_python", "sqlite")
    return ("sqlite", "cached_python")


def _reference_for(
    engine: str,
    python_reference: GraphResult,
    sqlite_reference: GraphResult,
) -> GraphResult:
    if engine == "cached_python":
        return python_reference
    return sqlite_reference


def _require_expected(workload: GraphWorkload, result: GraphResult) -> None:
    if workload.expected_rows is None:
        return
    if len(result.records) == workload.expected_rows:
        return
    raise LabError(
        f"{workload.name} returned {len(result.records)} rows, expected "
        f"{workload.expected_rows}. The predicates are fixed for the "
        f"{PREFIX_TITLE_COUNT}-title prefix. "
        "Refusing to report a comparison for a different result size."
    )


def _check_same(
    workload: GraphWorkload,
    left: tuple[TitleRecord, ...],
    right: tuple[TitleRecord, ...],
    where: str,
) -> None:
    mismatch = describe_record_mismatch(left, right)
    if mismatch != "rows match":
        raise LabError(
            f"{workload.name} results differ ({where}). {mismatch} "
            "No speedup is reported."
        )


def _check_accounting(
    workload: GraphWorkload,
    python_result: GraphResult,
    sqlite_result: GraphResult,
) -> None:
    if python_result.objects_materialized != 0:
        raise LabError(
            f"{workload.name} materialized {python_result.objects_materialized} "
            "Python records. The cached path should reuse the skeleton."
        )
    if sqlite_result.objects_materialized != len(sqlite_result.records):
        raise LabError(
            f"{workload.name} SQLite object count does not match the records returned."
        )
    if workload.kind == "full" and python_result.access != "reused_sequence":
        raise LabError(f"{workload.name} did not reuse the cached title sequence.")
    if workload.kind == "scoped" and python_result.access != "dict_probe":
        raise LabError(f"{workload.name} did not use the title_id dictionary.")
    if workload.kind == "filter" and python_result.access != "linear_scan":
        raise LabError(f"{workload.name} did not scan the cached titles.")


def describe_record_mismatch(
    left: tuple[TitleRecord, ...],
    right: tuple[TitleRecord, ...],
) -> str:
    if left == right:
        return "rows match"
    if len(left) != len(right):
        return f"{len(left)} rows versus {len(right)} rows"
    fields = (
        "title_id",
        "name",
        "type",
        "release_year",
        "runtime_minutes",
        "is_adult",
        "average_rating",
        "vote_count",
        "has_rating",
        "crew",
    )
    for index, (first, second) in enumerate(zip(left, right)):
        if first == second:
            continue
        differed = [name for name in fields if getattr(first, name) != getattr(second, name)]
        return (
            f"row {index} title_id {first.title_id} versus {second.title_id} "
            f"differs in {', '.join(differed)}"
        )
    return "rows differ"


def _read_counts(target: Target) -> dict[str, int | None]:
    titles = int(target.fetchone("SELECT COUNT(*) FROM titles")[0])
    ratings = int(target.fetchone("SELECT COUNT(*) FROM ratings")[0])
    crew = int(target.fetchone("SELECT COUNT(*) FROM title_crew")[0])
    limit_row = target.fetchone(
        "SELECT state_value FROM ingest_state WHERE state_key = ?",
        ("subset_limit",),
    )
    subset_limit = None if limit_row is None else int(limit_row[0])
    return {
        "titles": titles,
        "ratings": ratings,
        "crew_relationships": crew,
        "subset_limit": subset_limit,
    }


def _require_same_counts(skeleton: Skeleton, counts: dict[str, int | None]) -> None:
    if skeleton.title_count != counts["titles"] or skeleton.rating_count != counts["ratings"]:
        raise LabError(
            "The skeleton counts do not match SQLite. "
            f"skeleton titles={skeleton.title_count} ratings={skeleton.rating_count}; "
            f"sqlite titles={counts['titles']} ratings={counts['ratings']}"
        )
    if skeleton.crew_count != counts["crew_relationships"]:
        raise LabError(
            "The skeleton crew count does not match SQLite. "
            f"skeleton={skeleton.crew_count} sqlite={counts['crew_relationships']}"
        )
