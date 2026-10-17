"""One connection per operation, for SQLite and optional Postgres.

SQLite foreign keys are off unless this module turns them on. Every connection
opened here runs ``PRAGMA foreign_keys = ON``. The database file is always the
on-disk path the caller passes (the CLI uses data/local/imdb.sqlite).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import pathname2url

from object_graph_performance_lab.config import (
    database_url,
    redact_database_url,
    scrub_secret,
    sqlite_path,
)
from object_graph_performance_lab.errors import LabError


@dataclass
class Target:
    kind: str
    conn: Any
    label: str

    def sql(self, statement: str) -> str:
        if self.kind == "postgres":
            return statement.replace("?", "%s")
        return statement

    def execute(self, statement: str, params: tuple = ()):
        return self.conn.execute(self.sql(statement), params)

    def fetchone(self, statement: str, params: tuple = ()):
        return self.execute(statement, params).fetchone()

    def fetchall(self, statement: str, params: tuple = ()):
        return self.execute(statement, params).fetchall()

    def settle(self) -> None:
        """End a read transaction so the next write is its own commit."""
        if self.kind == "sqlite":
            if self.conn.in_transaction:
                self.conn.commit()
            return
        from psycopg.pq import TransactionStatus

        status = self.conn.info.transaction_status
        if status == TransactionStatus.IDLE:
            return
        if status == TransactionStatus.INERROR:
            self.conn.rollback()
            return
        self.conn.commit()

    def write_many(self, statement: str, rows: list[tuple]) -> None:
        """Insert one batch in a single transaction. Roll it back on failure."""
        if not rows:
            return
        translated = self.sql(statement)
        try:
            if self.kind == "sqlite":
                self.settle()
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.executemany(translated, rows)
                self.conn.commit()
            else:
                self.settle()
                with self.conn.transaction():
                    with self.conn.cursor() as cursor:
                        cursor.executemany(translated, rows)
        except Exception:
            if self.kind == "sqlite":
                self.conn.rollback()
            raise

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()


def connect_sqlite(path: Path, *, readonly: bool = False) -> Target:
    if readonly:
        if not path.is_file():
            raise LabError(
                f"SQLite database not found at {path}. Run init, then ingest."
            )
        uri = f"file:{pathname2url(str(path))}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    fk = conn.execute("PRAGMA foreign_keys").fetchone()
    if fk is None or fk[0] != 1:
        conn.close()
        raise LabError(f"SQLite refused to enable foreign keys for {path}")
    return Target(kind="sqlite", conn=conn, label=f"sqlite {path}")


def connect_postgres(url: str) -> Target:
    try:
        import psycopg
    except ImportError as exc:
        raise LabError(
            "psycopg is not installed. Install the project with "
            "`python -m pip install -e .` and try again."
        ) from exc
    described = redact_database_url(url)
    try:
        conn = psycopg.connect(url, connect_timeout=20)
    except Exception as exc:
        detail = scrub_secret(str(exc), url)
        raise LabError(
            "PostgreSQL connection failed. No rows were changed by this attempt.\n"
            f"Reached for: {described}\n"
            f"Error: {detail}\n"
            "Check DATABASE_URL, that the Neon project is running, and network "
            "access. Then retry the same command."
        ) from exc
    return Target(kind="postgres", conn=conn, label=f"postgres {described}")


def probe(target: Target) -> str:
    """Run SELECT 1 and describe which database answered."""
    row = target.fetchone("SELECT 1")
    if row is None or row[0] != 1:
        raise LabError(f"{target.label}: SELECT 1 did not return 1")
    if target.kind == "sqlite":
        return f"{target.label}: SELECT 1 ok"
    identity = target.fetchone("SELECT current_database(), current_user")
    database_name = identity[0] if identity else "?"
    user = identity[1] if identity else "?"
    return f"{target.label}: SELECT 1 ok (database {database_name}, user {user})"


def open_sqlite_target(*, readonly: bool) -> Target:
    return connect_sqlite(sqlite_path(), readonly=readonly)


def open_postgres_target() -> Target | None:
    """Connect when DATABASE_URL is set. Return None when it is missing.

    A set but unreachable URL raises LabError. A missing URL does not.
    """
    url = database_url()
    if url is None:
        return None
    return connect_postgres(url)
