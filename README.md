# object-graph-performance-lab

An object-graph performance lab. Later work stays in this same repository.

## Local setup

Requires Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
```

On Windows, activate the virtual environment with `.venv\Scripts\activate`.

## What stays out of Git

Secrets, downloaded datasets, SQLite databases, and generated benchmark artifacts are not committed. Copy `.env.example` to `.env` for local settings. `.env` is gitignored, and the example file contains placeholder keys only.
