"""Visual Basic chunker — tree-sitter-based (``tree_sitter_language_pack``,
grammar name ``"vb"``, pinned ``tree-sitter-language-pack>=1.6.1,<1.7``).

Flat model, mirroring ``python_chunker`` / ``javascript_chunker``:

* Six flat type-level kinds, each the FULL body of the declaration. All six
  appear in the grammar as the single child of a ``type_declaration``
  wrapper node with an IDENTICAL byte span, so this chunker unwraps it and
  chunks the inner block directly:

  - ``class``     — VB ``Class``     → grammar node ``class_block``
  - ``struct``    — VB ``Structure`` → grammar node ``structure_block``
  - ``interface`` — VB ``Interface`` → grammar node ``interface_block``
  - ``enum``      — VB ``Enum``      → grammar node ``enum_block``
  - ``module``    — VB ``Module``    → grammar node ``module_block``
  - ``delegate``  — VB ``Delegate ... (Sub|Function)`` → grammar node
    ``delegate_declaration`` (note: no ``_block`` suffix, unlike the rest).

  Each block exposes its name via a ``name`` field (an ``identifier``
  node) regardless of kind, so a single extraction path handles all six.

* Methods/Subs/Functions/Properties (``method_declaration``,
  ``constructor_declaration``, ``property_declaration``, ...) are never
  extracted separately — they stay embedded in their containing type's
  full text, same flat-model convention as Python/JS. Nested type
  declarations (a ``Class`` inside a ``Class``) are likewise left embedded
  rather than walked into, for the same reason.

* Namespaces (``namespace_block``) are NOT chunks themselves; they only
  contribute to ``qualified_name`` (e.g. ``Demo.App.Widget``). Namespaces
  nest — ``Namespace Outer`` / ``Namespace Inner`` produce a
  ``namespace_block`` inside a ``namespace_block`` — and this chunker
  recurses, joining prefixes with ``.``. A single dotted statement
  (``Namespace Demo.App``) is already ONE node whose ``name`` field
  (a ``namespace_name`` node) carries the full dotted text, so no extra
  joining is needed for that case.

* ``module_level`` gap chunks cover everything that isn't a type or a
  namespace container: ``Imports``, ``Option ... On/Off``, attribute
  blocks (``<Assembly: ...>``), comments, and blank lines — one chunk per
  MAXIMAL RUN of consecutive gap siblings, flushed whenever a type or
  namespace boundary is hit. Per-run (rather than one first-to-last span
  per file, as the older chunkers use) is required here to keep gap
  chunks non-overlapping with type chunks that sit between two gap runs.

  CAUTION (grammar limitation, verified against real samples, not a bug
  in this code): this grammar does NOT expose the literal ``Namespace`` /
  ``End Namespace`` (or ``End Class`` / ``End Module`` / ...) keywords as
  named — or even anonymous — child nodes; those bytes sit inside the
  parent's span with no child node covering them. So the "namespace
  header" gap chunk is only the ``namespace_name`` field's own span (e.g.
  ``Demo.App``), not the full ``Namespace Demo.App`` line. Nothing reads
  those keyword-only bytes; they are simply not chunked, matching the
  brief ("let it fall into module_level gaps rather than crashing" — here
  there is no crash risk, just a byte range no node claims).

* Also verified against a real sample: ``Inherits`` / ``Implements``
  clauses on a class parse into an ``ERROR`` node in this grammar version
  instead of a clean named clause. That's harmless for chunking purposes
  because the whole clause is still a byte-range CHILD of the class
  block, so the exact byte slice of the ``class`` chunk includes it
  either way — this chunker never inspects those clauses, it only takes
  the block's overall byte span.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter_language_pack import get_parser

from .base import BaseChunker, Chunk

# grammar node type -> chunk kind (see module docstring for the mapping story)
_TYPE_BLOCK_KINDS = {
    "class_block": "class",
    "structure_block": "struct",
    "interface_block": "interface",
    "enum_block": "enum",
    "module_block": "module",
    "delegate_declaration": "delegate",
}


class VisualBasicChunker(BaseChunker):
    def __init__(self) -> None:
        self._parser = get_parser("vb")

    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        tree = self._parser.parse(source_bytes)
        root = tree.root_node

        chunks: list[Chunk] = []
        self._walk_container(root, source_bytes, chunks, namespace_prefix=None)
        return chunks

    # ---- helpers ----------------------------------------------------------

    def _walk_container(
        self,
        container,
        source_bytes: bytes,
        out: list[Chunk],
        namespace_prefix: str | None,
    ) -> None:
        """Walk one nesting level (``source_file`` or a ``namespace_block``'s
        own body) in document order.

        Type declarations and nested namespaces are handled individually;
        every other sibling accumulates into ``gap_run`` and is flushed as
        one ``module_level`` chunk whenever a type/namespace boundary is
        hit (or at the end of the container). Because a flush always
        happens right before recursing/extracting, each gap run is a
        maximal span of CONSECUTIVE non-type/non-namespace siblings —
        disjoint from every type chunk and every nested namespace's own
        gap chunks by construction.
        """
        gap_run: list = []

        for child in container.children:
            if child.type == "namespace_block":
                self._flush_gap(gap_run, source_bytes, out)
                gap_run = []
                name_node = child.child_by_field_name("name")
                ns_name = self._node_text(name_node, source_bytes) if name_node is not None else None
                new_prefix = self._join_prefix(namespace_prefix, ns_name)
                self._walk_container(child, source_bytes, out, new_prefix)
                continue

            if child.type == "type_declaration":
                self._flush_gap(gap_run, source_bytes, out)
                gap_run = []
                block = self._unwrap_type_declaration(child)
                if block is not None:
                    out.append(self._extract_type_chunk(block, source_bytes, namespace_prefix))
                else:
                    # Unrecognized wrapped kind (grammar surprise) — don't
                    # crash, just let it fall into a module_level gap.
                    gap_run.append(child)
                continue

            gap_run.append(child)

        self._flush_gap(gap_run, source_bytes, out)

    @staticmethod
    def _join_prefix(prefix: str | None, name: str | None) -> str | None:
        if prefix and name:
            return f"{prefix}.{name}"
        return name or prefix

    @staticmethod
    def _unwrap_type_declaration(node):
        """``type_declaration`` -> its single ``*_block``/``delegate_declaration``
        child, or ``None`` if this grammar version wrapped something else.
        """
        for child in node.children:
            if child.type in _TYPE_BLOCK_KINDS:
                return child
        return None

    @staticmethod
    def _extract_type_chunk(block, source_bytes: bytes, namespace_prefix: str | None) -> Chunk:
        kind = _TYPE_BLOCK_KINDS[block.type]
        name_node = block.child_by_field_name("name")
        name = (
            VisualBasicChunker._node_text(name_node, source_bytes)
            if name_node is not None
            else None
        )
        qualified_name = VisualBasicChunker._join_prefix(namespace_prefix, name)

        text_bytes = source_bytes[block.start_byte : block.end_byte]
        return Chunk(
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            start_line=block.start_point[0] + 1,
            end_line=block.end_point[0] + 1,
            start_byte=block.start_byte,
            end_byte=block.end_byte,
            text=text_bytes.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _node_text(node, source_bytes: bytes) -> str:
        return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    @staticmethod
    def _flush_gap(nodes: list, source_bytes: bytes, out: list[Chunk]) -> None:
        if not nodes:
            return
        first, last = nodes[0], nodes[-1]
        text_bytes = source_bytes[first.start_byte : last.end_byte]
        out.append(
            Chunk(
                kind="module_level",
                name=None,
                qualified_name=None,
                start_line=first.start_point[0] + 1,
                end_line=last.end_point[0] + 1,
                start_byte=first.start_byte,
                end_byte=last.end_byte,
                text=text_bytes.decode("utf-8", errors="replace"),
            )
        )
