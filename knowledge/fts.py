"""Structural lookup: exact-name find + FTS5 grep.

Fast answers to the two questions an LLM agent runs most on any repo:

* ``find("VaultClient")`` — "where is the symbol named X?"
* ``grep("helm install")`` — "where is this substring / token used?"

Both skip the embedding model entirely. ``find`` hits the partial indexes
added in schema v2 (``idx_chunks_name`` / ``idx_chunks_qname``); ``grep``
hits the ``chunks_fts`` contentless FTS5 index (tokenized names +
``stored_text``). Milliseconds either way.

Returns :class:`knowledge.search.SearchResult` so callers can share the
``distance`` slot (unused here — always 0.0) and the citations formatter
with :mod:`knowledge.search`.
"""

from __future__ import annotations

import re

from . import config
from .db import Connection
from .search import SearchResult


_CHUNK_COLS = (
    "c.id, c.kind, c.name, c.qualified_name, c.start_line, c.end_line, "
    "f.rel_path, f.lang, p.name AS project_name, p.root_path, "
    "substr(c.stored_text, 1, 400) AS preview"
)


def find(
    conn: Connection,
    name: str,
    project_id: int | None = None,
    *,
    exact: bool = False,
    kind: str | None = None,
    lang: str | None = None,
    regex: bool = False,
    limit: int = config.DEFAULT_TOP_K,
) -> list[SearchResult]:
    """Find chunks by symbol name.

    Match modes (mutually exclusive, in priority order):

    * ``regex=True`` — Python ``re.search`` over ``name`` and
      ``qualified_name``. O(n) over indexed rows in scope (partial index
      only covers rows with a non-NULL name), but n is small for the
      usual case of a single project.
    * ``exact=True`` — SQL equality. O(log n) via partial index.
    * otherwise — SQL prefix (``LIKE 'name%'``). Uses the index too;
      SQLite optimizes trailing-wildcard LIKE on BINARY-collated columns.

    Returns :class:`SearchResult` with ``distance=0.0`` (unused). Caller
    gets the same tuple shape as :func:`knowledge.search.search` so the
    citations formatter works identically.
    """
    if regex:
        return _find_regex(conn, name, project_id, kind, lang, limit)

    where: list[str] = []
    params: list = []

    if exact:
        where.append("(c.name = ? OR c.qualified_name = ?)")
        params.extend([name, name])
    else:
        # Prefix search. SQLite matches ``LIKE 'x%'`` against an index on
        # BINARY-collated (default) columns. Escape wildcards in the
        # user's input so a literal '%' doesn't silently over-match.
        escaped = _escape_like(name)
        where.append(
            "(c.name LIKE ? ESCAPE '\\' OR c.qualified_name LIKE ? ESCAPE '\\')"
        )
        params.extend([escaped + "%", escaped + "%"])

    if project_id is not None:
        where.append("c.project_id = ?")
        params.append(project_id)
    if kind:
        where.append("c.kind = ?")
        params.append(kind)
    if lang:
        where.append("f.lang = ?")
        params.append(lang)

    # Order: exact-name matches first, then qualified-name matches, then
    # by chunk id for stability. Cheap, useful for human reading.
    sql = f"""
        SELECT {_CHUNK_COLS}
        FROM chunks c
        JOIN files    f ON f.id = c.file_id
        JOIN projects p ON p.id = c.project_id
        WHERE {' AND '.join(where)}
        ORDER BY (c.name = ?) DESC, c.id ASC
        LIMIT ?
    """
    params.extend([name, limit])

    rows = conn.execute(sql, params).fetchall()
    return [_row_to_result(r) for r in rows]


def grep(
    conn: Connection,
    pattern: str,
    project_id: int | None = None,
    *,
    kind: str | None = None,
    lang: str | None = None,
    limit: int = config.DEFAULT_TOP_K,
) -> list[SearchResult]:
    """FTS5 MATCH over chunk text + symbol names.

    ``pattern`` is passed through to FTS5 directly, so the caller gets the
    full FTS5 query syntax: quoted phrases, prefix ``foo*``, boolean
    ``foo AND bar``, column qualifier ``name:foo``, etc.

    Ranking is FTS5 default bm25 — most-relevant first.
    """
    # Over-fetch when post-filters are set — some MATCH hits will be
    # dropped by project/kind/lang. 3x slack is usually enough; deep
    # filters may still return under ``limit``.
    k_fetch = limit * 3 if (project_id or kind or lang) else limit

    where: list[str] = ["chunks_fts MATCH ?"]
    params: list = [pattern]
    if project_id is not None:
        where.append("c.project_id = ?")
        params.append(project_id)
    if kind:
        where.append("c.kind = ?")
        params.append(kind)
    if lang:
        where.append("f.lang = ?")
        params.append(lang)

    # bm25(chunks_fts) — lower rank = better match, so ORDER BY ASC.
    # Use the ``rank`` auxiliary column (FTS5 default BM25) so we don't
    # fight the order of user-supplied filters.
    sql = f"""
        SELECT {_CHUNK_COLS}
        FROM chunks_fts
        JOIN chunks   c ON c.id = chunks_fts.rowid
        JOIN files    f ON f.id = c.file_id
        JOIN projects p ON p.id = c.project_id
        WHERE {' AND '.join(where)}
        ORDER BY chunks_fts.rank
        LIMIT ?
    """
    params.append(k_fetch)

    rows = conn.execute(sql, params).fetchall()
    return [_row_to_result(r) for r in rows[:limit]]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _find_regex(
    conn: Connection,
    pattern: str,
    project_id: int | None,
    kind: str | None,
    lang: str | None,
    limit: int,
) -> list[SearchResult]:
    """Python-side regex filter.

    SQLite REGEXP needs a user-registered function; we avoid the
    connection-setup coupling by pulling candidate rows (scoped to
    non-NULL name via the partial index) and filtering in Python.
    Still fast in practice — the index keeps row counts low.
    """
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regex: {exc}") from exc

    where: list[str] = ["(c.name IS NOT NULL OR c.qualified_name IS NOT NULL)"]
    params: list = []
    if project_id is not None:
        where.append("c.project_id = ?")
        params.append(project_id)
    if kind:
        where.append("c.kind = ?")
        params.append(kind)
    if lang:
        where.append("f.lang = ?")
        params.append(lang)

    sql = f"""
        SELECT {_CHUNK_COLS}
        FROM chunks c
        JOIN files    f ON f.id = c.file_id
        JOIN projects p ON p.id = c.project_id
        WHERE {' AND '.join(where)}
        ORDER BY c.id ASC
    """
    out: list[SearchResult] = []
    for row in conn.execute(sql, params):
        name, qname = row[2], row[3]
        if (name and rx.search(name)) or (qname and rx.search(qname)):
            out.append(_row_to_result(row))
            if len(out) >= limit:
                break
    return out


def _escape_like(s: str) -> str:
    """Escape ``%``, ``_``, and ``\\`` for SQL LIKE with ESCAPE ``\\``."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_result(r) -> SearchResult:
    """Shape a row matching ``_CHUNK_COLS`` as a :class:`SearchResult`."""
    return SearchResult(
        chunk_id=r[0],
        kind=r[1],
        name=r[2],
        qualified_name=r[3],
        start_line=r[4],
        end_line=r[5],
        rel_path=r[6],
        lang=r[7],
        project_name=r[8],
        project_root=r[9],
        preview=r[10],
        distance=0.0,
    )
