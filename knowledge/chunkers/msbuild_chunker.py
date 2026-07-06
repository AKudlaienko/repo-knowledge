"""MSBuild project-file chunker — whole-file, no XML dissection.

``.csproj`` / ``.fsproj`` / ``.vbproj`` files are mostly boilerplate XML
(SDK attribute, a handful of ``ItemGroup``/``PropertyGroup`` blocks) — not
worth breaking into finer-grained chunks. Emits exactly one
``msbuild_project`` chunk spanning the entire file, named after the file's
stem (``Demo.App.csproj`` -> ``Demo.App``, matching the assembly/project
name convention). The indexer's oversized-chunk splitter (``big_split``)
already handles the rare huge project file, so no size handling is needed
here.

Project-reference edges are extracted separately by
``knowledge/resolvers/msbuild_resolver.py`` — this chunker only makes the
file's contents searchable.
"""

from __future__ import annotations

from pathlib import Path

from .base import BaseChunker, Chunk


class MSBuildChunker(BaseChunker):
    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        text = source_bytes.decode("utf-8", errors="replace")
        name = file_path.stem if file_path is not None else None
        total_lines = text.count("\n") + 1 if text else 1

        return [
            Chunk(
                kind="msbuild_project",
                name=name,
                qualified_name=name,
                start_line=1,
                end_line=total_lines,
                start_byte=0,
                end_byte=len(source_bytes),
                text=text,
            )
        ]
