"""Structural lookup: exact-name find + lexical grep.

Backend dispatch:

* SQLite: ``find`` hits the partial indexes ``idx_chunks_name`` /
  ``idx_chunks_qname``; ``grep`` hits the contentless FTS5 virtual table
  ``chunks_fts`` and returns rows ranked by the FTS5 ``rank`` (BM25).
* PostgreSQL: ``find`` uses the same partial indexes (PG supports them
  identically); ``grep`` uses the GENERATED ``chunks.search_vector``
  ``tsvector`` column with a GIN index, ranked by ``ts_rank_cd``.

Both paths return identical :class:`SearchResult` tuples. Ranking
semantics differ between BM25 (SQLite) and ``ts_rank_cd`` (PG) but for
the symbol+text style queries we run, top-K substance overlaps heavily.
"""

from __future__ import annotations

import contextlib
import re
import signal

from . import config, db
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

    ``find`` is structurally identical across backends — every clause uses
    plain SQL (LIKE, equality) that PG and SQLite execute the same way.
    Only the parameter style differs, which :func:`db.fetch_all` hides.
    """
    if regex:
        return _find_regex(conn, name, project_id, kind, lang, limit)

    where: list[str] = []
    params: list = []

    if exact:
        where.append("(c.name = ? OR c.qualified_name = ?)")
        params.extend([name, name])
    else:
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
    rows = db.fetch_all(conn, sql, tuple(params))
    return [row_to_result(r) for r in rows]


def grep(
    conn: Connection,
    pattern: str,
    project_id: int | None = None,
    *,
    kind: str | None = None,
    lang: str | None = None,
    limit: int = config.DEFAULT_TOP_K,
) -> list[SearchResult]:
    """Lexical full-text match over chunk text + symbol names.

    SQLite path: passes ``pattern`` to FTS5 verbatim — caller gets the full
    FTS5 syntax (phrases, prefix, AND/OR, column qualifiers).
    PostgreSQL path: parses ``pattern`` into a tsquery via :func:`_to_tsquery`.
    The PG syntax is more restricted — bare words become an OR query, and
    quoted phrases become ``<->`` proximity queries. Caller doesn't have to
    care unless they're using FTS5-only syntax that has no PG equivalent.
    """
    if db.current_mode() == "postgresql":
        return _grep_postgres(conn, pattern, project_id, kind, lang, limit)
    return _grep_sqlite(conn, pattern, project_id, kind, lang, limit)


def _grep_sqlite(
    conn: Connection,
    pattern: str,
    project_id: int | None,
    kind: str | None,
    lang: str | None,
    limit: int,
) -> list[SearchResult]:
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
    return [row_to_result(r) for r in rows[:limit]]


def build_pg_grep_query(
    pattern: str,
    project_id: int | None,
    kind: str | None,
    lang: str | None,
    limit: int,
) -> tuple[str, list] | None:
    """Pure builder for the PG lexical-grep query. No DB access — callable
    to prepare a statement for either a direct ``cur.execute`` or a
    pipelined ``conn.execute`` (see
    ``hybrid_search._pg_pipelined_channels``).

    Returns ``None`` when ``pattern`` doesn't survive :func:`_to_tsquery`
    (empty/garbage input) — callers should skip the FTS channel entirely
    rather than run a query with no predicate.

    Param shape: ``tsquery`` is bound twice — once for the SELECT
    ``ts_rank_cd`` projection, once for the WHERE ``@@`` filter. Unlike the
    vector query (decision id=102), both ``tsquery`` placeholders are
    *adjacent* in the SQL text (SELECT projection immediately followed by
    the WHERE clause, with filters coming after both), so the correct
    param list is ``[tsquery, tsquery, *filter_params, limit]``.
    """
    tsquery = _to_tsquery(pattern)
    if not tsquery:
        return None
    k_fetch = limit * 3 if (project_id or kind or lang) else limit

    where: list[str] = ["c.search_vector @@ to_tsquery('english', %s)"]
    filter_params: list = []
    if project_id is not None:
        where.append("c.project_id = %s")
        filter_params.append(project_id)
    if kind:
        where.append("c.kind = %s")
        filter_params.append(kind)
    if lang:
        where.append("f.lang = %s")
        filter_params.append(lang)

    sql = f"""
        SELECT {_CHUNK_COLS},
               ts_rank_cd(c.search_vector, to_tsquery('english', %s)) AS rank
        FROM chunks c
        JOIN files    f ON f.id = c.file_id
        JOIN projects p ON p.id = c.project_id
        WHERE {' AND '.join(where)}
        ORDER BY rank DESC
        LIMIT %s
    """
    params = [tsquery, tsquery, *filter_params, k_fetch]
    return sql, params


def _grep_postgres(
    conn: Connection,
    pattern: str,
    project_id: int | None,
    kind: str | None,
    lang: str | None,
    limit: int,
) -> list[SearchResult]:
    built = build_pg_grep_query(pattern, project_id, kind, lang, limit)
    if built is None:
        return []
    sql, params = built
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return parse_grep_rows(rows, limit)


def parse_grep_rows(rows, limit: int) -> list[SearchResult]:
    """Convert raw PG grep rows into ``SearchResult``.

    Trims the trailing ``rank`` column (present for ``ORDER BY`` only —
    the ``SearchResult`` builder doesn't store it) and applies the same
    over-fetch limit slicing as ``_grep_postgres``. Exposed (not private)
    so ``hybrid_search`` can convert rows fetched from a pipelined
    ``conn.execute`` the same way.
    """
    return [row_to_result(r[:11]) for r in rows[:limit]]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _to_tsquery(pattern: str) -> str:
    """Best-effort tsquery from an FTS5-shaped pattern.

    PG's tsquery wants ``foo & bar`` (AND), ``foo | bar`` (OR), ``foo:*``
    (prefix), or ``foo <-> bar`` (proximity). We support a useful subset:

    * Quoted phrases ``"foo bar"`` → ``foo <-> bar``
    * Bare words → joined with ``|`` (OR — better recall for free-text)
    * Trailing ``*`` → tsquery prefix ``foo:*``
    * FTS5-only constructs (column qualifiers ``name:foo``, ``AND``/``OR``
      keywords) are not translated; callers using those on PG get an
      empty result (better than corrupted ranking).
    """
    pattern = pattern.strip()
    if not pattern:
        return ""

    # Pull out quoted phrases first so their internal spaces don't get
    # confused with the OR-join below.
    phrases = re.findall(r'"([^"]+)"', pattern)
    rest = re.sub(r'"[^"]+"', " ", pattern)

    parts: list[str] = []
    for phrase in phrases:
        words = re.findall(r"\w+", phrase)
        if words:
            parts.append(" <-> ".join(words))

    for tok in re.findall(r"[A-Za-z0-9_]+\*?", rest):
        if tok.endswith("*") and len(tok) > 1:
            parts.append(f"{tok[:-1]}:*")
        elif len(tok) >= 2:
            parts.append(tok)

    return " | ".join(parts)


@contextlib.contextmanager
def _regex_time_budget(seconds: int = 5):
    """Abort a runaway user regex (L1: ReDoS).

    Python's ``re`` has no match-time timeout, so a pathological
    ``--regex '(a+)+'`` against a long symbol name can spin for seconds in C.
    Bound the whole match loop with a SIGALRM so a malicious/typo'd pattern
    can't hang the CLI. SIGALRM is Unix + main-thread only; where it's
    unavailable (Windows, non-main thread) we silently run without the guard.
    """
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _on_timeout(signum, frame):
        raise ValueError(
            f"regex match exceeded {seconds}s — pattern is too expensive "
            "(possible catastrophic backtracking); narrow it or use grep"
        )

    try:
        old = signal.signal(signal.SIGALRM, _on_timeout)
    except ValueError:
        # Not the main thread — can't install the handler; run unguarded.
        yield
        return
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _find_regex(
    conn: Connection,
    pattern: str,
    project_id: int | None,
    kind: str | None,
    lang: str | None,
    limit: int,
) -> list[SearchResult]:
    """Python-side regex filter — backend-agnostic (rows returned via fetch_all)."""
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
    with _regex_time_budget():
        for row in db.fetch_all(conn, sql, tuple(params)):
            name, qname = row[2], row[3]
            if (name and rx.search(name)) or (qname and rx.search(qname)):
                out.append(row_to_result(row))
                if len(out) >= limit:
                    break
    return out


def _escape_like(s: str) -> str:
    """Escape ``%``, ``_``, and ``\\`` for SQL LIKE with ESCAPE ``\\``."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def row_to_result(r) -> SearchResult:
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
