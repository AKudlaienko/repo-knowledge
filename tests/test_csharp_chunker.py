"""Tests for the C# chunker (tree-sitter-language-pack ``csharp`` grammar).

Imports ``CSharpChunker`` directly from ``knowledge.chunkers.csharp_chunker``
— per tasks/net_implementation.md this language isn't wired into the
chunker registry yet, so ``dispatch_chunker`` must not be used here.

Style follows tests/test_chunkers_smoke.py: exact byte slices, sane 1-based
line ranges, and kind-name coverage. Additionally asserts the C#-specific
requirements from the plan: qualified names include the namespace (both
block and file-scoped forms, including nesting), and ``module_level`` gap
chunks never overlap a type chunk's byte range.
"""
from __future__ import annotations

from pathlib import Path

from knowledge.chunkers.csharp_chunker import CSharpChunker


def _line_count(source: str) -> int:
    return source.count("\n") + 1


def _assert_sane_lines(chunk, total_lines: int) -> None:
    assert 1 <= chunk.start_line <= chunk.end_line <= total_lines


def _assert_exact_slice(chunk, source_bytes: bytes) -> None:
    assert chunk.text == source_bytes[chunk.start_byte : chunk.end_byte].decode("utf-8")


def _assert_no_overlaps(chunks) -> None:
    """No two chunks' byte ranges may overlap (module_level gaps included)."""
    ordered = sorted(chunks, key=lambda c: c.start_byte)
    for prev, cur in zip(ordered, ordered[1:]):
        assert prev.end_byte <= cur.start_byte, (
            f"{prev.kind}[{prev.start_byte}:{prev.end_byte}] overlaps "
            f"{cur.kind}[{cur.start_byte}:{cur.end_byte}]"
        )


FILE_SCOPED_SOURCE = '''using System;
using System.Collections.Generic;

namespace Demo.App;

public class Widget
{
    private int _x;

    public void Foo()
    {
        _x = 1;
    }
}

public struct Point
{
    public int X;
    public int Y;
}

public interface IWidget
{
    void Foo();
}

public record Person(string Name, int Age);

public enum Color
{
    Red,
    Green,
    Blue
}

public delegate void Handler(object sender);
'''


def test_file_scoped_namespace_all_kinds():
    chunker = CSharpChunker()
    source_bytes = FILE_SCOPED_SOURCE.encode("utf-8")
    chunks = chunker.chunk(source_bytes, Path("Widget.cs"))

    assert chunks, "expected at least one chunk"
    kinds = {c.kind for c in chunks}
    assert kinds == {"class", "struct", "interface", "record", "enum", "delegate", "module_level"}

    total_lines = _line_count(FILE_SCOPED_SOURCE)
    for c in chunks:
        _assert_sane_lines(c, total_lines)
        _assert_exact_slice(c, source_bytes)

    _assert_no_overlaps(chunks)

    # Qualified names include the file-scoped namespace.
    by_kind = {c.kind: c for c in chunks}
    assert by_kind["class"].name == "Widget"
    assert by_kind["class"].qualified_name == "Demo.App.Widget"
    assert "public void Foo()" in by_kind["class"].text  # methods stay embedded (flat model)

    assert by_kind["struct"].qualified_name == "Demo.App.Point"
    assert by_kind["interface"].qualified_name == "Demo.App.IWidget"
    assert by_kind["record"].qualified_name == "Demo.App.Person"
    assert by_kind["enum"].qualified_name == "Demo.App.Color"
    assert by_kind["delegate"].qualified_name == "Demo.App.Handler"

    # using directives + the file-scoped namespace header land in module_level.
    module_chunks = [c for c in chunks if c.kind == "module_level"]
    assert any("using System;" in c.text for c in module_chunks)
    assert any("namespace Demo.App;" in c.text for c in module_chunks)
    for c in module_chunks:
        assert c.name is None
        assert c.qualified_name is None


BLOCK_NAMESPACE_SOURCE = '''using System;

namespace Demo
{
    using System.Text;

    namespace App
    {
        public class Widget
        {
            public void Foo() { }
        }
    }

    public class Outer
    {
        public class Inner { }
    }
}
'''


def test_block_namespace_nested():
    chunker = CSharpChunker()
    source_bytes = BLOCK_NAMESPACE_SOURCE.encode("utf-8")
    chunks = chunker.chunk(source_bytes, Path("Nested.cs"))

    total_lines = _line_count(BLOCK_NAMESPACE_SOURCE)
    for c in chunks:
        _assert_sane_lines(c, total_lines)
        _assert_exact_slice(c, source_bytes)

    _assert_no_overlaps(chunks)

    by_qname = {c.qualified_name: c for c in chunks if c.kind == "class"}
    assert "Demo.App.Widget" in by_qname
    assert "Demo.Outer" in by_qname

    # Inner is nested inside Outer's own body -> stays embedded, no separate
    # chunk (same flat-model rule as a nested Python class).
    assert "Demo.Outer.Inner" not in by_qname
    assert "public class Inner" in by_qname["Demo.Outer"].text

    # The "using System.Text;" inside the Demo namespace body is real content
    # sitting in the gap before the nested App namespace -> must be
    # searchable via some module_level chunk.
    module_chunks = [c for c in chunks if c.kind == "module_level"]
    assert any("using System.Text;" in c.text for c in module_chunks)
    assert any("using System;" in c.text for c in module_chunks)


CSX_SOURCE = '''using System;

Console.WriteLine("Hello, world!");
var total = 0;
for (var i = 0; i < 10; i++)
{
    total += i;
}
Console.WriteLine(total);
'''


def test_csx_top_level_statements():
    chunker = CSharpChunker()
    source_bytes = CSX_SOURCE.encode("utf-8")
    chunks = chunker.chunk(source_bytes, Path("script.csx"))

    assert chunks, "expected at least one chunk"
    kinds = {c.kind for c in chunks}
    assert kinds == {"module_level"}  # no type declarations at all

    total_lines = _line_count(CSX_SOURCE)
    for c in chunks:
        _assert_sane_lines(c, total_lines)
        _assert_exact_slice(c, source_bytes)

    _assert_no_overlaps(chunks)

    combined = "".join(c.text for c in chunks)
    assert "Console.WriteLine(total)" in combined
    assert "for (var i = 0; i < 10; i++)" in combined


MIXED_GAP_SOURCE = '''using System;

[Serializable]
public class First { public int A; }

// a stray top-level statement between types is unusual outside a script,
// but the grammar allows it and the gap chunk must isolate it correctly.
var sideNote = "between types";

public class Second { public int B; }
'''


def test_module_level_gaps_do_not_duplicate_type_bodies():
    chunker = CSharpChunker()
    source_bytes = MIXED_GAP_SOURCE.encode("utf-8")
    chunks = chunker.chunk(source_bytes, Path("Gaps.cs"))

    _assert_no_overlaps(chunks)

    total_lines = _line_count(MIXED_GAP_SOURCE)
    for c in chunks:
        _assert_sane_lines(c, total_lines)
        _assert_exact_slice(c, source_bytes)

    class_chunks = {c.name: c for c in chunks if c.kind == "class"}
    assert set(class_chunks) == {"First", "Second"}
    # The attribute stays attached to the type it decorates.
    assert "[Serializable]" in class_chunks["First"].text

    module_chunks = [c for c in chunks if c.kind == "module_level"]
    # The gap between First and Second carries the stray statement, and no
    # module_level chunk's text contains either class's full declaration.
    assert any("sideNote" in c.text for c in module_chunks)
    for c in module_chunks:
        assert "public class First" not in c.text
        assert "public class Second" not in c.text
