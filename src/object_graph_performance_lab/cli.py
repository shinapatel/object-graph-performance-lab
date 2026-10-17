"""Command line for the IMDb load and Experiment 1.

    python -m object_graph_performance_lab.cli download
    python -m object_graph_performance_lab.cli init
    python -m object_graph_performance_lab.cli ingest --limit 10000
    python -m object_graph_performance_lab.cli verify
    python -m object_graph_performance_lab.cli inspect
    python -m object_graph_performance_lab.cli benchmark --experiment local-vs-remote
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from object_graph_performance_lab.benchmarks.report import format_summary, write_csv, write_json
from object_graph_performance_lab.benchmarks.runner import (
    INITIAL_CONNECTION_NOTE,
    open_measured_engine,
    run_comparison,
)
from object_graph_performance_lab.benchmarks.workloads import select_workloads
from object_graph_performance_lab.config import (
    DEFAULT_LIMIT,
    database_url,
    raw_dir,
    redact_database_url,
    repo_root,
    sqlite_path,
    standard_datasets,
)
from object_graph_performance_lab.db import (
    connect_postgres,
    connect_sqlite,
    open_postgres_target,
    open_sqlite_target,
    probe,
)
from object_graph_performance_lab.download import download_datasets
from object_graph_performance_lab.errors import LabError
from object_graph_performance_lab.ingest import ingest_files, read_state
from object_graph_performance_lab.reset import (
    drop_postgres_tables,
    remove_downloads,
    remove_sqlite_files,
)
from object_graph_performance_lab.schema import ensure_schema
from object_graph_performance_lab.summary import inspect_target
from object_graph_performance_lab.verify import verify_target


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m object_graph_performance_lab.cli",
        description=(
            "Load a deterministic IMDb non-commercial subset into local SQLite "
            "and, when DATABASE_URL is set, remote Postgres."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download", help="cache IMDb TSV.gz files under data/raw/")
    download.add_argument(
        "--force",
        action="store_true",
        help="replace cached files even when they already look like gzip",
    )

    sub.add_parser("init", help="create tables in SQLite and in Postgres when configured")

    ingest = sub.add_parser(
        "ingest",
        help="load the same deterministic subset into every configured database",
    )
    ingest.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=(
            "number of titles to take from the start of title.basics "
            f"(default {DEFAULT_LIMIT}; 0 means the entire file)"
        ),
    )

    verify = sub.add_parser(
        "verify",
        help="compare each database to the subset recorded at ingest",
    )
    verify.add_argument(
        "--limit",
        type=int,
        default=None,
        help="subset size to compare; defaults to the limit stored in ingest_state",
    )

    sub.add_parser("inspect", help="print counts, a few rows, and connection health")

    reset = sub.add_parser(
        "reset",
        help="delete the SQLite file and drop Phase 1 Postgres tables",
    )
    reset.add_argument(
        "--yes",
        action="store_true",
        help="required. without it, reset only prints what it would delete",
    )
    reset.add_argument(
        "--include-downloads",
        action="store_true",
        help="also delete the cached IMDb files under data/raw/",
    )

    benchmark = sub.add_parser(
        "benchmark",
        help="compare read-only query latency for one experiment",
    )
    benchmark.add_argument(
        "--experiment",
        required=True,
        help="experiment name; local-vs-remote compares SQLite with remote Postgres",
    )
    benchmark.add_argument(
        "--iterations",
        type=int,
        default=50,
        help="measured iterations per workload and engine (default 50, minimum 2)",
    )
    benchmark.add_argument(
        "--warmup",
        type=int,
        default=10,
        help="unrecorded iterations per workload and engine before measuring (default 10)",
    )
    benchmark.add_argument(
        "--workload",
        action="append",
        default=None,
        help="run only this workload; repeat the flag to choose several",
    )
    benchmark.add_argument(
        "--json",
        type=Path,
        default=None,
        help="write the full report here (default: artifacts/benchmarks/)",
    )
    benchmark.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="write one row per measured sample here (default: artifacts/benchmarks/)",
    )
    return parser


def _cmd_download(args: argparse.Namespace) -> int:
    download_datasets(raw_dir(), force=args.force)
    return 0


def _cmd_init(_args: argparse.Namespace) -> int:
    sqlite_target = open_sqlite_target(readonly=False)
    try:
        ensure_schema(sqlite_target)
        print(probe(sqlite_target), flush=True)
    finally:
        sqlite_target.close()

    if database_url() is None:
        print(
            "postgres: skipped because DATABASE_URL is not set. "
            "SQLite was initialized.",
            flush=True,
        )
        return 0

    postgres = open_postgres_target()
    if postgres is None:
        print("postgres: skipped because DATABASE_URL is not set.", flush=True)
        return 0
    try:
        ensure_schema(postgres)
        print(probe(postgres), flush=True)
    finally:
        postgres.close()
    return 0


def _open_writers() -> list:
    """Open Postgres first so a dead URL does not create a new SQLite file."""
    postgres = open_postgres_target()
    try:
        sqlite_target = open_sqlite_target(readonly=False)
    except Exception:
        if postgres is not None:
            postgres.close()
        raise
    targets = [sqlite_target]
    if postgres is not None:
        targets.append(postgres)
    return targets


def _close_all(targets: list) -> None:
    for target in targets:
        target.close()


def _cmd_ingest(args: argparse.Namespace) -> int:
    if args.limit < 0:
        raise LabError("--limit must be >= 0. Use 0 to read every title.")
    targets = _open_writers()
    try:
        for target in targets:
            print(probe(target), flush=True)
        ingest_files(standard_datasets(), targets, args.limit)
    finally:
        _close_all(targets)
    return 0


def _resolve_limit(sqlite_target, postgres, requested: int | None) -> int:
    limits: list[int] = []
    for target in (sqlite_target, postgres):
        if target is None:
            continue
        raw = read_state(target).get("subset_limit")
        if raw is None:
            continue
        limits.append(int(raw))
    if limits and len(set(limits)) != 1:
        raise LabError(
            "SQLite and PostgreSQL record different subset limits "
            f"({limits}). Resume or rebuild so both match before verify."
        )
    recorded = limits[0] if limits else None
    if requested is not None and recorded is not None and requested != recorded:
        raise LabError(
            f"--limit {requested} does not match the ingested subset_limit {recorded}. "
            "Verify the recorded subset, or pass the same limit."
        )
    if requested is not None:
        return requested
    if recorded is None:
        raise LabError(
            "No subset_limit is stored. Run ingest, or pass verify --limit N "
            "for a database that was loaded outside ingest_state."
        )
    return recorded


def _cmd_verify(args: argparse.Namespace) -> int:
    sqlite_target = open_sqlite_target(readonly=True)
    postgres = None
    exit_code = 0
    try:
        if database_url() is not None:
            postgres = open_postgres_target()
        limit = _resolve_limit(sqlite_target, postgres, args.limit)
        datasets = standard_datasets()
        errors = verify_target(sqlite_target, datasets, limit)
        if errors:
            exit_code = 1
        if postgres is None:
            print(
                "postgres: skipped because DATABASE_URL is not set.",
                flush=True,
            )
        else:
            pg_errors = verify_target(postgres, datasets, limit)
            if pg_errors:
                exit_code = 1
    finally:
        sqlite_target.close()
        if postgres is not None:
            postgres.close()
    return exit_code


def _cmd_inspect(_args: argparse.Namespace) -> int:
    sqlite_target = open_sqlite_target(readonly=True)
    try:
        inspect_target(sqlite_target)
    finally:
        sqlite_target.close()
    if database_url() is None:
        print("postgres: skipped because DATABASE_URL is not set.", flush=True)
        return 0
    postgres = open_postgres_target()
    assert postgres is not None
    try:
        inspect_target(postgres)
    finally:
        postgres.close()
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    path = sqlite_path()
    url = database_url()
    described = redact_database_url(url) if url else None
    if not args.yes:
        print("Refusing to delete data without --yes.", flush=True)
        print(f"Would delete the SQLite database at {path} (and its -wal/-shm files).", flush=True)
        if described:
            print(f"Would drop Phase 1 tables on {described}.", flush=True)
        else:
            print("PostgreSQL is not configured. No remote tables would be dropped.", flush=True)
        if args.include_downloads:
            print(f"Would delete cached dataset files in {raw_dir()}.", flush=True)
        else:
            print("Downloaded datasets would be kept.", flush=True)
        return 2

    if url is not None:
        postgres = connect_postgres(url)
        try:
            drop_postgres_tables(postgres)
            print(f"dropped Phase 1 tables on {described}", flush=True)
        finally:
            postgres.close()
    removed = remove_sqlite_files(path)
    if removed:
        for item in removed:
            print(f"deleted {item}", flush=True)
    else:
        print(f"no SQLite file at {path}", flush=True)
    if args.include_downloads:
        for item in remove_downloads(raw_dir()):
            print(f"deleted {item}", flush=True)
    else:
        print("downloaded datasets kept", flush=True)
    return 0


def _benchmark_output_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.json is not None and args.csv is not None:
        return args.json, args.csv
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = repo_root() / "artifacts" / "benchmarks"
    json_path = args.json or directory / f"local-vs-remote-{stamp}.json"
    csv_path = args.csv or directory / f"local-vs-remote-{stamp}.csv"
    return json_path, csv_path


def _cmd_benchmark(args: argparse.Namespace) -> int:
    if args.experiment != "local-vs-remote":
        raise LabError(
            f"Unknown experiment {args.experiment!r}. Choose: local-vs-remote"
        )
    if args.iterations < 2:
        raise LabError("iterations must be at least 2 so the standard deviation is defined")
    if args.warmup < 0:
        raise LabError("warmup must be at least 0")
    workloads = select_workloads(args.workload)
    if database_url() is None:
        raise LabError(
            "DATABASE_URL is not set. local-vs-remote needs the local SQLite file and Postgres."
        )
    if args.iterations < 30:
        print(
            "note: fewer than 30 measured iterations. The documented comparison uses at least 30.",
            file=sys.stderr,
        )

    sqlite_engine, sqlite_initial_ms = open_measured_engine(
        "sqlite",
        lambda: connect_sqlite(sqlite_path(), readonly=True),
    )
    try:
        postgres_engine, postgres_initial_ms = open_measured_engine(
            "postgres",
            lambda: connect_postgres(database_url() or ""),
        )
        try:
            report = run_comparison(
                experiment=args.experiment,
                workloads=workloads,
                engines=[sqlite_engine, postgres_engine],
                iterations=args.iterations,
                warmup=args.warmup,
                initial_connection={
                    "sqlite_ms": sqlite_initial_ms,
                    "postgres_ms": postgres_initial_ms,
                    "note": INITIAL_CONNECTION_NOTE,
                },
            )
        finally:
            postgres_engine.target.close()
    finally:
        sqlite_engine.target.close()

    json_path, csv_path = _benchmark_output_paths(args)
    write_json(report, json_path)
    write_csv(report, csv_path)
    print(format_summary(report), flush=True)
    print(f"json: {json_path}", flush=True)
    print(f"csv: {csv_path}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "download": _cmd_download,
        "init": _cmd_init,
        "ingest": _cmd_ingest,
        "verify": _cmd_verify,
        "inspect": _cmd_inspect,
        "reset": _cmd_reset,
        "benchmark": _cmd_benchmark,
    }
    try:
        return handlers[args.command](args)
    except LabError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
