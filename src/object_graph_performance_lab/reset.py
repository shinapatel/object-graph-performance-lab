"""Drop Phase 1 data only after an explicit confirmation."""

from __future__ import annotations

from pathlib import Path

from object_graph_performance_lab.config import DATASET_URLS
from object_graph_performance_lab.db import Target


def remove_sqlite_files(path: Path) -> list[Path]:
    removed: list[Path] = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(f"{path}{suffix}") if suffix else path
        if candidate.is_file():
            candidate.unlink()
            removed.append(candidate)
    return removed


def drop_postgres_tables(target: Target) -> None:
    for statement in (
        "DROP TABLE IF EXISTS title_crew",
        "DROP TABLE IF EXISTS ratings",
        "DROP TABLE IF EXISTS ingest_state",
        "DROP TABLE IF EXISTS titles",
    ):
        target.execute(statement)
    target.commit()


def remove_downloads(directory: Path) -> list[Path]:
    removed: list[Path] = []
    for name, _url in DATASET_URLS:
        for candidate in (directory / name, directory / f"{name}.partial"):
            if candidate.is_file():
                candidate.unlink()
                removed.append(candidate)
    return removed
