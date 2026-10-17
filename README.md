# object-graph-performance-lab

An independent public-data demonstration. It loads a subset of the [IMDb non-commercial datasets](https://developer.imdb.com/non-commercial-datasets/) into local SQLite and, when you configure one, a remote Neon Postgres database. Later work stays in this same repository.

This project is not a reproduction of an employer's systems. It does not ship benchmark numbers.

Phase 1 is the download, schema, ingest, consistency check, and inspect path. Later phases will cover SQLite versus remote Postgres, Python filtering versus SQL pushdown, and Django ORM versus raw SQL. Those are not implemented.

## What Phase 1 stores

IMDb publishes gzip-compressed TSV files. This phase uses three of them:

- `title.basics.tsv.gz` for the title row
- `title.ratings.tsv.gz` for the rating row
- `title.crew.tsv.gz` for directors and writers

`\N` in a scalar field becomes SQL NULL. An empty director or writer list becomes no crew rows, not a NULL person id.

The subset is deterministic. It is the first N distinct titles in `title.basics`, in the file's published order, plus the ratings and crew rows for those same title ids. N defaults to 10000. `--limit 0` reads the entire basics file, which is the supported path for a load of at least one million titles. SQLite and Postgres are loaded from that one id set. They are not sampled separately.

The files are refreshed by IMDb. A new download can change which rows fall in a prefix. `verify` reports a size change since ingest.

Dataset files are cached locally and are not part of this repository. Use them under IMDb's non-commercial terms. Do not redistribute them.

## Install

Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
```

On Windows, activate with `.venv\Scripts\activate`.

The command entry point is:

```bash
python -m object_graph_performance_lab.cli --help
```

## Neon and `.env`

SQLite works with no credentials. Postgres is used only when `DATABASE_URL` is set.

1. Create a Neon project at [neon.tech](https://neon.tech).
2. In the Neon dashboard, copy the Postgres connection URI for the database you want to use.
3. From the repo root, copy the example env file and paste that URI into `.env` as `DATABASE_URL`. Leave the value empty in `.env.example`. Do not commit `.env`.

```bash
cp .env.example .env
```

This repository does not include a connection string. `init` runs `SELECT 1` and prints the database name, user, and host. It does not print the password.

If `DATABASE_URL` is unset, `init`, `ingest`, `verify`, and `inspect` still run against SQLite and say that Postgres was skipped.

## Download, init, and populate

```bash
python -m object_graph_performance_lab.cli download
python -m object_graph_performance_lab.cli init
python -m object_graph_performance_lab.cli ingest --limit 10000
```

`download` caches the three files under `data/raw/`. A cached file that still reads as gzip is kept. `download --force` replaces it.

`init` creates tables and indexes. It does not delete rows. SQLite is always `data/local/imdb.sqlite` on disk, never `:memory:`. Foreign keys are enabled on every SQLite connection this program opens.

`ingest` loads the same subset into SQLite and into Postgres when `DATABASE_URL` is set. One connection is kept open for each database for the whole load. Inserts are batched and parameterized. Re-running the same `--limit` is safe: existing keys are left unchanged. A different `--limit` is refused, because ingest will not delete the rows that a previous limit already stored.

Other sizes:

```bash
python -m object_graph_performance_lab.cli ingest --limit 100000
python -m object_graph_performance_lab.cli ingest --limit 1000000
python -m object_graph_performance_lab.cli ingest --limit 0
```

Those commands require a database that is empty or already recorded with that same limit. Change the limit with the reset below.

## Verify and inspect

```bash
python -m object_graph_performance_lab.cli verify
python -m object_graph_performance_lab.cli inspect
```

`verify` compares each configured database with the subset recorded at ingest. It exits nonzero on a mismatch. It always checks counts, primary-key uniqueness, and foreign-key orphans, and it prints a few representative titles.

For subsets of at most 100,000 titles, it also compares every stored field. For larger subsets it uses an order-independent checksum of every stored row, plus a fixed sample (the first three selected titles and the last). The large-set checksum covers every stored row, but it does not name the column that differs, and it is not a proof against a crafted hash collision. The sample is not a proof about the rows that were not sampled. Neither mode checks original title, end year, genres, or person names, because those values are not stored. Ratings are compared at one decimal place, which is the precision IMDb publishes, not as raw float bits.

`inspect` prints the title count, rating count, crew-relationship count, a few example rows, the recorded subset size, and whether each connection answered `SELECT 1`. It does not measure latency.

## If ingest fails

Each batch is one transaction. A failed batch is rolled back. Earlier batches stay. SQLite is written first, so Postgres can be one batch behind. The command stops instead of treating the two databases as matched.

Resume with the same limit. Keys that are already present are not overwritten:

```bash
python -m object_graph_performance_lab.cli ingest --limit 10000
python -m object_graph_performance_lab.cli verify
```

A connection failure before any insert leaves both databases unchanged. Fix `DATABASE_URL` or the network and run the command again.

## Safe reset

`reset` without `--yes` only prints what it would delete.

```bash
python -m object_graph_performance_lab.cli reset --yes
python -m object_graph_performance_lab.cli init
python -m object_graph_performance_lab.cli ingest --limit 10000
```

`reset --yes` deletes `data/local/imdb.sqlite` (and its WAL files) and drops the Phase 1 tables on Postgres when `DATABASE_URL` is set. Postgres is dropped first. If that fails, the SQLite file is left in place. Cached downloads are kept unless you pass `--include-downloads`.

A SQLite browser that does not run `PRAGMA foreign_keys = ON` can insert orphan rows. `verify` still reports those orphans.

## Schema

| Column | SQLite | PostgreSQL | NULL |
| --- | --- | --- | --- |
| `titles.title_id` | TEXT primary key | TEXT primary key | never; this is the IMDb tconst |
| `titles.name`, `titles.type` | TEXT | TEXT | when the TSV cell is `\N` |
| `titles.release_year`, `titles.runtime_minutes` | INTEGER | INTEGER | when the TSV cell is `\N` |
| `titles.is_adult` | INTEGER 0/1 | BOOLEAN | when the flag is `\N`, which is not the same as false |
| `ratings.title_id` | TEXT primary key, foreign key | TEXT primary key, foreign key | never |
| `ratings.average_rating` | REAL | DOUBLE PRECISION | when the TSV cell is `\N` |
| `ratings.vote_count` | INTEGER | INTEGER | when the TSV cell is `\N` |
| `title_crew.title_id`, `person_id`, `role` | one row per title, person, and role | same | person id is never NULL |

`title_crew.role` is `director` or `writer`. A person who did both jobs has two rows. `person_id` is the IMDb nconst. Person names are not in these three files, so they are not stored. Repeated ids inside one director or writer list are stored once.

Indexes: title type, release year, vote count, person id, and role, plus the primary keys.

`ingest_state` records the subset limit, strategy, source file sizes, and whether the load completed. It is not title data.

SQLite enforces foreign keys only for connections that set the pragma. This program sets it every time it opens the file. PostgreSQL enforces them by default. SQLite uses WAL so the file can be reopened after the process exits.

## What stays out of Git

Secrets, downloaded datasets, SQLite databases, and generated benchmark artifacts are not committed. `.env.example` has placeholder keys only.
