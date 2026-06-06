"""Per-project migration tools.

Currently a single direction: SQLite → PostgreSQL. Bidirectional sync is
explicitly out of scope (see ``todo/01-postgresql-shared-mode.md`` →
"Non-goals").

Usage from the CLI: ``knowledge db migrate --project <name|abs-path>``.
The implementation lives in :mod:`knowledge.migrate.sqlite_to_pg`.
"""

from .sqlite_to_pg import (
    EmbeddingModelMismatch,
    MigrationConflict,
    MigrationError,
    MigrationPlan,
    execute,
    prepare,
)

__all__ = (
    "EmbeddingModelMismatch",
    "MigrationConflict",
    "MigrationError",
    "MigrationPlan",
    "execute",
    "prepare",
)
