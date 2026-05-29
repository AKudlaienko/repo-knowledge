"""Static cartography — per-file / per-dir / repo briefs.

Answers three questions an LLM agent asks early in any session:

* ``why <path>`` — what is this file, who uses it, who does it use?
* ``map [--dir D]`` — what does this subtree look like?
* ``brief`` — what does the whole repo look like?

All outputs are pure JOINs over data the indexer already extracted.
No LLM, no new storage, no re-embedding. The map is the value: ``chunks``
tells us the symbol kinds per file, ``files`` tells us sizes and langs,
``file_edges`` tells us who depends on whom.

Output is plain text optimized for an LLM reader — compact, predictable,
parseable. No markdown.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import db
from .db import Connection


# Kinds that represent structure/chrome rather than a user-authored symbol.
# Filtered out of "top symbols" listings so ``why`` and ``map`` surface
# what matters — real functions, classes, resources, tasks, etc.
_NON_SYMBOL_KINDS = frozenset({
    "big_parent",
    "big_subchunk",
    "module_level",
    "markdown_section",
    "yaml_doc",          # top-level YAML wrapper, almost never the "point" of a file
    "terraform_config",  # terraform { } block header
    "locals_block",      # container; locals_entry rows are the real symbols
    "helm_values_section",
    "json_object",       # generic JSON top-level
    "jinja_block",       # low-signal, usually wraps something else
})


@dataclass
class FileBrief:
    """One-file summary. Presentation concerns handled by caller."""
    rel_path: str
    lang: str
    size: int
    loc: int                       # lines of code (end_line of the widest chunk, else 0)
    last_commit_date: str | None   # ISO date, or None if not a git repo / path untracked
    description: str | None        # first meaningful doc line (docstring, heading, comment)
    top_symbols: list[tuple[str, str, int, int, int]]  # (kind, name, start, end, char_count)
    inbound: list[tuple[str, str]]   # (source_rel_path, edge_kind)
    outbound: list[tuple[str, str]]  # (target_rel_path, edge_kind)


def why(
    conn: Connection,
    rel_path: str,
    project_id: int,
    project_root: Path,
    *,
    top_n: int = 5,
    neighbors_n: int = 3,
) -> FileBrief | None:
    """Return a :class:`FileBrief` for ``rel_path``, or None if not indexed."""
    file_row = db.fetch_one(
        conn,
        "SELECT id, lang, size FROM files WHERE project_id = ? AND rel_path = ?",
        (project_id, rel_path),
    )
    if file_row is None:
        return None
    file_id, lang, size = file_row

    description = _extract_description(conn, file_id)
    top_symbols = _top_symbols(conn, file_id, top_n)
    loc = _file_loc(conn, file_id)
    last_commit = _last_commit_date(project_root, rel_path)
    inbound = _inbound_neighbors(conn, file_id, neighbors_n)
    outbound = _outbound_neighbors(conn, file_id, neighbors_n)

    return FileBrief(
        rel_path=rel_path,
        lang=lang,
        size=size,
        loc=loc,
        last_commit_date=last_commit,
        description=description,
        top_symbols=top_symbols,
        inbound=inbound,
        outbound=outbound,
    )


# ---------------------------------------------------------------------------
# map() — directory tree with per-dir aggregates
# ---------------------------------------------------------------------------


@dataclass
class DirEntry:
    dir_path: str                  # repo-relative; "" for repo root
    file_count: int
    dominant_lang: str | None
    top_kinds: list[tuple[str, int]]  # (kind, count), top 3 non-structural
    entrypoint: str | None         # highest in-degree file rooted in this subtree


# Upper bound on rows emitted from map() so we don't pour ~50 directories of
# Terraform / Ansible / Helm into the LLM context. Callers print a warning
# when the cut fires; the `--dir` flag lets the user narrow the scope.
_MAP_HARD_LIMIT = 200


def map_tree(
    conn: Connection,
    project_id: int,
    *,
    dir_filter: str | None = None,
    depth: int = 2,
) -> tuple[list[DirEntry], bool]:
    """Return (entries, was_truncated).

    Groups files by the first ``depth`` path components. ``dir_filter``
    scopes to files whose rel_path starts with that prefix — useful for
    ``knowledge map --dir terraform``.
    """
    where = ["project_id = ?"]
    params: list = [project_id]
    if dir_filter:
        prefix = dir_filter.rstrip("/")
        where.append("(rel_path = ? OR rel_path LIKE ?)")
        params.extend([prefix, f"{prefix}/%"])

    files = db.fetch_all(
        conn,
        f"SELECT id, rel_path, lang FROM files WHERE {' AND '.join(where)}",
        tuple(params),
    )

    # Bucket files by dir key. Root-level files (no "/") use "" as the key.
    # depth=N keeps the first N components — "a/b/c/d.py" at depth=2 → "a/b".
    buckets: dict[str, list[tuple[int, str, str]]] = {}
    for fid, rel, lang in files:
        parts = rel.split("/")
        if len(parts) == 1:
            key = ""
        else:
            key = "/".join(parts[:depth])
        buckets.setdefault(key, []).append((fid, rel, lang))

    entries: list[DirEntry] = []
    truncated = False
    for key in sorted(buckets.keys()):
        rows = buckets[key]
        if len(entries) >= _MAP_HARD_LIMIT:
            truncated = True
            break
        entries.append(_build_dir_entry(conn, key, rows))
    return entries, truncated


# ---------------------------------------------------------------------------
# brief() — repo-level summary
# ---------------------------------------------------------------------------


@dataclass
class RepoBrief:
    project_name: str
    project_root: str
    file_count: int
    chunk_count: int
    edge_count: int
    top_langs: list[tuple[str, int]]       # (lang, file_count), top 5
    hub_files: list[tuple[str, int]]        # (rel_path, in_degree), top 10
    last_updated: float | None


def brief(
    conn: Connection,
    project_id: int,
    project_name: str,
    project_root: str,
    last_updated: float | None,
) -> RepoBrief:
    """Repo-level snapshot. Single pass over each table, no joins needed."""
    file_count = db.fetch_one(
        conn, "SELECT COUNT(*) FROM files WHERE project_id = ?", (project_id,)
    )[0]
    chunk_count = db.fetch_one(
        conn, "SELECT COUNT(*) FROM chunks WHERE project_id = ?", (project_id,)
    )[0]
    edge_count = db.fetch_one(
        conn, "SELECT COUNT(*) FROM file_edges WHERE project_id = ?", (project_id,)
    )[0]

    top_langs_rows = db.fetch_all(
        conn,
        "SELECT lang, COUNT(*) AS n FROM files WHERE project_id = ? "
        "GROUP BY lang ORDER BY n DESC LIMIT 5",
        (project_id,),
    )
    top_langs = [(r[0], r[1]) for r in top_langs_rows]

    # Hub files = highest in-degree (most-referenced by others). Filters out
    # NULL target_file_id (external / unresolved edges). f.rel_path included
    # in GROUP BY for PostgreSQL strictness (sqlite is lenient).
    hub_rows = db.fetch_all(
        conn,
        """
        SELECT f.rel_path, COUNT(*) AS in_degree
        FROM file_edges e
        JOIN files f ON f.id = e.target_file_id
        WHERE e.project_id = ? AND e.target_file_id IS NOT NULL
        GROUP BY e.target_file_id, f.rel_path
        ORDER BY in_degree DESC, f.rel_path ASC
        LIMIT 10
        """,
        (project_id,),
    )
    hub_files = [(r[0], r[1]) for r in hub_rows]

    return RepoBrief(
        project_name=project_name,
        project_root=project_root,
        file_count=file_count,
        chunk_count=chunk_count,
        edge_count=edge_count,
        top_langs=top_langs,
        hub_files=hub_files,
        last_updated=last_updated,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _extract_description(conn: Connection, file_id: int) -> str | None:
    """First meaningful line describing the file.

    Priority, by chunk kind and position:
      1. ``module_level`` (Python): first docstring line — the chunk whose
         stored_text starts with \"\"\" or '''.
      2. ``markdown_section``: lowest-line section, first non-header line.
      3. Any chunk at line 1: first non-blank line of its stored_text.

    Returns ``None`` when no readable description exists — a config file
    with no header comment, for instance.
    """
    # Try module_level chunks first (Python). They include the file's
    # top-level docstring if present.
    rows = db.fetch_all(
        conn,
        "SELECT kind, stored_text FROM chunks "
        "WHERE file_id = ? AND kind IN ('module_level', 'markdown_section') "
        "ORDER BY start_line ASC LIMIT 3",
        (file_id,),
    )

    for kind, stored in rows:
        desc = _first_meaningful_line(stored, kind)
        if desc:
            return desc

    # Fallback: any chunk at line 1.
    row = db.fetch_one(
        conn,
        "SELECT stored_text FROM chunks "
        "WHERE file_id = ? AND start_line <= 3 "
        "ORDER BY start_line ASC LIMIT 1",
        (file_id,),
    )
    if row:
        return _first_meaningful_line(row[0], None)
    return None


def _first_meaningful_line(stored: str, kind: str | None) -> str | None:
    """Pull a one-line summary from a chunk's stored_text.

    Handles the common header shapes:
      * Python triple-quoted docstrings — strip the quotes.
      * Markdown — skip the ``#`` header, take the first body line.
      * YAML / shell / HCL — skip blank and comment-only lines is fine
        but a leading ``#`` comment often IS the description, so keep it
        sans the ``#``.
    """
    from .whitespace import decompress

    text = decompress(stored)
    lines = text.splitlines()

    # Python docstring: skip the opening quote line, take first body line.
    for i, raw in enumerate(lines):
        s = raw.strip()
        if s.startswith(('"""', "'''")):
            rest = s.strip('"\' ')
            if rest:
                return rest[:160]
            # Docstring on its own line → descend to next non-blank
            for j in range(i + 1, min(i + 5, len(lines))):
                nxt = lines[j].strip().strip('"\'')
                if nxt:
                    return nxt[:160]
            break

    if kind == "markdown_section":
        # Skip header (# ...) lines, return first non-blank body line.
        for raw in lines:
            s = raw.strip()
            if s and not s.startswith("#"):
                return s[:160]

    # Generic fallback: first non-blank non-quote line.
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if s.startswith(('"""', "'''")):
            continue
        # Strip single leading comment marker to expose the content.
        for marker in ("# ", "// ", "-- "):
            if s.startswith(marker):
                s = s[len(marker):]
                break
        return s[:160]
    return None


def _top_symbols(
    conn: Connection, file_id: int, n: int
) -> list[tuple[str, str, int, int, int]]:
    """Top-N named symbols by char_count, excluding structural kinds.

    Returns rows of ``(kind, name or qualified_name, start, end, char_count)``.
    Rows with NULL name AND NULL qualified_name are dropped — there's
    nothing to display.
    """
    placeholders = ",".join("?" * len(_NON_SYMBOL_KINDS))
    rows = db.fetch_all(
        conn,
        f"""
        SELECT kind,
               COALESCE(qualified_name, name) AS display_name,
               start_line, end_line, char_count
        FROM chunks
        WHERE file_id = ?
          AND kind NOT IN ({placeholders})
          AND (name IS NOT NULL OR qualified_name IS NOT NULL)
        ORDER BY char_count DESC, start_line ASC
        LIMIT ?
        """,
        (file_id, *_NON_SYMBOL_KINDS, n),
    )
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def _file_loc(conn: Connection, file_id: int) -> int:
    """Lines-of-code approximation: highest end_line in any chunk.

    Files with no chunks return 0 — fine; we're not claiming byte-exact LOC.
    """
    row = db.fetch_one(
        conn, "SELECT MAX(end_line) FROM chunks WHERE file_id = ?", (file_id,)
    )
    return int(row[0] or 0)


def _last_commit_date(project_root: Path, rel_path: str) -> str | None:
    """Git's view of when this path last moved — ``git log -1 --format=%cs``.

    Returns ISO date (``YYYY-MM-DD``). None if git isn't available, the
    path isn't tracked, or the repo has no commits.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "log", "-1", "--format=%cs", "--", rel_path],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    date = out.stdout.strip()
    return date or None


def _inbound_neighbors(
    conn: Connection, file_id: int, n: int
) -> list[tuple[str, str]]:
    """Top-N files that reference this file. ``(source_rel_path, edge_kind)``."""
    rows = db.fetch_all(
        conn,
        """
        SELECT f.rel_path, e.kind
        FROM file_edges e
        JOIN files f ON f.id = e.source_file_id
        WHERE e.target_file_id = ?
        ORDER BY f.rel_path ASC
        LIMIT ?
        """,
        (file_id, n),
    )
    return [(r[0], r[1]) for r in rows]


def _outbound_neighbors(
    conn: Connection, file_id: int, n: int
) -> list[tuple[str, str]]:
    """Top-N files this file references (target not NULL). ``(target_rel_path, kind)``."""
    rows = db.fetch_all(
        conn,
        """
        SELECT f.rel_path, e.kind
        FROM file_edges e
        JOIN files f ON f.id = e.target_file_id
        WHERE e.source_file_id = ? AND e.target_file_id IS NOT NULL
        ORDER BY f.rel_path ASC
        LIMIT ?
        """,
        (file_id, n),
    )
    return [(r[0], r[1]) for r in rows]


def _build_dir_entry(
    conn: Connection,
    dir_key: str,
    files_in_bucket: list[tuple[int, str, str]],
) -> DirEntry:
    """Summarize one directory bucket.

    Counts per-dir symbol kinds (excluding structural chrome) and picks the
    single file with the highest project-wide in-degree as the bucket's
    ``entrypoint`` — a crude but effective "read this first" signal.
    """
    file_ids = [fid for fid, _rel, _lang in files_in_bucket]

    # Dominant language = mode over the bucket. Ties broken alphabetically.
    lang_counts: dict[str, int] = {}
    for _fid, _rel, lang in files_in_bucket:
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
    dominant_lang = (
        max(lang_counts.items(), key=lambda kv: (kv[1], -ord(kv[0][0])))[0]
        if lang_counts else None
    )

    # Top 3 non-structural chunk kinds in this bucket.
    placeholders_ids = ",".join("?" * len(file_ids))
    placeholders_kinds = ",".join("?" * len(_NON_SYMBOL_KINDS))
    kind_rows = db.fetch_all(
        conn,
        f"""
        SELECT kind, COUNT(*) AS n FROM chunks
        WHERE file_id IN ({placeholders_ids})
          AND kind NOT IN ({placeholders_kinds})
        GROUP BY kind ORDER BY n DESC LIMIT 3
        """,
        (*file_ids, *_NON_SYMBOL_KINDS),
    )
    top_kinds = [(r[0], r[1]) for r in kind_rows]

    # Highest in-degree file in this bucket → "the one to read first".
    # f.rel_path in GROUP BY for PG strictness.
    entry_row = db.fetch_one(
        conn,
        f"""
        SELECT f.rel_path, COUNT(*) AS in_degree
        FROM file_edges e
        JOIN files f ON f.id = e.target_file_id
        WHERE e.target_file_id IN ({placeholders_ids})
        GROUP BY e.target_file_id, f.rel_path
        ORDER BY in_degree DESC, f.rel_path ASC
        LIMIT 1
        """,
        tuple(file_ids),
    )
    entrypoint = entry_row[0] if entry_row else None

    return DirEntry(
        dir_path=dir_key,
        file_count=len(files_in_bucket),
        dominant_lang=dominant_lang,
        top_kinds=top_kinds,
        entrypoint=entrypoint,
    )
