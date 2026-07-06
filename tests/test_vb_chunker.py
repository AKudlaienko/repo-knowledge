"""Tests for the Visual Basic chunker (tree-sitter grammar ``"vb"``).

Written alongside the .NET language support plan (see
``tasks/net_implementation.md``, VB bullet). ``VisualBasicChunker`` is
imported directly from ``knowledge.chunkers.vb_chunker`` — the registry
(``knowledge/chunkers/__init__.py``) is not wired up yet, so
``dispatch_chunker`` cannot be used here.

Covers: namespaces (including nested), all six flat type-level kinds
(class/struct/interface/enum/module/delegate), qualified names, Imports
landing in ``module_level``, exact byte slices, valid line ranges, and
non-overlapping gap chunks — matching the grammar node names discovered by
parsing real VB samples (documented in ``knowledge/chunkers/vb_chunker.py``'s
module docstring).
"""
from __future__ import annotations

from pathlib import Path

from knowledge.chunkers.vb_chunker import VisualBasicChunker


def _line_count(source: str) -> int:
    return source.count("\n") + 1


def _assert_sane_lines(chunk, total_lines: int) -> None:
    assert 1 <= chunk.start_line <= chunk.end_line <= total_lines


def _assert_exact_slice(chunk, source_bytes: bytes) -> None:
    assert chunk.text == source_bytes[chunk.start_byte : chunk.end_byte].decode("utf-8")


def _assert_no_overlaps(chunks) -> None:
    spans = sorted((c.start_byte, c.end_byte) for c in chunks)
    for prev, cur in zip(spans, spans[1:]):
        assert cur[0] >= prev[1], f"overlapping spans: {prev} and {cur}"


VB_SOURCE = '''Option Strict On

Imports System
Imports System.Collections.Generic

Namespace Demo.App

    Public Class Widget
        Inherits BaseWidget
        Implements IWidget

        Private _name As String

        Public Sub New(name As String)
            _name = name
        End Sub

        Public Function GetName() As String
            Return _name
        End Function
    End Class

    Public Structure Point
        Public X As Integer
        Public Y As Integer
    End Structure

    Public Interface IWidget
        Function GetName() As String
        Sub Render()
    End Interface

    Public Enum Color
        Red
        Green
        Blue
    End Enum

    Public Module Helpers
        Public Function Add(a As Integer, b As Integer) As Integer
            Return a + b
        End Function
    End Module

    Public Delegate Function Callback(x As Integer) As Integer

End Namespace

Namespace Demo.Nested
    Namespace Inner
        Public Class Deep
            Public Sub DoIt()
            End Sub
        End Class
    End Namespace
End Namespace

Public Class TopLevel
    Public Sub NoOp()
    End Sub
End Class
'''


def _chunk_source():
    chunker = VisualBasicChunker()
    source_bytes = VB_SOURCE.encode("utf-8")
    return chunker.chunk(source_bytes, Path("Widget.vb")), source_bytes


def test_vb_chunker_emits_all_six_type_kinds():
    chunks, _ = _chunk_source()
    kinds = {c.kind for c in chunks}
    assert {"class", "struct", "interface", "enum", "module", "delegate", "module_level"} <= kinds


def test_vb_chunker_sane_lines_and_exact_slices():
    chunks, source_bytes = _chunk_source()
    total_lines = _line_count(VB_SOURCE)

    assert chunks, "expected at least one chunk"
    for c in chunks:
        _assert_sane_lines(c, total_lines)
        _assert_exact_slice(c, source_bytes)


def test_vb_chunker_gap_chunks_do_not_overlap_anything():
    chunks, _ = _chunk_source()
    _assert_no_overlaps(chunks)


def test_vb_chunker_qualified_names_include_namespace():
    chunks, _ = _chunk_source()

    widget = next(c for c in chunks if c.kind == "class" and c.name == "Widget")
    assert widget.qualified_name == "Demo.App.Widget"
    # methods stay embedded (flat model) — no separate member chunks
    assert "Public Sub New" in widget.text
    assert "Public Function GetName" in widget.text

    point = next(c for c in chunks if c.kind == "struct")
    assert point.name == "Point"
    assert point.qualified_name == "Demo.App.Point"

    iface = next(c for c in chunks if c.kind == "interface")
    assert iface.name == "IWidget"
    assert iface.qualified_name == "Demo.App.IWidget"

    enum_chunk = next(c for c in chunks if c.kind == "enum")
    assert enum_chunk.name == "Color"
    assert enum_chunk.qualified_name == "Demo.App.Color"
    assert "Red" in enum_chunk.text and "Green" in enum_chunk.text and "Blue" in enum_chunk.text

    module_chunk = next(c for c in chunks if c.kind == "module")
    assert module_chunk.name == "Helpers"
    assert module_chunk.qualified_name == "Demo.App.Helpers"

    delegate_chunk = next(c for c in chunks if c.kind == "delegate")
    assert delegate_chunk.name == "Callback"
    assert delegate_chunk.qualified_name == "Demo.App.Callback"


def test_vb_chunker_handles_nested_namespaces():
    chunks, _ = _chunk_source()
    deep = next(c for c in chunks if c.name == "Deep")
    assert deep.kind == "class"
    assert deep.qualified_name == "Demo.Nested.Inner.Deep"


def test_vb_chunker_type_without_namespace_has_bare_qualified_name():
    chunks, _ = _chunk_source()
    top_level = next(c for c in chunks if c.name == "TopLevel")
    assert top_level.kind == "class"
    assert top_level.qualified_name == "TopLevel"


def test_vb_chunker_imports_and_option_land_in_module_level():
    chunks, _ = _chunk_source()
    module_level_chunks = [c for c in chunks if c.kind == "module_level"]
    assert module_level_chunks

    combined = "\n".join(c.text for c in module_level_chunks)
    assert "Option Strict On" in combined
    assert "Imports System" in combined
    assert "Imports System.Collections.Generic" in combined

    # module_level chunks carry no name/qualified_name (gap chunks, not symbols)
    for c in module_level_chunks:
        assert c.name is None
        assert c.qualified_name is None


def test_vb_chunker_empty_source_returns_no_chunks():
    chunker = VisualBasicChunker()
    assert chunker.chunk(b"", Path("Empty.vb")) == []
