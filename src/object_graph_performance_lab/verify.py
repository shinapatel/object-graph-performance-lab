"""Compare each database to the same on-disk subset.

Small subsets (at most EXHAUSTIVE_MAX titles) are compared field by field.
Larger subsets use counts, an order-independent checksum, foreign-key checks,
and a fixed sample. The checksum covers every stored row. It does not name the
column that differs, and it is not a proof against a crafted hash collision.

Neither mode checks columns this phase does not store: original title, end year,
genres, or person names.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal, ROUND_HALF_UP

from object_graph_performance_lab.config import DatasetPaths
from object_graph_performance_lab.db import Target, probe
from object_graph_performance_lab.errors import LabError
from object_graph_performance_lab.ingest import read_state
from object_graph_performance_lab.subset import iter_crew, iter_ratings, iter_titles
from object_graph_performance_lab.tsv import RatingRow, TitleRow

EXHAUSTIVE_MAX = 100_000

_TITLE_SELECT = """
SELECT title_id, name, type, release_year, runtime_minutes, is_adult
FROM titles
"""

_RATING_SELECT = "SELECT title_id, average_rating, vote_count FROM ratings"

_CREW_SELECT = "SELECT title_id, person_id, role FROM title_crew"


class RowChecksum:
    """Order-independent sum of SHA-256 row hashes, plus a row count.

    Adding a row twice changes the total. Two different multisets of canonical
    rows land on the same value only if their hashes collide.
    """

    def __init__(self) -> None:
        self.total = 0
        self.count = 0

    def add(self, line: str) -> None:
        digest = hashlib.sha256(line.encode("utf-8")).digest()
        self.total = (self.total + int.from_bytes(digest, "big")) & ((1 << 256) - 1)
        self.count += 1

    def hex(self) -> str:
        return f"{self.count}:{self.total:064x}"


def adult_token(value: object) -> str:
    if value is None:
        return ""
    if value is True or value == 1:
        return "1"
    if value is False or value == 0:
        return "0"
    raise LabError(f"unexpected adult flag {value!r}")


def decimal_token(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        quantized = value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return format(quantized, "f")
    return f"{float(value):.1f}"


def canonical_title(
    title_id: str,
    name: object,
    title_type: object,
    year: object,
    runtime: object,
    adult: object,
) -> str:
    return "|".join(
        (
            str(title_id),
            "" if name is None else str(name),
            "" if title_type is None else str(title_type),
            "" if year is None else str(int(year)),
            "" if runtime is None else str(int(runtime)),
            adult_token(adult),
        )
    )


def canonical_rating(title_id: str, average: object, votes: object) -> str:
    return "|".join(
        (
            str(title_id),
            decimal_token(average),
            "" if votes is None else str(int(votes)),
        )
    )


def canonical_crew(title_id: str, person_id: str, role: str) -> str:
    return f"{title_id}|{person_id}|{role}"


def canonical_title_row(title: TitleRow) -> str:
    return canonical_title(
        title.title_id,
        title.name,
        title.type,
        title.release_year,
        title.runtime_minutes,
        title.is_adult,
    )


def canonical_rating_row(rating: RatingRow) -> str:
    return canonical_rating(rating.title_id, rating.average_rating, rating.vote_count)


def _diff_maps(source: dict, database: dict, label: str) -> list[str]:
    errors: list[str] = []
    missing = sorted(set(source) - set(database))
    extra = sorted(set(database) - set(source))
    if missing:
        shown = ", ".join(map(str, missing[:8]))
        errors.append(f"{label} missing from the database ({len(missing)}): {shown}")
    if extra:
        shown = ", ".join(map(str, extra[:8]))
        errors.append(f"{label} present in the database but not the subset ({len(extra)}): {shown}")
    changed = [key for key in source.keys() & database.keys() if source[key] != database[key]]
    for key in sorted(changed)[:8]:
        errors.append(
            f"{label} {key} differs: source={source[key]!r} database={database[key]!r}"
        )
    if len(changed) > 8:
        errors.append(f"{label} {len(changed) - 8} more rows differ")
    return errors


def _fetch_batches(target: Target, statement: str):
    cursor = target.execute(statement)
    while True:
        rows = cursor.fetchmany(5_000)
        if not rows:
            break
        yield from rows


def _orphan_count(target: Target, table: str) -> int:
    if table not in {"ratings", "title_crew"}:
        raise LabError(f"unexpected table {table}")
    row = target.fetchone(
        f"""
        SELECT COUNT(*) FROM {table} AS child
        WHERE NOT EXISTS (
            SELECT 1 FROM titles AS titles
            WHERE titles.title_id = child.title_id
        )
        """
    )
    return int(row[0])


def _distinct_crew_count(target: Target) -> int:
    row = target.fetchone(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT title_id, person_id, role FROM title_crew
        ) AS distinct_crew
        """
    )
    return int(row[0])


def _describe(target: Target, title_id: str) -> str:
    title = target.fetchone(
        """
        SELECT name, type, release_year, runtime_minutes, is_adult
        FROM titles WHERE title_id = ?
        """,
        (title_id,),
    )
    if title is None:
        return f"{title_id} | missing from the database"
    rating = target.fetchone(
        "SELECT average_rating, vote_count FROM ratings WHERE title_id = ?",
        (title_id,),
    )
    crew = target.fetchall(
        """
        SELECT role, person_id FROM title_crew
        WHERE title_id = ?
        ORDER BY role, person_id
        """,
        (title_id,),
    )
    rating_text = "rating null"
    if rating is not None:
        average = "null" if rating[0] is None else decimal_token(rating[0])
        votes = "null" if rating[1] is None else str(rating[1])
        rating_text = f"rating {average} votes {votes}"
    crew_text = ", ".join(f"{role} {person_id}" for role, person_id in crew) or "no crew"
    year = "null" if title[2] is None else str(title[2])
    runtime = "null" if title[3] is None else str(title[3])
    return (
        f"{title_id} | {title[0]} | {title[1]} | year {year} | "
        f"runtime {runtime} | adult {adult_token(title[4]) or 'null'} | "
        f"{rating_text} | {crew_text}"
    )


def verify_target(target: Target, datasets: DatasetPaths, limit: int) -> list[str]:
    """Return human-readable mismatches. An empty list means this database matches."""
    if limit < 0:
        raise LabError("--limit must be >= 0. Use 0 to read every title.")
    missing = datasets.missing()
    if missing:
        names = ", ".join(str(path) for path in missing)
        raise LabError(f"Missing dataset files: {names}")

    errors: list[str] = []
    print(probe(target), flush=True)
    state = read_state(target)
    recorded = state.get("subset_limit")
    if recorded is not None and int(recorded) != limit:
        errors.append(
            f"ingest_state subset_limit={recorded} does not match verify limit {limit}"
        )
    for key, path in (
        ("basics_bytes", datasets.basics),
        ("ratings_bytes", datasets.ratings),
        ("crew_bytes", datasets.crew),
    ):
        recorded_size = state.get(key)
        actual_size = str(path.stat().st_size)
        if recorded_size is not None and recorded_size != actual_size:
            errors.append(
                f"{path.name} size changed since ingest "
                f"(recorded {recorded_size} bytes, now {actual_size}). "
                "A newer download can change the prefix. "
                "Rebuild with reset --yes, init, and ingest if that was intentional."
            )

    title_ids: set[str] = set()
    title_checksum = RowChecksum()
    rating_checksum = RowChecksum()
    crew_checksum = RowChecksum()
    title_map: dict[str, str] | None = {}
    samples: list[TitleRow] = []
    last_title: TitleRow | None = None
    exhaustive = True

    for title in iter_titles(datasets.basics, limit, title_ids):
        line = canonical_title_row(title)
        title_checksum.add(line)
        last_title = title
        if len(samples) < 3:
            samples.append(title)
        if exhaustive and title_map is not None:
            title_map[title.title_id] = line
            if len(title_map) > EXHAUSTIVE_MAX:
                exhaustive = False
                title_map = None
    if last_title is not None and all(item.title_id != last_title.title_id for item in samples):
        samples.append(last_title)

    sample_ids = {title.title_id for title in samples}
    rating_map: dict[str, str] | None = {} if exhaustive else None
    sample_ratings: dict[str, str] = {}
    for rating in iter_ratings(datasets.ratings, title_ids):
        line = canonical_rating_row(rating)
        rating_checksum.add(line)
        if rating.title_id in sample_ids:
            sample_ratings[rating.title_id] = line
        if rating_map is not None:
            rating_map[rating.title_id] = line

    crew_map: dict[tuple[str, str, str], str] | None = {} if exhaustive else None
    sample_crew: dict[tuple[str, str, str], str] = {}
    for title_id, person_id, role in iter_crew(datasets.crew, title_ids):
        line = canonical_crew(title_id, person_id, role)
        crew_checksum.add(line)
        if title_id in sample_ids:
            sample_crew[(title_id, person_id, role)] = line
        if crew_map is not None:
            crew_map[(title_id, person_id, role)] = line

    mode = "exhaustive row comparison" if exhaustive else "counts, checksum, foreign keys, and a sample"
    print(f"  mode: {mode} ({title_checksum.count:,} subset titles)", flush=True)
    print(
        "  not checked: original title, end year, genres, and person names",
        flush=True,
    )

    db_title_count = int(target.fetchone("SELECT COUNT(*) FROM titles")[0])
    db_rating_count = int(target.fetchone("SELECT COUNT(*) FROM ratings")[0])
    db_crew_count = int(target.fetchone("SELECT COUNT(*) FROM title_crew")[0])
    print(f"  titles: {db_title_count}", flush=True)
    print(f"  ratings: {db_rating_count}", flush=True)
    print(f"  crew relationships: {db_crew_count}", flush=True)

    if db_title_count != title_checksum.count:
        errors.append(
            f"title count {db_title_count} != subset {title_checksum.count}"
        )
    if db_rating_count != rating_checksum.count:
        errors.append(
            f"rating count {db_rating_count} != subset {rating_checksum.count}"
        )
    if db_crew_count != crew_checksum.count:
        errors.append(
            f"crew relationship count {db_crew_count} != subset {crew_checksum.count}"
        )

    distinct_titles = int(target.fetchone("SELECT COUNT(DISTINCT title_id) FROM titles")[0])
    if distinct_titles != db_title_count:
        errors.append(f"title primary keys are not unique ({distinct_titles} distinct of {db_title_count})")
    distinct_ratings = int(
        target.fetchone("SELECT COUNT(DISTINCT title_id) FROM ratings")[0]
    )
    if distinct_ratings != db_rating_count:
        errors.append("rating primary keys are not unique")
    if _distinct_crew_count(target) != db_crew_count:
        errors.append("crew primary keys are not unique")

    rating_orphans = _orphan_count(target, "ratings")
    crew_orphans = _orphan_count(target, "title_crew")
    print(f"  foreign-key orphans: ratings {rating_orphans}, crew {crew_orphans}", flush=True)
    if rating_orphans:
        errors.append(f"{rating_orphans} ratings have no matching title")
    if crew_orphans:
        errors.append(f"{crew_orphans} crew relationships have no matching title")

    if exhaustive and title_map is not None and rating_map is not None and crew_map is not None:
        db_titles = {
            row[0]: canonical_title(*row)
            for row in target.fetchall(_TITLE_SELECT)
        }
        db_ratings = {
            row[0]: canonical_rating(*row)
            for row in target.fetchall(_RATING_SELECT)
        }
        db_crew = {
            (row[0], row[1], row[2]): canonical_crew(*row)
            for row in target.fetchall(_CREW_SELECT)
        }
        errors.extend(_diff_maps(title_map, db_titles, "titles"))
        errors.extend(_diff_maps(rating_map, db_ratings, "ratings"))
        errors.extend(_diff_maps(crew_map, db_crew, "crew"))
    else:
        db_title_checksum = RowChecksum()
        for row in _fetch_batches(target, _TITLE_SELECT):
            db_title_checksum.add(canonical_title(*row))
        db_rating_checksum = RowChecksum()
        for row in _fetch_batches(target, _RATING_SELECT):
            db_rating_checksum.add(canonical_rating(*row))
        db_crew_checksum = RowChecksum()
        for row in _fetch_batches(target, _CREW_SELECT):
            db_crew_checksum.add(canonical_crew(*row))
        print(f"  title checksum: {db_title_checksum.hex()}", flush=True)
        if db_title_checksum.hex() != title_checksum.hex():
            errors.append(
                "title checksum mismatch "
                f"source={title_checksum.hex()} database={db_title_checksum.hex()}"
            )
        if db_rating_checksum.hex() != rating_checksum.hex():
            errors.append(
                "rating checksum mismatch "
                f"source={rating_checksum.hex()} database={db_rating_checksum.hex()}"
            )
        if db_crew_checksum.hex() != crew_checksum.hex():
            errors.append(
                "crew checksum mismatch "
                f"source={crew_checksum.hex()} database={db_crew_checksum.hex()}"
            )
        print(
            "  large-set checksum does not name the differing column and does not "
            "prove the absence of a hash collision. The sample below is not a "
            "proof about unsampled rows; the checksum covers those rows.",
            flush=True,
        )
        for title in samples:
            db_row = target.fetchone(
                """
                SELECT title_id, name, type, release_year, runtime_minutes, is_adult
                FROM titles WHERE title_id = ?
                """,
                (title.title_id,),
            )
            source_line = canonical_title_row(title)
            if db_row is None:
                errors.append(f"sample title {title.title_id} is missing")
            elif canonical_title(*db_row) != source_line:
                errors.append(
                    f"sample title {title.title_id} differs: "
                    f"source={source_line!r} database={canonical_title(*db_row)!r}"
                )
            source_rating = sample_ratings.get(title.title_id)
            db_rating = target.fetchone(
                "SELECT title_id, average_rating, vote_count FROM ratings WHERE title_id = ?",
                (title.title_id,),
            )
            if source_rating is None and db_rating is not None:
                errors.append(f"sample rating {title.title_id} is unexpected in the database")
            elif source_rating is not None and db_rating is None:
                errors.append(f"sample rating {title.title_id} is missing")
            elif source_rating is not None and db_rating is not None:
                if canonical_rating(*db_rating) != source_rating:
                    errors.append(f"sample rating {title.title_id} differs")
            db_links = {
                (row[0], row[1], row[2])
                for row in target.fetchall(
                    """
                    SELECT title_id, person_id, role FROM title_crew
                    WHERE title_id = ?
                    """,
                    (title.title_id,),
                )
            }
            source_links = {key for key in sample_crew if key[0] == title.title_id}
            if db_links != source_links:
                errors.append(
                    f"sample crew for {title.title_id} differs: "
                    f"source={sorted(source_links)} database={sorted(db_links)}"
                )

    print("  representative:", flush=True)
    if not samples:
        print("    (empty subset)", flush=True)
    for title in samples[:3]:
        print(f"    {_describe(target, title.title_id)}", flush=True)

    if errors:
        print(f"  result: mismatch ({len(errors)})", flush=True)
        for error in errors:
            print(f"  - {error}", flush=True)
    else:
        print("  result: ok", flush=True)
    return errors
