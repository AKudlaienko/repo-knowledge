"""End-to-end test: `knowledge build` over a temporary .NET repo.

Written alongside the .NET language support plan (``tasks/net_implementation.md``,
Test Plan bullet "End-to-end temporary-repository test asserting files, chunks,
names, and project edges after `knowledge build` with the embedder stubbed").

Builds a small multi-project repo on disk (C# app, F# library, VB "legacy"
project) and drives it through the real ``knowledge.indexer.build_project``
entry point — the same internal API ``knowledge build`` calls — rather than
re-testing individual chunkers/resolvers (already covered by
``test_csharp_chunker.py``, ``test_fsharp_chunker.py``, ``test_vb_chunker.py``,
``test_msbuild.py``). This proves the whole pipeline wires together: scanner
extension dispatch -> chunker dispatch -> resolver dispatch -> edge
resolution against the real ``files`` table, end to end.

The real sentence-transformer is expensive to load in unit tests, so
``knowledge.indexer.get_local_embedder`` (the name ``build_project`` actually
calls — bound via ``from .embedder import get_local_embedder`` at import
time) is patched with a stub that returns zero-vectors of
``config.EMBEDDING_DIM``, mirroring the ``stub_embedder`` fixture pattern in
``tests/test_memory_scrub.py``.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from knowledge import config, db as db_mod, indexer as indexer_mod

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db():
    """Yield an open SQLite connection backed by a temp file, then close it."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = Path(f.name)

    conn = db_mod.connect_sqlite(db_path)
    yield conn
    try:
        conn.close()
    except Exception:
        pass
    db_path.unlink(missing_ok=True)


@pytest.fixture()
def stub_embedder():
    """Patch get_local_embedder() so the build never loads the 130 MB model."""
    dim = config.EMBEDDING_DIM
    stub = MagicMock()
    stub.encode.side_effect = lambda texts: np.zeros((len(texts), dim), dtype=np.float32)

    with patch.object(indexer_mod, "get_local_embedder", return_value=stub):
        yield stub


@pytest.fixture()
def dotnet_repo(tmp_path):
    """Build a small multi-project .NET repo: C# app, F# lib, VB legacy proj.

    App -> Lib (csproj ProjectReference, Windows backslashes, proves
    separator normalization); Legacy -> App and Legacy -> Lib (vbproj,
    semicolon-separated Include list).
    """
    root = tmp_path / "repo"

    app_dir = root / "src" / "App"
    lib_dir = root / "src" / "Lib"
    legacy_dir = root / "src" / "Legacy"
    for d in (app_dir, lib_dir, legacy_dir):
        d.mkdir(parents=True)

    (app_dir / "Program.cs").write_text(
        """using System;

namespace Demo.App;

public class Program
{
    public static void Main(string[] args)
    {
        Console.WriteLine("hello");
    }
}
""",
        encoding="utf-8",
    )

    # Windows-style backslash separator on purpose — proves relations.py
    # normalizes `\` to `/` before resolving against the FileIndex.
    (app_dir / "App.csproj").write_text(
        """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="..\\Lib\\Lib.fsproj" />
  </ItemGroup>
</Project>
""",
        encoding="utf-8",
    )

    (lib_dir / "Library.fs").write_text(
        """namespace Demo

module Library =
    let add x y = x + y
""",
        encoding="utf-8",
    )

    (lib_dir / "Lib.fsproj").write_text(
        """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
""",
        encoding="utf-8",
    )

    (legacy_dir / "Thing.vb").write_text(
        """Namespace Demo.Legacy

    Public Class Thing
        Public Sub DoWork()
        End Sub
    End Class

End Namespace
""",
        encoding="utf-8",
    )

    # Semicolon-separated Include list referencing both other projects.
    (legacy_dir / "Legacy.vbproj").write_text(
        """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="../App/App.csproj;../Lib/Lib.fsproj" />
  </ItemGroup>
</Project>
""",
        encoding="utf-8",
    )

    return root


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _files_by_rel_path(conn, project_id) -> dict[str, tuple[int, str]]:
    """rel_path -> (file_id, lang)."""
    rows = conn.execute(
        "SELECT id, rel_path, lang FROM files WHERE project_id = ?", (project_id,)
    ).fetchall()
    return {rel_path: (fid, lang) for fid, rel_path, lang in rows}


def _chunks(conn, project_id):
    return conn.execute(
        "SELECT kind, name, qualified_name, file_id FROM chunks WHERE project_id = ?",
        (project_id,),
    ).fetchall()


def _edges(conn, project_id):
    return conn.execute(
        "SELECT source_file_id, target_file_id, kind, raw FROM file_edges "
        "WHERE project_id = ?",
        (project_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_build_project_indexes_dotnet_repo_end_to_end(tmp_db, stub_embedder, dotnet_repo):
    project_id, files_indexed, chunks_embedded = indexer_mod.build_project(
        tmp_db, dotnet_repo, verbose=False
    )

    assert files_indexed == 6
    assert chunks_embedded > 0

    # --- files: all 6 indexed with the correct lang tags ---
    files = _files_by_rel_path(tmp_db, project_id)
    assert set(files) == {
        "src/App/Program.cs",
        "src/App/App.csproj",
        "src/Lib/Library.fs",
        "src/Lib/Lib.fsproj",
        "src/Legacy/Thing.vb",
        "src/Legacy/Legacy.vbproj",
    }
    assert files["src/App/Program.cs"][1] == "csharp"
    assert files["src/App/App.csproj"][1] == "msbuild"
    assert files["src/Lib/Library.fs"][1] == "fsharp"
    assert files["src/Lib/Lib.fsproj"][1] == "msbuild"
    assert files["src/Legacy/Thing.vb"][1] == "visual_basic"
    assert files["src/Legacy/Legacy.vbproj"][1] == "msbuild"

    # --- chunks: expected kinds + qualified names present ---
    chunks = _chunks(tmp_db, project_id)

    program_class = next(
        c for c in chunks if c[0] == "class" and c[2] == "Demo.App.Program"
    )
    assert program_class[1] == "Program"
    assert program_class[3] == files["src/App/Program.cs"][0]

    add_fn = next(
        c for c in chunks if c[0] == "function" and c[2] == "Demo.Library.add"
    )
    assert add_fn[1] == "add"
    assert add_fn[3] == files["src/Lib/Library.fs"][0]

    thing_class = next(
        c for c in chunks if c[0] == "class" and c[2] == "Demo.Legacy.Thing"
    )
    assert thing_class[1] == "Thing"
    assert thing_class[3] == files["src/Legacy/Thing.vb"][0]

    msbuild_chunks = [c for c in chunks if c[0] == "msbuild_project"]
    assert len(msbuild_chunks) == 3
    msbuild_names = {c[1] for c in msbuild_chunks}
    assert msbuild_names == {"App", "Lib", "Legacy"}
    msbuild_by_file = {c[3]: c[1] for c in msbuild_chunks}
    assert msbuild_by_file[files["src/App/App.csproj"][0]] == "App"
    assert msbuild_by_file[files["src/Lib/Lib.fsproj"][0]] == "Lib"
    assert msbuild_by_file[files["src/Legacy/Legacy.vbproj"][0]] == "Legacy"

    # --- file_edges: dotnet_project_reference rows, resolved for all 3 ---
    edges = _edges(tmp_db, project_id)
    dotnet_edges = [e for e in edges if e[2] == "dotnet_project_reference"]
    assert len(dotnet_edges) == 3

    by_source_target = {(e[0], e[1]) for e in dotnet_edges}
    app_id = files["src/App/App.csproj"][0]
    lib_id = files["src/Lib/Lib.fsproj"][0]
    legacy_id = files["src/Legacy/Legacy.vbproj"][0]

    assert (app_id, lib_id) in by_source_target        # App -> Lib (backslash raw)
    assert (legacy_id, app_id) in by_source_target      # Legacy -> App
    assert (legacy_id, lib_id) in by_source_target      # Legacy -> Lib

    # Every resolved edge actually has a non-NULL target — proves the
    # backslash-separated raw in App.csproj normalized and resolved, not
    # just parsed.
    for _src, target, _kind, _raw in dotnet_edges:
        assert target is not None

    # The App -> Lib edge preserves the literal backslash raw verbatim.
    app_to_lib = next(e for e in dotnet_edges if e[0] == app_id)
    assert app_to_lib[3] == "..\\Lib\\Lib.fsproj"

    # The embedder was actually invoked (build never loaded the real model).
    assert stub_embedder.encode.called
