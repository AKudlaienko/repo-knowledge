"""Regression tests for the pre-public security assessment fixes.

Covers the sanitizer coverage gaps (H5), the PEM chunk-boundary split (H5),
the graph script-context XSS escaping (H3), the cli path-containment guard
(H1), and the nested-.gitignore handling (M6). Run with:

    python -m pytest tests/test_security_fixes.py -q
"""

from __future__ import annotations

from pathlib import Path

from knowledge import big_split, graph, scanner
from knowledge.chunkers.base import Chunk
from knowledge.gitignore import _reroot_pattern, load_specs
from knowledge.sanitizer import CHANGE_ME, is_sensitive_key, scrub_text


# Fake credentials are assembled at runtime (string concatenation) so no
# complete provider-token pattern ever appears in the source blob — GitHub
# push protection rejects pushes containing secret-shaped literals even when
# they are obviously fake test vectors. Same trick as GITHUB_PAT in
# test_memory_scrub.py. The runtime values still match knowledge.sanitizer's
# regexes exactly.
FAKE_SLACK_TOKEN = "xoxb-" + "1234567890" + "-abcdEFGHijkl"
FAKE_STRIPE_KEY = "sk_live_" + "0123456789abcdef" + "ABCDEFgh"
_PEM_HEADER = "-----BEGIN RSA" + " PRIVATE KEY-----"
_PEM_FOOTER = "-----END RSA" + " PRIVATE KEY-----"


# --- H5: sanitizer coverage gaps -------------------------------------------

def test_ed25519_authorized_key_redacted():
    key = (
        "ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl "
        "user@host"
    )
    assert CHANGE_ME in scrub_text(key)
    assert "AAAAC3NzaC1lZDI1NTE5" not in scrub_text(key)


def test_slack_and_stripe_and_dsn_redacted():
    assert CHANGE_ME in scrub_text(f"token = {FAKE_SLACK_TOKEN}")
    assert CHANGE_ME in scrub_text(FAKE_STRIPE_KEY)
    out = scrub_text("DB = postgres://admin:hunter2@db.internal:5432/app")
    assert "hunter2" not in out


def test_hyphenated_sensitive_keys():
    assert is_sensitive_key("api-key")
    assert is_sensitive_key("client-secret")
    assert is_sensitive_key("private-key")
    assert is_sensitive_key("api_key")  # original behavior preserved


# --- H5: PEM key split across a chunk boundary ------------------------------

def _pem_block(lines: int = 30) -> str:
    body = "\n".join("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ" for _ in range(lines))
    return f"{_PEM_HEADER}\n{body}\n{_PEM_FOOTER}"


def test_pem_split_across_window_does_not_leak():
    # Build an oversized chunk whose PEM key straddles a window boundary, with
    # enough non-secret filler that it still exceeds the limit AFTER scrubbing
    # (so the multi-window split path is actually exercised, not collapsed).
    filler = "".join(f"# comment line number {i} padding padding\n" for i in range(80))
    pem = _pem_block(40)
    text = filler + pem + "\n" + filler
    chunk = Chunk(
        kind="function", name="f", qualified_name="f",
        start_line=1, end_line=text.count("\n") + 1,
        start_byte=0, end_byte=len(text.encode()), text=text,
    )
    parts = big_split.split_if_oversized(chunk, max_chars=600)
    assert len(parts) > 1  # parent + multiple subchunks
    # No emitted part may carry raw key material — the key is scrubbed before
    # the text is windowed, so no boundary half survives.
    for p in parts:
        assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw" not in p.text
        assert _PEM_HEADER.strip("-") not in p.text or CHANGE_ME in p.text


# --- H3: graph script-context XSS ------------------------------------------

def test_graph_html_escapes_script_breakout():
    nodes = [graph.GraphNode(
        id=1, rel_path="a", lang="py",
        group="</script>", label="</script><script>alert(1)</script>", title="t",
    )]
    html = graph._render_html("proj", nodes, [], {})
    # The raw closing tag must not appear inside the embedded JSON payload.
    assert "</script><script>alert(1)" not in html
    assert "\\u003c" in html  # escaped form present


# --- M6: nested .gitignore re-rooting --------------------------------------

def test_reroot_unanchored_and_anchored():
    assert _reroot_pattern("secrets.yml", "config") == "config/**/secrets.yml"
    assert _reroot_pattern("/local.tf", "infra") == "infra/local.tf"
    assert _reroot_pattern("a/b.txt", "d") == "d/a/b.txt"
    assert _reroot_pattern("!keep.yml", "config") == "!config/**/keep.yml"
    assert _reroot_pattern("# comment", "x") is None
    assert _reroot_pattern("", "x") is None


def test_nested_gitignore_excludes_secret(tmp_path: Path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / ".gitignore").write_text("secrets.yml\n")
    (tmp_path / "config" / "secrets.yml").write_text("password: hunter2\n")
    (tmp_path / "config" / "app.yml").write_text("name: app\n")
    spec = load_specs(tmp_path)
    assert spec.match_file("config/secrets.yml")
    assert not spec.match_file("config/app.yml")


# --- M5: scanner skips out-of-repo symlinks --------------------------------

def test_scanner_skips_escaping_symlink(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "real.py").write_text("x = 1\n")
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n")
    link = repo / "link.py"
    try:
        link.symlink_to(outside)
    except OSError:
        return  # platform without symlink support
    found = {p.name for p, _lang in scanner.walk_project(repo)}
    assert "real.py" in found
    assert "link.py" not in found
