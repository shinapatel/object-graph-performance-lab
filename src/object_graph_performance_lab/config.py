"""Paths, dataset names, and environment settings.

DATASET files are cached under data/raw/ and are never part of this repository.
The SQLite database lives at data/local/imdb.sqlite.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_LIMIT = 10_000
BATCH_SIZE = 5_000
SUBSET_STRATEGY = "title.basics file order prefix; limit 0 means the entire file"

BASICS_NAME = "title.basics.tsv.gz"
RATINGS_NAME = "title.ratings.tsv.gz"
CREW_NAME = "title.crew.tsv.gz"

DATASET_URLS: tuple[tuple[str, str], ...] = (
    (BASICS_NAME, "https://datasets.imdbws.com/title.basics.tsv.gz"),
    (RATINGS_NAME, "https://datasets.imdbws.com/title.ratings.tsv.gz"),
    (CREW_NAME, "https://datasets.imdbws.com/title.crew.tsv.gz"),
)

_DOTENV_LOADED = False


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("pyproject.toml not found above the package")


def raw_dir() -> Path:
    return repo_root() / "data" / "raw"


def sqlite_path() -> Path:
    return repo_root() / "data" / "local" / "imdb.sqlite"


def load_local_env() -> None:
    """Load the repo-root .env without overriding variables already set."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    env_file = repo_root() / ".env"
    if not env_file.is_file():
        return
    from dotenv import load_dotenv

    load_dotenv(env_file, override=False)


def database_url() -> str | None:
    """Return DATABASE_URL, or None when it is unset or blank.

    The value is whatever the operator put in the environment or .env.
    This module never invents a connection string.
    """
    load_local_env()
    value = os.environ.get("DATABASE_URL", "").strip()
    return value or None


def redact_database_url(url: str) -> str:
    """Describe a Postgres URI without its password or query string."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "postgresql://<unparseable>"
    user = parts.username or "<no-user>"
    host = parts.hostname or "<no-host>"
    port = f":{parts.port}" if parts.port else ""
    database = parts.path.lstrip("/") or "<no-database>"
    described = f"postgresql://{user}@{host}{port}/{database}"
    if parts.password and parts.password in described:
        return "postgresql://<unparseable>"
    return described


def scrub_secret(text: str, url: str | None) -> str:
    """Remove a URI password if a driver copied it into an error."""
    if not url:
        return text
    password = urlsplit(url).password
    if password:
        return text.replace(password, "***")
    return text


@dataclass(frozen=True)
class DatasetPaths:
    basics: Path
    ratings: Path
    crew: Path

    def missing(self) -> list[Path]:
        return [path for path in (self.basics, self.ratings, self.crew) if not path.is_file()]


def standard_datasets(directory: Path | None = None) -> DatasetPaths:
    root = directory if directory is not None else raw_dir()
    return DatasetPaths(
        basics=root / BASICS_NAME,
        ratings=root / RATINGS_NAME,
        crew=root / CREW_NAME,
    )
