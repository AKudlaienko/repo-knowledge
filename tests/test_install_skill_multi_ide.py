"""Tests for `knowledge skill show` and the `--ide gemini` install target
(tasks/todo.md Item D).

Covers:
  1. `cmd_skill_show` — prints the full canonical skill body, frontmatter
     stripped, straight from the packaged skill-template/SKILL.md.
  2. `--ide gemini` — GEMINI.md is written via the same managed-block merge
     machinery as AGENTS.md: idempotent re-installs, foreign content preserved.
  3. `--ide all` now covers five IDEs (claude, cursor, codex, opencode, gemini).

No DB/embedder needed — these exercise filesystem effects only, isolated to
tmp_path via monkeypatch.chdir / Path.home patching.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from knowledge import cli


def _install_args(**overrides) -> argparse.Namespace:
    base = dict(ide="claude", user=False, symlink=False, always_apply=False, force=False)
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# `knowledge skill show`
# ---------------------------------------------------------------------------


def test_skill_show_prints_full_body(capsys) -> None:
    rc = cli.cmd_skill_show(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out

    src_dir = Path(cli.__file__).resolve().parent.parent / "skill-template"
    skill_text = (src_dir / "SKILL.md").read_text(encoding="utf-8")
    from knowledge import skill_render

    expected_body = skill_render.strip_frontmatter(skill_text).lstrip("\n")
    assert expected_body.rstrip("\n") in out
    # Frontmatter must not leak into the printed output.
    assert not out.startswith("---\n")
    assert "name: knowledge" not in out


# ---------------------------------------------------------------------------
# `--ide gemini` / `--ide all`
# ---------------------------------------------------------------------------


def test_ide_targets_include_gemini_and_total_five() -> None:
    assert "gemini" in cli._IDE_TARGETS
    assert len(cli._IDE_TARGETS) == 5


def test_parse_ides_all_expands_to_five() -> None:
    ides = cli._parse_ides("all")
    assert ides is not None
    assert len(ides) == 5
    assert set(ides) == {"claude", "cursor", "codex", "opencode", "gemini"}


def test_install_skill_gemini_project_scope_creates_gemini_md(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    rc = cli.cmd_install_skill(_install_args(ide="gemini"))
    assert rc == 0
    gemini_md = tmp_path / "GEMINI.md"
    assert gemini_md.is_file()
    content = gemini_md.read_text(encoding="utf-8")
    assert cli._AGENTS_BLOCK_BEGIN in content
    assert cli._AGENTS_BLOCK_END in content
    assert "Full guide: run `knowledge skill show`." in content


def test_install_skill_gemini_user_scope(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)  # keep project-scope settings resolution harmless
    rc = cli.cmd_install_skill(_install_args(ide="gemini", user=True))
    assert rc == 0
    assert (tmp_path / ".gemini" / "GEMINI.md").is_file()


def test_install_skill_gemini_merge_is_idempotent_and_preserves_foreign_content(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cli.cmd_install_skill(_install_args(ide="gemini"))
    gemini_md = tmp_path / "GEMINI.md"

    foreign = "\n# my project notes\nhello world\n"
    with gemini_md.open("a", encoding="utf-8") as fh:
        fh.write(foreign)

    # Re-install: managed block gets replaced, foreign content survives, and a
    # second re-install doesn't accumulate duplicate blocks or foreign content.
    cli.cmd_install_skill(_install_args(ide="gemini"))
    once = gemini_md.read_text(encoding="utf-8")
    assert "my project notes" in once
    assert once.count(cli._AGENTS_BLOCK_BEGIN) == 1
    assert once.count(cli._AGENTS_BLOCK_END) == 1

    cli.cmd_install_skill(_install_args(ide="gemini"))
    twice = gemini_md.read_text(encoding="utf-8")
    assert once == twice


def test_install_skill_all_writes_five_targets_without_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    rc = cli.cmd_install_skill(_install_args(ide="all"))
    assert rc == 0
    assert (tmp_path / ".claude" / "skills" / "knowledge" / "SKILL.md").is_file()
    assert (tmp_path / ".cursor" / "rules" / "knowledge.mdc").is_file()
    assert (tmp_path / "AGENTS.md").is_file()  # shared by codex + opencode
    assert (tmp_path / "GEMINI.md").is_file()


@pytest.mark.parametrize("ide", ["claude", "cursor", "codex", "opencode", "gemini"])
def test_install_skill_each_ide_individually(tmp_path, monkeypatch, ide) -> None:
    monkeypatch.chdir(tmp_path)
    rc = cli.cmd_install_skill(_install_args(ide=ide))
    assert rc == 0
