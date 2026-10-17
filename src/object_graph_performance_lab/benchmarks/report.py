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
