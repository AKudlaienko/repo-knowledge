"""Scanner + registry tests for the .NET language wiring.

Written alongside the .NET language support plan (``tasks/net_implementation.md``,
Test Plan bullet "Scanner and registry tests for every new extension and
language tag"). The per-chunker unit tests (``test_csharp_chunker.py``,
``test_fsharp_chunker.py``, ``test_vb_chunker.py``, ``test_msbuild.py``)
import chunker/resolver classes directly and were written before the
registries were wired up, so they deliberately bypass ``dispatch_chunker``/
``dispatch_resolver``. This file covers the wiring itself: every new
extension in ``config.EXT_TO_LANG`` classifies to the right language tag,
``dispatch_chunker`` returns the right chunker instance for that tag, and
``dispatch_resolver`` returns ``MSBuildResolver`` for ``msbuild`` and ``[]``
for the three source languages (their ``using``/``open``/``Imports``
statements reference namespaces, not files — see
``knowledge/resolvers/__init__.py`` docstring).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from knowledge import config
from knowledge.chunkers import dispatch_chunker
from knowledge.chunkers.csharp_chunker import CSharpChunker
from knowledge.chunkers.fsharp_chunker import FSharpChunker
from knowledge.chunkers.msbuild_chunker import MSBuildChunker
from knowledge.chunkers.vb_chunker import VisualBasicChunker
from knowledge.resolvers import dispatch_resolver
from knowledge.resolvers.msbuild_resolver import MSBuildResolver
from knowledge.scanner import classify_file

# (extension, expected lang tag, expected chunker class)
DOTNET_EXTENSIONS: list[tuple[str, str, type]] = [
    (".cs", "csharp", CSharpChunker),
    (".csx", "csharp", CSharpChunker),
    (".fs", "fsharp", FSharpChunker),
    (".fsi", "fsharp", FSharpChunker),
    (".fsx", "fsharp", FSharpChunker),
    (".vb", "visual_basic", VisualBasicChunker),
    (".csproj", "msbuild", MSBuildChunker),
    (".fsproj", "msbuild", MSBuildChunker),
    (".vbproj", "msbuild", MSBuildChunker),
]


@pytest.mark.parametrize("ext, expected_lang, _chunker_cls", DOTNET_EXTENSIONS)
def test_ext_to_lang_mapping(ext, expected_lang, _chunker_cls):
    assert config.EXT_TO_LANG[ext] == expected_lang


@pytest.mark.parametrize("ext, expected_lang, _chunker_cls", DOTNET_EXTENSIONS)
def test_classify_file_yields_expected_lang(ext, expected_lang, _chunker_cls):
    assert classify_file(Path(f"src/Demo{ext}")) == expected_lang


@pytest.mark.parametrize("ext, expected_lang, chunker_cls", DOTNET_EXTENSIONS)
def test_dispatch_chunker_returns_expected_class(ext, expected_lang, chunker_cls):
    chunker = dispatch_chunker(expected_lang)
    assert chunker is not None
    assert isinstance(chunker, chunker_cls)


@pytest.mark.parametrize("lang", ["csharp", "fsharp", "visual_basic"])
def test_dispatch_resolver_source_languages_have_no_resolver(lang):
    """C#/F#/VB source files get no resolver — their using/open/Imports
    reference namespaces, not files; .NET file edges come from MSBuild
    <ProjectReference> only (knowledge/resolvers/__init__.py docstring)."""
    assert dispatch_resolver(lang) == []


def test_dispatch_resolver_msbuild_returns_msbuild_resolver():
    resolvers = dispatch_resolver("msbuild")
    assert len(resolvers) == 1
    assert isinstance(resolvers[0], MSBuildResolver)


@pytest.mark.parametrize("ext", [".csproj", ".fsproj", ".vbproj"])
def test_dispatch_resolver_msbuild_with_file_path(ext):
    """dispatch_resolver ignores file_path for simple (non-YAML) langs, but
    passing one (as the indexer always does) must not change the result."""
    resolvers = dispatch_resolver("msbuild", Path(f"src/App/App{ext}"))
    assert len(resolvers) == 1
    assert isinstance(resolvers[0], MSBuildResolver)


def test_dispatch_chunker_instances_are_cached_per_lang():
    """dispatch_chunker caches one instance per language tag (see
    knowledge/chunkers/__init__.py _INSTANCES) — repeated calls for the same
    .NET lang must return the identical object, matching every other lang."""
    assert dispatch_chunker("csharp") is dispatch_chunker("csharp")
    assert dispatch_chunker("fsharp") is dispatch_chunker("fsharp")
    assert dispatch_chunker("visual_basic") is dispatch_chunker("visual_basic")
    assert dispatch_chunker("msbuild") is dispatch_chunker("msbuild")


def test_dispatch_chunker_unknown_lang_returns_none():
    assert dispatch_chunker("cobol") is None
