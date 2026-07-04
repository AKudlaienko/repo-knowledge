"""Filesystem locations.

Everything lives under ``~/.knowledge/`` by default. Override via the
``KNOWLEDGE_HOME`` env var (useful for tests and isolated runs).
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
from functools import lru_cache
from pathlib import Path


def user_dir() -> Path:
    """Return ``~/.knowledge/`` (creating it on first access)."""
    override = os.environ.get("KNOWLEDGE_HOME")
    root = Path(override).expanduser() if override else Path.home() / ".knowledge"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # M4: mkdir's mode is masked by the umask and ignored when the dir already
    # exists, so enforce 0o700 explicitly. This dir holds the index (cached
    # source), the model cache, and buffered work-notes/decisions; a 0o700 gate
    # on the parent keeps every file underneath unreadable by other local users
    # on a shared/multi-user host even if individual files are 0o644.
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def db_path() -> Path:
    """Single SQLite DB shared by all projects."""
    return user_dir() / "index.sqlite"


def models_dir() -> Path:
    """sentence-transformers cache."""
    p = user_dir() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def pg_types_cache_path() -> Path:
    """``~/.knowledge/pg_types_cache.json`` — cached pgvector type OIDs.

    Keyed by sha256(host|port|dbname) so one file can hold entries for every
    PostgreSQL target this laptop has connected to. See
    ``backends/postgres.py`` module docstring for the invalidation story.
    """
    return user_dir() / "pg_types_cache.json"


# Project-scoped config file name. Dropped into a repo root (or any cwd
# ancestor); the file *closer to the cwd* wins over the laptop default.
PROJECT_CONFIG_NAME = ".knowledge-config.json"


def home_config_path() -> Path:
    """Laptop-default config file location (``~/.knowledge/config.json``).

    This is the file ``knowledge config init`` writes by default. At runtime
    it's the last stop in the resolution done by
    :func:`knowledge.settings.load_settings` — every search ends here when
    nothing closer to the cwd has a ``.knowledge-config.json``. Same JSON
    schema as the in-repo file, so config moves between scopes by copying.

    Unlike the legacy ``$HOME/.knowledge.yaml``, this lives *inside*
    ``$HOME/.knowledge/`` (the state directory holding the sqlite DB, models
    cache, and stage files) — one home for all per-laptop knowledge state.
    """
    return user_dir() / "config.json"


def stage_dir() -> Path:
    """Scratch dir for staged work-summaries awaiting ingest.

    Layout (current):

        stage/
          <project-slug>/              # one dir per project root
            .root                      # absolute path of the repo (sidecar)
            sess-<session>.jsonl       # one file per Claude session/process
          pending.jsonl                # legacy (pre-slug) — absorbed once

    Per-project dirs isolate cross-project staging; per-session files kill
    the append/truncate race between concurrent ingests (see history.py).
    """
    p = user_dir() / "stage"
    p.mkdir(parents=True, exist_ok=True)
    return p


def legacy_stage_path() -> Path:
    """Pre-slug stage file. Absorbed on first ingest after upgrade, then
    deleted. Do not write to this path in new code.
    """
    return stage_dir() / "pending.jsonl"


def _slugify_root(root: Path) -> str:
    """Stable, human-readable dir name for a repo root.

    ``<basename>-<sha1(abspath)[:8]>``. Hash makes it collision-free across
    clones at different paths; the basename keeps it greppable.
    """
    abs_path = str(root.resolve())
    digest = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:8]
    base = "".join(
        c if (c.isalnum() or c == "-") else "-"
        for c in root.name.lower()
    ).strip("-") or "proj"
    # L3: cap the basename so ``<base>-<8hex>`` stays well under the 255-byte
    # filename limit (ext4/HFS+ raise ENAMETOOLONG otherwise, breaking every
    # command that needs the project stage dir). The hash keeps it unique.
    base = base[:200]
    return f"{base}-{digest}"


@lru_cache(maxsize=1)
def _session_id() -> str:
    """Per-process session identifier.

    Prefers ``CLAUDE_SESSION_ID`` (injected by Claude Code); falls back to
    ``pid<PID>-<epoch>`` for standalone runs. Sanitized to ``[A-Za-z0-9-_]``
    and capped at 64 chars so it's safe as a filename component.
    """
    sid = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if sid:
        safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in sid)
        return safe[:64] or f"pid{os.getpid()}-{int(time.time())}"
    return f"pid{os.getpid()}-{int(time.time())}"


def project_stage_dir(root: Path) -> Path:
    """``~/.knowledge/stage/<slug>/`` for ``root``. Creates it on first use."""
    p = stage_dir() / _slugify_root(root)
    p.mkdir(parents=True, exist_ok=True)
    return p


def session_stage_file(root: Path) -> Path:
    """Per-session JSONL inside the project-stage dir."""
    return project_stage_dir(root) / f"sess-{_session_id()}.jsonl"


def outbox_file(root: Path) -> Path:
    """Failure buffer for user-authored writes (decisions/history) that
    couldn't reach the shared DB.

    One JSONL file per project under the same project-stage dir, with a
    distinct name so it never collides with the per-session ``sess-*.jsonl``
    history stage files. Drained on the next reachable ``knowledge`` command.
    """
    return project_stage_dir(root) / "outbox.jsonl"


def query_cache_dir() -> Path:
    """``~/.knowledge/cache/`` — local per-project query-cache files."""
    p = user_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def query_cache_db(root: Path) -> Path:
    """Per-project local SQLite file backing ``query_cache.py``.

    Slug derivation mirrors :func:`project_stage_dir` (same
    ``_slugify_root`` helper) so a repo root maps to a stable filename
    regardless of storage mode (local sqlite or shared_postgresql) — the
    query cache is local-only in both. Creates the parent dir; the file
    itself is created lazily by ``query_cache.py`` on first connect.
    """
    return query_cache_dir() / f"{_slugify_root(root)}.sqlite"


def root_sidecar_path(project_dir: Path) -> Path:
    """``.root`` sidecar holding the absolute repo path for ``project_dir``.

    Written on first append so ingest can map a stage subdir back to its
    project without re-hashing every registered root.
    """
    return project_dir / ".root"


def daemon_dir() -> Path:
    """``~/.knowledge/daemon/`` — embedder-daemon socket + log (Item F).

    Path-only; does NOT create or validate permissions. Callers that intend
    to actually use the socket must go through
    :func:`ensure_daemon_dir_safe`, which is the single place perms/symlink
    checks happen (server startup and, transitively, the client's
    connect-or-spawn decision).
    """
    return user_dir() / "daemon"


def ensure_daemon_dir_safe() -> Path | None:
    """Create (0700) or validate ``daemon_dir()``; return ``None`` if unsafe.

    Mirrors the care taken with ``pg_types_cache_path()`` writes, but goes
    one step further: rather than silently re-chmod'ing a looser-permission
    or symlinked dir back to 0700 (which could paper over a planted symlink
    on a shared host), an existing dir that isn't a real 0700 directory is
    treated as untrusted and rejected outright. Callers (client and server)
    must fall back to the local in-process embedder in that case rather
    than touching the socket.
    """
    p = daemon_dir()
    if p.is_symlink():
        return None
    if p.exists():
        try:
            st = p.stat()
        except OSError:
            return None
        if not stat.S_ISDIR(st.st_mode):
            return None
        if stat.S_IMODE(st.st_mode) & 0o077:
            return None
        return p
    try:
        p.mkdir(parents=True, mode=0o700, exist_ok=False)
    except OSError:
        return None
    try:
        p.chmod(0o700)  # belt-and-suspenders — mkdir's mode is umask-masked
    except OSError:
        pass
    return p


def daemon_socket_path() -> Path:
    """``~/.knowledge/daemon/embed.sock`` — the embedder daemon's Unix socket."""
    return daemon_dir() / "embed.sock"


def daemon_log_path() -> Path:
    """``~/.knowledge/daemon/daemon.log`` — stdout/stderr of a spawned daemon."""
    return daemon_dir() / "daemon.log"


def iter_stage_project_dirs() -> list[Path]:
    """List project-stage subdirs under ``stage_dir()``. Non-recursive."""
    root = stage_dir()
    if not root.exists():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir())
