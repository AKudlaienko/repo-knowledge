"""PostgreSQL schema for the shared backend (storage.mode = shared_postgresql).

Files in this directory are numbered (``001_init.sql``, ``002_…``) and
applied in order by ``knowledge db init-postgres``. Each file is idempotent
on its own so re-running the command is safe.
"""

from importlib import resources
from pathlib import Path


def list_migrations() -> list[Path]:
    """Return absolute paths of every ``NNN_*.sql`` migration, in order.

    Sorted lexicographically — the ``NNN_`` prefix gives stable order for
    up to 999 migrations, well past anything we'd plausibly need for a
    single-table-set schema.
    """

    files = []
    with resources.as_file(resources.files(__package__)) as base:
        for path in Path(base).glob("*.sql"):
            files.append(path)
    files.sort(key=lambda p: p.name)
    return files
