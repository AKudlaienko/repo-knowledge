"""SQLite connection + schema.

Uses APSW instead of the stdlib ``sqlite3`` module because macOS Homebrew /
python.org Python builds stdlib sqlite3 without loadable-extension support,
which would break ``sqlite-vec``. APSW always supports it and ships wheels
for every platform we care about.

One DB, many projects. All tables namespaced by ``project_id``. Vector index
lives in the ``sqlite-vec`` virtual table ``chunks_vec``; project scoping is a
plain JOIN on ``chunks.project_id``.

Schema bumps: change ``config.SCHEMA_VERSION``. A mismatch between stored and
compiled version forces a full rebuild (the CLI prints a clear message rather
than silently migrating — better UX for a local tool).
"""

from __future__ import annotations

from pathlib import Path

import apsw
import sqlite_vec

from . import config, paths

# Re-exported for type hints elsewhere — callers import ``Connection`` from
# this module, not from ``apsw`` directly, so the backend stays swappable.
Connection = apsw.Connection


def connect(db_path: Path | None = None) -> Connection:
    """Open the DB, load ``sqlite-vec``, turn on foreign keys + WAL.

    Side-effect: ``init_schema(conn)`` is called on first open so callers
    don't need to remember to bootstrap.
    """
    target = db_path or paths.db_path()
    conn = apsw.Connection(str(target))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    init_schema(conn)
    return conn


# Individual DDL statements — APSW has no ``executescript``; each statement
# is issued separately. Keeps the transaction semantics predictable.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        root_path   TEXT NOT NULL UNIQUE,
        git_remote  TEXT,
        created_at  REAL NOT NULL,
        last_build  REAL,
        last_update REAL,
        file_count  INTEGER NOT NULL DEFAULT 0,
        chunk_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        id            INTEGER PRIMARY KEY,
        project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        rel_path      TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        mtime         REAL NOT NULL,
        size          INTEGER NOT NULL,
        lang          TEXT NOT NULL,
        last_scanned  REAL NOT NULL,
        UNIQUE(project_id, rel_path)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_files_project ON files(project_id)",
    """
    CREATE TABLE IF NOT EXISTS chunks (
        id             INTEGER PRIMARY KEY,
        project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        parent_id      INTEGER REFERENCES chunks(id) ON DELETE CASCADE,
        sibling_order  INTEGER,
        kind           TEXT NOT NULL,
        name           TEXT,
        qualified_name TEXT,
        start_line     INTEGER NOT NULL,
        end_line       INTEGER NOT NULL,
        start_byte     INTEGER NOT NULL,
        end_byte       INTEGER NOT NULL,
        char_count     INTEGER NOT NULL,
        content_hash   TEXT NOT NULL,
        stored_text    TEXT NOT NULL,
        embedded_text  TEXT NOT NULL,
        metadata       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_file    ON chunks(file_id)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_parent  ON chunks(parent_id, sibling_order)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_hash    ON chunks(content_hash)",
    # Partial indexes for exact-name lookup (schema v2). `knowledge find`
    # hits these for O(log n) lookups — anonymous chunks (markdown
    # sections, shell blocks) skip the index entirely.
    "CREATE INDEX IF NOT EXISTS idx_chunks_name  ON chunks(project_id, name) "
    "WHERE name IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_chunks_qname ON chunks(project_id, qualified_name) "
    "WHERE qualified_name IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS history (
        id            INTEGER PRIMARY KEY,
        project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        created_at    REAL NOT NULL,
        short_summary TEXT NOT NULL,
        long_summary  TEXT NOT NULL,
        session_id    TEXT,
        tags          TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_history_project_time ON history(project_id, created_at DESC)",
    # file_edges: per-project dependency graph (imports, requires, includes).
    # Populated by knowledge/resolvers/* during build/update. target_file_id
    # is nullable: NULL = external (stdlib, node_modules, unresolved
    # template). raw is the literal string from source, preserved even for
    # resolved edges so LLM output can show "from .utils" alongside the file
    # it resolved to.
    """
    CREATE TABLE IF NOT EXISTS file_edges (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        source_file_id  INTEGER NOT NULL REFERENCES files(id)    ON DELETE CASCADE,
        target_file_id  INTEGER          REFERENCES files(id)    ON DELETE CASCADE,
        kind            TEXT    NOT NULL,
        raw             TEXT    NOT NULL,
        symbol          TEXT,
        line            INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_file_edges_src     ON file_edges(source_file_id)",
    "CREATE INDEX IF NOT EXISTS idx_file_edges_tgt     ON file_edges(target_file_id) "
    "WHERE target_file_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_file_edges_project ON file_edges(project_id)",
    # project_variables: per-project Jinja/Terraform variable substitutions.
    # Consulted when resolving an edge whose ``raw`` contains ``{{ name }}``
    # (Ansible/Helm) or ``${var.name}`` (Terraform). ``scope`` namespaces
    # values by domain so ``deploy_env`` can mean different things for
    # ansible vs terraform vs helm; ``all`` is a catch-all merged into any
    # scope-specific lookup (scope-specific wins on name collision).
    """
    CREATE TABLE IF NOT EXISTS project_variables (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        scope       TEXT    NOT NULL,
        name        TEXT    NOT NULL,
        value       TEXT    NOT NULL,
        created_at  REAL    NOT NULL,
        updated_at  REAL    NOT NULL,
        UNIQUE(project_id, scope, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_project_variables "
    "ON project_variables(project_id, scope)",
    # query_cache (schema v2): per-project, per-HEAD-sha answer cache for
    # `knowledge ask`. Keyed by (project_id, query_hash, head_sha); the
    # hash already includes schema_version so v2→v3 upgrades invalidate
    # automatically. TTL 1h on expires_at. Invalidated in bulk on
    # build/update when ≥1 chunk changes (see indexer.py).
    """
    CREATE TABLE IF NOT EXISTS query_cache (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        query_hash  TEXT    NOT NULL,
        head_sha    TEXT    NOT NULL,
        result_json TEXT    NOT NULL,
        created_at  REAL    NOT NULL,
        expires_at  REAL    NOT NULL,
        UNIQUE(project_id, query_hash, head_sha)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_query_cache_exp ON query_cache(expires_at)",
    # decisions (schema v2): durable record of non-obvious choices made
    # during sessions. Complements `history` (one entry per unit of work)
    # with structured fields that make "what did we decide about X?"
    # answerable without parsing prose. `files_touched` is a JSON array
    # of rel_paths — not a FK table because most queries are "give me
    # everything" rather than "which decisions touched file Y".
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        created_at    REAL    NOT NULL,
        topic         TEXT    NOT NULL,
        decision      TEXT    NOT NULL,
        rationale     TEXT,
        files_touched TEXT,
        session_id    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_decisions_proj_time "
    "ON decisions(project_id, created_at DESC)",
)


def init_schema(conn: Connection) -> None:
    """Create tables if missing, seed ``meta`` with versions on first run."""
    for stmt in _SCHEMA_STATEMENTS:
        conn.execute(stmt)

    # Vector tables — created separately so we can template the dimension.
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
            chunk_id INTEGER PRIMARY KEY,
            embedding FLOAT[{config.EMBEDDING_DIM}]
        )
        """
    )
    # history_vec embeds ONLY the short_summary. Long summaries are retrieved
    # by ID when the caller drills in — keeps the vector index lean.
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS history_vec USING vec0(
            history_id INTEGER PRIMARY KEY,
            embedding FLOAT[{config.EMBEDDING_DIM}]
        )
        """
    )
    # decisions_vec (schema v2): embeds ``topic || ' :: ' || decision``.
    # Same shape as history_vec — cheap semantic search for "what did we
    # decide about X?".
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS decisions_vec USING vec0(
            decision_id INTEGER PRIMARY KEY,
            embedding FLOAT[{config.EMBEDDING_DIM}]
        )
        """
    )

    # chunks_fts (schema v2): FTS5 over chunk symbol names + stored_text
    # for `knowledge grep`. Contentless (`content=''`) — we don't need
    # highlight()/snippet(), just MATCH-for-rowid, so tokens-only halves
    # the disk footprint vs storing a copy of stored_text.
    #
    # Triggers keep the FTS in sync with chunks. Contentless FTS5 DELETE
    # requires the OLD content via the special 'delete' command — trivial
    # from AFTER DELETE / AFTER UPDATE triggers which have OLD.* available,
    # awkward to replicate as explicit indexer calls.
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            name,
            qualified_name,
            stored_text,
            content=''
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, name, qualified_name, stored_text)
            VALUES (new.id, new.name, new.qualified_name, new.stored_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, name, qualified_name, stored_text)
            VALUES ('delete', old.id, old.name, old.qualified_name, old.stored_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, name, qualified_name, stored_text)
            VALUES ('delete', old.id, old.name, old.qualified_name, old.stored_text);
            INSERT INTO chunks_fts(rowid, name, qualified_name, stored_text)
            VALUES (new.id, new.name, new.qualified_name, new.stored_text);
        END
        """
    )

    # v1 → v2 migration backfill. Contentless FTS5 `'delete'` commands require
    # the OLD content to match what was indexed; feeding the trigger OLD rows
    # that were never inserted into the FTS corrupts the index. On fresh v2
    # DBs this branch is a no-op (both tables empty). On upgraded DBs it
    # populates FTS once, so the first post-upgrade rebuild's DELETE triggers
    # operate against consistent state. Guarded so we don't re-pay the cost
    # on every connect.
    fts_count = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    chunks_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if chunks_count > 0 and fts_count == 0:
        conn.execute(
            "INSERT INTO chunks_fts(rowid, name, qualified_name, stored_text) "
            "SELECT id, name, qualified_name, stored_text FROM chunks"
        )

    # Seed versions on first run. APSW auto-commits outside of explicit
    # transaction blocks, so these INSERTs are durable immediately.
    existing = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    wanted = {
        "schema_version": config.SCHEMA_VERSION,
        "chunker_version": config.CHUNKER_VERSION,
        "embedding_model": config.MODEL,
        "embedding_dim": str(config.EMBEDDING_DIM),
    }
    for k, v in wanted.items():
        if k not in existing:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (k, v))


def get_meta(conn: Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
