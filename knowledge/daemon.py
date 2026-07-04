"""Embedder daemon (Item F, v1: warm model, opt-out).

Every cache-miss ``ask``/``search``/``decide``/``history add`` pays ~2.3s of
torch import + BAAI/bge-small-en-v1.5 load in a throwaway process — the
dominant remaining latency in both storage modes (local sqlite and shared
PostgreSQL). This module keeps one warm model resident in a small
background daemon so repeat invocations skip that cost entirely.

Scope decision (v1): the daemon hosts ONLY the embedding model. No DB
connections, no verb proxying — ``embed(texts) -> vectors`` is the whole
protocol. That keeps it stateless w.r.t. projects/config, makes fallback
trivial, and benefits sqlite + PG users equally. v2 (PG connection pool +
verb-level RPC) is deferred; the protocol carries a version field (``"v"``)
so v2 can extend it without breaking v1 clients/servers talking past each
other.

Protocol: newline-delimited JSON over a Unix domain socket at
``~/.knowledge/daemon/embed.sock`` (see ``paths.daemon_socket_path()``).
One connection = one request/response — the client opens a fresh
connection per call, sends exactly one JSON object terminated by ``\\n``,
reads exactly one JSON response terminated by ``\\n``, and closes. This
keeps the server's accept loop trivially serial (encode is CPU-bound
anyway, so there is no concurrency to win by keeping connections open).

Requests::

    {"v": 1, "op": "embed", "model": "<name>", "texts": ["...", ...]}
    {"v": 1, "op": "ping"}
    {"v": 1, "op": "shutdown"}

Responses (always one of these two shapes)::

    {"ok": true, ...}
    {"ok": false, "error": "..."}

``embed``    -> ``{"ok": true, "vectors": [[...], ...]}`` (float lists;
                the client converts to ``np.float32``).
``ping``     -> ``{"ok": true, "pid": int, "model": str, "version": str,
                "started_at": float, "last_used": float}`` (both timestamps
                are ``time.time()`` epoch seconds — NOT ``time.monotonic()``,
                which has no cross-process meaning — so a client can report
                human idle-seconds by subtracting from its own clock).
``shutdown`` -> ``{"ok": true}``, then the server closes the connection and
                exits its accept loop.

Staleness: the server stamps every ``ping`` response with the resolved
embedding-model name (see :func:`resolved_model_name`, which mirrors
``Embedder._ensure_loaded()``'s own resolution so client and server can
never disagree about "what model is this") and the running package
version. The client compares both; a mismatch (stale daemon left over from
an upgrade, or a different repo's ``embedding_model`` override) triggers a
``shutdown`` + one respawn attempt before falling back to the local
embedder.

Security: the socket lives under a 0700 directory
(:func:`knowledge.paths.ensure_daemon_dir_safe`) and the socket file itself
is chmod'd 0600 right after bind. An existing daemon dir that is a symlink
or has looser-than-0700 permissions is treated as untrusted — both the
client and the server refuse to use it and fall back to (or start) the
local embedder instead of touching the socket.

The daemon must never be able to break or noticeably slow down a command:
every failure mode on the client side (connect refused, spawn failed,
handshake stale twice, protocol error, timeout) falls back to the ordinary
in-process :class:`knowledge.embedder.Embedder` silently.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import paths

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

# Per-connection socket timeout on the SERVER side. Generous on purpose: a
# slow/hung client (e.g. a huge --all-projects batch) must not be able to
# wedge the daemon for other callers, but 30s comfortably covers any
# realistic encode() batch on CPU.
SERVER_RECV_TIMEOUT_SECONDS = 30.0

# Client-side budget for "connect, and if that fails spawn + retry". Keeps
# the daemon path from ever noticeably delaying a command beyond this.
_CLIENT_SPAWN_RETRY_BUDGET_SECONDS = 2.0
_CLIENT_SPAWN_RETRY_INTERVAL_SECONDS = 0.1
_CLIENT_REQUEST_TIMEOUT_SECONDS = 30.0
_CLIENT_PING_TIMEOUT_SECONDS = 2.0

EncodeFn = Callable[[list[str]], "np.ndarray"]


def resolved_model_name() -> str:
    """The exact model-identity string used for the staleness handshake.

    Mirrors ``Embedder._ensure_loaded()``'s resolution rule verbatim
    (settings.embedding_model override, else the built-in default) so the
    client and server can never disagree about which model is loaded.
    """
    from . import config, settings as settings_mod

    try:
        s = settings_mod.load_settings()
        user_model = (s.embedding_model or "").strip()
    except Exception:
        user_model = ""
    return user_model or config.MODEL


def _package_version() -> str:
    from . import __version__

    return __version__


def daemon_enabled() -> bool:
    """Whether the daemon should be tried at all.

    ``KNOWLEDGE_NO_DAEMON=1`` (any truthy-looking value) wins over
    everything — the per-invocation/CI escape hatch. Otherwise falls back
    to ``daemon.enabled`` in the resolved config (default ``True``).
    """
    if os.environ.get("KNOWLEDGE_NO_DAEMON", "").strip() in ("1", "true", "True"):
        return False
    from . import settings as settings_mod

    try:
        s = settings_mod.load_settings()
    except Exception:
        return True  # config is broken; that's settings' problem, not ours
    return s.daemon.enabled


def _idle_timeout_seconds() -> int:
    from . import settings as settings_mod

    try:
        s = settings_mod.load_settings()
    except Exception:
        return 1200
    return s.daemon.idle_timeout_seconds


# ---------------------------------------------------------------------------
# Wire helpers (shared by client and server)
# ---------------------------------------------------------------------------


def _recv_line(sock: socket.socket) -> str | None:
    """Read one ``\\n``-terminated line. ``None`` if the peer closed before
    sending anything. Any bytes after the first newline are discarded —
    fine under the one-request-per-connection protocol."""
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            if not chunks:
                return None
            data = b"".join(chunks)
            return data.split(b"\n", 1)[0].decode("utf-8", errors="replace")
        chunks.append(chunk)
        if b"\n" in chunk:
            data = b"".join(chunks)
            return data.split(b"\n", 1)[0].decode("utf-8", errors="replace")


def _send_line(sock: socket.socket, obj: dict) -> None:
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def _local_encode_fn() -> EncodeFn:
    """The real local embedder's ``.encode``, used as the server's default.

    Imported lazily (the ``Embedder`` class itself defers torch/
    sentence-transformers imports to first ``encode()`` call) so importing
    this module never pulls in torch.
    """
    from .embedder import Embedder

    return Embedder().encode


@dataclass
class DaemonServer:
    """Foreground Unix-socket server — one process hosts one warm model.

    ``encode_fn`` is injectable (constructor arg) precisely so tests can
    run the real accept loop with a fake encoder (e.g. ``lambda texts:
    np.zeros((len(texts), 8), dtype=np.float32)``) and never import torch.
    Production callers (``knowledge daemon run``) get the default, which
    wraps the real local :class:`knowledge.embedder.Embedder`.
    """

    socket_path: Path = field(default_factory=paths.daemon_socket_path)
    encode_fn: EncodeFn = field(default_factory=_local_encode_fn)
    idle_timeout_seconds: float = field(default_factory=_idle_timeout_seconds)
    model_name: str = field(default_factory=resolved_model_name)
    version: str = field(default_factory=_package_version)

    _sock: socket.socket | None = field(default=None, init=False, repr=False)
    _started_at: float = field(default_factory=time.time, init=False, repr=False)
    _last_used: float = field(default_factory=time.time, init=False, repr=False)

    def serve_forever(self) -> None:
        """Bind, listen, and serve until idle-timeout or a ``shutdown`` op.

        Always unlinks the socket file on the way out (clean exit or
        exception) so a dead daemon never leaves a connect-refused socket
        blocking the next spawn attempt.
        """
        safe_dir = paths.ensure_daemon_dir_safe()
        if safe_dir is None:
            logger.error(
                "refusing to start: %s is a symlink or has looser-than-0700 "
                "permissions — remove/fix it by hand before running the daemon",
                paths.daemon_dir(),
            )
            return

        sock_path = self.socket_path
        try:
            if sock_path.exists() or sock_path.is_symlink():
                sock_path.unlink()  # stale socket from a crashed prior run
        except OSError:
            pass

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(sock_path))
        except OSError:
            logger.exception("failed to bind %s", sock_path)
            srv.close()
            return
        os.chmod(str(sock_path), 0o600)

        try:
            srv.listen(5)
            # The listening socket's own accept() timeout IS the idle-exit
            # clock: it fires exactly `idle_timeout_seconds` after the last
            # accept() returned (i.e. after the last request finished being
            # handled), with no extra ticker thread needed. A fresh
            # connection resets the wait by definition (accept() returns
            # normally and the loop re-enters accept() from "now").
            srv.settimeout(self.idle_timeout_seconds)
            self._sock = srv
            logger.info(
                "listening on %s (model=%s, idle_timeout=%.1fs)",
                sock_path, self.model_name, self.idle_timeout_seconds,
            )

            while True:
                try:
                    conn, _addr = srv.accept()
                except socket.timeout:
                    logger.info(
                        "idle for %.1fs — exiting", self.idle_timeout_seconds
                    )
                    break
                with conn:
                    keep_going = self._handle_conn(conn)
                if not keep_going:
                    logger.info("shutdown requested — exiting")
                    break
        finally:
            try:
                srv.close()
            except OSError:
                pass
            try:
                sock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _handle_conn(self, conn: socket.socket) -> bool:
        """Handle exactly one request. Returns ``False`` to stop serving."""
        conn.settimeout(SERVER_RECV_TIMEOUT_SECONDS)
        try:
            line = _recv_line(conn)
        except (OSError, socket.timeout) as exc:
            logger.warning("recv failed: %s", exc)
            return True
        if line is None:
            return True  # peer connected and closed without sending anything

        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            self._reply(conn, {"ok": False, "error": f"bad json: {exc}"})
            return True
        if not isinstance(req, dict):
            self._reply(conn, {"ok": False, "error": "request must be a JSON object"})
            return True

        if req.get("v") != PROTOCOL_VERSION:
            self._reply(conn, {
                "ok": False,
                "error": f"protocol version mismatch: server={PROTOCOL_VERSION} "
                         f"client={req.get('v')!r}",
            })
            return True

        op = req.get("op")
        if op == "embed":
            self._reply(conn, self._op_embed(req))
            return True
        if op == "ping":
            self._reply(conn, self._op_ping())
            return True
        if op == "shutdown":
            self._reply(conn, {"ok": True})
            return False
        self._reply(conn, {"ok": False, "error": f"unknown op: {op!r}"})
        return True

    def _reply(self, conn: socket.socket, resp: dict) -> None:
        try:
            _send_line(conn, resp)
        except OSError as exc:
            logger.warning("send failed: %s", exc)

    def _op_embed(self, req: dict) -> dict:
        texts = req.get("texts")
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            return {"ok": False, "error": "texts must be a list of strings"}
        req_model = req.get("model")
        if req_model and req_model != self.model_name:
            # Defense in depth: the client is expected to catch this via
            # `ping` before ever sending `embed`, but a mismatched client
            # library (or a race right at respawn) should still get a
            # clean rejection instead of silently mixed-model vectors.
            return {
                "ok": False,
                "error": f"model mismatch: server={self.model_name!r} "
                         f"request={req_model!r}",
            }
        try:
            vectors = self.encode_fn(texts)
        except Exception as exc:  # noqa: BLE001 — surface any encode failure
            logger.exception("encode failed")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._last_used = time.time()
        return {"ok": True, "vectors": np.asarray(vectors, dtype=np.float32).tolist()}

    def _op_ping(self) -> dict:
        # Deliberately does NOT bump `_last_used` — `ping` is a health/status
        # check, not "the embedder was used". Bumping it would make
        # `knowledge daemon status`'s own idle-seconds reading lie (every
        # status check would reset the clock it's trying to report).
        return {
            "ok": True,
            "pid": os.getpid(),
            "model": self.model_name,
            "version": self.version,
            "started_at": self._started_at,
            "last_used": self._last_used,
        }


def run_server(idle_timeout_seconds: float | None = None) -> None:
    """Entry point for ``knowledge daemon run``. Always the LOCAL embedder —
    never calls ``embedder.get_embedder()`` (which is what would recurse
    into spawning another daemon)."""
    kwargs: dict = {}
    if idle_timeout_seconds is not None:
        kwargs["idle_timeout_seconds"] = idle_timeout_seconds
    DaemonServer(**kwargs).serve_forever()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class DaemonUnavailable(Exception):
    """Internal: the daemon can't be reached/used. Every caller inside this
    module catches this and falls back to the local embedder; it should
    never escape to ``embedder.get_embedder()`` callers."""


class DaemonEmbedder:
    """Client-side proxy — same ``.encode(texts) -> np.float32`` surface as
    :class:`knowledge.embedder.Embedder`, delegating to a warm background
    daemon over a Unix socket.

    Importing/constructing this class never touches torch or
    sentence-transformers — only the server side (or the local fallback
    ``Embedder``) does that. Do not construct this directly; go through
    ``embedder.get_embedder()``, which owns the enabled/reachable/spawnable
    decision and the fallback-to-local guarantee.
    """

    def __init__(self, socket_path: Path | None = None) -> None:
        self.socket_path = socket_path or paths.daemon_socket_path()

    def encode(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        try:
            resp = self._request(
                {
                    "v": PROTOCOL_VERSION,
                    "op": "embed",
                    "model": resolved_model_name(),
                    "texts": list(texts),
                },
                timeout=_CLIENT_REQUEST_TIMEOUT_SECONDS,
            )
            if not resp.get("ok"):
                raise DaemonUnavailable(resp.get("error", "embed failed"))
        except DaemonUnavailable as exc:
            # The daemon died between the get_embedder() handshake and this
            # call (idle exit, crash, `daemon stop`). Load the model locally
            # instead — slower, but the command still succeeds. Lazy import:
            # this is the only client-path line that can pull in torch, and
            # only after the daemon has already failed.
            logger.debug("daemon encode failed (%s) — falling back local", exc)
            from .embedder import _local_embedder

            return _local_embedder().encode(list(texts), batch_size=batch_size)
        return np.array(resp["vectors"], dtype=np.float32)

    def ping(self, timeout: float = _CLIENT_PING_TIMEOUT_SECONDS) -> dict:
        resp = self._request({"v": PROTOCOL_VERSION, "op": "ping"}, timeout=timeout)
        if not resp.get("ok"):
            raise DaemonUnavailable(resp.get("error", "ping failed"))
        return resp

    def shutdown(self, timeout: float = _CLIENT_PING_TIMEOUT_SECONDS) -> None:
        try:
            self._request({"v": PROTOCOL_VERSION, "op": "shutdown"}, timeout=timeout)
        except DaemonUnavailable:
            pass  # already gone — fine, that was the goal

    def _request(self, req: dict, timeout: float) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(str(self.socket_path))
                _send_line(sock, req)
                line = _recv_line(sock)
        except OSError as exc:
            raise DaemonUnavailable(str(exc)) from exc
        if line is None:
            raise DaemonUnavailable("no response from daemon")
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise DaemonUnavailable(f"malformed response: {exc}") from exc


def _spawn_daemon() -> None:
    """Detached background spawn of ``knowledge daemon run``.

    ``sys.executable -m knowledge`` survives a ``PATH`` that doesn't
    include the venv's bin dir (unlike relying on the installed console
    script). ``knowledge/__main__.py`` makes ``python -m knowledge`` work.
    stdout/stderr append to ``daemon.log``; ``start_new_session=True``
    detaches it from this process's session so it outlives us.
    """
    log_path = paths.daemon_log_path()
    with open(log_path, "a", encoding="utf-8") as log_fh:
        subprocess.Popen(
            [sys.executable, "-m", "knowledge", "daemon", "run"],
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(Path.home()),
        )


def _connect(socket_path: Path, timeout: float) -> DaemonEmbedder | None:
    client = DaemonEmbedder(socket_path)
    try:
        client.ping(timeout=timeout)
    except DaemonUnavailable:
        return None
    return client


def _fresh_client(socket_path: Path) -> DaemonEmbedder | None:
    """Connect, spawn-on-demand if needed, and verify freshness.

    Returns a ready-to-use :class:`DaemonEmbedder`, or ``None`` on any
    failure — callers fall back to the local embedder silently in that
    case, per the "daemon must never break a command" requirement.
    """
    client = _connect(socket_path, timeout=_CLIENT_PING_TIMEOUT_SECONDS)

    if client is None:
        _spawn_daemon()
        deadline = time.monotonic() + _CLIENT_SPAWN_RETRY_BUDGET_SECONDS
        while client is None and time.monotonic() < deadline:
            time.sleep(_CLIENT_SPAWN_RETRY_INTERVAL_SECONDS)
            client = _connect(socket_path, timeout=_CLIENT_PING_TIMEOUT_SECONDS)
        if client is None:
            return None

    # Staleness check: model/version mismatch -> shutdown + respawn once.
    try:
        info = client.ping(timeout=_CLIENT_PING_TIMEOUT_SECONDS)
    except DaemonUnavailable:
        return None

    fresh = (
        info.get("model") == resolved_model_name()
        and info.get("version") == _package_version()
    )
    if fresh:
        return client

    logger.info(
        "daemon is stale (model=%s version=%s) — respawning",
        info.get("model"), info.get("version"),
    )
    client.shutdown(timeout=_CLIENT_PING_TIMEOUT_SECONDS)
    # Wait briefly for the old server to finish exiting (it unlinks the
    # socket file on the way out). Spawning before that unlink completes
    # would let the dying server delete the NEW server's freshly bound
    # socket, wasting the respawn.
    vanish_deadline = time.monotonic() + 1.0
    while socket_path.exists() and time.monotonic() < vanish_deadline:
        time.sleep(0.05)
    _spawn_daemon()
    deadline = time.monotonic() + _CLIENT_SPAWN_RETRY_BUDGET_SECONDS
    respawned: DaemonEmbedder | None = None
    while respawned is None and time.monotonic() < deadline:
        time.sleep(_CLIENT_SPAWN_RETRY_INTERVAL_SECONDS)
        respawned = _connect(socket_path, timeout=_CLIENT_PING_TIMEOUT_SECONDS)
    return respawned


def get_daemon_embedder() -> DaemonEmbedder | None:
    """The decision function behind ``embedder.get_embedder()``.

    Returns a ready :class:`DaemonEmbedder` when the daemon is enabled AND
    reachable-or-spawnable AND fresh; returns ``None`` for every other
    outcome (disabled, unsafe daemon dir, connect+spawn+retry all failed,
    stale-and-respawn-failed) so the caller falls back to the ordinary
    local embedder. Never raises.
    """
    try:
        if not daemon_enabled():
            return None
        if paths.ensure_daemon_dir_safe() is None:
            return None
        return _fresh_client(paths.daemon_socket_path())
    except Exception:  # noqa: BLE001 — the daemon must never break a command
        logger.exception("unexpected error deciding daemon usage — falling back local")
        return None
