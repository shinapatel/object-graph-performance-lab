"""Portable Phase 1 schema for SQLite and PostgreSQL.

Both engines store the same columns and NULL semantics:

- titles.title_id is the IMDb tconst and the primary key.
- name, type, release_year, runtime_minutes, and is_adult are NULL when the
  TSV cell is ``\\N``. is_adult NULL means the flag was missing, not false.
- ratings.title_id references titles. average_rating and vote_count are NULL
  when the source cell is ``\\N``.
- title_crew is the normalized crew representation: one row per
  (title, person, role). Role is ``director`` or ``writer``. A person who did
  both jobs has two rows. ``\\N`` or an empty director/writer list is stored as
  no rows, not as a NULL person id. person_id is the IMDb nconst. This phase
  does not load name.basics, so person names are not stored.
- ingest_state records the subset limit and completion flag. It is not title data.

Engine differences:

- SQLite is_adult is INTEGER 0/1 (or NULL). PostgreSQL is_adult is BOOLEAN
  (or NULL). The application writes Python bool values, which each driver stores
  in its native form, and reads both forms back as the same logical flag.
- SQLite average_rating is REAL. PostgreSQL average_rating is DOUBLE PRECISION.
  Both are binary floats. Verification compares the one-decimal IMDb rendering,
  not raw float bits.
- SQLite enforces foreign keys only after ``PRAGMA foreign_keys = ON`` on that
  connection. This application sets the pragma every time it opens the file.
  Other SQLite tools can insert orphans unless they set it too. PostgreSQL
  enforces foreign keys by default.
- SQLite journal mode is WAL so the file can be reopened after the process exits.
  The database is never opened as ``:memory:``.
"""

from __future__ import annotations

from object_graph_performance_lab.db import Target

_SQLITE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS titles (
        title_id TEXT PRIMARY KEY,
        name TEXT,
        type TEXT,
        release_year INTEGER,
        runtime_minutes INTEGER,
        is_adult INTEGER CHECK (is_adult IS NULL OR is_adult IN (0, 1))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ratings (
        title_id TEXT PRIMARY KEY,
        average_rating REAL,
        vote_count INTEGER,
        FOREIGN KEY (title_id) REFERENCES titles (title_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS title_crew (
        title_id TEXT NOT NULL,
        person_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('director', 'writer')),
        PRIMARY KEY (title_id, person_id, role),
        FOREIGN KEY (title_id) REFERENCES titles (title_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingest_state (
        state_key TEXT PRIMARY KEY,
        state_value TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_titles_type ON titles (type)",
    "CREATE INDEX IF NOT EXISTS idx_titles_release_year ON titles (release_year)",
    "CREATE INDEX IF NOT EXISTS idx_ratings_vote_count ON ratings (vote_count)",
    "CREATE INDEX IF NOT EXISTS idx_title_crew_person_id ON title_crew (person_id)",
    "CREATE INDEX IF NOT EXISTS idx_title_crew_role ON title_crew (role)",
)

_POSTGRES_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS titles (
        title_id TEXT PRIMARY KEY,
        name TEXT,
        type TEXT,
        release_year INTEGER,
        runtime_minutes INTEGER,
        is_adult BOOLEAN
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ratings (
        title_id TEXT PRIMARY KEY,
        average_rating DOUBLE PRECISION,
        vote_count INTEGER,
        FOREIGN KEY (title_id) REFERENCES titles (title_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS title_crew (
        title_id TEXT NOT NULL,
        person_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('director', 'writer')),
        PRIMARY KEY (title_id, person_id, role),
        FOREIGN KEY (title_id) REFERENCES titles (title_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingest_state (
        state_key TEXT PRIMARY KEY,
        state_value TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_titles_type ON titles (type)",
    "CREATE INDEX IF NOT EXISTS idx_titles_release_year ON titles (release_year)",
    "CREATE INDEX IF NOT EXISTS idx_ratings_vote_count ON ratings (vote_count)",
    "CREATE INDEX IF NOT EXISTS idx_title_crew_person_id ON title_crew (person_id)",
    "CREATE INDEX IF NOT EXISTS idx_title_crew_role ON title_crew (role)",
)


def statements(kind: str) -> tuple[str, ...]:
    if kind == "sqlite":
        return _SQLITE_STATEMENTS
    if kind == "postgres":
        return _POSTGRES_STATEMENTS
    raise ValueError(f"unknown database kind {kind}")


def ensure_schema(target: Target) -> None:
    """Create tables and indexes if they are missing. Does not drop or update rows."""
    for statement in statements(target.kind):
        target.execute(statement)
    target.commit()
