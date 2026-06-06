"""Copy one project from local SQLite to the configured shared PostgreSQL.

Two-phase API:

* :func:`prepare` opens both sides, validates everything that can be
  checked without writing (project resolution, embedding-model match,
  conflict on PG, row counts), and returns a :class:`MigrationPlan`. Used
  for both the user-facing ``--dry-run`` and as a pre-flight by
  :func:`execute`.
* :func:`execute` takes that plan and copies all rows under one PG
  transaction with an advisory lock. ID remapping is exhaustive
  (every FK column gets remapped — see the ID-remap section in
  ``todo/01-postgresql-shared-mode.md``).

Atomicity story: the write transaction commits as a single unit, so a
half-finished migrate either rolls back cleanly or doesn't happen. The
source SQLite project row is **never** deleted — explicitly, so users can
re-run if PG state is wiped or compare both sides afterwards.

What's not migrated (deliberately):
* ``query_cache`` — local, short-TTL, warms naturally on PG side.
* ``meta`` — process-global versioning; PG side has its own.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config as kconfig
from ..projects import _git_remote, normalize_git_remote


class MigrationError(Exception):
    """Base class for ``execute``/``prepare`` aborts. Caller maps to exit 2."""


class MigrationConflict(MigrationError):
    """Project already on target PG (matched by git remote or root_path)."""


class EmbeddingModelMismatch(MigrationError):
    """Source SQLite was indexed under a different model than current config.

    Pgvector's ``vector(384)`` column is fixed-dimension; mixing vectors
    from different models would corrupt nearest-neighbor search silently.
    """


@dataclass(frozen=True)
class MigrationPlan:
    """Read-only snapshot of what a migrate run will copy.

    Returned by :func:`prepare`. Callers can either pass it to
    :func:`execute` or inspect/print it for ``--dry-run``.
    """

    sqlite_project_id: int
    project_name: str
    project_root: Path
    git_remote: str | None
    git_remote_normalized: str | None
    project_key_kind: str  # "git_remote" | "root_path"
    source_embedding_model: str
    target_embedding_model: str
    file_count: int = 0
    chunk_count: int = 0
    chunk_embedding_count: int = 0
    edge_count: int = 0
    history_count: int = 0
    history_embedding_count: int = 0
    decision_count: int = 0
    decision_embedding_count: int = 0
    variable_count: int = 0


# ---------------------------------------------------------------------------
# Phase 1: prepare (read-only)
# ---------------------------------------------------------------------------


def prepare(
    sqlite_conn: Any,
    pg_conn: Any,
    selector: str,
) -> MigrationPlan:
    """Resolve, validate, count. Returns a plan; no writes performed.

    ``selector`` is a project name or absolute path — same shape as
    ``knowledge ask --project``.
    """

    project_row = _resolve_source_project(sqlite_conn, selector)
    if project_row is None:
        raise MigrationError(
            f"no project named or rooted at {selector!r} in local sqlite"
        )
    pid, name, root_path, git_remote = project_row
    project_root = Path(root_path)

    # Embedding-model match: pgvector(384) is fixed-dim, mixing models
    # silently corrupts nearest-neighbor search. The current process's
    # config.MODEL is what `knowledge build` would use on PG, so the
    # source must match.
    source_model = _read_meta_string(sqlite_conn, "embedding_model") or ""
    if source_model and source_model != kconfig.MODEL:
        raise EmbeddingModelMismatch(
            f"source SQLite was indexed with model {source_model!r}; "
            f"current config uses {kconfig.MODEL!r}. Migrating would mix "
            "incompatible vectors. Re-build the source project under the "
            "current model before migrating, or align the config."
        )

    # Project key for cross-laptop dedup. We re-derive from the project
    # root rather than relying on the stored git_remote because sqlite
    # rows pre-date the git_remote_normalized column.
    fresh_remote = git_remote or _git_remote(project_root)
    norm = normalize_git_remote(fresh_remote)
    key_kind = "git_remote" if norm else "root_path"

    # Conflict check on target.
    existing = _check_pg_conflict(pg_conn, norm, str(project_root.resolve()))
    if existing is not None:
        existing_id, existing_name = existing
        raise MigrationConflict(
            f"project already on shared PG: id={existing_id} "
            f"name={existing_name!r} (matched by {key_kind}). Use "
            "`knowledge build` / `knowledge update` to refresh it; "
            "do not re-migrate."
        )

    # Row counts for the user-facing summary.
    counts = _source_counts(sqlite_conn, pid)

    return MigrationPlan(
        sqlite_project_id=pid,
        project_name=name,
        project_root=project_root,
        git_remote=fresh_remote,
        git_remote_normalized=norm,
        project_key_kind=key_kind,
        source_embedding_model=source_model or kconfig.MODEL,
        target_embedding_model=kconfig.MODEL,
        **counts,
    )


def _resolve_source_project(
    sqlite_conn: Any, selector: str
) -> tuple[int, str, str, str | None] | None:
    """Resolve ``selector`` against source SQLite. Same rules as projects.resolve_project,
    but uses raw APSW (no current_mode() dispatch — caller is talking to
    sqlite source while current_mode() may report postgresql).
    """

    p = Path(selector).expanduser()
    if p.is_absolute():
        row = sqlite_conn.execute(
            "SELECT id, name, root_path, git_remote FROM projects "
            "WHERE root_path = ?",
            (str(p.resolve()),),
        ).fetchone()
        return row

    rows = sqlite_conn.execute(
        "SELECT id, name, root_path, git_remote FROM projects WHERE name = ?",
        (selector,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        # Same name, different roots — refuse and force the user to pick.
        roots = "\n  ".join(r[2] for r in rows)
        raise MigrationError(
            f"project name {selector!r} matches {len(rows)} rows in source "
            f"sqlite. Re-run `knowledge db migrate --project <abs-path>`. "
            f"Candidates:\n  {roots}"
        )
    return rows[0]


def _read_meta_string(sqlite_conn: Any, key: str) -> str | None:
    row = sqlite_conn.execute(
        "SELECT value FROM meta WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def _check_pg_conflict(
    pg_conn: Any, git_remote_normalized: str | None, root_path: str
) -> tuple[int, str] | None:
    """Return (id, name) of an existing PG project that would collide, else None."""

    with pg_conn.cursor() as cur:
        if git_remote_normalized:
            cur.execute(
                "SELECT id, name FROM projects WHERE git_remote_normalized = %s",
                (git_remote_normalized,),
            )
        else:
            cur.execute(
                "SELECT id, name FROM projects "
                "WHERE git_remote_normalized IS NULL AND root_path = %s",
                (root_path,),
            )
        return cur.fetchone()


def _source_counts(sqlite_conn: Any, project_id: int) -> dict[str, int]:
    """Row counts for the migration summary."""

    def _count(sql: str, *params) -> int:
        row = sqlite_conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    return {
        "file_count": _count(
            "SELECT COUNT(*) FROM files WHERE project_id = ?", project_id
        ),
        "chunk_count": _count(
            "SELECT COUNT(*) FROM chunks WHERE project_id = ?", project_id
        ),
        "chunk_embedding_count": _count(
            "SELECT COUNT(*) FROM chunks_vec v "
            "JOIN chunks c ON c.id = v.chunk_id WHERE c.project_id = ?",
            project_id,
        ),
        "edge_count": _count(
            "SELECT COUNT(*) FROM file_edges WHERE project_id = ?", project_id
        ),
        "history_count": _count(
            "SELECT COUNT(*) FROM history WHERE project_id = ?", project_id
        ),
        "history_embedding_count": _count(
            "SELECT COUNT(*) FROM history_vec v "
            "JOIN history h ON h.id = v.history_id WHERE h.project_id = ?",
            project_id,
        ),
        "decision_count": _count(
            "SELECT COUNT(*) FROM decisions WHERE project_id = ?", project_id
        ),
        "decision_embedding_count": _count(
            "SELECT COUNT(*) FROM decisions_vec v "
            "JOIN decisions d ON d.id = v.decision_id WHERE d.project_id = ?",
            project_id,
        ),
        "variable_count": _count(
            "SELECT COUNT(*) FROM project_variables WHERE project_id = ?",
            project_id,
        ),
    }


# ---------------------------------------------------------------------------
# Phase 2: execute (writes)
# ---------------------------------------------------------------------------


# Cross-host serialization: two laptops migrating the same project at the
# same time would race on the conflict-check / INSERT path. ``hashtext`` is
# stable across Postgres versions and gives us a deterministic 32-bit key
# from the project_key string.
_MIGRATE_LOCK_KEY_SQL = "SELECT hashtext(%s)"


def execute(
    sqlite_conn: Any,
    pg_conn: Any,
    plan: MigrationPlan,
) -> dict[str, int]:
    """Run the migration described by ``plan``. Returns row counts inserted.

    All writes happen in a single PG transaction. On any error the txn
    rolls back and the target is unchanged. The local SQLite project is
    NEVER deleted — the user can rerun, compare, or roll back themselves.
    """

    project_key = plan.git_remote_normalized or str(plan.project_root.resolve())

    counts: dict[str, int] = {}
    with pg_conn.cursor() as cur:
        # Compute and acquire the migrate lock. Held for the duration of
        # the surrounding txn (advisory_xact_lock auto-releases on commit
        # or rollback).
        cur.execute(_MIGRATE_LOCK_KEY_SQL, (f"migrate:{project_key}",))
        lock_key = cur.fetchone()[0]
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))

        # Re-check conflict under the lock — protects against a teammate
        # winning the race between prepare() and execute().
        if plan.git_remote_normalized:
            cur.execute(
                "SELECT 1 FROM projects WHERE git_remote_normalized = %s",
                (plan.git_remote_normalized,),
            )
        else:
            cur.execute(
                "SELECT 1 FROM projects "
                "WHERE git_remote_normalized IS NULL AND root_path = %s",
                (str(plan.project_root.resolve()),),
            )
        if cur.fetchone() is not None:
            raise MigrationConflict(
                "another client created the same project on PG between "
                "prepare and execute. Try again."
            )

        new_pid = _insert_project(cur, plan)
        file_id_map = _copy_files(cur, sqlite_conn, plan.sqlite_project_id, new_pid)
        chunk_id_map = _copy_chunks(
            cur, sqlite_conn, plan.sqlite_project_id, new_pid, file_id_map
        )
        emb_count = _copy_chunk_embeddings(
            cur, sqlite_conn, plan.sqlite_project_id, chunk_id_map
        )
        edge_count = _copy_file_edges(
            cur, sqlite_conn, plan.sqlite_project_id, new_pid, file_id_map
        )
        var_count = _copy_project_variables(
            cur, sqlite_conn, plan.sqlite_project_id, new_pid
        )
        history_id_map = _copy_history(
            cur, sqlite_conn, plan.sqlite_project_id, new_pid
        )
        h_emb_count = _copy_history_embeddings(
            cur, sqlite_conn, plan.sqlite_project_id, history_id_map
        )
        decision_id_map = _copy_decisions(
            cur, sqlite_conn, plan.sqlite_project_id, new_pid
        )
        d_emb_count = _copy_decision_embeddings(
            cur, sqlite_conn, plan.sqlite_project_id, decision_id_map
        )

        # Refresh denormalized counts on the new project row.
        cur.execute(
            "UPDATE projects SET file_count = %s, chunk_count = %s "
            "WHERE id = %s",
            (len(file_id_map), len(chunk_id_map), new_pid),
        )

        cur.execute(
            "INSERT INTO migration_log("
            "project_id, source, sqlite_project_id, migrated_at, note) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                new_pid,
                "sqlite",
                plan.sqlite_project_id,
                time.time(),
                f"key_kind={plan.project_key_kind}, "
                f"source_model={plan.source_embedding_model}",
            ),
        )

        counts = {
            "files": len(file_id_map),
            "chunks": len(chunk_id_map),
            "chunk_embeddings": emb_count,
            "file_edges": edge_count,
            "project_variables": var_count,
            "history": len(history_id_map),
            "history_embeddings": h_emb_count,
            "decisions": len(decision_id_map),
            "decision_embeddings": d_emb_count,
            "pg_project_id": new_pid,
        }

    pg_conn.commit()
    return counts


# ---------------------------------------------------------------------------
# Per-table copy helpers
# ---------------------------------------------------------------------------


def _insert_project(cur: Any, plan: MigrationPlan) -> int:
    cur.execute(
        "INSERT INTO projects("
        "name, root_path, git_remote, git_remote_normalized, "
        "created_at, last_build, last_update, file_count, chunk_count) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 0) "
        "RETURNING id",
        (
            plan.project_name,
            str(plan.project_root.resolve()),
            plan.git_remote,
            plan.git_remote_normalized,
            time.time(),
            None,
            None,
        ),
    )
    return int(cur.fetchone()[0])


def _copy_files(
    cur: Any, sqlite_conn: Any, sqlite_pid: int, new_pid: int
) -> dict[int, int]:
    rows = sqlite_conn.execute(
        "SELECT id, rel_path, content_hash, mtime, size, lang, last_scanned "
        "FROM files WHERE project_id = ? ORDER BY id",
        (sqlite_pid,),
    ).fetchall()
    out: dict[int, int] = {}
    for old_id, rel, hsh, mtime, size, lang, scanned in rows:
        cur.execute(
            "INSERT INTO files("
            "project_id, rel_path, content_hash, mtime, size, lang, last_scanned) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (new_pid, rel, hsh, mtime, size, lang, scanned),
        )
        out[int(old_id)] = int(cur.fetchone()[0])
    return out


def _copy_chunks(
    cur: Any,
    sqlite_conn: Any,
    sqlite_pid: int,
    new_pid: int,
    file_id_map: dict[int, int],
) -> dict[int, int]:
    """Insert chunks parents-first so ``parent_id`` always points to a real row.

    Order: rows with parent_id NULL first (top-level chunks and
    big_parents), then the rest in original order. Within each group we
    iterate by old id ascending; chunkers always emit parents before
    children, so a parent always appears before any child that references
    it in the same pass.
    """

    rows = sqlite_conn.execute(
        "SELECT id, file_id, parent_id, sibling_order, kind, name, "
        "qualified_name, start_line, end_line, start_byte, end_byte, "
        "char_count, content_hash, stored_text, embedded_text, metadata "
        "FROM chunks WHERE project_id = ? "
        "ORDER BY (parent_id IS NULL) DESC, parent_id, id",
        (sqlite_pid,),
    ).fetchall()

    out: dict[int, int] = {}
    for r in rows:
        (
            old_id, old_file_id, old_parent_id, sibling_order, kind, name,
            qualified_name, start_line, end_line, start_byte, end_byte,
            char_count, content_hash, stored_text, embedded_text, metadata,
        ) = r
        new_parent_id = (
            out.get(int(old_parent_id)) if old_parent_id is not None else None
        )
        cur.execute(
            "INSERT INTO chunks("
            "project_id, file_id, parent_id, sibling_order, kind, name, "
            "qualified_name, start_line, end_line, start_byte, end_byte, "
            "char_count, content_hash, stored_text, embedded_text, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (
                new_pid,
                file_id_map[int(old_file_id)],
                new_parent_id,
                sibling_order,
                kind,
                name,
                qualified_name,
                start_line,
                end_line,
                start_byte,
                end_byte,
                char_count,
                content_hash,
                stored_text,
                embedded_text,
                metadata,
            ),
        )
        out[int(old_id)] = int(cur.fetchone()[0])
    return out


def _copy_chunk_embeddings(
    cur: Any,
    sqlite_conn: Any,
    sqlite_pid: int,
    chunk_id_map: dict[int, int],
) -> int:
    """Convert each BLOB embedding to a numpy array and insert as vector(384).

    sqlite-vec stores embeddings as raw float32 little-endian bytes;
    pgvector accepts numpy arrays directly when ``register_vector`` was
    called on the connection (PostgresBackend.connect handles that).
    """

    import numpy as np  # local import — only needed during migrate

    rows = sqlite_conn.execute(
        "SELECT v.chunk_id, v.embedding FROM chunks_vec v "
        "JOIN chunks c ON c.id = v.chunk_id WHERE c.project_id = ?",
        (sqlite_pid,),
    ).fetchall()
    count = 0
    for old_chunk_id, blob in rows:
        if old_chunk_id not in chunk_id_map:
            # Orphan vec0 row — skip rather than insert against a missing
            # chunk. Should be rare; means the source DB has the cleanup
            # bug captured in project_vec0_cleanup_convention memory.
            continue
        vec = np.frombuffer(blob, dtype=np.float32)
        cur.execute(
            "INSERT INTO chunk_embeddings(chunk_id, embedding) "
            "VALUES (%s, %s)",
            (chunk_id_map[int(old_chunk_id)], vec),
        )
        count += 1
    return count


def _copy_file_edges(
    cur: Any,
    sqlite_conn: Any,
    sqlite_pid: int,
    new_pid: int,
    file_id_map: dict[int, int],
) -> int:
    rows = sqlite_conn.execute(
        "SELECT source_file_id, target_file_id, kind, raw, symbol, line "
        "FROM file_edges WHERE project_id = ?",
        (sqlite_pid,),
    ).fetchall()
    count = 0
    for source_id, target_id, kind, raw, symbol, line in rows:
        new_source = file_id_map.get(int(source_id))
        if new_source is None:
            continue  # would dangle; skip silently
        new_target = (
            file_id_map.get(int(target_id))
            if target_id is not None else None
        )
        cur.execute(
            "INSERT INTO file_edges("
            "project_id, source_file_id, target_file_id, kind, raw, symbol, line) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (new_pid, new_source, new_target, kind, raw, symbol, line),
        )
        count += 1
    return count


def _copy_project_variables(
    cur: Any, sqlite_conn: Any, sqlite_pid: int, new_pid: int
) -> int:
    rows = sqlite_conn.execute(
        "SELECT scope, name, value, created_at, updated_at "
        "FROM project_variables WHERE project_id = ?",
        (sqlite_pid,),
    ).fetchall()
    for scope, name, value, created_at, updated_at in rows:
        cur.execute(
            "INSERT INTO project_variables("
            "project_id, scope, name, value, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (new_pid, scope, name, value, created_at, updated_at),
        )
    return len(rows)


def _copy_history(
    cur: Any, sqlite_conn: Any, sqlite_pid: int, new_pid: int
) -> dict[int, int]:
    rows = sqlite_conn.execute(
        "SELECT id, created_at, short_summary, long_summary, session_id, tags "
        "FROM history WHERE project_id = ? ORDER BY id",
        (sqlite_pid,),
    ).fetchall()
    out: dict[int, int] = {}
    for old_id, created_at, short, long_, session_id, tags in rows:
        cur.execute(
            "INSERT INTO history("
            "project_id, created_at, short_summary, long_summary, "
            "session_id, tags) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (new_pid, created_at, short, long_, session_id, tags),
        )
        out[int(old_id)] = int(cur.fetchone()[0])
    return out


def _copy_history_embeddings(
    cur: Any,
    sqlite_conn: Any,
    sqlite_pid: int,
    history_id_map: dict[int, int],
) -> int:
    import numpy as np

    rows = sqlite_conn.execute(
        "SELECT v.history_id, v.embedding FROM history_vec v "
        "JOIN history h ON h.id = v.history_id WHERE h.project_id = ?",
        (sqlite_pid,),
    ).fetchall()
    count = 0
    for old_id, blob in rows:
        if old_id not in history_id_map:
            continue
        vec = np.frombuffer(blob, dtype=np.float32)
        cur.execute(
            "INSERT INTO history_embeddings(history_id, embedding) "
            "VALUES (%s, %s)",
            (history_id_map[int(old_id)], vec),
        )
        count += 1
    return count


def _copy_decisions(
    cur: Any, sqlite_conn: Any, sqlite_pid: int, new_pid: int
) -> dict[int, int]:
    rows = sqlite_conn.execute(
        "SELECT id, created_at, topic, decision, rationale, files_touched, "
        "session_id FROM decisions WHERE project_id = ? ORDER BY id",
        (sqlite_pid,),
    ).fetchall()
    out: dict[int, int] = {}
    for old_id, created_at, topic, decision, rationale, files_touched, session_id in rows:
        cur.execute(
            "INSERT INTO decisions("
            "project_id, created_at, topic, decision, rationale, "
            "files_touched, session_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (new_pid, created_at, topic, decision, rationale,
             files_touched, session_id),
        )
        out[int(old_id)] = int(cur.fetchone()[0])
    return out


def _copy_decision_embeddings(
    cur: Any,
    sqlite_conn: Any,
    sqlite_pid: int,
    decision_id_map: dict[int, int],
) -> int:
    import numpy as np

    rows = sqlite_conn.execute(
        "SELECT v.decision_id, v.embedding FROM decisions_vec v "
        "JOIN decisions d ON d.id = v.decision_id WHERE d.project_id = ?",
        (sqlite_pid,),
    ).fetchall()
    count = 0
    for old_id, blob in rows:
        if old_id not in decision_id_map:
            continue
        vec = np.frombuffer(blob, dtype=np.float32)
        cur.execute(
            "INSERT INTO decision_embeddings(decision_id, embedding) "
            "VALUES (%s, %s)",
            (decision_id_map[int(old_id)], vec),
        )
        count += 1
    return count
