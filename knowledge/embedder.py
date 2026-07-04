"""Text → vector embedding via sentence-transformers.

BAAI/bge-small-en-v1.5 — 384-dim, ~130MB model. Downloaded on first use
to ``~/.knowledge/models/`` (overridable via ``HF_HOME`` etc., but the
default keeps everything under the tool's own home dir for tidiness).

Embeddings are L2-normalized at the model layer (``normalize_embeddings=
True``). That means cosine similarity == dot product, and sqlite-vec's
default distance metric (L2 on normalized vectors) gives the same ranking
as cosine distance — exactly what we want.

The module-level ``Embedder`` instance is a lazy singleton: the model is
loaded on the first ``encode`` call, then reused. Re-instantiating the
class repeatedly is safe — the underlying model is cached per-instance,
but callers should use ``get_embedder()`` to share the loaded model.

Embedder daemon (Item F): ``get_embedder()`` prefers a warm background
daemon (:mod:`knowledge.daemon`) over the in-process model when the daemon
is enabled (config ``daemon.enabled``, default true; ``KNOWLEDGE_NO_DAEMON=1``
env wins) and reachable-or-spawnable. Callers see the exact same
``.encode(texts) -> np.float32`` surface either way; every daemon failure
mode falls back to this module's local path silently.
"""

from __future__ import annotations

import numpy as np

from . import config, paths

_DEFAULT: "Embedder | None" = None

# Daemon-vs-local decision, made once per process. The positive case caches
# the connected DaemonEmbedder (so we don't re-ping per get_embedder call);
# the negative case caches the None (so a failed spawn's ~2s retry budget is
# paid at most once per process, not once per encode site). Tests reset both
# via monkeypatch.
_DAEMON_DECIDED: bool = False
_DAEMON_CLIENT = None  # daemon.DaemonEmbedder | None


class Embedder:
    def __init__(self) -> None:
        self._model = None

    def _ensure_loaded(self) -> None:
        if self._model is None:
            import logging
            import os
            import warnings

            # Resolve model name: honour user override from settings, fall
            # back to the built-in default.
            from . import settings as settings_mod
            try:
                _s = settings_mod.load_settings()
                _user_model = (_s.embedding_model or "").strip()
            except Exception:
                _user_model = ""

            if _user_model:
                # User supplied their own model — revision and safetensors
                # are their trust decision; we don't pin or override.
                model_name = _user_model
                model_revision = None
                model_kwargs: dict = {}
            else:
                # Default model: pin to the verified on-disk commit SHA
                # (supply-chain safety — see config.MODEL_REVISION).
                # Prefer safetensors over pickle (.bin) for the same reason:
                # safetensors is mmapped and cannot execute code on load.
                model_name = config.MODEL
                model_revision = config.MODEL_REVISION
                model_kwargs = {"use_safetensors": True}

            # The model is downloaded once and cached at paths.models_dir().
            # Every subsequent process load reads from disk, not network.
            #
            # The "unauthenticated requests to the HF Hub" warning fires
            # because SentenceTransformer(...) init unconditionally calls
            # the Hub API to check for model updates. Setting a logger
            # level doesn't help — the warning is emitted by the HTTP
            # layer. The real fix is offline mode: once the cache exists,
            # skip the update check entirely. On first run the env var is
            # NOT set, so the download proceeds normally; every run after
            # that is purely disk-bound.
            model_slug = model_name.replace("/", "--")
            model_dir = paths.models_dir() / f"models--{model_slug}"
            if model_dir.exists():
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

            # Belt-and-suspenders for the first-run case where offline
            # mode isn't yet on but we still want quieter output.
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
            warnings.filterwarnings(
                "ignore", message=r".*unauthenticated requests.*"
            )
            logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

            # Imported lazily so `knowledge --help` and fast commands don't
            # pay the torch/sentence-transformers import cost (~1-2s).
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                model_name,
                cache_folder=str(paths.models_dir()),
                revision=model_revision,
                model_kwargs=model_kwargs or None,
            )

    def encode(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        """Return an ``(N, EMBEDDING_DIM)`` float32 array. L2-normalized."""
        self._ensure_loaded()
        assert self._model is not None
        embs = self._model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=len(texts) > 64,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return embs.astype(np.float32)


def _local_embedder() -> Embedder:
    """The in-process lazy singleton — the pre-daemon behavior, untouched.

    Also the fallback target for :class:`knowledge.daemon.DaemonEmbedder`
    when the daemon dies between the ``get_embedder()`` handshake and an
    actual ``encode`` call.
    """
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Embedder()
    return _DEFAULT


def get_local_embedder() -> Embedder:
    """In-process embedder for bulk / long-running callers (the indexer).

    ``build``/``update`` embed whole-repo batches: routing thousands of
    texts through the daemon socket would serialize megabytes of JSON and
    monopolize the daemon's serial accept loop, blocking every interactive
    ``ask`` on the machine behind a minutes-long encode — while the ~2.5s
    model load the daemon exists to avoid is noise over a build's runtime.
    Interactive verbs keep using :func:`get_embedder`.
    """
    return _local_embedder()


def get_embedder():
    """Shared embedder for search/decisions/history/consolidate.

    Returns a :class:`knowledge.daemon.DaemonEmbedder` proxy (same
    ``.encode`` surface) when the embedder daemon is enabled and its socket
    is usable or spawnable; otherwise the ordinary in-process
    :class:`Embedder` singleton. The daemon decision never raises and never
    imports torch — the local path keeps its lazy-load semantics untouched.

    ``knowledge daemon run`` itself does NOT go through here (its server
    constructs a local :class:`Embedder` directly), so there is no way for
    the daemon to recurse into spawning itself.
    """
    global _DAEMON_DECIDED, _DAEMON_CLIENT
    if not _DAEMON_DECIDED:
        _DAEMON_DECIDED = True
        try:
            from . import daemon as daemon_mod

            _DAEMON_CLIENT = daemon_mod.get_daemon_embedder()
        except Exception:  # noqa: BLE001 — daemon must never break a command
            _DAEMON_CLIENT = None
    if _DAEMON_CLIENT is not None:
        return _DAEMON_CLIENT
    return _local_embedder()
