"""Security-focused unit tests for knowledge/settings.py.

Covers:
  H2(b)  host/database with injection characters are percent-encoded in the DSN
  H2(b)  an invalid sslmode raises SettingsError at parse time
  H4     weak-TLS warning fires for remote host + weak sslmode, not for localhost
  L2     mask_dsn fully masks a space-containing keyword password and a URL
         password that contains '@'
"""
from __future__ import annotations

import importlib
import json
import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

import knowledge.settings as settings_mod
from knowledge.settings import (
    Settings,
    PostgresSettings,
    SettingsError,
    mask_dsn,
    resolve_pg_dsn,
    _parse_storage_block,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(
    host: str = "db.example.com",
    database: str = "knowledge",
    sslmode: str = "require",
    user_env: str = "KPGU",
    password_env: str = "KPGP",
) -> Settings:
    """Build a minimal shared_postgresql Settings without touching the filesystem."""
    pg = PostgresSettings(
        host=host,
        database=database,
        sslmode=sslmode,
        user_env=user_env,
        password_env=password_env,
    )
    return Settings(mode="shared_postgresql", postgresql=pg)


def _env(user: str = "alice", password: str = "s3cret") -> dict:
    return {"KPGU": user, "KPGP": password}


# ---------------------------------------------------------------------------
# H2(b) — DSN field injection: host and database are percent-encoded
# ---------------------------------------------------------------------------

class TestDsnEncoding:
    def test_host_with_at_sign_is_encoded(self):
        """host='legit@attacker.com' must not be confused with an @-separator."""
        s = _make_settings(host="legit@attacker.com")
        with patch.dict(os.environ, _env()):
            dsn = resolve_pg_dsn(s)
        # The encoded form replaces @ with %40
        assert "legit%40attacker.com" in dsn
        # The raw @ must NOT appear after the credentials separator
        # (i.e. only one @ in the URL — the cred/host separator)
        assert dsn.count("@") == 1

    def test_host_with_question_mark_is_encoded(self):
        """host='host?injected=1' must not bleed into the query string."""
        s = _make_settings(host="host?injected=1")
        with patch.dict(os.environ, _env()):
            dsn = resolve_pg_dsn(s)
        assert "host%3Finjected%3D1" in dsn
        # The query string portion must start with sslmode, not injected
        assert "sslmode=" in dsn
        assert "injected=1" not in dsn.split("?", 1)[1]

    def test_database_with_at_sign_is_encoded(self):
        """database='x@y' must not be mistaken for a host@db boundary."""
        s = _make_settings(database="x@y")
        with patch.dict(os.environ, _env()):
            dsn = resolve_pg_dsn(s)
        assert "x%40y" in dsn
        assert dsn.count("@") == 1

    def test_database_with_question_mark_is_encoded(self):
        """database='x?host=attacker' must not inject query params."""
        s = _make_settings(database="x?host=attacker")
        with patch.dict(os.environ, _env()):
            dsn = resolve_pg_dsn(s)
        assert "x%3Fhost%3Dattacker" in dsn
        # 'host=attacker' must not appear in the raw query portion
        _, _, qs = dsn.partition("?")
        assert "host=attacker" not in qs

    def test_normal_dsn_structure_preserved(self):
        """A benign host/db produces the expected URL structure."""
        s = _make_settings(host="pg.internal", database="mydb")
        with patch.dict(os.environ, _env(password="p@ss/word")):
            dsn = resolve_pg_dsn(s)
        assert dsn.startswith("postgresql://")
        assert "@pg.internal:" in dsn
        assert "/mydb?" in dsn


# ---------------------------------------------------------------------------
# H2(b) — sslmode allowlist: invalid value raises SettingsError
# ---------------------------------------------------------------------------

class TestSslmodeValidation:
    def _storage_block(self, sslmode: str) -> dict:
        return {
            "mode": "shared_postgresql",
            "postgresql": {
                "host": "db.example.com",
                "sslmode": sslmode,
            },
        }

    @pytest.mark.parametrize("valid", [
        "disable", "allow", "prefer", "require", "verify-ca", "verify-full",
    ])
    def test_valid_sslmode_accepted(self, valid):
        pg, mode = _parse_storage_block(self._storage_block(valid), source="test")
        assert pg.sslmode == valid

    @pytest.mark.parametrize("bad", [
        "REQUIRE", "none", "tls", "ssl", "true", "", "verify_ca",
    ])
    def test_invalid_sslmode_raises(self, bad):
        with pytest.raises(SettingsError, match="sslmode"):
            _parse_storage_block(self._storage_block(bad), source="test")


# ---------------------------------------------------------------------------
# H4 — weak-TLS warning
# ---------------------------------------------------------------------------

class TestTlsWarning:
    """_emit_tls_warning prints to stderr for remote+weak, not for localhost."""

    def setup_method(self):
        # Reset the module-level guard before each test so warnings are fresh.
        settings_mod._tls_warned = False

    def test_warns_for_remote_weak_sslmode(self, capsys):
        settings_mod._emit_tls_warning("db.example.com", "require")
        captured = capsys.readouterr()
        assert "sslmode=require" in captured.err
        assert "verify-full" in captured.err
        # Must not contain DSN or password
        assert "postgresql://" not in captured.err
        assert "secret" not in captured.err

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", ""])
    def test_no_warn_for_localhost(self, host, capsys):
        settings_mod._emit_tls_warning(host, "require")
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("strong", ["verify-ca", "verify-full"])
    def test_no_warn_for_strong_sslmode(self, strong, capsys):
        settings_mod._emit_tls_warning("db.example.com", strong)
        assert capsys.readouterr().err == ""

    def test_warns_at_most_once(self, capsys):
        settings_mod._emit_tls_warning("db.example.com", "prefer")
        settings_mod._emit_tls_warning("db.example.com", "disable")
        out = capsys.readouterr().err
        assert out.count("warning:") == 1


# ---------------------------------------------------------------------------
# L2 — mask_dsn: keyword form fully masks space-containing passwords
# ---------------------------------------------------------------------------

class TestMaskDsn:
    def test_keyword_form_bare_password(self):
        dsn = "host=pg.internal dbname=mydb user=alice password=s3cret"
        assert mask_dsn(dsn) == "host=pg.internal dbname=mydb user=alice password=***"

    def test_keyword_form_password_with_spaces(self):
        """Old split()-based masking leaked 'ret'' for password='sec ret'."""
        dsn = "host=pg.internal password='sec ret' dbname=mydb"
        result = mask_dsn(dsn)
        assert "sec" not in result
        assert "ret" not in result
        assert "password=***" in result

    def test_keyword_form_password_double_quoted(self):
        dsn = 'host=pg.internal password="my pass" dbname=mydb'
        result = mask_dsn(dsn)
        assert "my pass" not in result
        assert "password=***" in result

    def test_url_form_at_in_password(self):
        """URL password containing '@' must still be fully masked."""
        dsn = "postgresql://alice:s3cr%40t@db.host:5432/mydb?sslmode=require"
        result = mask_dsn(dsn)
        assert "s3cr" not in result
        assert "ali***" in result
        # Only the credential-separator @ remains; the one inside creds is gone
        assert "@db.host" in result

    def test_url_form_normal(self):
        dsn = "postgresql://bob:hunter2@pg.host/db"
        result = mask_dsn(dsn)
        assert "hunter2" not in result
        assert "bob***" in result

    def test_url_form_no_creds_unchanged(self):
        dsn = "postgresql://pg.host/db"
        assert mask_dsn(dsn) == dsn
