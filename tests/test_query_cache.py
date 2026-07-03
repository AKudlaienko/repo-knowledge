"""Unit tests for Item A — query cache goes local-only (both modes).

Covers (see tasks/todo.md "Item A" / knowledge decide for this item):
  1. put/get round-trip via the local per-project SQLite file.
  2. a bumped ``index_stamp`` misses even though query_hash/head_sha match.
  3. cache ops never touch a backend DB connection — no ``conn`` parameter
     exists on ``get``/``put``/``invalidate`` at all, which is the strongest
     version of that guarantee (nothing to accidentally pass).
  4. TTL expiry still works; ``invalidate`` wipes all rows for a project.
  5. two different project roots get two independent cache files.

``KNOWLEDGE_HOME`` is monkeypatched per-test to an isolated ``tmp_path`` so
these tests never touch the real ``~/.knowledge/``.
"""
from __future__ import annotations

import inspect
import time

import pytest

from knowledge import query_cache
from knowledge.search import SearchResult


@pytest.fixture()
def knowledge_home(tmp_path, monkeypatch):
    """Isolate ``~/.knowledge/`` to a tmp dir for the duration of the test."""
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path))
    return tmp_path


def _result(chunk_id: int = 1, rel_path: str = "a.py") -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        kind="function",
        name="foo",
        qualified_name="mod.foo",
        start_line=1,
        end_line=5,
        rel_path=rel_path,
        lang="python",
        project_name="demo",
        project_root="/tmp/demo",
        preview="def foo(): ...",
        distance=0.1,
    )


# ---------------------------------------------------------------------------
# 1. put/get round-trip
# ---------------------------------------------------------------------------


def test_put_get_round_trip(knowledge_home, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    key = query_cache.compute_key("how does X work", None, None, 10)

    assert query_cache.get(root, key, "deadbeef", 100.0) is None

    results = [_result(1), _result(2, rel_path="b.py")]
    query_cache.put(root, key, "deadbeef", 100.0, results)

    cached = query_cache.get(root, key, "deadbeef", 100.0)
    assert cached is not None
    assert [r.chunk_id for r in cached] == [1, 2]
    assert cached[0].rel_path == "a.py"
    assert cached[1].rel_path == "b.py"

    # The cache file lives under KNOWLEDGE_HOME/cache/, not the repo root.
    cache_dir = tmp_path / "cache"
    assert cache_dir.is_dir()
    assert list(cache_dir.glob("*.sqlite"))


# ---------------------------------------------------------------------------
# 2. index_stamp mismatch -> miss
# ---------------------------------------------------------------------------


def test_bumped_index_stamp_misses(knowledge_home, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    key = query_cache.compute_key("q", None, None, 10)

    query_cache.put(root, key, "deadbeef", 100.0, [_result()])

    # Same query_hash + head_sha, but the index moved (a build/update ran).
    assert query_cache.get(root, key, "deadbeef", 200.0) is None
    # Old stamp still resolves until overwritten.
    assert query_cache.get(root, key, "deadbeef", 100.0) is not None

    # put() with the new stamp overwrites the row in place (same PK).
    query_cache.put(root, key, "deadbeef", 200.0, [_result()])
    assert query_cache.get(root, key, "deadbeef", 200.0) is not None
    assert query_cache.get(root, key, "deadbeef", 100.0) is None


# ---------------------------------------------------------------------------
# 3. cache ops never touch a backend conn
# ---------------------------------------------------------------------------


def test_get_put_invalidate_have_no_conn_parameter():
    """The whole point of Item A: the main-DB conn disappears from the
    cache functions' signatures entirely, so there's no network path left
    to accidentally take in shared_postgresql mode."""
    for fn in (query_cache.get, query_cache.put, query_cache.invalidate,
               query_cache.sweep_expired):
        params = inspect.signature(fn).parameters
        assert "conn" not in params, f"{fn.__name__} still takes a conn"


class _ExplodingConn:
    """Stand-in for a backend conn that must never be touched."""

    def __getattr__(self, name):
        raise AssertionError(f"cache op touched backend conn.{name}")


def test_cache_ops_ignore_a_poisoned_backend_object(knowledge_home, tmp_path):
    """Even if some future caller mistakenly threads a backend conn
    through, the cache module has no parameter to receive it and never
    imports/uses knowledge.db, so it can't reach it implicitly either."""
    root = tmp_path / "proj"
    root.mkdir()
    poisoned = _ExplodingConn()  # noqa: F841 -- never touched, that's the point

    key = query_cache.compute_key("q", None, None, 5)
    query_cache.put(root, key, "sha", 1.0, [_result()])
    assert query_cache.get(root, key, "sha", 1.0) is not None

    # query_cache module itself must not import knowledge.db (the main-DB
    # module) -- that would reintroduce a backend coupling.
    import knowledge.query_cache as qc_mod
    assert not hasattr(qc_mod, "db")


# ---------------------------------------------------------------------------
# 4. TTL expiry + invalidate
# ---------------------------------------------------------------------------


def test_ttl_expiry(knowledge_home, tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    key = query_cache.compute_key("q", None, None, 5)

    fake_now = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])

    query_cache.put(root, key, "sha", 1.0, [_result()])
    assert query_cache.get(root, key, "sha", 1.0) is not None

    # Jump past the 1h TTL.
    fake_now[0] += query_cache._TTL_SECONDS + 1
    assert query_cache.get(root, key, "sha", 1.0) is None


def test_invalidate_wipes_all_rows(knowledge_home, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    key_a = query_cache.compute_key("q1", None, None, 5)
    key_b = query_cache.compute_key("q2", None, None, 5)

    query_cache.put(root, key_a, "sha1", 1.0, [_result()])
    query_cache.put(root, key_b, "sha2", 1.0, [_result()])

    deleted = query_cache.invalidate(root)
    assert deleted == 2
    assert query_cache.get(root, key_a, "sha1", 1.0) is None
    assert query_cache.get(root, key_b, "sha2", 1.0) is None


# ---------------------------------------------------------------------------
# 5. independent cache files per project root
# ---------------------------------------------------------------------------


def test_two_projects_get_independent_cache_files(knowledge_home, tmp_path):
    root_a = tmp_path / "proj-a"
    root_b = tmp_path / "proj-b"
    root_a.mkdir()
    root_b.mkdir()
    key = query_cache.compute_key("same question", None, None, 10)

    query_cache.put(root_a, key, "sha", 1.0, [_result(1)])

    assert query_cache.get(root_a, key, "sha", 1.0) is not None
    assert query_cache.get(root_b, key, "sha", 1.0) is None
