"""Storage backends.

Exactly one backend is active per ``knowledge`` invocation, selected from
a discoverable ``.knowledge.yaml`` (see :mod:`knowledge.settings` for the
walk-up + home-fallback resolution):

* :class:`SqliteBackend` — default, single-laptop, APSW + sqlite-vec.
* :class:`PostgresBackend` — opt-in (``storage.mode = "shared_postgresql"``),
  team-shared, psycopg3 + pgvector + tsvector.

Backend objects expose only the operations that *differ* between engines
(connect, transaction, advisory lock). Per-feature SQL strings stay in the
relevant module (search.py, fts.py, indexer.py, …) and dispatch on
``backend.name`` for the small number of cases that diverge.

Phase 1a (this commit) ships the scaffolding only. The dispatch wiring in
indexer/search/fts/hybrid_search lands in Phase 1b.
"""

from .base import Backend  # re-export for convenience
from .sqlite import SqliteBackend

# Import psycopg lazily — it is an optional dependency. Importing it
# eagerly here would force every sqlite-only user to install psycopg.

__all__ = ("Backend", "SqliteBackend", "load_backend")


def load_backend(settings=None) -> Backend:
    """Return the configured :class:`Backend` for the current process.

    Pulled out of :mod:`knowledge.db` so callers can choose between the
    legacy ``db.connect()`` (sqlite-only, raw APSW connection) and the
    backend abstraction without circular imports.
    """

    from .. import settings as settings_mod

    s = settings or settings_mod.load_settings()
    if s.mode == "sqlite":
        return SqliteBackend()
    if s.mode == "shared_postgresql":
        from .postgres import PostgresBackend  # local import — optional dep

        return PostgresBackend(s)
    raise RuntimeError(f"unknown storage mode: {s.mode!r}")
