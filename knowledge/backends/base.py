"""Backend protocol — the operations that genuinely differ across engines.

Anything that doesn't appear here (e.g. SELECT chunks WHERE project_id = ?)
stays in the per-feature modules; the modules dispatch on
``backend.name`` for the handful of statements that aren't portable
verbatim. The protocol is deliberately thin to keep blast radius small in
Phase 1.

Methods that are no-ops on SQLite (advisory locks) still appear here — the
SQLite implementation just returns a context manager that does nothing, so
caller code doesn't have to special-case the backend.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, ClassVar, Protocol


class Backend(Protocol):
    """Storage engine adapter.

    Implementations:
      * :class:`knowledge.backends.sqlite.SqliteBackend`
      * :class:`knowledge.backends.postgres.PostgresBackend`
    """

    name: ClassVar[str]
    """``"sqlite"`` or ``"postgresql"``. Used by feature modules for the
    handful of dispatch points where SQL strings differ (e.g. parameter
    style, vector insert format)."""

    def connect(self) -> Any:
        """Open a fresh connection to the configured database.

        Returned object is the driver-native connection — APSW's
        ``Connection`` for SQLite, psycopg3's ``Connection`` for PG. The
        connection is the unit of transaction boundary; close it when done.
        """
        ...

    def transaction(self, conn: Any) -> AbstractContextManager:
        """Context manager that wraps a transaction.

        SQLite/APSW: ``with conn`` — savepoint that commits on exit.
        PostgreSQL/psycopg: ``conn.transaction()`` from psycopg3.
        """
        ...

    def advisory_lock_project(
        self, conn: Any, project_id: int, *, exclusive: bool = True
    ) -> AbstractContextManager:
        """Hold a project-scoped advisory lock for the duration of the txn.

        SQLite: no-op (single-writer DB; APSW journal handles concurrency).
        PostgreSQL: ``pg_advisory_xact_lock(_LOCK_NAMESPACE, project_id)``.

        Must be called *inside* an open transaction so the xact-scoped
        release semantics match across backends.
        """
        ...

    def try_advisory_lock_project(
        self, conn: Any, project_id: int, *, exclusive: bool = True
    ) -> bool:
        """Non-blocking lock acquire. ``True`` on success, ``False`` if busy.

        SQLite: always ``True`` (no-op lock). PostgreSQL: maps to
        ``pg_try_advisory_xact_lock``. Caller is responsible for raising the
        appropriate ``project busy`` error on ``False``.
        """
        ...

    def connection_error_types(self) -> tuple[type[BaseException], ...]:
        """Exception types that mean "the database was unreachable".

        Lets callers buffer user-authored writes to a local outbox instead of
        crashing when the configured backend can't be reached. SQLite returns
        ``()`` (local file — no connection-loss concept), so the buffering
        clause is a no-op there. PostgreSQL returns psycopg's connection-class
        errors.
        """
        ...
