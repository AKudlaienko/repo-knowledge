"""Tests for compact `knowledge decisions` output (todo/tasks/todo.md Item C).

Covers:
  1. `_truncate_words` — the pure truncation helper (boundary, mid-word cut,
     short passthrough, empty/None).
  2. `_print_decisions` — default output truncates decision/why and shortens
     the author; `full=True` round-trips the untruncated text exactly.

No DB/embedder needed — `Decision` is a plain NamedTuple constructed in-memory.
"""
from __future__ import annotations

import time

import pytest

from knowledge.cli import _print_decisions, _print_resume, _short_author, _truncate_words
from knowledge.decisions import Decision
from knowledge.resume import ResumeBrief


# ---------------------------------------------------------------------------
# _truncate_words
# ---------------------------------------------------------------------------

def test_truncate_words_short_text_unchanged():
    assert _truncate_words("short text", 200) == "short text"


def test_truncate_words_exact_limit_unchanged():
    text = "x" * 50
    assert _truncate_words(text, 50) == text


def test_truncate_words_cuts_at_word_boundary():
    text = "one two three four five six seven eight nine ten"
    # limit lands mid-word inside "four"; expect cut back to "three"
    result = _truncate_words(text, 20)
    assert result.endswith("…")
    assert not result[:-1].endswith(" ")
    # everything before the ellipsis must be a prefix of the original text
    assert text.startswith(result[:-1])
    assert len(result) <= 21


def test_truncate_words_no_space_hard_cuts():
    text = "a" * 300
    result = _truncate_words(text, 100)
    assert result == "a" * 100 + "…"


@pytest.mark.parametrize("value", [None, ""])
def test_truncate_words_none_and_empty_passthrough(value):
    assert _truncate_words(value, 200) == value


# ---------------------------------------------------------------------------
# _short_author
# ---------------------------------------------------------------------------

def test_short_author_drops_email():
    assert _short_author("AKudlaienko <kudlayenkoandriy@gmail.com>") == "AKudlaienko"


def test_short_author_plain_name_unchanged():
    assert _short_author("AKudlaienko") == "AKudlaienko"


def test_short_author_bare_email_unchanged():
    assert _short_author("<kudlayenkoandriy@gmail.com>") == "<kudlayenkoandriy@gmail.com>"


def test_short_author_none_passthrough():
    assert _short_author(None) is None


# ---------------------------------------------------------------------------
# _print_decisions — compact default vs. --full round-trip
# ---------------------------------------------------------------------------

LONG_DECISION = (
    "This is a very long decision text that goes on and on describing a "
    "complicated architectural tradeoff involving several subsystems, "
    "round trips, and caching layers that were all considered carefully "
    "before landing on the final approach that we ultimately shipped."
)
LONG_RATIONALE = (
    "The rationale is similarly long-winded and explains in great detail "
    "why the team chose this particular path over several alternatives "
    "that were discussed at length during the review."
)


def _make_decision(**overrides) -> Decision:
    base = dict(
        id=42,
        project_id=1,
        created_at=time.time(),
        topic="test-topic",
        decision=LONG_DECISION,
        rationale=LONG_RATIONALE,
        files_touched=["knowledge/cli.py"],
        session_id=None,
        author="AKudlaienko <kudlayenkoandriy@gmail.com>",
        supersedes=None,
        override_reason=None,
    )
    base.update(overrides)
    return Decision(**base)


def test_print_decisions_default_truncates(capsys):
    entries = [(_make_decision(), None)]
    _print_decisions(entries)
    out = capsys.readouterr().out

    assert LONG_DECISION not in out
    assert LONG_RATIONALE not in out
    assert "…" in out
    assert "by AKudlaienko" in out
    assert "kudlayenkoandriy@gmail.com" not in out


def test_print_decisions_full_roundtrips_verbatim(capsys):
    entries = [(_make_decision(), None)]
    _print_decisions(entries, full=True)
    out = capsys.readouterr().out

    assert LONG_DECISION in out
    assert LONG_RATIONALE in out
    assert "by AKudlaienko <kudlayenkoandriy@gmail.com>" in out


def test_print_decisions_default_blank_line_between_entries(capsys):
    entries = [(_make_decision(id=1), None), (_make_decision(id=2), None)]
    _print_decisions(entries)
    out = capsys.readouterr().out
    assert "\n\n" in out


def test_print_decisions_full_no_blank_line_between_entries(capsys):
    entries = [(_make_decision(id=1), None), (_make_decision(id=2), None)]
    _print_decisions(entries, full=True)
    out = capsys.readouterr().out
    assert "\n\n" not in out


def test_print_decisions_empty():
    # Should not raise; prints the "(no decisions)" placeholder.
    _print_decisions([])


# ---------------------------------------------------------------------------
# `[fact]` marker (Item H) — compact printer + resume, both kinds
# ---------------------------------------------------------------------------

def test_print_decisions_fact_marker_compact(capsys):
    entries = [(_make_decision(kind="fact"), None)]
    _print_decisions(entries)
    out = capsys.readouterr().out
    assert "topic:    [fact] test-topic" in out


def test_print_decisions_fact_marker_full(capsys):
    entries = [(_make_decision(kind="fact"), None)]
    _print_decisions(entries, full=True)
    out = capsys.readouterr().out
    assert "topic:    [fact] test-topic" in out


def test_print_decisions_plain_decision_no_marker(capsys):
    entries = [(_make_decision(kind="decision"), None)]
    _print_decisions(entries)
    out = capsys.readouterr().out
    assert "[fact]" not in out


def test_print_resume_fact_marker():
    fact = _make_decision(kind="fact", topic="pg-types-cache-stale-oid")
    decision = _make_decision(kind="decision", topic="cache invalidation")
    brief = ResumeBrief(
        project_name="repo-knowledge",
        project_root="/tmp/repo-knowledge",
        last_decisions=[fact, decision],
    )
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_resume(brief)
    out = buf.getvalue()
    assert "[fact] pg-types-cache-stale-oid" in out
    assert "cache invalidation" in out
    # The plain decision's line must NOT carry the marker.
    for line in out.splitlines():
        if "cache invalidation" in line:
            assert "[fact]" not in line
