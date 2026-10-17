"""Streaming parsers for the IMDb non-commercial TSV files.

``\\N`` becomes None. Empty cells are treated the same way. Crew lists are
comma-separated nconsts; a null list becomes an empty tuple, and repeated ids
inside one list are kept once, in first-seen order.
"""

from __future__ import annotations

import csv
import gzip
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from object_graph_performance_lab.errors import LabError

TITLE_COLUMNS = (
    "tconst",
    "titleType",
    "primaryTitle",
    "isAdult",
    "startYear",
    "runtimeMinutes",
)
RATING_COLUMNS = ("tconst", "averageRating", "numVotes")
CREW_COLUMNS = ("tconst", "directors", "writers")


@dataclass(frozen=True)
class TitleRow:
    title_id: str
    name: str | None
    type: str | None
    release_year: int | None
    runtime_minutes: int | None
    is_adult: bool | None


@dataclass(frozen=True)
class RatingRow:
    title_id: str
    average_rating: Decimal | None
    vote_count: int | None


def missing_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if text == "" or text == r"\N":
        return None
    return text


def parse_int(value: str | None) -> int | None:
    text = missing_to_none(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"expected an integer, got {value!r}") from exc


def parse_adult(value: str | None) -> bool | None:
    text = missing_to_none(value)
    if text is None:
        return None
    if text == "0":
        return False
    if text == "1":
        return True
    raise ValueError(f"expected adult flag 0, 1, or \\N, got {value!r}")


def parse_decimal(value: str | None) -> Decimal | None:
    text = missing_to_none(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except Exception as exc:
        raise ValueError(f"expected a decimal, got {value!r}") from exc


def parse_id_list(value: str | None) -> tuple[str, ...]:
    text = missing_to_none(value)
    if text is None:
        return ()
    seen: list[str] = []
    known: set[str] = set()
    for part in text.split(","):
        item = part.strip()
        if not item or item == r"\N" or item in known:
            continue
        known.add(item)
        seen.append(item)
    return tuple(seen)


def parse_title_row(raw: dict[str, str | None]) -> TitleRow | None:
    title_id = missing_to_none(raw.get("tconst"))
    if title_id is None:
        return None
    return TitleRow(
        title_id=title_id,
        name=missing_to_none(raw.get("primaryTitle")),
        type=missing_to_none(raw.get("titleType")),
        release_year=parse_int(raw.get("startYear")),
        runtime_minutes=parse_int(raw.get("runtimeMinutes")),
        is_adult=parse_adult(raw.get("isAdult")),
    )


def parse_rating_row(raw: dict[str, str | None]) -> RatingRow | None:
    title_id = missing_to_none(raw.get("tconst"))
    if title_id is None:
        return None
    return RatingRow(
        title_id=title_id,
        average_rating=parse_decimal(raw.get("averageRating")),
        vote_count=parse_int(raw.get("numVotes")),
    )


def parse_crew_links(raw: dict[str, str | None]) -> tuple[tuple[str, str, str], ...]:
    title_id = missing_to_none(raw.get("tconst"))
    if title_id is None:
        return ()
    links: list[tuple[str, str, str]] = []
    for person_id in parse_id_list(raw.get("directors")):
        links.append((title_id, person_id, "director"))
    for person_id in parse_id_list(raw.get("writers")):
        links.append((title_id, person_id, "writer"))
    return tuple(links)


def open_tsv(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def iter_dicts(path: Path, required: tuple[str, ...]) -> Iterator[dict[str, str | None]]:
    try:
        with open_tsv(path) as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            fieldnames = reader.fieldnames or []
            cleaned = [name.strip() if isinstance(name, str) else name for name in fieldnames]
            reader.fieldnames = cleaned
            missing = [column for column in required if column not in cleaned]
            if missing:
                raise LabError(f"{path} is missing columns: {', '.join(missing)}")
            for raw in reader:
                yield raw
    except gzip.BadGzipFile as exc:
        raise LabError(
            f"{path} is not a readable gzip file. "
            "Run download --force if this is a dataset cache."
        ) from exc
    except EOFError as exc:
        raise LabError(
            f"{path} ended early. Run download --force if this is a dataset cache."
        ) from exc
