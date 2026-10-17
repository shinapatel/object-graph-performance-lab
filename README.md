# object-graph-performance-lab

An independent public-data demonstration. It loads a subset of the [IMDb non-commercial datasets](https://developer.imdb.com/non-commercial-datasets/) into local SQLite and, when you configure one, a remote Neon Postgres database. Later work stays in this same repository.

This project is not a reproduction of an employer's systems or of Rippling's proprietary Object Graph. Experiment 2 is an independent, legacy-inspired reconstruction on public IMDb data. It is not Rippling's implementation, and it is not a historical measurement. The repository does not ship a canned timing table. Each benchmark command measures one run and prints those measurements.

Phase 1 is the download, schema, ingest, consistency check, and inspect path. Experiment 1 compares query latency on embedded SQLite with the same queries on remote Postgres. Experiment 2 compares a warm cached Python skeleton with warm SQLite query pushdown. Django ORM versus raw SQL is not implemented.

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

## Experiment 1: SQLite versus remote Postgres

This compares one process talking to embedded SQLite with the same process talking to Neon Postgres over the network. It is an architectural demonstration on the public IMDb subset. It is not a reproduction of Rippling's Object Graph benchmarks.

Run it after Phase 1 has loaded the same 10,000-title prefix into both databases:

```bash
python -m object_graph_performance_lab.cli benchmark --experiment local-vs-remote --iterations 50 --warmup 10
```

Optional flags: repeat `--workload` to run one workload, and pass `--json` and `--csv` to choose output paths. The default writes both files under `artifacts/benchmarks/`, which is gitignored. The command exits nonzero if Postgres is not configured, a query fails, or the two databases do not return the same rows. It does not ingest, reset, or modify stored rows.

Workloads, in order:

| Workload | What it asks | Predicate on this 10,000-title prefix |
| --- | --- | --- |
| `primary_key_lookup` | One title by id | `title_id = tt0000001` (1 row) |
| `selective_query` | A small indexed slice | `release_year = 1900` (85 rows) |
| `relationship_lookup` | Crew for one title, joined to the title | `title_id = tt0005408` (21 crew rows) |
| `larger_result_set` | A few thousand titles | `type = movie` (4,200 rows) |
| `connection_establishment` | A new connection plus `SELECT 1` | Steady state, after warmup |

The SQL text is the same on both engines. PostgreSQL receives `%s` placeholders because the driver layer rewrites `?`. `is_adult` is an integer in SQLite and a boolean in PostgreSQL; the parity check maps both to 0 or 1. Each statement has an equivalent index already created in Phase 1 (primary key, release year, title type, or the crew primary key). No index was added for this experiment.

Timings use `time.perf_counter_ns()`. Each sample covers executing the statement and fetching the rows into a Python list. The same connection is reused for the query workloads. Warmup iterations run first and are discarded. Measured iterations alternate which engine goes first. The run records median, p95, minimum, maximum, and sample standard deviation in milliseconds, plus the row count. p95 is a linear interpolation: the rank is `0.95 * (n - 1)`. A failed iteration aborts the run instead of being removed. Parity is checked before the table is printed, and again on every measured iteration.

Connection setup is a separate workload. The first connection of the process is also recorded and then left out of those steady-state samples. That first connection is not a controlled Neon scale-to-zero cold start. Letting Neon suspend, then connecting once, would be a different measurement and is not mixed into the medians.

How to read the table: `PG/SQLite` is the Postgres median divided by the SQLite median. A large ratio on the primary-key lookup usually means driver overhead and a network round trip dominate a tiny query. A smaller ratio on the larger result set means more of the time is spent moving rows, while SQLite is still reading local pages. Do not treat the ratio as pure network latency. The engines have different planners, caches, drivers, and hardware. Neon's compute size is not matched to the SQLite process. Page cache from the warmup, and from anything else that touched the database earlier in the same session, affects both sides.

JSON keeps every sample, the summary statistics, dataset counts, the parity flag, query plans, and these limitations. CSV keeps one row per measured iteration so the summaries can be recomputed.

## Experiment 2: cached Python versus SQLite

This compares two ways to serve the same local metadata.

The cached Python path loads title rows, rating fields, and crew links into a skeleton once. It keeps that skeleton for the whole run. A full-graph request returns the cached sequence. A scoped request probes a `title_id` dictionary. A contextual filter walks the cached titles and keeps references to the records that match. It does not query PostgreSQL, and it does not rebuild the skeleton per request.

The SQLite path reuses the Phase 1 database as the precomputed artifact. Each request applies the same predicate in SQL, fetches the matching rows, and constructs records only for those matches.

Both paths return the same record shape: title fields, rating fields, a flag for whether a ratings row exists, and crew pairs ordered by role then person id. The command exits nonzero when the results differ, and it does not write a success report for that run.

```bash
python -m object_graph_performance_lab.cli benchmark --experiment cached-python-vs-sqlite --iterations 50 --warmup 10
```

Optional flags: repeat `--workload` to run one workload, and pass `--json` and `--csv` to choose output paths. The default writes both files under `artifacts/benchmarks/`, which is gitignored. The command does not ingest, reset, or modify stored rows. It does not open Postgres.

Workloads, in catalog order, on the 10,000-title prefix:

| Workload | What it asks | Result size |
| --- | --- | --- |
| `full_graph` | Every stored title, with no selective predicate | 10,000 rows (100%) |
| `scoped_graph` | Every 200th title id, in title-id order | 50 rows (0.50%) |
| `filter_year_1900` | `release_year = 1900` | 85 rows (0.85%) |
| `filter_rated_since_1916` | `release_year >= 1916` and a ratings row | 1,012 rows (10.12%) |
| `filter_type_movie` | `type = movie` | 4,200 rows (42%) |

The defaults are refused when the SQLite file does not contain 10,000 titles. Rating availability means a row in `ratings`. That row can still have a NULL average and a NULL vote count. A NULL release year fails `release_year >= N` on both sides.

Timings use `time.perf_counter_ns()`. Each sample is one complete request: the Python filter or the SQL execution, the fetch, and record construction. Skeleton loading, connection setup, scoped-id selection, warmup, parity checks, and `EXPLAIN QUERY PLAN` are outside the samples. The skeleton stays in memory. Warmup iterations run first and are discarded. Measured iterations alternate which side goes first. The run records median, p95, minimum, maximum, and sample standard deviation. p95 uses rank `0.95 * (n - 1)`. A failed iteration aborts the run. The documented procedure uses at least 10 warmup iterations and at least 30 measured iterations; the comparison command above uses 50.

The terminal output also prints skeleton initialization time, traced Python allocation, and process RSS before and after the load. Initialization time includes the tracer. It is not compared with creating the SQLite file. Traced peak includes temporary row lists. Traced retained is what the skeleton still references. RSS is the whole process, including the interpreter and the SQLite page cache.

How to read `Python/SQLite`: it is the cached Python median divided by the SQLite median. A ratio above 1 means the SQLite request was faster. A ratio below 1 means the cached Python request was faster. The full-graph Python request returns an existing sequence, so it does little work. The SQLite request reads and allocates. A scoped Python request is a dictionary probe. A contextual Python filter scans every cached title; SQLite can use `idx_titles_release_year`, `idx_titles_type`, and the ratings primary key. No index was added for this experiment, and Python does not grow extra indexes for type or year.

The engines do different work once the cache exists. Python reuses objects. SQLite constructs new ones and can avoid touching non-matching rows when an index applies. Page cache from the skeleton load, and from warmup, affects SQLite. At 10,000 titles a Python scan is small. A larger catalog can move the crossover. Do not read the ratio as a claim about Rippling's Object Graph.

JSON keeps every sample, the skeleton measurements, selectivity, rows examined and materialized, query plans, and these limitations. CSV keeps one row per measured iteration.

## What stays out of Git

Secrets, downloaded datasets, SQLite databases, and generated benchmark artifacts are not committed. `.env.example` has placeholder keys only.
