"""Run equivalent read-only workloads against two engines and compare them.

The timed region is one statement plus fetching and materializing the rows.
Warmup runs, dataset counts, parity checks, and EXPLAIN output are not samples.
Measured iterations alternate which engine goes first. A failed or mismatched
iteration aborts the run; it is not dropped.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from object_graph_performance_lab.benchmarks.stats import median_ratio, summarize_ns
from object_graph_performance_lab.benchmarks.workloads import (
    Workload,
    describe_mismatch,
    normalize_rows,
)
from object_graph_performance_lab.db import Target
from object_graph_performance_lab.errors import LabError

EngineFactory = Callable[[], Target]

INITIAL_CONNECTION_NOTE = (
    "First connection plus SELECT 1 in this process, before warmup. "
    "Excluded from steady-state samples. Not a controlled Neon scale-to-zero cold start."
)


@dataclass
class Engine:
    name: str
    target: Target
    connect: EngineFactory


def _order(iteration: int, engines: list[Engine]) -> list[Engine]:
    if iteration % 2 == 0:
        return list(engines)
    return list(reversed(engines))


def _prepare_session(target: Target) -> None:
    """Keep later statements read-only. This is not part of a timed sample.

    The opening SELECT 1 starts a Postgres transaction, so that transaction is
    committed before autocommit is enabled.
    """
    if target.kind == "postgres":
        target.settle()
        target.conn.autocommit = True
        target.execute("SET default_transaction_read_only = on")
        return
    target.execute("PRAGMA query_only = ON")


def _timed_fetch(target: Target, sql: str, params: tuple) -> tuple[int, list[tuple]]:
    started = time.perf_counter_ns()
    rows = list(target.fetchall(sql, params))
    elapsed = time.perf_counter_ns() - started
    return elapsed, rows


def _require_band(workload: Workload, row_count: int) -> None:
    if workload.min_rows <= row_count <= workload.max_rows:
        return
    raise LabError(
        f"{workload.name} returned {row_count} rows, outside "
        f"{workload.min_rows}-{workload.max_rows}. "
        "The predicates are fixed for the 10,000-title prefix. "
        "Refusing to report a comparison for a different result size."
    )


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


def _explain(engine: Engine, workload: Workload) -> str:
    try:
        if engine.target.kind == "sqlite":
            statement = f"EXPLAIN QUERY PLAN {workload.sql}"
        else:
            statement = f"EXPLAIN {workload.sql}"
        rows = engine.target.fetchall(statement, workload.params)
    except Exception as exc:
        return f"explain failed: {exc}"
    return "\n".join(" | ".join(str(value) for value in row) for row in rows)


def _check_same(workload: Workload, left: list[tuple], right: list[tuple], where: str) -> None:
    mismatch = describe_mismatch(left, right)
    if mismatch != "rows match":
        raise LabError(
            f"{workload.name} results differ ({where}). {mismatch} "
            "No speedup is reported."
        )


def _run_query_workload(
    workload: Workload,
    engines: list[Engine],
    iterations: int,
    warmup: int,
) -> dict:
    expected: dict[str, list[tuple]] = {}
    for engine in engines:
        _elapsed, rows = _timed_fetch(engine.target, workload.sql, workload.params)
        normalized = normalize_rows(workload.columns, rows)
        _require_band(workload, len(normalized))
        expected[engine.name] = normalized
    _check_same(
        workload,
        expected[engines[0].name],
        expected[engines[1].name],
        "parity check before timing",
    )
    plans = {engine.name: _explain(engine, workload) for engine in engines}
    reference = expected[engines[0].name]

    for index in range(warmup):
        for engine in _order(index, engines):
            _elapsed, rows = _timed_fetch(engine.target, workload.sql, workload.params)
            normalized = normalize_rows(workload.columns, rows)
            _check_same(workload, reference, normalized, f"warmup {index} on {engine.name}")

    samples: dict[str, list[int]] = {engine.name: [] for engine in engines}
    for index in range(iterations):
        for engine in _order(index, engines):
            try:
                elapsed, rows = _timed_fetch(engine.target, workload.sql, workload.params)
            except Exception as exc:
                raise LabError(
                    f"{workload.name} failed on {engine.name} at measured iteration {index}. "
                    f"That iteration was not dropped. Error: {exc}"
                ) from exc
            normalized = normalize_rows(workload.columns, rows)
            _check_same(
                workload,
                reference,
                normalized,
                f"measured iteration {index} on {engine.name}",
            )
            samples[engine.name].append(elapsed)

    return _workload_report(workload, samples, len(reference), plans)


def _time_connect(factory: EngineFactory) -> tuple[int, Target]:
    started = time.perf_counter_ns()
    target = factory()
    row = target.fetchone("SELECT 1")
    elapsed = time.perf_counter_ns() - started
    if row is None or row[0] != 1:
        target.close()
        raise LabError("SELECT 1 did not succeed on a new connection")
    return elapsed, target


def _run_connection_workload(
    workload: Workload,
    engines: list[Engine],
    iterations: int,
    warmup: int,
) -> dict:
    samples: dict[str, list[int]] = {engine.name: [] for engine in engines}
    for index in range(warmup):
        for engine in _order(index, engines):
            _elapsed, target = _time_connect(engine.connect)
            target.close()
    for index in range(iterations):
        for engine in _order(index, engines):
            try:
                elapsed, target = _time_connect(engine.connect)
            except Exception as exc:
                raise LabError(
                    f"{workload.name} failed on {engine.name} at measured iteration {index}. "
                    f"That iteration was not dropped. Error: {exc}"
                ) from exc
            target.close()
            samples[engine.name].append(elapsed)
    return _workload_report(workload, samples, 1, {})


def _workload_report(
    workload: Workload,
    samples_ns: dict[str, list[int]],
    rows_returned: int,
    plans: dict[str, str],
) -> dict:
    engines_report: dict[str, dict] = {}
    for name, samples in samples_ns.items():
        summary = summarize_ns(samples)
        engines_report[name] = summary
    try:
        ratio = median_ratio(
            float(engines_report["postgres"]["median_ms"]),
            float(engines_report["sqlite"]["median_ms"]),
        )
    except ValueError as exc:
        raise LabError(f"{workload.name}: {exc}. No speedup is reported.") from exc
    return {
        "name": workload.name,
        "summary": workload.summary,
        "sql": workload.sql,
        "params": list(workload.params),
        "rows_returned": rows_returned,
        "parity_passed": True,
        "query_plans": plans,
        "engines": engines_report,
        "median_ratio_postgres_over_sqlite": ratio,
    }


def run_comparison(
    *,
    experiment: str,
    workloads: tuple[Workload, ...],
    engines: list[Engine],
    iterations: int,
    warmup: int,
    initial_connection: dict | None = None,
) -> dict:
    """Compare engines. ``engines`` must be sqlite then postgres."""
    if iterations < 2:
        raise LabError("iterations must be at least 2 so the standard deviation is defined")
    if warmup < 0:
        raise LabError("warmup must be at least 0")
    if [engine.name for engine in engines] != ["sqlite", "postgres"]:
        raise LabError("this comparison expects the sqlite engine, then the postgres engine")

    counts = {engine.name: _read_counts(engine.target) for engine in engines}
    if counts["sqlite"] != counts["postgres"]:
        raise LabError(
            "Dataset counts differ, so query timings would not be comparable. "
            f"sqlite={counts['sqlite']} postgres={counts['postgres']}"
        )

    workload_reports = []
    for workload in workloads:
        if workload.establishes_connection:
            workload_reports.append(
                _run_connection_workload(workload, engines, iterations, warmup)
            )
        else:
            workload_reports.append(
                _run_query_workload(workload, engines, iterations, warmup)
            )

    limitations = [
        "Small-query time includes client driver overhead. On Postgres it also includes network round trips, so those can dominate the server's execution time.",
        "Query planners, page caches, and hardware differ. The ratio is not only network latency.",
        "Neon compute size and SQLite's local CPU are not matched.",
        "The initial connection is not a controlled scale-to-zero cold start. It is excluded from steady-state samples.",
        "This is not a comparison against Rippling's legacy Python Object Graph.",
    ]
    if iterations < 30:
        limitations.append(
            "This run used fewer than 30 measured iterations. The documented procedure uses at least 30."
        )
    report = {
        "experiment": experiment,
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "dataset": counts["sqlite"],
        "iterations": iterations,
        "warmup": warmup,
        "parity_passed": True,
        "errors": [],
        "measurement": {
            "clock": "time.perf_counter_ns",
            "timed_region": "statement execution, fetch, and materialization into a Python list",
            "excluded": [
                "database initialization",
                "ingestion",
                "verification",
                "warmup iterations",
                "parity comparison",
                "EXPLAIN",
            ],
            "iteration_order": "even iterations run sqlite then postgres; odd iterations reverse that",
            "stdev": "sample standard deviation, divisor n - 1",
            "p95": "linear interpolation between closest ranks, rank = 0.95 * (n - 1)",
            "ratio": "postgres median_ms / sqlite median_ms",
            "dialect": (
                "The SQL text is the same. PostgreSQL placeholders are %s after "
                "translation from ?. is_adult is compared as 0/1. Postgres sessions "
                "used for timed queries are autocommit and read-only. No extra indexes."
            ),
        },
        "limitations": limitations,
        "workloads": workload_reports,
    }
    if initial_connection is not None:
        report["initial_connection"] = initial_connection
    return report


def open_measured_engine(name: str, factory: EngineFactory) -> tuple[Engine, float]:
    """Connect once. Return the engine and the initial connect+SELECT 1 latency in ms."""
    elapsed_ns, target = _time_connect(factory)
    try:
        _prepare_session(target)
    except Exception:
        target.close()
        raise
    return Engine(name=name, target=target, connect=factory), elapsed_ns / 1_000_000
