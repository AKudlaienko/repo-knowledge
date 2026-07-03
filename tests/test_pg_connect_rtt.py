"""Unit tests for Item B — PG connect() round-trip cuts.

Covers (see tasks/todo.md "Item B" / knowledge decide id=188):
  1. Type-OID cache file helpers: round-trip, atomic write perms (0600),
     corrupted JSON tolerated, a null ``vector`` entry invalidates the cache.
  2. register_pgvector_types() with mocked pgvector/psycopg registrars:
     cache hit reconstructs TypeInfo and skips TypeInfo.fetch entirely; cache
     miss fetches then writes the file; refresh_types=True bypasses a valid
     cache.
  3. gssencmode kwarg logic in PostgresBackend.connect(): absent in DSN ->
     kwarg added; present in DSN -> not added; PGGSSENCMODE env set -> not
     added.

No live PostgreSQL is available in CI, so all psycopg/pgvector interaction is
mocked or stubbed. KNOWLEDGE_HOME is monkeypatched per-test to an isolated
tmp_path so the cache file never touches the real ``~/.knowledge/``.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import knowledge.paths as paths_mod
import knowledge.backends.postgres as pg_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def knowledge_home(tmp_path, monkeypatch):
    """Isolate ``~/.knowledge/`` to a tmp dir for the duration of the test."""
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Cache file read/write helpers
# ---------------------------------------------------------------------------


def test_cache_round_trip(knowledge_home):
    data = {
        "abc123": {
            "vector": [16401, 16402],
            "bit": [1560, 1561],
            "halfvec": None,
            "sparsevec": None,
        }
    }
    pg_mod._write_pg_types_cache(data)
    loaded = pg_mod._load_pg_types_cache()
    assert loaded == data


def test_cache_file_perms_0600(knowledge_home):
    pg_mod._write_pg_types_cache({"k": {"vector": [1, 2], "bit": [3, 4]}})
    path = paths_mod.pg_types_cache_path()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_cache_write_is_atomic_no_leftover_tmp(knowledge_home):
    pg_mod._write_pg_types_cache({"k": {"vector": [1, 2], "bit": [3, 4]}})
    leftovers = [p for p in knowledge_home.iterdir() if ".tmp-" in p.name]
    assert leftovers == []


def test_corrupted_cache_tolerated(knowledge_home):
    path = paths_mod.pg_types_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert pg_mod._load_pg_types_cache() == {}


def test_missing_cache_file_tolerated(knowledge_home):
    assert pg_mod._load_pg_types_cache() == {}


def test_non_dict_json_tolerated(knowledge_home):
    path = paths_mod.pg_types_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert pg_mod._load_pg_types_cache() == {}


def test_null_vector_entry_invalidates():
    # vector must exist per upstream assumption; a null entry for it makes
    # the whole cached entry untrustworthy, forcing a re-fetch.
    assert not pg_mod._entry_is_valid(
        {"vector": None, "bit": [1, 2], "halfvec": None, "sparsevec": None}
    )


def test_null_bit_entry_invalidates():
    assert not pg_mod._entry_is_valid(
        {"vector": [1, 2], "bit": None, "halfvec": None, "sparsevec": None}
    )


def test_valid_entry_with_null_optional_types():
    assert pg_mod._entry_is_valid(
        {"vector": [1, 2], "bit": [3, 4], "halfvec": None, "sparsevec": None}
    )


def test_not_a_dict_invalidates():
    assert not pg_mod._entry_is_valid(None)
    assert not pg_mod._entry_is_valid([1, 2])


# ---------------------------------------------------------------------------
# 2. register_pgvector_types with mocked pgvector/psycopg
# ---------------------------------------------------------------------------


def _install_fake_pgvector_modules(monkeypatch, *, fetch_return):
    """Install fake ``pgvector.psycopg.{vector,bit,halfvec,sparsevec}`` and a
    fake ``psycopg.types.TypeInfo`` into ``sys.modules`` / attributes so
    ``_register_from_cache`` / ``_register_from_server`` import cleanly
    without the real pgvector/psycopg C-extension machinery.

    Returns a dict of MagicMocks: the four ``register_*_info`` callables plus
    the fake ``TypeInfo`` class (whose ``.fetch`` is a MagicMock configured
    via ``fetch_return``, a callable ``name -> TypeInfo-like-or-None``).
    """
    register_vector_info = MagicMock(name="register_vector_info")
    register_bit_info = MagicMock(name="register_bit_info")
    register_halfvec_info = MagicMock(name="register_halfvec_info")
    register_sparsevec_info = MagicMock(name="register_sparsevec_info")

    vector_mod = types.ModuleType("pgvector.psycopg.vector")
    vector_mod.register_vector_info = register_vector_info
    bit_mod = types.ModuleType("pgvector.psycopg.bit")
    bit_mod.register_bit_info = register_bit_info
    halfvec_mod = types.ModuleType("pgvector.psycopg.halfvec")
    halfvec_mod.register_halfvec_info = register_halfvec_info
    sparsevec_mod = types.ModuleType("pgvector.psycopg.sparsevec")
    sparsevec_mod.register_sparsevec_info = register_sparsevec_info

    monkeypatch.setitem(sys.modules, "pgvector.psycopg.vector", vector_mod)
    monkeypatch.setitem(sys.modules, "pgvector.psycopg.bit", bit_mod)
    monkeypatch.setitem(sys.modules, "pgvector.psycopg.halfvec", halfvec_mod)
    monkeypatch.setitem(sys.modules, "pgvector.psycopg.sparsevec", sparsevec_mod)

    class FakeTypeInfo:
        def __init__(self, name, oid, array_oid):
            self.name = name
            self.oid = oid
            self.array_oid = array_oid

        @staticmethod
        def fetch(conn, name):
            return fetch_return(name)

    # ``_register_pgvector_types`` does ``import psycopg.types`` and reads
    # ``psycopg.types.TypeInfo`` off the real (already-imported) module, so
    # patch the attribute in place rather than trying to shadow the import.
    import psycopg.types as real_psycopg_types

    monkeypatch.setattr(real_psycopg_types, "TypeInfo", FakeTypeInfo)
    fake_psycopg_types = SimpleNamespace(TypeInfo=FakeTypeInfo)

    return {
        "register_vector_info": register_vector_info,
        "register_bit_info": register_bit_info,
        "register_halfvec_info": register_halfvec_info,
        "register_sparsevec_info": register_sparsevec_info,
        "psycopg_types": fake_psycopg_types,
        "TypeInfo": FakeTypeInfo,
    }


def test_cache_hit_reconstructs_type_info_and_skips_fetch(monkeypatch, knowledge_home):
    def fetch_should_not_be_called(name):  # pragma: no cover - failure path
        raise AssertionError(f"TypeInfo.fetch({name!r}) called on a cache hit")

    fakes = _install_fake_pgvector_modules(monkeypatch, fetch_return=fetch_should_not_be_called)

    cache_key = "somehostkey"
    cached_entry = {
        "vector": [16401, 16402],
        "bit": [1560, 1561],
        "halfvec": None,
        "sparsevec": [17000, 17001],
    }
    pg_mod._write_pg_types_cache({cache_key: cached_entry})

    conn = MagicMock(name="conn")
    pg_mod._register_pgvector_types(conn, cache_key, refresh_types=False)

    # vector/bit/sparsevec registered from reconstructed TypeInfo; halfvec (null) skipped.
    assert fakes["register_vector_info"].call_count == 1
    (call_conn, call_info) = fakes["register_vector_info"].call_args.args
    assert call_conn is conn
    assert (call_info.name, call_info.oid, call_info.array_oid) == ("vector", 16401, 16402)

    (call_conn, call_info) = fakes["register_bit_info"].call_args.args
    assert (call_info.name, call_info.oid, call_info.array_oid) == ("bit", 1560, 1561)

    fakes["register_halfvec_info"].assert_not_called()

    (call_conn, call_info) = fakes["register_sparsevec_info"].call_args.args
    assert (call_info.name, call_info.oid, call_info.array_oid) == ("sparsevec", 17000, 17001)


def test_cache_miss_fetches_then_writes_file(monkeypatch, knowledge_home):
    fetch_calls = []

    def fake_fetch(name):
        fetch_calls.append(name)
        oids = {
            "vector": (16401, 16402),
            "bit": (1560, 1561),
            "halfvec": None,       # simulate extension without halfvec
            "sparsevec": (17000, 17001),
        }
        val = oids[name]
        if val is None:
            return None
        return SimpleNamespace(name=name, oid=val[0], array_oid=val[1])

    fakes = _install_fake_pgvector_modules(monkeypatch, fetch_return=fake_fetch)

    cache_key = "freshkey"
    conn = MagicMock(name="conn")
    pg_mod._register_pgvector_types(conn, cache_key, refresh_types=False)

    assert fetch_calls == ["vector", "bit", "halfvec", "sparsevec"]
    assert fakes["register_vector_info"].call_count == 1
    assert fakes["register_bit_info"].call_count == 1
    assert fakes["register_halfvec_info"].call_count == 0
    assert fakes["register_sparsevec_info"].call_count == 1

    written = pg_mod._load_pg_types_cache()
    assert written[cache_key] == {
        "vector": [16401, 16402],
        "bit": [1560, 1561],
        "halfvec": None,
        "sparsevec": [17000, 17001],
    }


def test_refresh_types_bypasses_valid_cache(monkeypatch, knowledge_home):
    fetch_calls = []

    def fake_fetch(name):
        fetch_calls.append(name)
        return SimpleNamespace(name=name, oid=999, array_oid=998)

    fakes = _install_fake_pgvector_modules(monkeypatch, fetch_return=fake_fetch)

    cache_key = "refreshme"
    # Seed a perfectly valid cache entry first.
    pg_mod._write_pg_types_cache(
        {
            cache_key: {
                "vector": [1, 2],
                "bit": [3, 4],
                "halfvec": None,
                "sparsevec": None,
            }
        }
    )

    conn = MagicMock(name="conn")
    pg_mod._register_pgvector_types(conn, cache_key, refresh_types=True)

    # refresh_types=True must re-fetch even though the cache was valid.
    assert fetch_calls == ["vector", "bit", "halfvec", "sparsevec"]
    written = pg_mod._load_pg_types_cache()
    assert written[cache_key]["vector"] == [999, 998]


def test_invalid_cached_entry_triggers_refetch(monkeypatch, knowledge_home):
    """A null 'vector' entry (extension dropped/recreated) invalidates the
    whole cached entry and forces a re-fetch, per spec."""
    fetch_calls = []

    def fake_fetch(name):
        fetch_calls.append(name)
        return SimpleNamespace(name=name, oid=42, array_oid=43)

    _install_fake_pgvector_modules(monkeypatch, fetch_return=fake_fetch)

    cache_key = "brokenkey"
    pg_mod._write_pg_types_cache(
        {
            cache_key: {
                "vector": None,  # simulates a stale/invalid entry
                "bit": [3, 4],
                "halfvec": None,
                "sparsevec": None,
            }
        }
    )

    conn = MagicMock(name="conn")
    pg_mod._register_pgvector_types(conn, cache_key, refresh_types=False)
    assert fetch_calls == ["vector", "bit", "halfvec", "sparsevec"]


# ---------------------------------------------------------------------------
# 3. gssencmode kwarg logic
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal stand-in for a psycopg connection object."""


def _stub_backend(monkeypatch, dsn, connect_capture, *, pggssencmode=None):
    """Wire PostgresBackend.connect() with a stubbed psycopg module and a
    no-op type registration so only the gssencmode-kwarg logic is exercised.
    """
    import psycopg  # real module for conninfo_to_dict + OperationalError etc.

    fake_connect = MagicMock(return_value=_FakeConn())

    def capturing_connect(dsn_arg, **kwargs):
        connect_capture["dsn"] = dsn_arg
        connect_capture["kwargs"] = kwargs
        return fake_connect(dsn_arg, **kwargs)

    monkeypatch.setattr(psycopg, "connect", capturing_connect)
    monkeypatch.setattr(pg_mod, "_register_pgvector_types", lambda *a, **k: None)

    if pggssencmode is None:
        monkeypatch.delenv("PGGSSENCMODE", raising=False)
    else:
        monkeypatch.setenv("PGGSSENCMODE", pggssencmode)

    settings = SimpleNamespace(mode="shared_postgresql")
    backend = pg_mod.PostgresBackend(settings)
    monkeypatch.setattr(
        "knowledge.settings.resolve_pg_dsn", lambda s: dsn
    )
    return backend


def test_gssencmode_added_when_absent(monkeypatch, knowledge_home):
    capture: dict = {}
    backend = _stub_backend(
        monkeypatch, "postgresql://u:p@host:5432/db?sslmode=require", capture
    )
    backend.connect()
    assert capture["kwargs"].get("gssencmode") == "disable"


def test_gssencmode_not_added_when_present_in_dsn(monkeypatch, knowledge_home):
    capture: dict = {}
    backend = _stub_backend(
        monkeypatch,
        "postgresql://u:p@host:5432/db?sslmode=require&gssencmode=prefer",
        capture,
    )
    backend.connect()
    assert "gssencmode" not in capture["kwargs"]


def test_gssencmode_not_added_when_env_set(monkeypatch, knowledge_home):
    capture: dict = {}
    backend = _stub_backend(
        monkeypatch,
        "postgresql://u:p@host:5432/db?sslmode=require",
        capture,
        pggssencmode="prefer",
    )
    backend.connect()
    assert "gssencmode" not in capture["kwargs"]


def test_refresh_types_flag_threaded_through_connect(monkeypatch, knowledge_home):
    """connect(refresh_types=True) must be forwarded to _register_pgvector_types."""
    capture: dict = {}
    calls = []

    def fake_register(conn, cache_key, *, refresh_types):
        calls.append(refresh_types)

    backend = _stub_backend(
        monkeypatch, "postgresql://u:p@host:5432/db?sslmode=require", capture
    )
    monkeypatch.setattr(pg_mod, "_register_pgvector_types", fake_register)
    backend.connect(refresh_types=True)
    assert calls == [True]
