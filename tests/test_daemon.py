"""Unit tests for Item F — the embedder daemon (v1: warm model, opt-out).

No torch anywhere: the server's ``encode_fn`` is injectable, so every test
runs the REAL accept loop (real Unix socket, real newline-JSON protocol) in
a thread with a fake encoder (``np.zeros``). Covers, per tasks/todo.md:

  1. embed round-trip — dtype float32, shape (n, dim).
  2. ping fields — pid, model, version, started_at, last_used.
  3. idle exit with a sub-second timeout (0.2s); socket file removed.
  4. ``KNOWLEDGE_NO_DAEMON=1`` → ``get_embedder()`` returns the local class.
  5. config ``daemon.enabled: false`` → same.
  6. unreachable socket + failed spawn (non-existent binary) → silent
     local fallback.
  7. version/model mismatch → shutdown + respawn-once path.
  8. ``knowledge daemon status`` / ``stop`` CLI verbs (exit codes + output).
  9. security — daemon dir with loose perms or symlinked → refused.
 10. settings — daemon block absent/partial/invalid.

Isolation: ``KNOWLEDGE_HOME`` points at a short tempdir under ``/tmp``
(NOT pytest's ``tmp_path``: macOS ``$TMPDIR`` paths are long enough to
overflow the ~104-byte ``AF_UNIX sun_path`` limit) and the cwd moves there
too, so ``settings.load_settings()``'s walk-up never finds a real config.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from knowledge import cli, daemon, embedder, paths, settings

FAKE_DIM = 8


def _fake_encode(texts: list[str]) -> np.ndarray:
    return np.zeros((len(texts), FAKE_DIM), dtype=np.float32)


@pytest.fixture()
def knowledge_home(monkeypatch):
    """Short-path KNOWLEDGE_HOME + cwd isolation + embedder-cache reset."""
    d = Path(tempfile.mkdtemp(prefix="knd-", dir="/tmp"))
    monkeypatch.setenv("KNOWLEDGE_HOME", str(d))
    monkeypatch.delenv("KNOWLEDGE_NO_DAEMON", raising=False)
    monkeypatch.chdir(d)
    # get_embedder() caches its daemon-vs-local decision per process; reset
    # so each test decides fresh under its own env/config.
    monkeypatch.setattr(embedder, "_DAEMON_DECIDED", False)
    monkeypatch.setattr(embedder, "_DAEMON_CLIENT", None)
    monkeypatch.setattr(embedder, "_DEFAULT", None)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _start_server(
    sock_path: Path,
    idle_timeout: float = 10.0,
    model_name: str | None = None,
    version: str | None = None,
) -> tuple[daemon.DaemonServer, threading.Thread]:
    kwargs: dict = {
        "socket_path": sock_path,
        "encode_fn": _fake_encode,
        "idle_timeout_seconds": idle_timeout,
    }
    if model_name is not None:
        kwargs["model_name"] = model_name
    if version is not None:
        kwargs["version"] = version
    server = daemon.DaemonServer(**kwargs)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    deadline = time.monotonic() + 5.0
    while not sock_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sock_path.exists(), "server never bound its socket"
    return server, t


# ---------------------------------------------------------------------------
# 1+2. embed round-trip and ping fields (real socket, fake encoder)
# ---------------------------------------------------------------------------


def test_embed_round_trip_dtype_and_shape(knowledge_home):
    sock = paths.daemon_socket_path()
    _server, t = _start_server(sock)
    try:
        client = daemon.DaemonEmbedder(sock)
        out = client.encode(["hello", "world", "!"])
        assert isinstance(out, np.ndarray)
        assert out.dtype == np.float32
        assert out.shape == (3, FAKE_DIM)
    finally:
        daemon.DaemonEmbedder(sock).shutdown()
        t.join(timeout=5)


def test_ping_fields_and_last_used_semantics(knowledge_home):
    sock = paths.daemon_socket_path()
    _server, t = _start_server(sock)
    try:
        client = daemon.DaemonEmbedder(sock)
        info = client.ping()
        assert info["ok"] is True
        assert info["pid"] == os.getpid()  # server thread lives in this process
        assert info["model"] == daemon.resolved_model_name()
        assert info["version"] == daemon._package_version()
        assert info["started_at"] <= time.time()
        assert info["last_used"] <= time.time()

        # embed bumps last_used; ping does not (status must not reset idle).
        before = client.ping()["last_used"]
        client.encode(["x"])
        after = client.ping()["last_used"]
        assert after > before
        assert client.ping()["last_used"] == after
    finally:
        daemon.DaemonEmbedder(sock).shutdown()
        t.join(timeout=5)


def test_socket_file_mode_0600(knowledge_home):
    sock = paths.daemon_socket_path()
    _server, t = _start_server(sock)
    try:
        assert stat.S_IMODE(sock.stat().st_mode) == 0o600
    finally:
        daemon.DaemonEmbedder(sock).shutdown()
        t.join(timeout=5)


def test_bad_requests_get_ok_false(knowledge_home):
    sock = paths.daemon_socket_path()
    _server, t = _start_server(sock)

    def _raw(payload: bytes) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(str(sock))
            s.sendall(payload)
            data = b""
            while b"\n" not in data:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.split(b"\n", 1)[0])

    try:
        assert _raw(b"not json\n")["ok"] is False
        assert _raw(b'{"v": 99, "op": "ping"}\n')["ok"] is False  # bad protocol version
        assert _raw(b'{"v": 1, "op": "explode"}\n')["ok"] is False  # unknown op
        resp = _raw(b'{"v": 1, "op": "embed", "texts": "not-a-list"}\n')
        assert resp["ok"] is False
        # model mismatch on embed is rejected, not silently served
        resp = _raw(b'{"v": 1, "op": "embed", "model": "other", "texts": ["a"]}\n')
        assert resp["ok"] is False and "mismatch" in resp["error"]
        # server survived all of the above
        assert daemon.DaemonEmbedder(sock).ping()["ok"] is True
    finally:
        daemon.DaemonEmbedder(sock).shutdown()
        t.join(timeout=5)


# ---------------------------------------------------------------------------
# 3. idle exit
# ---------------------------------------------------------------------------


def test_idle_exit_removes_socket(knowledge_home):
    sock = paths.daemon_socket_path()
    _server, t = _start_server(sock, idle_timeout=0.2)
    t.join(timeout=5)
    assert not t.is_alive(), "server did not idle-exit"
    assert not sock.exists(), "socket file not removed on idle exit"


def test_shutdown_op_stops_server_and_removes_socket(knowledge_home):
    sock = paths.daemon_socket_path()
    _server, t = _start_server(sock)
    daemon.DaemonEmbedder(sock).shutdown()
    t.join(timeout=5)
    assert not t.is_alive()
    assert not sock.exists()


# ---------------------------------------------------------------------------
# 4+5. disable switches → local embedder class chosen
# ---------------------------------------------------------------------------


def test_env_no_daemon_wins(knowledge_home, monkeypatch):
    # even with a live daemon on the socket, env=1 must pick the local class
    sock = paths.daemon_socket_path()
    _server, t = _start_server(sock)
    try:
        monkeypatch.setenv("KNOWLEDGE_NO_DAEMON", "1")
        emb = embedder.get_embedder()
        assert isinstance(emb, embedder.Embedder)
    finally:
        daemon.DaemonEmbedder(sock).shutdown()
        t.join(timeout=5)


def test_config_daemon_disabled(knowledge_home, monkeypatch):
    cfg = knowledge_home / "config.json"  # == paths.home_config_path()
    cfg.write_text(json.dumps({"daemon": {"enabled": False}}), encoding="utf-8")
    spawns: list[bool] = []
    monkeypatch.setattr(daemon, "_spawn_daemon", lambda: spawns.append(True))
    emb = embedder.get_embedder()
    assert isinstance(emb, embedder.Embedder)
    assert spawns == [], "disabled daemon must not be spawned"


def test_env_wins_over_config_enabled_true(knowledge_home, monkeypatch):
    cfg = knowledge_home / "config.json"
    cfg.write_text(json.dumps({"daemon": {"enabled": True}}), encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_NO_DAEMON", "1")
    assert daemon.daemon_enabled() is False


# ---------------------------------------------------------------------------
# 6. unreachable socket + failed spawn → silent local fallback
# ---------------------------------------------------------------------------


def test_failed_spawn_falls_back_local(knowledge_home, monkeypatch):
    # Point the spawn at a non-existent binary and shrink the retry budget.
    monkeypatch.setattr(daemon.sys, "executable", "/nonexistent/python3-nope")
    monkeypatch.setattr(daemon, "_CLIENT_SPAWN_RETRY_BUDGET_SECONDS", 0.2)
    emb = embedder.get_embedder()
    assert isinstance(emb, embedder.Embedder)


def test_encode_falls_back_local_when_daemon_dies(knowledge_home, monkeypatch):
    """A DaemonEmbedder whose daemon vanished must still serve encode()."""
    sock = paths.daemon_socket_path()
    _server, t = _start_server(sock)
    client = daemon.DaemonEmbedder(sock)
    client.shutdown()
    t.join(timeout=5)

    calls: list[list[str]] = []

    class _FakeLocal:
        def encode(self, texts, batch_size=32):
            calls.append(list(texts))
            return np.ones((len(texts), FAKE_DIM), dtype=np.float32)

    monkeypatch.setattr(embedder, "_local_embedder", lambda: _FakeLocal())
    out = client.encode(["a", "b"])
    assert calls == [["a", "b"]]
    assert out.shape == (2, FAKE_DIM)


# ---------------------------------------------------------------------------
# 7. version/model mismatch → shutdown + respawn once
# ---------------------------------------------------------------------------


def test_stale_daemon_shutdown_and_respawn(knowledge_home, monkeypatch):
    sock = paths.daemon_socket_path()
    stale_server, stale_t = _start_server(sock, model_name="old/stale-model")

    respawned_threads: list[threading.Thread] = []

    def _fake_spawn():
        _srv, t = _start_server(sock)  # fresh identity (real model/version)
        respawned_threads.append(t)

    monkeypatch.setattr(daemon, "_spawn_daemon", _fake_spawn)

    client = daemon._fresh_client(sock)
    assert client is not None, "respawned daemon should have been adopted"
    info = client.ping()
    assert info["model"] == daemon.resolved_model_name()

    stale_t.join(timeout=5)
    assert not stale_t.is_alive(), "stale daemon was not shut down"
    assert len(respawned_threads) == 1, "respawn must happen exactly once"

    daemon.DaemonEmbedder(sock).shutdown()
    for t in respawned_threads:
        t.join(timeout=5)


def test_stale_daemon_respawn_fails_returns_none(knowledge_home, monkeypatch):
    sock = paths.daemon_socket_path()
    _server, stale_t = _start_server(sock, version="0.0.0-ancient")
    monkeypatch.setattr(daemon, "_spawn_daemon", lambda: None)  # spawn no-op
    monkeypatch.setattr(daemon, "_CLIENT_SPAWN_RETRY_BUDGET_SECONDS", 0.2)

    assert daemon._fresh_client(sock) is None
    stale_t.join(timeout=5)
    assert not stale_t.is_alive()


# ---------------------------------------------------------------------------
# 8. CLI verbs
# ---------------------------------------------------------------------------


def test_cli_status_not_running(knowledge_home, capsys):
    assert cli.main(["daemon", "status"]) == 1
    assert "not running" in capsys.readouterr().out


def test_cli_status_running(knowledge_home, capsys):
    sock = paths.daemon_socket_path()
    _server, t = _start_server(sock)
    try:
        assert cli.main(["daemon", "status"]) == 0
        out = capsys.readouterr().out
        assert "running" in out
        assert str(os.getpid()) in out
        assert daemon.resolved_model_name() in out
    finally:
        daemon.DaemonEmbedder(sock).shutdown()
        t.join(timeout=5)


def test_cli_stop_running_and_not_running(knowledge_home, capsys):
    sock = paths.daemon_socket_path()
    _server, t = _start_server(sock)
    assert cli.main(["daemon", "stop"]) == 0
    t.join(timeout=5)
    assert not t.is_alive()
    assert "stopped" in capsys.readouterr().out

    # idempotent: stopping again is still exit 0
    assert cli.main(["daemon", "stop"]) == 0
    assert "not running" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 9. security — daemon dir must be a real 0700 directory
# ---------------------------------------------------------------------------


def test_loose_perms_daemon_dir_refused(knowledge_home):
    d = paths.daemon_dir()
    d.mkdir(parents=True)
    d.chmod(0o755)
    assert paths.ensure_daemon_dir_safe() is None
    assert daemon.get_daemon_embedder() is None  # client refuses too


def test_symlinked_daemon_dir_refused(knowledge_home):
    real = knowledge_home / "elsewhere"
    real.mkdir(mode=0o700)
    paths.daemon_dir().symlink_to(real)
    assert paths.ensure_daemon_dir_safe() is None


def test_server_refuses_unsafe_dir(knowledge_home):
    d = paths.daemon_dir()
    d.mkdir(parents=True)
    d.chmod(0o755)
    server = daemon.DaemonServer(
        socket_path=paths.daemon_socket_path(),
        encode_fn=_fake_encode,
        idle_timeout_seconds=5.0,
    )
    server.serve_forever()  # must return immediately without binding
    assert not paths.daemon_socket_path().exists()


def test_fresh_daemon_dir_created_0700(knowledge_home):
    p = paths.ensure_daemon_dir_safe()
    assert p is not None
    assert stat.S_IMODE(p.stat().st_mode) == 0o700


# ---------------------------------------------------------------------------
# 10. settings — daemon config block parsing
# ---------------------------------------------------------------------------


def _load_with_config(home: Path, payload: dict) -> settings.Settings:
    (home / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    return settings.load_settings(start_dir=home)


def test_settings_daemon_block_absent_defaults(knowledge_home):
    s = _load_with_config(knowledge_home, {"cache_bytes": 123})
    assert s.daemon.enabled is True
    assert s.daemon.idle_timeout_seconds == 1200


def test_settings_daemon_block_partial(knowledge_home):
    s = _load_with_config(knowledge_home, {"daemon": {"idle_timeout_seconds": 60}})
    assert s.daemon.enabled is True
    assert s.daemon.idle_timeout_seconds == 60


def test_settings_daemon_block_full(knowledge_home):
    s = _load_with_config(
        knowledge_home, {"daemon": {"enabled": False, "idle_timeout_seconds": 30}}
    )
    assert s.daemon.enabled is False
    assert s.daemon.idle_timeout_seconds == 30


@pytest.mark.parametrize(
    "block",
    [
        {"daemon": "yes"},
        {"daemon": {"enabled": "yes"}},
        {"daemon": {"idle_timeout_seconds": "soon"}},
        {"daemon": {"idle_timeout_seconds": 0}},
        {"daemon": {"idle_timeout_seconds": -5}},
    ],
)
def test_settings_daemon_block_invalid(knowledge_home, block):
    with pytest.raises(settings.SettingsError):
        _load_with_config(knowledge_home, block)


def test_no_torch_imported_by_client_path(knowledge_home):
    """The daemon client path must never pull in torch/sentence-transformers."""
    code = (
        "import sys, os\n"
        "os.environ['KNOWLEDGE_HOME'] = sys.argv[1]\n"
        "os.chdir(sys.argv[1])\n"
        "import knowledge.daemon, knowledge.embedder\n"
        "knowledge.daemon.daemon_enabled()\n"
        "knowledge.daemon.resolved_model_name()\n"
        "knowledge.daemon.DaemonEmbedder()\n"
        "assert 'torch' not in sys.modules, 'torch leaked into the client path'\n"
        "assert 'sentence_transformers' not in sys.modules\n"
    )
    import subprocess

    res = subprocess.run(
        [sys.executable, "-c", code, str(knowledge_home)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
