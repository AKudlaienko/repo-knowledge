"""F# chunker — tree-sitter-based, grammar-driven qualified names.

Emits chunk kinds:

* ``module``       — every ``module X.Y`` (top-of-file, dotted "named" form)
  and every nested ``module Y = ...`` block. Full body text (attributes and
  header included); recursion continues INTO the body so nested constructs
  also get their own chunks (see below). This deliberately overlaps with its
  own children's byte ranges — same as how a Python/JS ``class`` chunk holds
  full text while methods stay embedded, except F# modules additionally
  surface their direct children as separate chunks because module-scoped
  ``let``s are top-level declarations in their own right, not "methods".
* ``type``         — every ``type ...`` definition: records, unions, classes,
  interfaces, abbreviations. Full body, one chunk each (members/cases stay
  embedded — flat model, no separate per-case/per-member chunks).
* ``function`` / ``value`` — every ``let``/``val`` binding directly inside a
  namespace, module, or at the file root.
* ``module_level`` — gap chunks: ``namespace X`` headers (namespace itself
  is NOT a chunk kind — it only contributes a qualifying prefix), ``open``
  statements, and anything else that isn't one of the above.

Qualified names are built by walking the namespace/module nesting chain
recorded by the grammar itself, e.g. ``let add`` inside ``module Math``
inside ``namespace Demo`` -> qualified_name ``Demo.Math.add``. Three forms
are handled, all via the same recursive walk: top-of-file ``namespace X``,
top-of-file ``module X.Y`` (tree-sitter node type ``named_module``), and
nested ``module Y = ...`` blocks (node type ``module_defn``).

Function vs. value — the grammar already makes this distinction for us, so
we use it directly instead of guessing from parameter counts:

* ``.fs`` / ``.fsx`` — a ``let`` binding's LHS node is either
  ``function_declaration_left`` (has params, even a bare ``()``) or
  ``value_declaration_left`` (no params). We classify accordingly.
* ``.fsi`` — signature files use a different LHS shape (``value_definition``
  wrapping ``value_declaration_left`` in both cases), so we classify by the
  type signature instead: if the trailing ``curried_spec`` type node
  contains an ``arguments_spec`` child (i.e. the signature is an arrow type
  with at least one named argument), it's a ``function``; otherwise it's a
  ``value``.

Grammar note (verified locally, tree-sitter-language-pack 1.6.1, root node
type ``file``): the F# grammar's handling of top-level *script* sequencing
(multiple ``let`` bindings at the top of a ``.fsx`` with no enclosing
module) is unreliable — consecutive top-level statements get nested inside
an ``application_expression`` chain rather than appearing as sibling file
children, because the grammar targets module-based programs foremost. Per
the plan, ``.fsx`` files are NOT structurally parsed for functions/values:
the entire script is emitted as a single ``module_level`` chunk. ``.fsi``
files use the separate ``fsharp_signature`` grammar (verified — its root is
also a ``file`` node with an analogous namespace/module/type shape).
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter_language_pack import get_parser

from .base import BaseChunker, Chunk

_CONTAINER_TYPES = ("namespace", "named_module", "module_defn")

# Direct children of a namespace/named_module/module_defn node that are
# either syntax we don't care about (keywords, ``=``) or already fully
# captured by the container's own chunk text (``attributes``). Whatever's
# left after removing these + the name node is the container's body.
_CONTAINER_SKIP_TYPES = {"namespace", "module", "=", "attributes"}

_LEFT_TYPES = {"function_declaration_left", "value_declaration_left"}


class FSharpChunker(BaseChunker):
    def __init__(self) -> None:
        self._parser = get_parser("fsharp")
        self._signature_parser = get_parser("fsharp_signature")

    def chunk(self, source_bytes: bytes, file_path: Path | None = None) -> list[Chunk]:
        if not source_bytes:
            return []

        suffix = file_path.suffix.lower() if file_path is not None else ""

        if suffix == ".fsx":
            return self._chunk_script(source_bytes)

        parser = self._signature_parser if suffix == ".fsi" else self._parser
        tree = parser.parse(source_bytes)

        chunks: list[Chunk] = []
        _walk(tree.root_node.children, None, source_bytes, chunks)
        return chunks

    @staticmethod
    def _chunk_script(source_bytes: bytes) -> list[Chunk]:
        """``.fsx``: one whole-file ``module_level`` chunk — see module
        docstring for why we don't attempt per-statement extraction."""
        text = source_bytes.decode("utf-8", errors="replace")
        return [
            Chunk(
                kind="module_level",
                name=None,
                qualified_name=None,
                start_line=1,
                end_line=text.count("\n") + 1,
                start_byte=0,
                end_byte=len(source_bytes),
                text=text,
            )
        ]


# ---------------------------------------------------------------------------
# Recursive descent over one level of siblings (root children, or a
# container's body). Maintains a contiguous run of "gap" nodes so the
# emitted module_level chunks never overlap each other or any real chunk.
# ---------------------------------------------------------------------------


def _walk(nodes: list, prefix: str | None, source_bytes: bytes, chunks: list[Chunk]) -> None:
    gap_run: list = []

    def flush_gap() -> None:
        if gap_run:
            chunks.append(_gap_chunk(gap_run, source_bytes))
            gap_run.clear()

    for node in nodes:
        if node.start_byte == node.end_byte:
            continue  # zero-width virtual token (grammar-inserted ``;`` etc.)

        if node.type in _CONTAINER_TYPES:
            flush_gap()
            _walk_container(node, prefix, source_bytes, chunks)
            continue

        if node.type == "type_definition":
            flush_gap()
            chunks.append(_type_chunk(node, prefix, source_bytes))
            continue

        if node.type in ("declaration_expression", "value_definition"):
            built = _function_or_value_chunk(node, prefix, source_bytes)
            if built is None:
                gap_run.append(node)
                continue
            flush_gap()
            chunks.append(built)
            continue

        gap_run.append(node)

    flush_gap()


def _walk_container(node, prefix: str | None, source_bytes: bytes, chunks: list[Chunk]) -> None:
    name_node = _direct_name_node(node)
    name = _text(name_node, source_bytes) if name_node is not None else None
    body = _container_body(node, name_node)
    new_prefix = _join(prefix, name)

    if node.type == "namespace":
        # ``namespace`` isn't a chunk kind — only a qualifying prefix — but
        # its header text ("namespace Demo") still needs a home.
        if name_node is not None:
            chunks.append(
                _range_chunk(
                    "module_level",
                    None,
                    None,
                    node.start_byte,
                    name_node.end_byte,
                    node.start_point,
                    name_node.end_point,
                    source_bytes,
                )
            )
    else:
        # named_module (top-of-file ``module X.Y``) or module_defn (nested
        # ``module Y = ...``): both get a ``module`` chunk covering the
        # ENTIRE node (header + full body) — see module docstring.
        chunks.append(
            _range_chunk(
                "module",
                name,
                new_prefix,
                node.start_byte,
                node.end_byte,
                node.start_point,
                node.end_point,
                source_bytes,
            )
        )

    _walk(body, new_prefix, source_bytes, chunks)


# ---------------------------------------------------------------------------
# Leaf chunk builders
# ---------------------------------------------------------------------------


def _type_chunk(node, prefix: str | None, source_bytes: bytes) -> Chunk:
    type_name_node = _find_first(node, {"type_name"})
    name = _text(type_name_node, source_bytes) if type_name_node is not None else None
    qualified_name = _join(prefix, name) if name else prefix
    return _range_chunk(
        "type", name, qualified_name, node.start_byte, node.end_byte, node.start_point, node.end_point, source_bytes
    )


def _function_or_value_chunk(node, prefix: str | None, source_bytes: bytes) -> Chunk | None:
    """Build a function/value chunk, or return None if this node's shape
    doesn't match a pattern we recognize (caller treats it as a gap)."""
    if node.type == "declaration_expression":
        left = _find_first(node, _LEFT_TYPES)
        if left is None:
            return None
        kind = "function" if left.type == "function_declaration_left" else "value"
    elif node.type == "value_definition":
        left = _find_first(node, {"value_declaration_left"})
        if left is None:
            return None
        type_node = node.children[-1] if node.children else None
        has_args = type_node is not None and type_node.type == "curried_spec" and any(
            child.type == "arguments_spec" for child in type_node.children
        )
        kind = "function" if has_args else "value"
    else:
        return None

    name_node = _find_first(left, {"identifier"})
    name = _text(name_node, source_bytes) if name_node is not None else None
    qualified_name = _join(prefix, name) if name else prefix
    return _range_chunk(
        kind, name, qualified_name, node.start_byte, node.end_byte, node.start_point, node.end_point, source_bytes
    )


def _gap_chunk(nodes: list, source_bytes: bytes) -> Chunk:
    first, last = nodes[0], nodes[-1]
    return _range_chunk(
        "module_level", None, None, first.start_byte, last.end_byte, first.start_point, last.end_point, source_bytes
    )


# ---------------------------------------------------------------------------
# Node utilities
# ---------------------------------------------------------------------------


def _direct_name_node(node):
    """First DIRECT child that is a bare/dotted identifier — the container's
    own name. Safe against attribute prefixes (``[<AutoOpen>]``) shifting
    positional indices, since we match by type, not position. Body node
    types (import_decl, module_defn, type_definition, declaration_expression,
    value_definition) never appear as bare identifier/long_identifier
    children, so this can't accidentally pick up a body node."""
    for child in node.children:
        if child.type in ("identifier", "long_identifier"):
            return child
    return None


def _container_body(node, name_node) -> list:
    body = []
    for child in node.children:
        if child is name_node:
            continue
        if child.type in _CONTAINER_SKIP_TYPES:
            continue
        if child.start_byte == child.end_byte:
            continue
        body.append(child)
    return body


def _find_first(node, types: set[str]):
    """DFS for the first descendant (or self) whose type is in ``types``."""
    if node.type in types:
        return node
    for child in node.children:
        found = _find_first(child, types)
        if found is not None:
            return found
    return None


def _join(prefix: str | None, name: str | None) -> str | None:
    if not name:
        return prefix
    return f"{prefix}.{name}" if prefix else name


def _text(node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _range_chunk(
    kind: str,
    name: str | None,
    qualified_name: str | None,
    start_byte: int,
    end_byte: int,
    start_point,
    end_point,
    source_bytes: bytes,
) -> Chunk:
    text = source_bytes[start_byte:end_byte].decode("utf-8", errors="replace")
    return Chunk(
        kind=kind,
        name=name,
        qualified_name=qualified_name,
        start_line=start_point[0] + 1,
        end_line=end_point[0] + 1,
        start_byte=start_byte,
        end_byte=end_byte,
        text=text,
    )
