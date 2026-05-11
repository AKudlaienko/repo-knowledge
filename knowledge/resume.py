"""Session-resume brief: "where did I leave off?"

``knowledge resume`` is the single opinionated command a new session is
supposed to run first. Output is plain text, ~1000–1500 tokens, four
blocks in order:

1. **Last 5 decisions** — topic + one-line decision, newest first.
2. **Most-touched files (7d)** — union of file names the agent wrote
   about in staged/ingested history AND files git saw commits on in the
   last 7 days, intersected with the project's indexed files.
3. **Pending in stage** — any ``history stage`` entries not yet
   ingested, so work that never made it to SQLite doesn't get lost.
4. **Hub files** — top 3 by in-degree from ``file_edges``, the spine
   of the codebase.

Intentionally free of file mtime: ``git checkout``, ``rsync``, and
editor-autosaves all rewrite mtimes unrelated to the agent's focus.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import decisions as decisions_mod
from . import history, paths
from .db import Connection


# Upper bounds for each section — keeps the output within the target
# token budget without an explicit tokenizer. Tune if the shape changes.
_DECISIONS_N = 5
_TOUCHED_N = 10
_HUB_N = 3
_PENDING_PREVIEW_N = 5
_HISTORY_SCAN_N = 50     # recent history entries scanned for file tokens
_HISTORY_DAYS = 7
_GIT_DAYS = 7


# Path-token regex: at least one slash, trailing known extension.
# Broad enough to catch references across all languages we index.
_PATH_RX = re.compile(
    r"\b[a-zA-Z0-9_./\-]+\.(?:py|ts|tsx|js|jsx|mjs|cjs|"
    r"yml|yaml|tf|tfvars|hcl|"
    r"md|json|sh|bash|j2|jinja|jinja2|tpl|tftpl|Dockerfile)\b"
)


@dataclass
class ResumeBrief:
    project_name: str
    project_root: str
    last_decisions: list[decisions_mod.Decision] = field(default_factory=list)
    touched_files: list[tuple[str, int]] = field(default_factory=list)   # (rel_path, score)
    pending_stage: list[dict] = field(default_factory=list)
    hub_files: list[tuple[str, int]] = field(default_factory=list)        # (rel_path, in_degree)
    total_history_entries: int = 0
    total_decisions: int = 0


def build(
    conn: Connection,
    project_id: int,
    project_name: str,
    project_root: Path,
) -> ResumeBrief:
    """Assemble the four-section brief. Pure reads, no writes."""
    last_decisions = decisions_mod.recent(
        conn, project_id=project_id, limit=_DECISIONS_N
    )
    touched = _touched_files(conn, project_id, project_root)
    pending = _pending_stage_entries(project_root)
    hubs = _hub_files(conn, project_id, _HUB_N)

    total_history = conn.execute(
        "SELECT COUNT(*) FROM history WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    total_decisions = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE project_id = ?", (project_id,)
    ).fetchone()[0]

    return ResumeBrief(
        project_name=project_name,
        project_root=str(project_root),
        last_decisions=last_decisions,
        touched_files=touched,
        pending_stage=pending,
        hub_files=hubs,
        total_history_entries=total_history,
        total_decisions=total_decisions,
    )


# ---------------------------------------------------------------------------
# Section assemblers
# ---------------------------------------------------------------------------


def _touched_files(
    conn: Connection,
    project_id: int,
    project_root: Path,
) -> list[tuple[str, int]]:
    """Union of history-mentioned + git-touched paths, intersected with files.

    Score = number of mentions (across history tokens + git commits).
    Higher score = more contextually hot. Output truncated at
    ``_TOUCHED_N``.

    History is the strongest signal — the agent wrote about these files
    explicitly. Git log is a supplementary signal for files the
    human/agent touched but didn't mention in a stage entry.
    """
    counts: dict[str, int] = {}

    # (a) regex scan across recent history
    hist_entries = history.recent(
        conn, project_id=project_id, days=_HISTORY_DAYS, limit=_HISTORY_SCAN_N
    )
    for e in hist_entries:
        blob = " ".join(
            s for s in (e.short_summary, e.long_summary, e.tags) if s
        )
        # Dedupe within one entry: a verbose long_summary mentioning a
        # filename 5× shouldn't out-weight five distinct entries that
        # each name it once.
        seen_in_entry = {m.group(0) for m in _PATH_RX.finditer(blob)}
        for p in seen_in_entry:
            counts[p] = counts.get(p, 0) + 1

    # (b) git log --name-only --since=7.days
    for p in _git_touched_files(project_root, _GIT_DAYS):
        counts[p] = counts.get(p, 0) + 1

    if not counts:
        return []

    # (c) intersect with indexed files. Anything not in files/ — typos,
    # deleted files, out-of-tree refs — drops here. One query, no loop.
    candidates = list(counts.keys())
    placeholders = ",".join("?" * len(candidates))
    rows = conn.execute(
        f"SELECT rel_path FROM files "
        f"WHERE project_id = ? AND rel_path IN ({placeholders})",
        (project_id, *candidates),
    ).fetchall()
    indexed = {r[0] for r in rows}

    scored = [(p, counts[p]) for p in candidates if p in indexed]
    # Sort by score desc, then path asc for stability.
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:_TOUCHED_N]


def _git_touched_files(project_root: Path, days: int) -> set[str]:
    """Files with a commit in the last ``days`` days. ``set`` deduplicates."""
    try:
        out = subprocess.run(
            [
                "git", "-C", str(project_root),
                "log", f"--since={days}.days",
                "--name-only", "--pretty=format:",
            ],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    if out.returncode != 0:
        return set()
    return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}


def _pending_stage_entries(project_root: Path) -> list[dict]:
    """Read staged JSONL entries that haven't been ingested yet.

    Returns a preview (first ``_PENDING_PREVIEW_N`` entries) from all
    ``sess-*.jsonl`` under this project's stage dir. Doesn't include
    ``.inflight-*`` renames (those are owned by a running ingest).
    """
    project_dir = paths.project_stage_dir(project_root)
    if not project_dir.exists():
        return []

    out: list[dict] = []
    for sf in sorted(project_dir.glob("sess-*.jsonl")):
        try:
            raw = sf.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
            if len(out) >= _PENDING_PREVIEW_N:
                return out
    return out


def _hub_files(
    conn: Connection, project_id: int, n: int
) -> list[tuple[str, int]]:
    """Top-N files by in-degree in the project's file_edges graph."""
    rows = conn.execute(
        """
        SELECT f.rel_path, COUNT(*) AS in_degree
        FROM file_edges e
        JOIN files f ON f.id = e.target_file_id
        WHERE e.project_id = ? AND e.target_file_id IS NOT NULL
        GROUP BY e.target_file_id
        ORDER BY in_degree DESC, f.rel_path ASC
        LIMIT ?
        """,
        (project_id, n),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]
