"""Per-project answer cache for ``knowledge ask``.

Keyed on ``(project_id, sha256(query|kind|lang|top_k|schema_version), git HEAD sha)``.
One-hour TTL; bulk-wiped per project whenever the indexer mutates any
chunk in that project.

Deliberately NOT keyed on ``git status --porcelain``. The agent's
in-flight edits would cause pathological misses otherwise — the cache is
for query-side acceleration, not a correctness guarantee against
unstaged edits.

Caches the **pre-rerank** result list only. Rerank is cheap (map lookups
+ arithmetic) and its inputs (recent git log, session stage) change over
time, so we always apply it fresh on each call.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from . import config, db
from .db import Connection
from .search import SearchResult


_TTL_SECONDS = 3600  # 1 hour


def compute_key(
    query: str,
    kind: str | None,
    lang: str | None,
    top_k: int,
) -> str:
    """Stable hash for cache lookup.

    Includes ``config.SCHEMA_VERSION`` so any schema bump auto-invalidates
    cached answers without a separate clear step.
    """
    raw = f"{query}|{kind or ''}|{lang or ''}|{top_k}|{config.SCHEMA_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_head_sha(root: Path) -> str:
    """Return ``git HEAD`` SHA, or empty string if not available.

    Empty string is a valid cache key too — a non-git directory's cache
    is invalidated by every ``knowledge update`` via the project wipe.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def get(
    conn: Connection,
    project_id: int,
    query_hash: str,
    head_sha: str,
) -> list[SearchResult] | None:
    """Return cached ``SearchResult`` list, or ``None`` on miss/expired."""
    now = time.time()
    row = db.fetch_one(
        conn,
        "SELECT result_json FROM query_cache "
        "WHERE project_id = ? AND query_hash = ? AND head_sha = ? "
        "  AND expires_at > ?",
        (project_id, query_hash, head_sha, now),
    )
    if row is None:
        return None
    try:
        items = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        # Poisoned row — silently treat as miss; next put() overwrites it.
        return None
    return [SearchResult(**d) for d in items]


def put(
    conn: Connection,
    project_id: int,
    query_hash: str,
    head_sha: str,
    results: list[SearchResult],
) -> None:
    """Persist the pre-rerank result list with 1h TTL.

    Idempotent: re-caching the same key overwrites the prior entry,
    refreshing the TTL. ``created_at`` tracks the latest write.
    """
    now = time.time()
    expires = now + _TTL_SECONDS
    # SearchResult is a NamedTuple; _asdict() is stable.
    payload = json.dumps([r._asdict() for r in results], default=str)
    # ON CONFLICT ... DO UPDATE is supported by both SQLite (>=3.24) and
    # PostgreSQL with identical syntax. The ``excluded`` pseudo-table works
    # the same on both.
    db.execute(
        conn,
        "INSERT INTO query_cache(project_id, query_hash, head_sha, "
        "result_json, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(project_id, query_hash, head_sha) DO UPDATE SET "
        "  result_json = excluded.result_json, "
        "  created_at  = excluded.created_at, "
        "  expires_at  = excluded.expires_at",
        (project_id, query_hash, head_sha, payload, now, expires),
    )


def wipe_project(conn: Connection, project_id: int) -> int:
    """Drop all cached answers for a project. Returns rows deleted.

    Called from the indexer whenever chunk state changes — pre-rerank
    results embed chunk ids + file paths, so stale chunks yield stale
    citations.
    """
    db.execute(
        conn, "DELETE FROM query_cache WHERE project_id = ?", (project_id,)
    )
    # Row count: APSW exposes ``Connection.changes()`` for the last
    # statement; psycopg only exposes ``rowcount`` on the cursor — and
    # ``db.execute`` already discarded that cursor. Caller doesn't use
    # the return value for any control flow, so ``0`` is a safe stand-in
    # for the PG path.
    return conn.changes() if hasattr(conn, "changes") else 0


def sweep_expired(conn: Connection) -> int:
    """Drop rows past their TTL. Cheap opportunistic housekeeping."""
    db.execute(
        conn,
        "DELETE FROM query_cache WHERE expires_at < ?",
        (time.time(),),
    )
    return conn.changes() if hasattr(conn, "changes") else 0
