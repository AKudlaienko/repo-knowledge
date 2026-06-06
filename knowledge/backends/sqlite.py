"""SQLite backend — the historical default.

Wraps :func:`knowledge.db.connect` (APSW + sqlite-vec) without changing its
behavior. The advisory-lock methods are no-ops because SQLite already
serializes writers via its journal and APSW exposes ``BUSY_TIMEOUT``; we
never need cross-process locks for the single-laptop use case.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any, ClassVar


class SqliteBackend:
    """Adapter around the existing :func:`knowledge.db.connect` flow."""

    name: ClassVar[str] = "sqlite"

    def connect(self) -> Any:
        # Local import to avoid a circular module-load between
        # ``backends`` and ``db`` (db.get_backend dispatches via
        # ``backends.load_backend``).
        from .. import db

        return db.connect()

    @contextmanager
    def transaction(self, conn: Any):
        # APSW: ``with conn`` is an implicit savepoint that commits on
        # successful exit and rolls back on exception. We delegate to it.
        with conn:
            yield

    def advisory_lock_project(
        self, conn: Any, project_id: int, *, exclusive: bool = True
    ) -> AbstractContextManager:
        # SQLite has no cross-process advisory lock primitive; the file
        # journal already serializes writers. Returning ``nullcontext``
        # lets callers use the same ``with backend.advisory_lock_project(...)``
        # shape regardless of backend.
        del conn, project_id, exclusive  # explicitly unused
        return nullcontext()

    def try_advisory_lock_project(
        self, conn: Any, project_id: int, *, exclusive: bool = True
    ) -> bool:
        del conn, project_id, exclusive  # explicitly unused
        return True
