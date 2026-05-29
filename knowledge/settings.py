"""Runtime configuration for the ``knowledge`` CLI.

This module is the single source of truth for *runtime* settings (which
backend to talk to, how to assemble a DSN, etc.). Hardcoded build-time
constants like the embedding model name still live in :mod:`knowledge.config`.

Resolution rule (one file, one format, walk-up):

1. ``KNOWLEDGE_DATABASE_URL`` env (CI override) — full DSN, wins everything.
2. Walk up from cwd to filesystem root looking for ``.knowledge.yaml``.
   First match wins. The same file name is used at every scope — the file
   *closer to the cwd* wins.
3. If the walk found nothing, fall back to ``$HOME/.knowledge.yaml``
   (covers the case where cwd is outside the user's home tree).
4. If still nothing, defaults (``mode = sqlite``).

Same file name and same YAML schema everywhere — there's no "user JSON vs
project YAML" split. Pick a scope by where you put the file:

* in your repo root → applies only inside that repo
* in your home dir   → applies everywhere else on this laptop

Credentials never go on disk. The YAML carries env-var **names** only;
actual values come from ``os.environ`` at connect time. ``config show``
reports which file was selected so the active scope is never ambiguous.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from . import paths

StorageMode = Literal["sqlite", "shared_postgresql"]

_DEFAULT_USER_ENV = "KNOWLEDGE_PG_USER"
_DEFAULT_PASSWORD_ENV = "KNOWLEDGE_PG_PASSWORD"

# Forbidden top-level keys in any ``.knowledge.yaml``. Defense-in-depth on
# top of CI lint of the example file: catches the "I'll just put my
# password in here for testing" footgun before any network call.
_FORBIDDEN_TOP_LEVEL = ("password", "user")


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int = 5432
    database: str = "knowledge"
    sslmode: str = "require"
    user_env: str = _DEFAULT_USER_ENV
    password_env: str = _DEFAULT_PASSWORD_ENV
    connect_timeout_seconds: int = 10


@dataclass(frozen=True)
class Settings:
    """Loaded runtime settings.

    ``config_source`` is ``"default"`` when no file was found anywhere, or
    the absolute path of the ``.knowledge.yaml`` that won the resolution.
    """

    mode: StorageMode = "sqlite"
    postgresql: PostgresSettings | None = None
    cache_bytes: int = 2 * 1024 * 1024 * 1024
    embedding_model: str | None = None
    config_source: str = "default"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class SettingsError(Exception):
    """Raised when a discovered ``.knowledge.yaml`` is unparseable or invalid."""


def load_settings(start_dir: Path | None = None) -> Settings:
    """Find the active ``.knowledge.yaml`` and load it.

    Walks up from ``start_dir`` (default cwd) to the filesystem root,
    returning the first ``.knowledge.yaml`` found. If the walk exits
    without a hit, falls back to ``$HOME/.knowledge.yaml`` (handy when
    cwd is outside the home tree). Returns built-in defaults when nothing
    is found anywhere.

    Malformed YAML or a forbidden field → :class:`SettingsError` (the CLI
    maps to exit code 2).
    """

    yaml_path = _find_yaml(start_dir or Path.cwd())
    if yaml_path is None:
        return Settings()

    raw = _parse_yaml(yaml_path) or {}
    if not isinstance(raw, dict):
        raise SettingsError(f"{yaml_path}: top level must be a mapping")

    for forbidden in _FORBIDDEN_TOP_LEVEL:
        if forbidden in raw:
            raise SettingsError(
                f"{yaml_path}: top-level '{forbidden}' field is not allowed. "
                f"Credentials must come from env vars (see config.example.env). "
                f"Use 'storage.postgresql.{forbidden}_env' to name the env var "
                f"that holds the value."
            )

    storage = raw.get("storage", {}) or {}
    pg_settings, mode = _parse_storage_block(storage, source=str(yaml_path))

    cache_bytes = int(raw.get("cache_bytes", 2 * 1024 * 1024 * 1024))
    embedding_model = raw.get("embedding_model")
    if embedding_model is not None and not isinstance(embedding_model, str):
        raise SettingsError(
            f"{yaml_path}: embedding_model must be a string or null"
        )

    return Settings(
        mode=mode,
        postgresql=pg_settings,
        cache_bytes=cache_bytes,
        embedding_model=embedding_model,
        config_source=str(yaml_path),
    )


def _find_yaml(start: Path) -> Path | None:
    """Walk up from ``start`` looking for ``.knowledge.yaml``; home fallback.

    Returns the closest match (cwd or any ancestor). If the walk-up runs
    off the filesystem root with no hit, also checks
    ``$HOME/.knowledge.yaml`` so users running from a tmpdir or a
    non-home tree still pick up their laptop default.
    """

    p = start.resolve()
    for d in [p, *p.parents]:
        candidate = d / ".knowledge.yaml"
        if candidate.exists():
            return candidate
        if d == d.parent:
            break

    home_default = paths.config_path()
    if home_default.exists():
        return home_default
    return None


def _parse_yaml(path: Path):
    """Load YAML from ``path``, with a clear error if PyYAML is unavailable."""

    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover — pyyaml is in core deps
        raise SettingsError(
            f"PyYAML is required to read {path}; reinstall the package"
        ) from exc

    try:
        return yaml.safe_load(path.read_text("utf-8"))
    except yaml.YAMLError as exc:
        raise SettingsError(f"{path}: invalid YAML — {exc}") from None


def _parse_storage_block(
    storage, source: str
) -> tuple[PostgresSettings | None, StorageMode]:
    """Validate and unpack a ``storage`` block.

    Empty or missing block → defaults (``mode = sqlite``, no postgres).
    """

    if not storage:
        return None, "sqlite"
    if not isinstance(storage, dict):
        raise SettingsError(f"{source}: 'storage' must be a mapping")

    mode = storage.get("mode", "sqlite")
    if mode not in ("sqlite", "shared_postgresql"):
        raise SettingsError(
            f"{source}: storage.mode must be 'sqlite' or 'shared_postgresql' "
            f"(got {mode!r})"
        )

    pg_block = storage.get("postgresql")
    if pg_block is None:
        return None, mode
    if not isinstance(pg_block, dict):
        raise SettingsError(f"{source}: storage.postgresql must be a mapping")

    for forbidden in ("password", "user"):
        if forbidden in pg_block:
            raise SettingsError(
                f"{source}: storage.postgresql.{forbidden} is not allowed; "
                f"use '{forbidden}_env' to name an env var instead."
            )

    host = pg_block.get("host")
    if mode == "shared_postgresql" and not host:
        raise SettingsError(
            f"{source}: storage.postgresql.host is required when "
            f"mode == 'shared_postgresql'"
        )

    pg_settings = PostgresSettings(
        host=host or "",
        port=int(pg_block.get("port", 5432)),
        database=pg_block.get("database", "knowledge"),
        sslmode=pg_block.get("sslmode", "require"),
        user_env=pg_block.get("user_env", _DEFAULT_USER_ENV),
        password_env=pg_block.get("password_env", _DEFAULT_PASSWORD_ENV),
        connect_timeout_seconds=int(
            pg_block.get("connect_timeout_seconds", 10)
        ),
    )
    return pg_settings, mode


# ---------------------------------------------------------------------------
# DSN assembly
# ---------------------------------------------------------------------------


class DsnError(Exception):
    """Raised when a PG DSN cannot be assembled (missing env, bad mode, …)."""


def dsn_source(settings: Settings) -> str:
    """Where the effective DSN comes from.

    Returns one of:
      * ``"KNOWLEDGE_DATABASE_URL"`` — env override active
      * ``"<config-path> + env"`` — file-driven config + env-var lookup
      * ``"default"`` — sqlite mode, no DSN applies
    """

    if os.environ.get("KNOWLEDGE_DATABASE_URL"):
        return "KNOWLEDGE_DATABASE_URL"
    if settings.mode == "sqlite":
        return "default"
    return f"{settings.config_source} + env"


def resolve_pg_dsn(settings: Settings) -> str:
    """Build the libpq DSN, reading credentials from env at the last moment.

    Precedence:
      1. ``KNOWLEDGE_DATABASE_URL`` if set — wins over everything (CI hatch).
      2. Structured ``storage.postgresql`` block + env-var lookup.

    Raises :class:`DsnError` (caller maps to exit code 2) when:
      * mode is sqlite (caller should not have asked)
      * postgresql block missing
      * referenced env var unset
    """

    override = os.environ.get("KNOWLEDGE_DATABASE_URL")
    if override:
        return override

    if settings.mode != "shared_postgresql":
        raise DsnError(
            "storage.mode is 'sqlite'; no PostgreSQL DSN to resolve"
        )
    pg = settings.postgresql
    if pg is None:
        raise DsnError(
            "storage.postgresql block missing in config — "
            "see knowledge/config.example.yaml for a template"
        )

    user = os.environ.get(pg.user_env)
    password = os.environ.get(pg.password_env)
    if not user or not password:
        missing = [
            name
            for name, value in (
                (pg.user_env, user),
                (pg.password_env, password),
            )
            if not value
        ]
        raise DsnError(
            "missing PostgreSQL credentials in environment: "
            f"{', '.join(missing)}. "
            "See knowledge/config.example.env — copy and source it, or set "
            "KNOWLEDGE_DATABASE_URL for a one-shot CI override."
        )

    # URL-encode user/password — passwords can contain ``@``, ``/``, ``:``,
    # which are libpq DSN delimiters and would silently corrupt parsing.
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{pg.host}:{pg.port}/{pg.database}?sslmode={pg.sslmode}"
        f"&connect_timeout={pg.connect_timeout_seconds}"
    )


def mask_dsn(dsn: str) -> str:
    """Replace the password in a libpq URL with ``***`` for display.

    The ``user`` portion keeps its first three chars + ``***``; the
    password is masked entirely. Non-URL DSNs (e.g. ``host=… password=…``
    keyword form) are scrubbed by ``KEY=value`` regex on the password key
    only — host/db remain visible.
    """

    if dsn.startswith(("postgres://", "postgresql://")):
        scheme_end = dsn.find("://") + 3
        rest = dsn[scheme_end:]
        at = rest.rfind("@")
        if at < 0:
            return dsn
        creds, host_part = rest[:at], rest[at:]
        if ":" in creds:
            user, _ = creds.split(":", 1)
        else:
            user = creds
        masked_user = (user[:3] + "***") if user else "***"
        return f"{dsn[:scheme_end]}{masked_user}{host_part}"
    out: list[str] = []
    for part in dsn.split():
        if part.lower().startswith("password="):
            out.append("password=***")
        else:
            out.append(part)
    return " ".join(out)


# ---------------------------------------------------------------------------
# Reporting helpers (used by `knowledge config show` / `check-env`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigReport:
    """Snapshot of the current configuration state, for display / smoke tests."""

    settings: Settings
    dsn_source: str
    dsn_masked: str | None
    env_status: dict[str, bool] = field(default_factory=dict)
    error: str | None = None


def build_report() -> ConfigReport:
    """Best-effort report of the current configuration.

    Never raises: errors that would normally raise (missing env vars,
    malformed config) are captured into ``ConfigReport.error`` so the CLI
    can print them as part of ``config show`` instead of crashing.
    """

    try:
        settings = load_settings()
    except SettingsError as exc:
        return ConfigReport(
            settings=Settings(),
            dsn_source="default",
            dsn_masked=None,
            error=str(exc),
        )

    source = dsn_source(settings)
    env_status: dict[str, bool] = {}
    dsn_masked: str | None = None
    error: str | None = None

    if settings.mode == "shared_postgresql":
        pg = settings.postgresql
        if pg is not None:
            env_status = {
                pg.user_env: bool(os.environ.get(pg.user_env)),
                pg.password_env: bool(os.environ.get(pg.password_env)),
            }
        try:
            dsn_masked = mask_dsn(resolve_pg_dsn(settings))
        except DsnError as exc:
            error = str(exc)

    return ConfigReport(
        settings=settings,
        dsn_source=source,
        dsn_masked=dsn_masked,
        env_status=env_status,
        error=error,
    )
