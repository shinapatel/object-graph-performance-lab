"""Object-graph performance lab.

Phase 1 loads one deterministic IMDb subset into SQLite and optional Postgres.
Experiment 1 times those two engines. Experiment 2 times a cached Python
skeleton against SQLite query pushdown.
"""

__version__ = "0.1.0"
