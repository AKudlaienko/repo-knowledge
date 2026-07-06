"""C# chunker — tree-sitter-based.

Flat top-level chunks, mirroring ``python_chunker``/``javascript_chunker``:

* ``class`` / ``struct`` / ``interface`` / ``record`` / ``enum`` / ``delegate``
  — each namespace-level type declaration, FULL body text (methods/members
  stay embedded; nested types stay embedded too, same as a nested Python
  class inside a class body — no separate chunk).
* ``module_level`` — using directives, namespace headers, top-level
  statements (C# 9 scripts/``.csx``), and attributes that sit between type
  declarations. Unlike the single file-spanning ``module_level`` chunk in
  ``python_chunker``, gaps here are flushed once per contiguous run so the
  emitted chunks never overlap a type chunk's byte range.

Namespaces (not files) qualify names. Both forms are supported:

* Block namespaces — ``namespace Demo.App { ... }`` (including nesting):
  recurse into the body with an extended prefix; the header/braces that
  aren't covered by a nested chunk fall into the enclosing gap.
* File-scoped namespaces — ``namespace Demo.App;`` (C# 10): update the
  prefix in place for the remaining siblings in the same list; no body to
  recurse into.

``.csx`` script files parse with the same grammar — top-level statements
have no ``compilation_unit``-level type declaration to attach to, so they
land in ``module_level`` like any other gap.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter_language_pack import get_parser

from .base import BaseChunker, Chunk

# tree-sitter-c-sharp node type -> chunk kind. All six share a "name" field.
_TYPE_KINDS = {
    "class_declaration": "class",
    "struct_declaration": "struct",
    "interface_declaration": "interface",
    "record_declaration": "record",
    "enum_declaration": "enum",
    "delegate_declaration": "delegate",
}


class CSharpChunker(BaseChunker):
    def __init__(self) -> None:
        self._parser = get_parser("csharp")

    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        tree = self._parser.parse(source_bytes)
        root = tree.root_node

        chunks: list[Chunk] = []
        self._walk(
            root.children,
            ns_prefix="",
            wrap_start_byte=root.start_byte,
            wrap_start_line=root.start_point[0] + 1,
            wrap_end_byte=root.end_byte,
            wrap_end_line=root.end_point[0] + 1,
            source_bytes=source_bytes,
            chunks=chunks,
        )
        return chunks

    # ---- helpers ----------------------------------------------------------

    def _walk(
        self,
        nodes,
        ns_prefix: str,
        wrap_start_byte: int,
        wrap_start_line: int,
        wrap_end_byte: int,
        wrap_end_line: int,
        source_bytes: bytes,
        chunks: list[Chunk],
    ) -> None:
        """Emit type chunks for ``nodes`` plus non-overlapping gap chunks.

        ``wrap_start``/``wrap_end`` bound the gap runs: the compilation unit
        passes file start/end (0 / EOF); a recursed block namespace passes
        its own start/end so the "namespace X {" header and closing "}" —
        which aren't inside any child node — still land in a gap chunk
        instead of being silently dropped.
        """
        gap_start_byte, gap_start_line = wrap_start_byte, wrap_start_line

        for node in nodes:
            kind = _TYPE_KINDS.get(node.type)
            if kind is not None:
                self._flush_gap(
                    gap_start_byte, gap_start_line,
                    node.start_byte, node.start_point[0] + 1,
                    source_bytes, chunks,
                )
                chunks.append(self._extract_type(node, ns_prefix, source_bytes, kind))
                gap_start_byte, gap_start_line = node.end_byte, node.end_point[0] + 1

            elif node.type == "namespace_declaration":
                # Block namespace: recurse into its body with an extended
                # prefix. The header text before the body and the closing
                # brace after it are covered by the recursive call's own
                # wrap_start/wrap_end (see docstring above).
                self._flush_gap(
                    gap_start_byte, gap_start_line,
                    node.start_byte, node.start_point[0] + 1,
                    source_bytes, chunks,
                )
                new_prefix = self._extend_prefix(ns_prefix, node, source_bytes)
                body = node.child_by_field_name("body")
                if body is not None:
                    self._walk(
                        body.children,
                        new_prefix,
                        node.start_byte, node.start_point[0] + 1,
                        node.end_byte, node.end_point[0] + 1,
                        source_bytes, chunks,
                    )
                gap_start_byte, gap_start_line = node.end_byte, node.end_point[0] + 1

            elif node.type == "file_scoped_namespace_declaration":
                # No body — just switches the prefix for the remaining
                # siblings in this list. The header line itself stays part
                # of whatever gap run is flushed next (using directives
                # before it, blank lines after it, etc.).
                ns_prefix = self._extend_prefix(ns_prefix, node, source_bytes)

            # else: using directives, attributes, top-level statements —
            # accumulate silently; their bytes are already inside whatever
            # gap span gets flushed at the next boundary.

        self._flush_gap(
            gap_start_byte, gap_start_line,
            wrap_end_byte, wrap_end_line,
            source_bytes, chunks,
        )

    @staticmethod
    def _extend_prefix(ns_prefix: str, ns_node, source_bytes: bytes) -> str:
        name_node = ns_node.child_by_field_name("name")
        if name_node is None:
            return ns_prefix
        ns_name = source_bytes[name_node.start_byte : name_node.end_byte].decode(
            "utf-8", errors="replace"
        )
        return f"{ns_prefix}.{ns_name}" if ns_prefix else ns_name

    @staticmethod
    def _extract_type(node, ns_prefix: str, source_bytes: bytes, kind: str) -> Chunk:
        name_node = node.child_by_field_name("name")
        name = None
        if name_node is not None:
            name = source_bytes[name_node.start_byte : name_node.end_byte].decode(
                "utf-8", errors="replace"
            )

        qualified_name = f"{ns_prefix}.{name}" if ns_prefix and name else name

        text_bytes = source_bytes[node.start_byte : node.end_byte]
        return Chunk(
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            text=text_bytes.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _flush_gap(
        start_byte: int, start_line: int,
        end_byte: int, end_line: int,
        source_bytes: bytes, chunks: list[Chunk],
    ) -> None:
        """Emit one ``module_level`` chunk for ``[start_byte, end_byte)``.

        Skips empty and whitespace-only spans so blank lines between types
        don't produce a flood of content-free chunks.
        """
        if end_byte <= start_byte:
            return
        text = source_bytes[start_byte:end_byte].decode("utf-8", errors="replace")
        if not text.strip():
            return
        chunks.append(
            Chunk(
                kind="module_level",
                name=None,
                qualified_name=None,
                start_line=start_line,
                end_line=end_line,
                start_byte=start_byte,
                end_byte=end_byte,
                text=text,
            )
        )
