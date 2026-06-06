"""PostgreSQL backend — opt-in via ``storage.mode = "shared_postgresql"``.

psycopg3 + pgvector + tsvector. Connection-per-invocation: the CLI doesn't
hold pools (re-introduce when daemon mode lands; see
``todo/01-postgresql-shared-mode.md`` "Non-goals").

Advisory locks scope every mutation to a single project so two ``knowledge
update`` runs against the same project across hosts can't corrupt the index.
The whole indexer transaction holds the lock for v1.

# TODO(shared-postgres-v2): chunk indexer txn to release the advisory lock
# between batches so concurrent ``update``s on the same project don't block
# each other on huge first builds. Trade-off: gives up build atomicity (a
# killed process leaves partial state, recovery path needed). See
# todo/01-postgresql-shared-mode.md → "Concurrency rules".
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from ..settings import Settings

# Namespace constant for ``pg_advisory_xact_lock(_LOCK_NAMESPACE, project_id)``.
# 0x6B6E6F77 == ASCII "know" — distinctive enough that conflicts with other
# tools using advisory locks on the same database are extremely unlikely
# while still fitting in the 32-bit lock-key first slot.
_LOCK_NAMESPACE = 0x6B6E6F77


class _DependencyMissing(RuntimeError):
    """Raised when psycopg/pgvector aren't installed.

    Lets callers print a helpful ``pip install repo-knowledge[postgres]``
    hint instead of an opaque ``ModuleNotFoundError``.
    """


def _require_psycopg():
    try:
        import psycopg  # type: ignore

        return psycopg
    except ImportError as exc:  # pragma: no cover - dep guard
        raise _DependencyMissing(
            "psycopg is not installed. shared_postgresql mode requires "
            "the optional 'postgres' extra: "
            "pip install -e '.[postgres]'"
        ) from exc


def _require_pgvector():
    try:
        import pgvector.psycopg  # type: ignore  # noqa: F401

        return True
    except ImportError as exc:  # pragma: no cover - dep guard
        raise _DependencyMissing(
            "pgvector is not installed. shared_postgresql mode requires "
            "pgvector for vector(384) marshalling: "
            "pip install -e '.[postgres]'"
        ) from exc


class PostgresBackend:
    """psycopg3 + pgvector adapter for the shared backend."""

    name: ClassVar[str] = "postgresql"

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings

    def connect(self) -> Any:
        from .. import settings as settings_mod

        psycopg = _require_psycopg()
        _require_pgvector()
        from pgvector.psycopg import register_vector  # type: ignore

        dsn = settings_mod.resolve_pg_dsn(self._settings)
        # autocommit=False so caller code uses ``with backend.transaction(conn):``
        # consistently with the SQLite path.
        conn = psycopg.connect(dsn, autocommit=False)
        register_vector(conn)
        return conn

    @contextmanager
    def transaction(self, conn: Any):
        # psycopg3 connection ``with`` block manages a transaction boundary
        # for us — commit on clean exit, rollback on exception. Nested
        # ``transaction()`` calls become savepoints, matching APSW
        # semantics closely enough that callers don't have to care.
        with conn.transaction():
            yield

    def advisory_lock_project(
        self, conn: Any, project_id: int, *, exclusive: bool = True
    ) -> AbstractContextManager:
        # Held for the duration of the surrounding xact — that's why this
        # MUST be called inside ``backend.transaction(conn)``. The lock
        # auto-releases on commit/rollback; no manual ``pg_advisory_unlock``
        # needed.
        return self._lock_ctx(conn, project_id, exclusive=exclusive, blocking=True)

    def try_advisory_lock_project(
        self, conn: Any, project_id: int, *, exclusive: bool = True
    ) -> bool:
        sql = (
            "SELECT pg_try_advisory_xact_lock(%s, %s)"
            if exclusive
            else "SELECT pg_try_advisory_xact_lock_shared(%s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (_LOCK_NAMESPACE, project_id))
            row = cur.fetchone()
            return bool(row and row[0])

    @contextmanager
    def _lock_ctx(self, conn: Any, project_id: int, *, exclusive: bool, blocking: bool):
        if not blocking:
            # try_advisory_lock_project handles the non-blocking variant.
            ok = self.try_advisory_lock_project(
                conn, project_id, exclusive=exclusive
            )
            if not ok:
                raise RuntimeError(
                    f"project {project_id} is being indexed by another client; retry"
                )
            yield
            return
        sql = (
            "SELECT pg_advisory_xact_lock(%s, %s)"
            if exclusive
            else "SELECT pg_advisory_xact_lock_shared(%s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (_LOCK_NAMESPACE, project_id))
        yield
        # No release — pg_advisory_xact_* releases at txn end.

    # -----------------------------------------------------------------------
    # Schema bootstrap. Used by ``knowledge db init-postgres``.
    # -----------------------------------------------------------------------

    def apply_schema(self, conn: Any) -> list[str]:
        """Run every NNN_*.sql migration in ``knowledge.schema.postgres``.

        Returns the list of file basenames that were applied. Each file is
        idempotent (uses IF NOT EXISTS), so re-running this is safe; the
        return value is mostly for human-readable output, not state tracking.
        """

        from ..schema import postgres as schema_pkg

        applied: list[str] = []
        with conn.transaction():
            for path in schema_pkg.list_migrations():
                sql = path.read_text("utf-8")
                with conn.cursor() as cur:
                    cur.execute(sql)
                applied.append(path.name)
        return applied
