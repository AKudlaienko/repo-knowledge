"""Filesystem locations.

Everything lives under ``~/.knowledge/`` by default. Override via the
``KNOWLEDGE_HOME`` env var (useful for tests and isolated runs).
"""

from __future__ import annotations

import hashlib
import os
import time
from functools import lru_cache
from pathlib import Path


def user_dir() -> Path:
    """Return ``~/.knowledge/`` (creating it on first access)."""
    override = os.environ.get("KNOWLEDGE_HOME")
    root = Path(override).expanduser() if override else Path.home() / ".knowledge"
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    """Single SQLite DB shared by all projects."""
    return user_dir() / "index.sqlite"


def models_dir() -> Path:
    """sentence-transformers cache."""
    p = user_dir() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    """Laptop-default config file location (``~/.knowledge.yaml``).

    This is the file ``knowledge config init`` writes by default. At runtime
    the same file is also discoverable via the cwd walk-up done in
    :func:`knowledge.settings.load_settings` — every search ends here when
    nothing closer to the cwd has a ``.knowledge.yaml``. Same shape as the
    in-repo file so callers can move config from one scope to the other
    by copying.

    Note: this lives at ``$HOME/.knowledge.yaml`` (a *file* in home), not
    inside ``$HOME/.knowledge/`` (the state directory holding the sqlite
    DB, models cache, and stage files). Two distinct paths, two distinct
    purposes.
    """
    return Path.home() / ".knowledge.yaml"


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


def root_sidecar_path(project_dir: Path) -> Path:
    """``.root`` sidecar holding the absolute repo path for ``project_dir``.

    Written on first append so ingest can map a stage subdir back to its
    project without re-hashing every registered root.
    """
    return project_dir / ".root"


def iter_stage_project_dirs() -> list[Path]:
    """List project-stage subdirs under ``stage_dir()``. Non-recursive."""
    root = stage_dir()
    if not root.exists():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir())
