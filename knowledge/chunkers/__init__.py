"""Chunker registry.

``dispatch_chunker(lang)`` returns a cached chunker instance for a language,
or ``None`` if no chunker is implemented for that language yet (scanner
yields files in many languages; indexer skips the ones without a chunker).
"""

from __future__ import annotations

from .base import BaseChunker, Chunk
from .csharp_chunker import CSharpChunker
from .dockerfile_chunker import DockerfileChunker
from .fsharp_chunker import FSharpChunker
from .hcl_chunker import HclChunker
from .javascript_chunker import JavaScriptChunker, TypeScriptChunker
from .jinja_chunker import JinjaChunker
from .json_chunker import JsonChunker
from .markdown_chunker import MarkdownChunker
from .msbuild_chunker import MSBuildChunker
from .python_chunker import PythonChunker
from .shell_chunker import ShellChunker
from .vb_chunker import VisualBasicChunker
from .yaml_chunker import YamlChunker

# Language tag (from config.EXT_TO_LANG) → chunker class. Full coverage:
# every language the scanner recognises has a chunker.
_CHUNKERS: dict[str, type[BaseChunker]] = {
    "python":       PythonChunker,
    "yaml":         YamlChunker,
    "hcl":          HclChunker,
    "javascript":   JavaScriptChunker,
    "typescript":   TypeScriptChunker,
    "json":         JsonChunker,
    "shell":        ShellChunker,
    "jinja":        JinjaChunker,
    "dockerfile":   DockerfileChunker,
    "markdown":     MarkdownChunker,
    "csharp":       CSharpChunker,
    "fsharp":       FSharpChunker,
    "visual_basic": VisualBasicChunker,
    "msbuild":      MSBuildChunker,
}

_INSTANCES: dict[str, BaseChunker] = {}


def dispatch_chunker(lang: str) -> BaseChunker | None:
    cls = _CHUNKERS.get(lang)
    if cls is None:
        return None
    inst = _INSTANCES.get(lang)
    if inst is None:
        inst = cls()
        _INSTANCES[lang] = inst
    return inst


__all__ = ["Chunk", "BaseChunker", "dispatch_chunker"]
