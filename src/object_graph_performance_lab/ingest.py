"""Load one deterministic subset into every configured database.

Inserts are ``ON CONFLICT DO NOTHING``. Existing title, rating, and crew rows
are not updated or deleted. Each batch is one transaction on one persistent
connection per database. SQLite is written first, then PostgreSQL. If a later
batch fails, that batch is rolled back and the command stops. Earlier batches
stay. Re-running the same ``--limit`` resumes. A different limit is refused
when a database already recorded another limit, so a smaller load cannot pretend
the extra rows are gone.
"""

from __future__ import annotations

from datetime import datetime, timezone

from object_graph_performance_lab.config import (
    BATCH_SIZE,
    SUBSET_STRATEGY,
    DatasetPaths,
    database_url,
    scrub_secret,
)
from object_graph_performance_lab.db import Target
from object_graph_performance_lab.errors import LabError
from object_graph_performance_lab.schema import ensure_schema
from object_graph_performance_lab.subset import iter_crew, iter_ratings, iter_titles

TITLE_INSERT = """
INSERT INTO titles (
    title_id, name, type, release_year, runtime_minutes, is_adult
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (title_id) DO NOTHING
"""

RATING_INSERT = """
INSERT INTO ratings (title_id, average_rating, vote_count)
VALUES (?, ?, ?)
ON CONFLICT (title_id) DO NOTHING
"""

CREW_INSERT = """
INSERT INTO title_crew (title_id, person_id, role)
VALUES (?, ?, ?)
ON CONFLICT (title_id, person_id, role) DO NOTHING
"""

STATE_UPSERT = """
INSERT INTO ingest_state (state_key, state_value) VALUES (?, ?)
ON CONFLICT (state_key) DO UPDATE SET state_value = excluded.state_value
"""


def read_state(target: Target) -> dict[str, str]:
    rows = target.fetchall("SELECT state_key, state_value FROM ingest_state")
    return {str(row[0]): str(row[1]) for row in rows}


def write_state(target: Target, values: dict[str, str]) -> None:
    target.write_many(STATE_UPSERT, list(values.items()))


def _recovery(target: Target, exc: Exception, limit: int) -> LabError:
    return LabError(
        f"{target.label} ingest batch failed and that batch was rolled back.\n"
        f"Error: {scrub_secret(str(exc), database_url())}\n"
        "Rows from earlier batches were kept. This command does not delete them.\n"
        "Targets are written SQLite first, then PostgreSQL, so a later target can "
        "be one batch behind.\n"
        "The databases may not match until this is resumed and verify succeeds.\n"
        "Resume with the same subset. Keys that are already present are left as they are:\n"
        f"  python -m object_graph_performance_lab.cli ingest --limit {limit}\n"
        "To drop the loaded tables and start over:\n"
        "  python -m object_graph_performance_lab.cli reset --yes"
    )


def _write_all(targets: list[Target], sql: str, rows: list[tuple], limit: int) -> None:
    for target in targets:
        try:
            target.write_many(sql, rows)
        except LabError:
            raise
        except Exception as exc:
            raise _recovery(target, exc, limit) from exc


def _scan_printer(label: str):
    def report(scanned: int) -> None:
        print(f"{label} scanned {scanned:,} source rows", flush=True)

    return report


def ingest_files(
    datasets: DatasetPaths,
    targets: list[Target],
    limit: int,
    *,
    batch_size: int = BATCH_SIZE,
) -> None:
    if limit < 0:
        raise LabError("--limit must be >= 0. Use 0 to read every title.")
    if batch_size < 1:
        raise LabError("batch size must be >= 1")
    if not targets:
        raise LabError("no databases were opened for ingest")
    missing = datasets.missing()
    if missing:
        names = ", ".join(str(path) for path in missing)
        raise LabError(
            f"Missing dataset files: {names}. "
            "Run: python -m object_graph_performance_lab.cli download"
        )

    for target in targets:
        ensure_schema(target)
    for target in targets:
        recorded = read_state(target).get("subset_limit")
        if recorded is None:
            continue
        try:
            recorded_limit = int(recorded)
        except ValueError as exc:
            raise LabError(
                f"{target.label} has a corrupt subset_limit {recorded!r}. "
                "Inspect the database, or rebuild with reset --yes."
            ) from exc
        if recorded_limit != limit:
            raise LabError(
                f"{target.label} already records subset_limit={recorded_limit}. "
                f"Refusing to ingest --limit {limit} because existing rows are not "
                "overwritten or deleted. Resume with the recorded limit, or rebuild "
                "with: python -m object_graph_performance_lab.cli reset --yes"
            )

    started = {
        "subset_limit": str(limit),
        "subset_strategy": SUBSET_STRATEGY,
        "status": "incomplete",
    }
    for target in targets:
        write_state(target, started)

    if limit == 0:
        print("Subset: every title in title.basics, in file order.", flush=True)
        print(
            "limit 0 reads the full published file. That is the path for a load of "
            "at least one million titles.",
            flush=True,
        )
    else:
        print(
            f"Subset: the first {limit} distinct titles in title.basics file order.",
            flush=True,
        )
    print(
        "One id set is applied to every target. Existing rows are not updated or deleted.",
        flush=True,
    )
    for target in targets:
        print(f"target {target.label}", flush=True)

    title_ids: set[str] = set()
    title_batch: list[tuple] = []
    title_count = 0
    for title in iter_titles(
        datasets.basics, limit, title_ids, on_scan=_scan_printer("titles")
    ):
        title_batch.append(
            (
                title.title_id,
                title.name,
                title.type,
                title.release_year,
                title.runtime_minutes,
                title.is_adult,
            )
        )
        title_count += 1
        if len(title_batch) >= batch_size:
            _write_all(targets, TITLE_INSERT, title_batch, limit)
            title_batch = []
            print(f"titles processed {title_count:,}", flush=True)
    _write_all(targets, TITLE_INSERT, title_batch, limit)
    print(f"titles selected {title_count:,}", flush=True)

    rating_batch: list[tuple] = []
    rating_count = 0
    for rating in iter_ratings(
        datasets.ratings, title_ids, on_scan=_scan_printer("ratings")
    ):
        average = None if rating.average_rating is None else float(rating.average_rating)
        rating_batch.append((rating.title_id, average, rating.vote_count))
        rating_count += 1
        if len(rating_batch) >= batch_size:
            _write_all(targets, RATING_INSERT, rating_batch, limit)
            rating_batch = []
            print(f"ratings kept {rating_count:,}", flush=True)
    _write_all(targets, RATING_INSERT, rating_batch, limit)
    print(f"ratings kept {rating_count:,}", flush=True)

    crew_batch: list[tuple] = []
    crew_count = 0
    for link in iter_crew(datasets.crew, title_ids, on_scan=_scan_printer("crew")):
        crew_batch.append(link)
        crew_count += 1
        if len(crew_batch) >= batch_size:
            _write_all(targets, CREW_INSERT, crew_batch, limit)
            crew_batch = []
            print(f"crew relationships kept {crew_count:,}", flush=True)
    _write_all(targets, CREW_INSERT, crew_batch, limit)
    print(f"crew relationships kept {crew_count:,}", flush=True)

    finished = {
        "subset_limit": str(limit),
        "subset_strategy": SUBSET_STRATEGY,
        "status": "complete",
        "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "basics_bytes": str(datasets.basics.stat().st_size),
        "ratings_bytes": str(datasets.ratings.stat().st_size),
        "crew_bytes": str(datasets.crew.stat().st_size),
        "title_count": str(title_count),
        "rating_count": str(rating_count),
        "crew_count": str(crew_count),
    }
    for target in targets:
        write_state(target, finished)
        counts = (
            target.fetchone("SELECT COUNT(*) FROM titles")[0],
            target.fetchone("SELECT COUNT(*) FROM ratings")[0],
            target.fetchone("SELECT COUNT(*) FROM title_crew")[0],
        )
        print(
            f"done {target.label}: titles={counts[0]} ratings={counts[1]} "
            f"crew_relationships={counts[2]}",
            flush=True,
        )
