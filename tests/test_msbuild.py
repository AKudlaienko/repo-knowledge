"""MSBuild chunker + resolver tests.

Covers the whole-file ``msbuild_project`` chunker, the pure
``MSBuildResolver`` (tree-sitter-xml based ``ProjectReference``
extraction), and ``relations._resolve_msbuild`` (path resolution against
a ``FileIndex``). The msbuild chunker/resolver registries aren't wired
up yet (that's a separate orchestrator task), so these tests import the
classes and functions directly rather than going through
``dispatch_chunker``/``dispatch_resolver``.
"""
from __future__ import annotations

from pathlib import Path

from knowledge.chunkers.msbuild_chunker import MSBuildChunker
from knowledge.relations import FileIndex, _resolve_msbuild
from knowledge.resolvers.base import Edge
from knowledge.resolvers.msbuild_resolver import MSBuildResolver


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------


CSPROJ_SOURCE = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="..\\Lib\\Lib.csproj" />
  </ItemGroup>
</Project>
"""


def test_msbuild_chunker_emits_one_whole_file_chunk():
    chunker = MSBuildChunker()
    chunks = chunker.chunk(CSPROJ_SOURCE.encode("utf-8"), Path("Demo.App.csproj"))

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.kind == "msbuild_project"
    assert chunk.name == "Demo.App"
    assert chunk.qualified_name == "Demo.App"
    assert chunk.start_byte == 0
    assert chunk.end_byte == len(CSPROJ_SOURCE.encode("utf-8"))
    assert chunk.text == CSPROJ_SOURCE
    assert chunk.start_line == 1
    assert chunk.end_line == CSPROJ_SOURCE.count("\n") + 1


def test_msbuild_chunker_name_none_without_file_path():
    chunker = MSBuildChunker()
    chunks = chunker.chunk(CSPROJ_SOURCE.encode("utf-8"), None)
    assert len(chunks) == 1
    assert chunks[0].name is None


# ---------------------------------------------------------------------------
# Resolver — extraction
# ---------------------------------------------------------------------------


def _extract(xml: str, path: str = "App.csproj"):
    resolver = MSBuildResolver()
    return resolver.extract(xml.encode("utf-8"), Path(path))


def test_single_project_reference():
    xml = """<Project>
  <ItemGroup>
    <ProjectReference Include="../Lib/Lib.csproj" />
  </ItemGroup>
</Project>
"""
    edges = _extract(xml)
    assert len(edges) == 1
    e = edges[0]
    assert e.kind == "dotnet_project_reference"
    assert e.raw == "../Lib/Lib.csproj"
    assert e.symbol is None
    assert e.line == 3


def test_multiple_project_references():
    xml = """<Project>
  <ItemGroup>
    <ProjectReference Include="../Lib/Lib.csproj" />
    <ProjectReference Include="../Utils/Utils.fsproj" />
  </ItemGroup>
</Project>
"""
    edges = _extract(xml)
    kinds = {e.kind for e in edges}
    assert kinds == {"dotnet_project_reference"}
    raws = {e.raw for e in edges}
    assert raws == {"../Lib/Lib.csproj", "../Utils/Utils.fsproj"}


def test_semicolon_separated_include_list():
    xml = """<Project>
  <ItemGroup>
    <ProjectReference Include="../Lib/Lib.csproj;../Other/Other.vbproj" />
  </ItemGroup>
</Project>
"""
    edges = _extract(xml)
    assert len(edges) == 2
    raws = {e.raw for e in edges}
    assert raws == {"../Lib/Lib.csproj", "../Other/Other.vbproj"}
    assert all(e.kind == "dotnet_project_reference" for e in edges)


def test_windows_backslash_path_preserved_verbatim():
    xml = """<Project>
  <ItemGroup>
    <ProjectReference Include="..\\Lib\\Lib.csproj" />
  </ItemGroup>
</Project>
"""
    edges = _extract(xml)
    assert len(edges) == 1
    assert edges[0].raw == "..\\Lib\\Lib.csproj"
    assert edges[0].kind == "dotnet_project_reference"


def test_property_expression_is_unresolved():
    xml = """<Project>
  <ItemGroup>
    <ProjectReference Include="$(ProjectDir)../Lib/Lib.csproj" />
  </ItemGroup>
</Project>
"""
    edges = _extract(xml)
    assert len(edges) == 1
    assert edges[0].kind == "unresolved"
    assert edges[0].raw == "$(ProjectDir)../Lib/Lib.csproj"


def test_glob_is_unresolved():
    xml = """<Project>
  <ItemGroup>
    <ProjectReference Include="../Libs/**/*.csproj" />
  </ItemGroup>
</Project>
"""
    edges = _extract(xml)
    assert len(edges) == 1
    assert edges[0].kind == "unresolved"
    assert edges[0].raw == "../Libs/**/*.csproj"


def test_semicolon_list_with_mixed_literal_and_property():
    xml = """<Project>
  <ItemGroup>
    <ProjectReference Include="../Lib/Lib.csproj;$(SolutionDir)Shared/Shared.csproj" />
  </ItemGroup>
</Project>
"""
    edges = _extract(xml)
    assert len(edges) == 2
    by_raw = {e.raw: e.kind for e in edges}
    assert by_raw["../Lib/Lib.csproj"] == "dotnet_project_reference"
    assert by_raw["$(SolutionDir)Shared/Shared.csproj"] == "unresolved"


def test_ignored_elements_produce_no_edges():
    xml = """<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.1" />
    <Reference Include="System.Data" />
    <Using Include="System.Linq" />
  </ItemGroup>
  <Import Project="Directory.Build.props" />
</Project>
"""
    edges = _extract(xml)
    assert edges == []


def test_case_insensitive_element_and_attribute_matching():
    xml = """<Project>
  <ItemGroup>
    <projectreference INCLUDE="../Lib/Lib.csproj" />
  </ItemGroup>
</Project>
"""
    edges = _extract(xml)
    assert len(edges) == 1
    assert edges[0].kind == "dotnet_project_reference"
    assert edges[0].raw == "../Lib/Lib.csproj"


def test_empty_include_entries_are_skipped():
    xml = """<Project>
  <ItemGroup>
    <ProjectReference Include="../Lib/Lib.csproj;;" />
  </ItemGroup>
</Project>
"""
    edges = _extract(xml)
    assert len(edges) == 1
    assert edges[0].raw == "../Lib/Lib.csproj"


# ---------------------------------------------------------------------------
# relations._resolve_msbuild
# ---------------------------------------------------------------------------


def _edge(raw: str) -> Edge:
    return Edge(kind="dotnet_project_reference", raw=raw, symbol=None, line=1)


def _index(by_path: dict[str, int]) -> FileIndex:
    return FileIndex(project_id=1, root=Path("/repo"), by_path=by_path)


def test_resolve_msbuild_relative_posix_path():
    index = _index({"src/App/App.csproj": 1, "src/Lib/Lib.csproj": 2})
    edge = _edge("../Lib/Lib.csproj")
    assert _resolve_msbuild(edge, index, "src/App/App.csproj") == 2


def test_resolve_msbuild_windows_separators():
    index = _index({"src/App/App.csproj": 1, "src/Lib/Lib.csproj": 2})
    edge = _edge("..\\Lib\\Lib.csproj")
    assert _resolve_msbuild(edge, index, "src/App/App.csproj") == 2


def test_resolve_msbuild_missing_target_returns_none():
    index = _index({"src/App/App.csproj": 1})
    edge = _edge("../Lib/Lib.csproj")
    assert _resolve_msbuild(edge, index, "src/App/App.csproj") is None


def test_resolve_msbuild_path_escaping_root_returns_none():
    index = _index({"App.csproj": 1})
    edge = _edge("../Outside/Outside.csproj")
    # source_rel has no directory component (project root) — a single
    # ".." already walks above the project root.
    assert _resolve_msbuild(edge, index, "App.csproj") is None


def test_resolve_msbuild_same_directory_reference():
    index = _index({"src/App.csproj": 1, "src/Lib.csproj": 2})
    edge = _edge("Lib.csproj")
    assert _resolve_msbuild(edge, index, "src/App.csproj") == 2


def test_resolve_msbuild_empty_raw_returns_none():
    index = _index({"src/App.csproj": 1})
    edge = _edge("")
    assert _resolve_msbuild(edge, index, "src/App.csproj") is None
