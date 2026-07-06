"""MSBuild project-reference resolver — tree-sitter-xml, pure.

``.csproj`` / ``.fsproj`` / ``.vbproj`` files are XML, so we reuse the
pinned ``tree-sitter-language-pack`` ``xml`` grammar (see the
``tree-sitter-language-pack-migration`` decision — verified working at
1.6.x) rather than writing a bespoke MSBuild parser. Root node is
``document``; every tag is an ``element`` wrapping either an ``STag`` +
``content`` + ``ETag`` triple (has children) or a self-closing
``EmptyElemTag``. Attribute values keep their surrounding quote
characters as the first/last child token — the literal value is
everything between them, no separate text node.

Edges emitted:

* ``dotnet_project_reference`` — one per literal ``Include`` path on a
  ``<ProjectReference>`` element. Semicolon-separated ``Include`` lists
  (``a.csproj;b.fsproj``) split into one edge per entry. ``raw`` is
  preserved **verbatim** (Windows ``\\`` separators included) — path
  normalization and resolution both happen downstream in
  ``relations._resolve_msbuild``, per the resolver-stays-pure rule.
* ``unresolved`` — an ``Include`` entry containing an MSBuild property
  expression (``$(ProjectDir)``) or a glob character (``*``). These
  can't be resolved statically; ``raw`` keeps the original expression
  visible to the LLM (existing convention — ``insert_edges``/
  ``resolve_edges`` bypass resolution for this kind).

MSBuild element and attribute names are case-insensitive in practice
(``dotnet build`` accepts ``<projectreference Include=...>``), so both
the tag name and the ``Include`` attribute name are matched
case-insensitively.

Deliberately ignored (never produce an edge): ``PackageReference``
(NuGet, not a project file), assembly ``Reference`` (GAC/DLL), solution
membership, ``<Import>`` (props/targets — out of scope for v1 per the
.NET support plan), and namespace imports (C# ``using`` / VB
``Imports`` — those aren't XML elements in project files at all, they
live in source files and don't reference other files).
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter_language_pack import get_parser

from .base import BaseResolver, Edge

_TAG_NODE_TYPES = ("STag", "EmptyElemTag")


class MSBuildResolver(BaseResolver):
    def __init__(self) -> None:
        self._parser = get_parser("xml")

    def extract(
        self,
        source_bytes: bytes,
        file_path: Path | None = None,
    ) -> list[Edge]:
        tree = self._parser.parse(source_bytes)
        edges: list[Edge] = []
        self._walk(tree.root_node, source_bytes, edges)
        return edges

    # ---- walker -------------------------------------------------------

    def _walk(self, node, src: bytes, out: list[Edge]) -> None:
        if node.type == "element":
            self._handle_element(node, src, out)
        for child in node.children:
            self._walk(child, src, out)

    def _handle_element(self, node, src: bytes, out: list[Edge]) -> None:
        tag_node = None
        for child in node.children:
            if child.type in _TAG_NODE_TYPES:
                tag_node = child
                break
        if tag_node is None:
            return

        # tree-sitter-xml doesn't expose grammar field names (no
        # ``child_by_field_name`` support) — match by node type instead.
        name_node = None
        for child in tag_node.children:
            if child.type == "Name":
                name_node = child
                break
        if name_node is None:
            return

        tag_name = _text(name_node, src)
        if tag_name.lower() != "projectreference":
            return

        include_value = self._find_attr_value(tag_node, src, "include")
        if include_value is None:
            return

        line = node.start_point[0] + 1
        for entry in include_value.split(";"):
            raw = entry.strip()
            if not raw:
                continue
            if "$(" in raw or "*" in raw:
                out.append(Edge(kind="unresolved", raw=raw, symbol=None, line=line))
            else:
                out.append(
                    Edge(
                        kind="dotnet_project_reference",
                        raw=raw,
                        symbol=None,
                        line=line,
                    )
                )

    @staticmethod
    def _find_attr_value(tag_node, src: bytes, attr_name_lower: str) -> str | None:
        """Return the unquoted value of the first attribute matching
        ``attr_name_lower`` (case-insensitive), or None if absent.
        """
        for child in tag_node.children:
            if child.type != "Attribute":
                continue
            attr_name = None
            att_value = None
            for grandchild in child.children:
                if grandchild.type == "Name" and attr_name is None:
                    attr_name = grandchild
                elif grandchild.type == "AttValue":
                    att_value = grandchild
            if attr_name is None or att_value is None:
                continue
            if _text(attr_name, src).lower() != attr_name_lower:
                continue
            return _unquote(_text(att_value, src))
        return None


def _text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _unquote(value: str) -> str:
    """Strip the surrounding quote characters an ``AttValue`` node keeps
    as part of its span (tree-sitter-xml has no separate text node for
    the content between the quotes)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value
