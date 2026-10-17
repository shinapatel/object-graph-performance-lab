"""JSON and CSV writers for one benchmark report. No credentials are included."""

from __future__ import annotations

import csv
import json
from pathlib import Path


def write_json(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def write_csv(report: dict, path: Path) -> None:
    """One row per measured iteration. Warmup iterations are not included."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
        "experiment",
        "timestamp",
        "workload",
        "engine",
        "iteration",
        "latency_ms",
        "rows_returned",
        "parity_passed",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for workload in report["workloads"]:
            for engine_name, engine in workload["engines"].items():
                for iteration, latency_ms in enumerate(engine["samples_ms"]):
                    writer.writerow(
                        {
                            "experiment": report["experiment"],
                            "timestamp": report["timestamp"],
                            "workload": workload["name"],
                            "engine": engine_name,
                            "iteration": iteration,
                            "latency_ms": latency_ms,
                            "rows_returned": workload["rows_returned"],
                            "parity_passed": workload["parity_passed"],
                        }
                    )


def format_summary(report: dict) -> str:
    """Terminal table. The ratio is Postgres median divided by SQLite median."""
    headers = (
        "Workload",
        "SQLite median ms",
        "Postgres median ms",
        "PG/SQLite",
        "Rows",
    )
    body: list[tuple[str, str, str, str, str]] = []
    for workload in report["workloads"]:
        if workload["name"] == "connection_establishment":
            continue
        sqlite_median = workload["engines"]["sqlite"]["median_ms"]
        postgres_median = workload["engines"]["postgres"]["median_ms"]
        body.append(
            (
                workload["name"],
                f"{sqlite_median:.3f}",
                f"{postgres_median:.3f}",
                f"{workload['median_ratio_postgres_over_sqlite']:.2f}",
                str(workload["rows_returned"]),
            )
        )
    if not body:
        lines = [
            "No query workloads in this run.",
            "PG/SQLite is the Postgres median divided by the SQLite median.",
        ]
        _append_connection_notes(lines, report)
        return "\n".join(lines)
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in body))
        for index in range(len(headers))
    ]
    def render(row: tuple[str, ...]) -> str:
        cells = []
        for index, value in enumerate(row):
            if index == 0:
                cells.append(value.ljust(widths[index]))
            else:
                cells.append(value.rjust(widths[index]))
        return " | ".join(cells)

    lines = [
        render(headers),
        " | ".join("-" * width for width in widths),
        *(render(row) for row in body),
        "",
        "PG/SQLite is the Postgres median divided by the SQLite median.",
        "It is not a pure network-latency ratio.",
    ]
    _append_connection_notes(lines, report)
    return "\n".join(lines)


def _append_connection_notes(lines: list[str], report: dict) -> None:
    connection = next(
        (
            workload
            for workload in report["workloads"]
            if workload["name"] == "connection_establishment"
        ),
        None,
    )
    if connection is not None:
        lines.extend(
            [
                "",
                "Connection establishment (new connection + SELECT 1, steady state):",
                (
                    f"  SQLite median {connection['engines']['sqlite']['median_ms']:.3f} ms, "
                    f"Postgres median {connection['engines']['postgres']['median_ms']:.3f} ms, "
                    f"PG/SQLite {connection['median_ratio_postgres_over_sqlite']:.2f}"
                ),
            ]
        )
    initial = report.get("initial_connection")
    if initial is not None:
        lines.extend(
            [
                "",
                "Initial connection before warmup, excluded from the samples above:",
                (
                    f"  SQLite {initial['sqlite_ms']:.3f} ms, "
                    f"Postgres {initial['postgres_ms']:.3f} ms"
                ),
                f"  {initial['note']}",
            ]
        )
    lines.extend(["", f"Parity validation passed: {report['parity_passed']}"])


def format_cached_summary(report: dict) -> str:
    """Terminal summary for cached Python versus SQLite.

    Python/SQLite is the cached Python median divided by the SQLite median.
    A ratio above 1 means the SQLite request was faster.
    """
    skeleton = report["skeleton"]
    lines = [
        (
            f"Skeleton initialization: {skeleton['initialization_ms']:.3f} ms "
            "(excluded from request samples)"
        ),
        (
            f"Skeleton traced peak: {skeleton['traced_peak_bytes']} bytes; "
            f"traced retained: {skeleton['traced_retained_bytes']} bytes"
        ),
        (
            "Process RSS before load: "
            f"{_bytes(skeleton['rss_before_bytes'])}; after load: "
            f"{_bytes(skeleton['rss_after_bytes'])}"
        ),
        "",
    ]
    for workload in report["workloads"]:
        python_stats = workload["engines"]["cached_python"]
        sqlite_stats = workload["engines"]["sqlite"]
        lines.append(
            f"{workload['name']}  rows={workload['rows_returned']}  "
            f"selectivity={workload['selectivity_label']}  "
            f"python_examined={workload['python_records_examined']}  "
            f"python_objects={workload['python_objects_materialized']}  "
            f"sqlite_title_rows={workload['sqlite_title_rows_fetched']}  "
            f"sqlite_crew_rows={workload['sqlite_crew_rows_fetched']}  "
            f"sqlite_objects={workload['sqlite_objects_materialized']}"
        )
        lines.append(f"  cached_python  {_stats_line(python_stats)}")
        lines.append(f"  sqlite         {_stats_line(sqlite_stats)}")
        lines.append(
            f"  Python/SQLite  {workload['median_ratio_python_over_sqlite']:.2f}"
        )
        lines.append("")
    lines.extend(
        [
            "Python/SQLite is the cached Python median divided by the SQLite median.",
            "A ratio above 1 means the SQLite request was faster.",
            f"Parity validation passed: {report['parity_passed']}",
        ]
    )
    return "\n".join(lines)


def _stats_line(stats: dict) -> str:
    return (
        f"median {stats['median_ms']:.3f} ms  "
        f"p95 {stats['p95_ms']:.3f}  "
        f"min {stats['min_ms']:.3f}  "
        f"max {stats['max_ms']:.3f}  "
        f"stdev {stats['stdev_ms']:.3f}"
    )


def _bytes(value: int | None) -> str:
    if value is None:
        return "unavailable"
    return f"{value} bytes"
