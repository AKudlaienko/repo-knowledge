"""Project registry + current-repo detection.

A "project" is one indexed repo. Identity rules differ by backend:

* SQLite (single-laptop default): keyed by canonicalized absolute
  ``root_path`` — the historical behavior. Two clones of the same repo
  at different paths are distinct rows with the same default name.
* PostgreSQL (shared mode): keyed by **normalized git remote**
  (``git_remote_normalized``) so the same repo cloned at different paths
  on different laptops collapses to one row. Falls back to ``root_path``
  uniqueness when the repo has no ``.git`` remote (loose directory).
  The PG schema enforces this with partial unique indexes — see
  ``knowledge/schema/postgres/001_init.sql``.

APSW note (sqlite path): there are no ``.commit()`` calls — APSW
auto-commits outside explicit transaction blocks. The PG path uses
``backend.transaction(conn)`` from feature-module callers when atomicity
matters; the read paths in this module rely on psycopg's autocommit-off
default which still commits on connection close.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from . import db
from .db import Connection


class Project(NamedTuple):
    id: int
    name: str
    root_path: Path
    git_remote: str | None
    created_at: float
    last_build: float | None
    last_update: float | None
    file_count: int
    chunk_count: int


class AmbiguousProjectName(Exception):
    """Raised when a project-name selector matches multiple rows.

    ``name`` is only a display label; the canonical key is
    ``root_path`` (sqlite) or ``git_remote_normalized`` (postgres).
    Callers must disambiguate by passing an absolute root path.
    """

    def __init__(self, name: str, matches: list[Project]) -> None:
        self.name = name
        self.matches = matches
        super().__init__(
            f"project name '{name}' matches {len(matches)} projects"
        )


_SELECT_COLS = (
    "id, name, root_path, git_remote, created_at, last_build, "
    "last_update, file_count, chunk_count"
)


def current_project_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (default cwd) until we find a ``.git/`` dir.

    Falls back to the start directory if no git root is found — callers that
    require a git repo should check explicitly.
    """
    p = (start or Path.cwd()).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p


def _git_remote(root: Path) -> str | None:
    """Best-effort origin URL; ``None`` if not a git repo or no origin set."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


# Match ssh-style git URLs like ``git@github.com:org/repo`` so we can
# rewrite them to a stable ``https://github.com/org/repo`` form. Two
# teammates pulling the same repo over different transports (https vs
# ssh) end up with the same normalized key.
_SSH_GIT_RX = re.compile(r"^([^@]+)@([^:]+):(.+)$")


def normalize_git_remote(url: str | None) -> str | None:
    """Return the canonical form of a git remote URL, or ``None``.

    Normalization steps:

    * Strip embedded credentials: ``https://user:pass@host/...`` →
      ``https://host/...``
    * Rewrite ``git@host:org/repo`` → ``https://host/org/repo``
    * Strip trailing ``.git``
    * Lowercase the host portion (path stays case-sensitive — GitHub
      isn't, but other forges are)

    A ``None`` input returns ``None`` so callers can pass through the
    "no remote configured" case without branching.
    """

    if not url:
        return None
    s = url.strip()
    if not s:
        return None

    m = _SSH_GIT_RX.match(s)
    if m:
        # ssh form (user@host:path) — drop the user, switch to https.
        _user, host, path = m.group(1), m.group(2), m.group(3)
        s = f"https://{host}/{path}"

    # Strip embedded credentials in https/git URLs:
    # ``https://user:pass@host/...`` → ``https://host/...``
    s = re.sub(r"^(https?://)([^/@]+@)", r"\1", s)
    s = re.sub(r"^(git://)([^/@]+@)", r"\1", s)

    # Lowercase the host. Split scheme + rest, then host + path.
    if "://" in s:
        scheme, rest = s.split("://", 1)
        if "/" in rest:
            host, tail = rest.split("/", 1)
            s = f"{scheme}://{host.lower()}/{tail}"
        else:
            s = f"{scheme}://{rest.lower()}"

    if s.endswith(".git"):
        s = s[: -len(".git")]
    return s


def get_or_create_project(
    conn: Connection,
    root: Path,
    name_override: str | None = None,
) -> Project:
    """Return the project row for ``root``, creating it if missing.

    SQLite lookup keys on ``root_path``. PostgreSQL prefers
    ``git_remote_normalized`` (so two clones of the same repo at
    different paths share one row), falling back to ``root_path`` when
    the repo has no remote.
    """
    root = root.resolve()
    remote = _git_remote(root)
    norm = normalize_git_remote(remote)

    if db.current_mode() == "postgresql" and norm is not None:
        row = db.fetch_one(
            conn,
            f"SELECT {_SELECT_COLS} FROM projects "
            "WHERE git_remote_normalized = ?",
            (norm,),
        )
    else:
        row = db.fetch_one(
            conn,
            f"SELECT {_SELECT_COLS} FROM projects WHERE root_path = ?",
            (str(root),),
        )
    if row:
        return _row_to_project(row)

    name = name_override or root.name
    now = time.time()
    if db.current_mode() == "postgresql":
        new_id = db.execute_returning_id(
            conn,
            "INSERT INTO projects(name, root_path, git_remote, "
            "git_remote_normalized, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, str(root), remote, norm, now),
        )
    else:
        new_id = db.execute_returning_id(
            conn,
            "INSERT INTO projects(name, root_path, git_remote, created_at) "
            "VALUES (?, ?, ?, ?)",
            (name, str(root), remote, now),
        )
    return Project(
        id=new_id,
        name=name,
        root_path=root,
        git_remote=remote,
        created_at=now,
        last_build=None,
        last_update=None,
        file_count=0,
        chunk_count=0,
    )


def resolve_project(
    conn: Connection,
    selector: str | None,
) -> Project | None:
    """Resolve a project by name or absolute path. Returns None if unknown.

    When ``selector`` is None, uses the current git root (cwd-based). Does
    NOT create the project — use ``get_or_create_project`` for that.

    Raises :class:`AmbiguousProjectName` if a non-absolute ``selector``
    matches more than one project — e.g. the same repo built from two
    clones. Callers must present the colliding roots and re-query with an
    absolute path.
    """
    if selector is None:
        root = current_project_root()
        row = db.fetch_one(
            conn,
            f"SELECT {_SELECT_COLS} FROM projects WHERE root_path = ?",
            (str(root),),
        )
        return _row_to_project(row) if row else None

    p = Path(selector).expanduser()
    if p.is_absolute():
        row = db.fetch_one(
            conn,
            f"SELECT {_SELECT_COLS} FROM projects WHERE root_path = ?",
            (str(p.resolve()),),
        )
        return _row_to_project(row) if row else None

    rows = db.fetch_all(
        conn,
        f"SELECT {_SELECT_COLS} FROM projects WHERE name = ?",
        (selector,),
    )
    if not rows:
        return None
    if len(rows) > 1:
        raise AmbiguousProjectName(
            selector, [_row_to_project(r) for r in rows]
        )
    return _row_to_project(rows[0])


def list_projects(conn: Connection) -> list[Project]:
    rows = db.fetch_all(
        conn, f"SELECT {_SELECT_COLS} FROM projects ORDER BY name"
    )
    return [_row_to_project(r) for r in rows]


def list_projects_by_name(conn: Connection, name: str) -> list[Project]:
    """All rows sharing ``name``. Multiple rows = same-named repos at
    different roots — legal, but ambiguous for name-based selectors.
    """
    rows = db.fetch_all(
        conn,
        f"SELECT {_SELECT_COLS} FROM projects WHERE name = ?",
        (name,),
    )
    return [_row_to_project(r) for r in rows]


def next_free_suffix(conn: Connection, base: str) -> str:
    """Return ``f"{base}_{N}"`` with the smallest N >= 2 that's unused.

    Used at build time when the requested short name collides with an
    existing project and the user wants to keep both.
    """
    taken = {r[0] for r in db.fetch_all(conn, "SELECT name FROM projects")}
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def forget_project(conn: Connection, project_id: int) -> None:
    """Cascade-delete a project and all its data.

    SQLite path: vec0 virtual tables (``chunks_vec`` / ``history_vec`` /
    ``decisions_vec``) don't participate in FK cascade, so we wipe them
    explicitly before dropping the project row. ``history.id`` reuses
    rowids, so leaving ``history_vec`` orphans corrupts the next ingest
    (see project memory ``project_vec0_cleanup_convention``).

    PostgreSQL path: side tables (``chunk_embeddings`` / ``history_embeddings`` /
    ``decision_embeddings``) are real tables with ``ON DELETE CASCADE`` —
    the projects-row delete sweeps them automatically.
    """
    if db.current_mode() == "sqlite":
        conn.execute(
            "DELETE FROM chunks_vec WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE project_id = ?)",
            (project_id,),
        )
        conn.execute(
            "DELETE FROM history_vec WHERE history_id IN "
            "(SELECT id FROM history WHERE project_id = ?)",
            (project_id,),
        )
        conn.execute(
            "DELETE FROM decisions_vec WHERE decision_id IN "
            "(SELECT id FROM decisions WHERE project_id = ?)",
            (project_id,),
        )
    db.execute(conn, "DELETE FROM projects WHERE id = ?", (project_id,))


def update_counts(conn: Connection, project_id: int) -> None:
    """Refresh ``file_count`` + ``chunk_count`` denormals after mutation."""
    fc = db.fetch_one(
        conn,
        "SELECT COUNT(*) FROM files WHERE project_id = ?",
        (project_id,),
    )[0]
    cc = db.fetch_one(
        conn,
        "SELECT COUNT(*) FROM chunks WHERE project_id = ?",
        (project_id,),
    )[0]
    db.execute(
        conn,
        "UPDATE projects SET file_count = ?, chunk_count = ? WHERE id = ?",
        (fc, cc, project_id),
    )


def _row_to_project(row) -> Project:
    return Project(
        id=row[0],
        name=row[1],
        root_path=Path(row[2]),
        git_remote=row[3],
        created_at=row[4],
        last_build=row[5],
        last_update=row[6],
        file_count=row[7],
        chunk_count=row[8],
    )
