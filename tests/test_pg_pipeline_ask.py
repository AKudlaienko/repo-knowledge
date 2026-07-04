"""Unit tests for Item E — psycopg pipeline mode for the ``ask`` read path.

Covers (see tasks/todo.md "Item E" / knowledge decide id "item-e-pipeline-ask"):
  (a) ``search.build_pg_vector_query`` param-shape lock-in for decision id=102:
      ``q_vec`` at positions ``[0]`` and ``[-2]``, filters sandwiched between,
      ``top_k`` last — across no-filter / kind / lang / kind+lang / all-filter
      combinations.
  (b) ``fts.build_pg_grep_query`` returns ``None`` on empty/garbage patterns
      (nothing survives ``_to_tsquery``), and a real pattern's param shape.
  (c) ``hybrid_search._pg_pipelined_channels`` issues both statements inside
      one ``conn.pipeline()`` block, fetches only after the block exits, and
      produces results identical to ``_sequential_channels`` given the same
      underlying rows.
  (d) A pipeline-stage exception (e.g. a malformed ``to_tsquery`` pattern)
      triggers ``conn.rollback()`` then falls back to the exact sequential
      code path, degrading to vec-only results — never let a malformed FTS
      query kill ``ask``.

No live PostgreSQL is available in CI, so the psycopg connection is a hand
rolled stub (``_FakeConn``) supporting both the pipeline-mode interface
(``.pipeline()`` + ``.execute()``) and the plain-cursor interface
(``.cursor()`` used by ``search._search_postgres`` / ``fts._grep_postgres``
on the sequential/fallback path) — fed from the same row data so the two
control flows can be directly compared for equivalence.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledge import fts as fts_mod
from knowledge import hybrid_search
from knowledge import search as search_mod


# ---------------------------------------------------------------------------
# Shared fixtures / fakes
# ---------------------------------------------------------------------------


class _QVecSentinel:
    """Identity-comparable stand-in for an embedded query vector.

    The builders treat ``q_vec`` opaquely (just a bind param), so identity
    (``is``) is all the param-shape tests need — no real numpy vector math
    is exercised here.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<Q_VEC>"


Q_VEC = _QVecSentinel()


def _vec_row(chunk_id: int, distance: float) -> tuple:
    """A raw row shaped like ``build_pg_vector_query``'s SELECT (12 cols)."""
    return (
        chunk_id, "function", f"fn{chunk_id}", f"mod.fn{chunk_id}", 1, 5,
        f"file{chunk_id}.py", "python", "demo", "/tmp/demo",
        f"preview {chunk_id}", distance,
    )


def _fts_row(chunk_id: int, rank: float) -> tuple:
    """A raw row shaped like ``build_pg_grep_query``'s SELECT (12 cols,
    trailing ``rank`` trimmed by ``fts.parse_grep_rows``)."""
    return (
        chunk_id, "function", f"fn{chunk_id}", f"mod.fn{chunk_id}", 1, 5,
        f"file{chunk_id}.py", "python", "demo", "/tmp/demo",
        f"preview {chunk_id}", rank,
    )


def _tag_for_sql(sql: str) -> str:
    return "fts" if ("to_tsquery" in sql or "search_vector" in sql) else "vec"


class _FakeCursor:
    def __init__(self, conn: "_FakeConn", tag: str, rows: list) -> None:
        self._conn = conn
        self._tag = tag
        self._rows = rows

    def fetchall(self) -> list:
        self._conn.events.append(("fetchall", self._tag))
        return self._rows


class _FakePipelineCtx:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_FakePipelineCtx":
        self._conn.events.append("pipeline_enter")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._conn.events.append("pipeline_exit")
        return False  # never suppress — mirrors psycopg's real Pipeline ctx


class _DeferredCursor:
    """Stand-in for ``with conn.cursor() as cur: cur.execute(...); cur.fetchall()``,
    the plain (non-pipelined) path used by ``_search_postgres`` / ``_grep_postgres``.
    """

    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._tag: str | None = None
        self._rows: list = []

    def __enter__(self) -> "_DeferredCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql: str, params) -> None:
        self._tag = _tag_for_sql(sql)
        self._conn.events.append(("cursor_execute", self._tag))
        if self._tag == "fts" and self._conn._raise_on_fts:
            raise RuntimeError("simulated to_tsquery syntax error")
        self._rows = (
            self._conn._fts_rows if self._tag == "fts" else self._conn._vec_rows
        )

    def fetchall(self) -> list:
        self._conn.events.append(("cursor_fetchall", self._tag))
        return self._rows


class _FakeConn:
    """Stand-in psycopg connection supporting both the pipeline-mode
    interface and the plain-cursor interface, fed from the same row data.
    """

    def __init__(self, vec_rows: list, fts_rows: list, *, raise_on_fts: bool = False) -> None:
        self._vec_rows = vec_rows
        self._fts_rows = fts_rows
        self._raise_on_fts = raise_on_fts
        self.events: list = []
        self.rollback_called = False

    # -- pipeline-mode interface -------------------------------------------------
    def pipeline(self) -> _FakePipelineCtx:
        return _FakePipelineCtx(self)

    def execute(self, sql: str, params) -> _FakeCursor:
        tag = _tag_for_sql(sql)
        self.events.append(("execute", tag))
        if tag == "fts" and self._raise_on_fts:
            raise RuntimeError("simulated to_tsquery syntax error")
        rows = self._fts_rows if tag == "fts" else self._vec_rows
        return _FakeCursor(self, tag, rows)

    # -- plain-cursor interface ---------------------------------------------
    def cursor(self) -> _DeferredCursor:
        return _DeferredCursor(self)

    def rollback(self) -> None:
        self.rollback_called = True
        self.events.append("rollback")


@pytest.fixture()
def fake_embedder(monkeypatch):
    """Patch both modules' bound ``get_embedder`` alias.

    ``hybrid_search`` and ``search`` each bind ``from .embedder import
    get_embedder`` at import time (order-dependent aliasing bit us once
    before, see decision id=192/test_memory_scrub.py) — patch both sites,
    not ``knowledge.embedder.get_embedder`` itself.
    """
    embedder = SimpleNamespace(encode=lambda texts: [Q_VEC])
    monkeypatch.setattr(hybrid_search, "get_embedder", lambda: embedder)
    monkeypatch.setattr(search_mod, "get_embedder", lambda: embedder)
    return embedder


@pytest.fixture()
def pg_mode(monkeypatch):
    """``db.current_mode()`` returns 'postgresql' for every module that
    imported the shared ``db`` module object (hybrid_search, search, fts).
    """
    monkeypatch.setattr(hybrid_search.db, "current_mode", lambda: "postgresql")


# ---------------------------------------------------------------------------
# (a) search.build_pg_vector_query — decision id=102 param-shape guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "project_id,kind,lang,expected_filters",
    [
        (None, None, None, []),
        (None, "function", None, ["function"]),
        (None, None, "python", ["python"]),
        (None, "function", "python", ["function", "python"]),
        (7, "function", "python", [7, "function", "python"]),
    ],
    ids=["no-filter", "kind-only", "lang-only", "kind+lang", "project+kind+lang"],
)
def test_build_pg_vector_query_param_shape(project_id, kind, lang, expected_filters):
    sql, params = search_mod.build_pg_vector_query(
        Q_VEC, project_id, kind, lang, top_k=10
    )
    # decision id=102: q_vec bound at [0] (SELECT projection) and [-2]
    # (ORDER BY operand), filters sandwiched between, LIMIT last.
    assert params[0] is Q_VEC
    assert params[-2] is Q_VEC
    assert params[-1] == 10
    assert params[1:-2] == expected_filters
    assert len(params) == len(expected_filters) + 3
    assert "chunk_embeddings" in sql
    assert sql.count("%s") == len(params)


def test_build_pg_vector_query_filters_do_not_double_qvec():
    """Regression guard for the exact bug decision id=102 caught: a
    'pre-seed filter list with q_vec then prepend again' pattern would
    put both q_vec occurrences adjacent at the front instead of straddling
    the filters — this must not happen."""
    sql, params = search_mod.build_pg_vector_query(
        Q_VEC, 7, "function", "python", top_k=5
    )
    assert params == [Q_VEC, 7, "function", "python", Q_VEC, 5]


# ---------------------------------------------------------------------------
# (b) fts.build_pg_grep_query — None on empty/garbage, param shape otherwise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["", "   ", "a", "!!!", "*"])
def test_build_pg_grep_query_returns_none_for_empty_or_garbage(pattern):
    assert fts_mod.build_pg_grep_query(pattern, None, None, None, limit=10) is None


def test_build_pg_grep_query_param_shape_for_real_pattern():
    built = fts_mod.build_pg_grep_query("hello world", None, None, None, limit=10)
    assert built is not None
    sql, params = built
    # tsquery bound twice, adjacent (SELECT rank projection, then WHERE @@),
    # unlike the vector query where filters sit between the two occurrences.
    assert params[0] == params[1]
    assert params[-1] == 10
    assert sql.count("%s") == len(params)


def test_build_pg_grep_query_filters_between_tsquery_pair_and_limit():
    built = fts_mod.build_pg_grep_query(
        "hello world", 7, "function", "python", limit=10
    )
    assert built is not None
    _, params = built
    tsquery = params[0]
    assert params == [tsquery, tsquery, 7, "function", "python", 30]  # k_fetch = 10*3


# ---------------------------------------------------------------------------
# (c) pipeline path: one round trip, fetch-after-block, matches sequential
# ---------------------------------------------------------------------------


def test_pg_pipeline_runs_both_statements_in_one_block_then_fetches(
    pg_mode, fake_embedder
):
    vec_rows = [_vec_row(1, 0.1), _vec_row(2, 0.2)]
    fts_rows = [_fts_row(2, 0.9), _fts_row(3, 0.5)]
    conn = _FakeConn(vec_rows, fts_rows)

    vec_results, fts_results = hybrid_search._pg_pipelined_channels(
        conn, "hello world", None, None, None, fetch_k=10
    )

    assert vec_results == search_mod.rows_to_results(vec_rows)
    assert fts_results == fts_mod.parse_grep_rows(fts_rows, 10)

    # Both executes happen strictly inside the pipeline block; both
    # fetchall calls happen strictly after it exits.
    assert conn.events == [
        "pipeline_enter",
        ("execute", "vec"),
        ("execute", "fts"),
        "pipeline_exit",
        ("fetchall", "vec"),
        ("fetchall", "fts"),
    ]


def test_pg_pipeline_skips_fts_execute_when_pattern_is_empty(pg_mode, fake_embedder):
    vec_rows = [_vec_row(1, 0.1)]
    conn = _FakeConn(vec_rows, fts_rows=[])

    # A pattern with nothing but symbols/short tokens survives
    # ``_to_fts_match`` (which just OR-joins word-ish tokens) but not
    # ``_to_tsquery`` — build_pg_grep_query returns None, so the pipeline
    # must not issue a second execute at all.
    vec_results, fts_results = hybrid_search._pg_pipelined_channels(
        conn, "a", None, None, None, fetch_k=10
    )

    assert fts_results == []
    assert vec_results == search_mod.rows_to_results(vec_rows)
    assert conn.events == [
        "pipeline_enter",
        ("execute", "vec"),
        "pipeline_exit",
        ("fetchall", "vec"),
    ]


def test_pg_pipeline_matches_sequential_channels(pg_mode, fake_embedder):
    vec_rows = [_vec_row(1, 0.1), _vec_row(2, 0.2)]
    fts_rows = [_fts_row(2, 0.9), _fts_row(3, 0.5)]

    conn_pipe = _FakeConn(vec_rows, fts_rows)
    conn_seq = _FakeConn(vec_rows, fts_rows)

    pipe_vec, pipe_fts = hybrid_search._pg_pipelined_channels(
        conn_pipe, "hello world", None, None, None, fetch_k=10
    )
    seq_vec, seq_fts = hybrid_search._sequential_channels(
        conn_seq, "hello world", None, None, None, fetch_k=10
    )

    assert pipe_vec == seq_vec
    assert pipe_fts == seq_fts

    merged_pipe = hybrid_search._rrf_merge(pipe_vec, pipe_fts, limit=10)
    merged_seq = hybrid_search._rrf_merge(seq_vec, seq_fts, limit=10)
    assert merged_pipe == merged_seq


# ---------------------------------------------------------------------------
# (d) pipeline exception -> rollback -> sequential fallback (vec-only)
# ---------------------------------------------------------------------------


def test_pg_pipeline_exception_rolls_back_and_falls_back_to_vec_only(
    pg_mode, fake_embedder
):
    vec_rows = [_vec_row(1, 0.1)]
    fts_rows = [_fts_row(1, 0.9)]  # never actually surfaces — fts raises first
    conn = _FakeConn(vec_rows, fts_rows, raise_on_fts=True)

    vec_results, fts_results = hybrid_search._pg_pipelined_channels(
        conn, "hello world", None, None, None, fetch_k=10
    )

    assert conn.rollback_called is True
    assert fts_results == []  # fts.grep's own try/except swallows the error
    assert vec_results == search_mod.rows_to_results(vec_rows)

    # Prove it's the literal sequential fallback (conn.cursor() path), not a
    # special-cased pipeline recovery: the pipeline attempt aborts on the
    # fts execute, then the fallback re-runs vec via conn.cursor() and
    # attempts (and again catches) fts via conn.cursor() too.
    assert conn.events == [
        "pipeline_enter",
        ("execute", "vec"),
        ("execute", "fts"),
        "pipeline_exit",
        "rollback",
        ("cursor_execute", "vec"),
        ("cursor_fetchall", "vec"),
        ("cursor_execute", "fts"),
    ]


def test_pg_pipeline_exception_fallback_equals_direct_sequential_call(
    pg_mode, fake_embedder
):
    """The fallback path must produce the exact same result as calling
    ``_sequential_channels`` directly on an equivalent (non-raising for vec)
    connection — i.e. there is no bespoke fallback logic duplicating
    ``_sequential_channels``."""
    vec_rows = [_vec_row(1, 0.1), _vec_row(5, 0.4)]
    fts_rows = [_fts_row(9, 0.9)]

    conn_pipe = _FakeConn(vec_rows, fts_rows, raise_on_fts=True)
    conn_seq = _FakeConn(vec_rows, fts_rows, raise_on_fts=True)

    fallback_vec, fallback_fts = hybrid_search._pg_pipelined_channels(
        conn_pipe, "hello world", None, None, None, fetch_k=10
    )
    direct_vec, direct_fts = hybrid_search._sequential_channels(
        conn_seq, "hello world", None, None, None, fetch_k=10
    )

    assert fallback_vec == direct_vec
    assert fallback_fts == direct_fts == []


# ---------------------------------------------------------------------------
# ask() dispatch wiring: PG -> pipelined, SQLite -> sequential (unchanged)
# ---------------------------------------------------------------------------


def test_ask_dispatches_to_pipelined_channels_on_postgres(monkeypatch, tmp_path):
    monkeypatch.setattr(hybrid_search.db, "current_mode", lambda: "postgresql")
    calls: list[str] = []

    def fake_pipelined(conn, query, project_id, kind, lang, fetch_k):
        calls.append("pipelined")
        return [], []

    def fake_sequential(conn, query, project_id, kind, lang, fetch_k):
        calls.append("sequential")
        return [], []

    monkeypatch.setattr(hybrid_search, "_pg_pipelined_channels", fake_pipelined)
    monkeypatch.setattr(hybrid_search, "_sequential_channels", fake_sequential)
    monkeypatch.setattr(
        hybrid_search, "_rerank", lambda conn, results, project_id, project_root: results
    )

    results = hybrid_search.ask(
        object(), "some query", project_id=1, project_root=tmp_path, use_cache=False,
    )
    assert calls == ["pipelined"]
    assert results == []


def test_ask_dispatches_to_sequential_channels_on_sqlite(monkeypatch, tmp_path):
    monkeypatch.setattr(hybrid_search.db, "current_mode", lambda: "sqlite")
    calls: list[str] = []

    monkeypatch.setattr(
        hybrid_search,
        "_pg_pipelined_channels",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run on sqlite")),
    )

    def fake_sequential(conn, query, project_id, kind, lang, fetch_k):
        calls.append("sequential")
        return [], []

    monkeypatch.setattr(hybrid_search, "_sequential_channels", fake_sequential)
    monkeypatch.setattr(
        hybrid_search, "_rerank", lambda conn, results, project_id, project_root: results
    )

    results = hybrid_search.ask(
        object(), "some query", project_id=1, project_root=tmp_path, use_cache=False,
    )
    assert calls == ["sequential"]
    assert results == []
