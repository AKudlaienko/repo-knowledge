"""Vector search + result formatting.

Backend dispatch:

* SQLite (sqlite-vec): KNN syntax takes the query vector via ``MATCH`` and a
  ``k`` parameter inside the ``WHERE`` clause; embeddings live in the
  ``chunks_vec`` virtual table. Filters (project, kind, lang) are applied
  by joining ``chunks`` + ``files`` post-KNN — for thousands of chunks per
  project this is fine.
* PostgreSQL (pgvector): KNN via ``ORDER BY embedding <=> $vec LIMIT k``
  using the cosine-distance operator on an HNSW index. Embeddings live in
  the side table ``chunk_embeddings`` so the wide ``chunks`` row stays
  cheap to scan.

Both paths return identical :class:`SearchResult` tuples, so the
formatters and downstream rerank don't care which backend ran the query.
"""

from __future__ import annotations

from typing import NamedTuple

from . import config, db
from .db import Connection
from .embedder import get_embedder


class SearchResult(NamedTuple):
    chunk_id: int
    kind: str
    name: str | None
    qualified_name: str | None
    start_line: int
    end_line: int
    rel_path: str
    lang: str
    project_name: str
    project_root: str
    preview: str
    distance: float


def search(
    conn: Connection,
    query: str,
    project_id: int | None = None,
    kind: str | None = None,
    lang: str | None = None,
    top_k: int = config.DEFAULT_TOP_K,
) -> list[SearchResult]:
    embedder = get_embedder()
    q_vec = embedder.encode([query])[0]

    if db.current_mode() == "postgresql":
        return _search_postgres(conn, q_vec, project_id, kind, lang, top_k)
    return _search_sqlite(conn, q_vec, project_id, kind, lang, top_k)


def _search_sqlite(
    conn: Connection,
    q_vec,
    project_id: int | None,
    kind: str | None,
    lang: str | None,
    top_k: int,
) -> list[SearchResult]:
    # Over-fetch when post-filters are set — some KNN hits will be dropped
    # by project/kind/lang. 3x slack handles the common case.
    k_fetch = top_k * 3 if (project_id or kind or lang) else top_k

    where_clauses: list[str] = []
    params: list = [q_vec.tobytes(), k_fetch]
    if project_id is not None:
        where_clauses.append("c.project_id = ?")
        params.append(project_id)
    if kind:
        where_clauses.append("c.kind = ?")
        params.append(kind)
    if lang:
        where_clauses.append("f.lang = ?")
        params.append(lang)
    extra_where = ("AND " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT c.id, c.kind, c.name, c.qualified_name, c.start_line, c.end_line,
               f.rel_path, f.lang, p.name AS project_name, p.root_path,
               substr(c.stored_text, 1, 400) AS preview, v.distance
        FROM chunks_vec v
        JOIN chunks   c ON c.id = v.chunk_id
        JOIN files    f ON f.id = c.file_id
        JOIN projects p ON p.id = c.project_id
        WHERE v.embedding MATCH ? AND k = ?
        {extra_where}
        ORDER BY v.distance ASC
        LIMIT ?
    """
    params.append(top_k)
    rows = conn.execute(sql, params).fetchall()
    return [row_to_result(r) for r in rows]


def build_pg_vector_query(
    q_vec,
    project_id: int | None,
    kind: str | None,
    lang: str | None,
    top_k: int,
) -> tuple[str, list]:
    """Pure builder for the pgvector KNN query. No DB access — callable to
    prepare a statement for either a direct ``cur.execute`` or a pipelined
    ``conn.execute`` (see ``hybrid_search._pg_pipelined_channels``).

    pgvector accepts numpy arrays directly when ``register_vector`` was
    called on the connection (see ``PostgresBackend.connect``). The cosine
    distance operator is ``<=>`` and matches our L2-normalized
    embeddings — same metric as sqlite-vec's default.

    **Param shape is decision id=102 and must not change**: SQL placeholder
    order is distance projection, filter clauses, ORDER BY operand, LIMIT.
    ``q_vec`` appears twice (once for the projected distance column, once
    for the ORDER BY operator) with the filters sandwiched *between* the
    two occurrences — so the param list must be built as
    ``[q_vec, *filter_params, q_vec, top_k]`` from a separate,
    initially-empty ``filter_params`` list. Pre-seeding a list with
    ``q_vec`` and prepending it again silently doubles ``q_vec`` at the
    front and breaks the param/placeholder count (this exact bug was
    caught live against production PG — see decision id=102).
    """
    where_clauses: list[str] = []
    filter_params: list = []
    if project_id is not None:
        where_clauses.append("c.project_id = %s")
        filter_params.append(project_id)
    if kind:
        where_clauses.append("c.kind = %s")
        filter_params.append(kind)
    if lang:
        where_clauses.append("f.lang = %s")
        filter_params.append(lang)
    extra_where = ("AND " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT c.id, c.kind, c.name, c.qualified_name, c.start_line, c.end_line,
               f.rel_path, f.lang, p.name AS project_name, p.root_path,
               substr(c.stored_text, 1, 400) AS preview,
               (e.embedding <=> %s) AS distance
        FROM chunk_embeddings e
        JOIN chunks   c ON c.id = e.chunk_id
        JOIN files    f ON f.id = c.file_id
        JOIN projects p ON p.id = c.project_id
        WHERE TRUE {extra_where}
        ORDER BY e.embedding <=> %s
        LIMIT %s
    """
    params = [q_vec, *filter_params, q_vec, top_k]
    return sql, params


def _search_postgres(
    conn: Connection,
    q_vec,
    project_id: int | None,
    kind: str | None,
    lang: str | None,
    top_k: int,
) -> list[SearchResult]:
    sql, params = build_pg_vector_query(q_vec, project_id, kind, lang, top_k)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows_to_results(rows)


def rows_to_results(rows) -> list[SearchResult]:
    """Convert raw pgvector-query rows into ``SearchResult``.

    Exposed (not private) so ``hybrid_search`` can convert rows fetched
    from a pipelined ``conn.execute`` the same way ``_search_postgres``
    converts rows from a plain cursor.
    """
    return [row_to_result(r) for r in rows]


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
        distance=float(r[11]),
    )


def get_chunk(conn: Connection, chunk_id: int):
    """Fetch a single chunk row by id. Used by ``knowledge get`` / ``path``."""
    return db.fetch_one(
        conn,
        "SELECT c.id, c.kind, c.name, c.qualified_name, c.start_line, c.end_line, "
        "c.start_byte, c.end_byte, c.stored_text, f.rel_path, p.root_path, "
        "c.parent_id "
        "FROM chunks c JOIN files f ON f.id = c.file_id "
        "JOIN projects p ON p.id = c.project_id WHERE c.id = ?",
        (chunk_id,),
    )


def get_family(conn: Connection, chunk_id: int) -> list:
    """Return the chunk plus its parent/children in hierarchy order.

    If ``chunk_id`` refers to a ``big_parent``: returns ``[parent, sub_0,
    sub_1, ...]`` sorted by ``sibling_order``.
    If it refers to a ``big_subchunk``: returns the same family rooted at
    its parent.
    Otherwise (regular chunk with no parent/children): returns just the one.
    """
    row = db.fetch_one(
        conn, "SELECT id, kind, parent_id FROM chunks WHERE id = ?", (chunk_id,)
    )
    if row is None:
        return []
    _cid, kind, parent_id = row

    if kind == "big_subchunk" and parent_id is not None:
        root_id = parent_id
    else:
        root_id = chunk_id

    return db.fetch_all(
        conn,
        """
        SELECT c.id, c.kind, c.name, c.start_line, c.end_line,
               c.start_byte, c.end_byte, c.stored_text,
               f.rel_path, p.root_path, c.sibling_order
        FROM chunks c
        JOIN files    f ON f.id = c.file_id
        JOIN projects p ON p.id = c.project_id
        WHERE c.id = ? OR c.parent_id = ?
        ORDER BY CASE WHEN c.id = ? THEN -1 ELSE c.sibling_order END
        """,
        (root_id, root_id, root_id),
    )
