"""Tests for the F# chunker (tree-sitter-language-pack ``fsharp`` /
``fsharp_signature`` grammars).

Imports ``FSharpChunker`` directly from ``knowledge.chunkers.fsharp_chunker``
— the chunker registry (``knowledge/chunkers/__init__.py``) is not wired up
yet, so this deliberately bypasses ``dispatch_chunker``.
"""
from __future__ import annotations

from pathlib import Path

from knowledge.chunkers.fsharp_chunker import FSharpChunker


def _line_count(source: str) -> int:
    return source.count("\n") + 1


def _assert_sane_lines(chunk, total_lines: int) -> None:
    assert 1 <= chunk.start_line <= chunk.end_line <= total_lines


def _assert_exact_slice(chunk, source_bytes: bytes) -> None:
    assert chunk.text == source_bytes[chunk.start_byte : chunk.end_byte].decode("utf-8")


def _assert_no_gap_overlaps(chunks) -> None:
    gaps = sorted((c for c in chunks if c.kind == "module_level"), key=lambda c: c.start_byte)
    for a, b in zip(gaps, gaps[1:]):
        assert a.end_byte <= b.start_byte, f"overlapping module_level chunks: {a} vs {b}"


NAMESPACE_MODULE_SOURCE = '''namespace Demo

open System
open System.Collections.Generic

module Math =
    let add x y = x + y

    let pi = 3.14159

    type Shape =
        | Circle of float
        | Square of float

    module Inner =
        let helper z = z * 2

type Point = { X: float; Y: float }

type IShape =
    abstract member Area : unit -> float

let topLevelFn a b = a + b

let topLevelVal = 42
'''


def test_namespace_module_functions_values_types_smoke():
    chunker = FSharpChunker()
    source_bytes = NAMESPACE_MODULE_SOURCE.encode("utf-8")
    chunks = chunker.chunk(source_bytes, Path("Demo/Math.fs"))

    assert chunks, "expected at least one chunk"
    kinds = {c.kind for c in chunks}
    assert kinds == {"module_level", "module", "function", "value", "type"}

    total_lines = _line_count(NAMESPACE_MODULE_SOURCE)
    for c in chunks:
        _assert_sane_lines(c, total_lines)
        _assert_exact_slice(c, source_bytes)

    _assert_no_gap_overlaps(chunks)

    # namespace header itself isn't a chunk kind, only a qualifying prefix —
    # but its text still needs to land somewhere (module_level gap).
    header_gap = next(c for c in chunks if c.kind == "module_level" and "namespace Demo" in c.text)
    assert header_gap.name is None

    # open statements grouped into a module_level gap.
    opens_gap = next(c for c in chunks if c.kind == "module_level" and "open System" in c.text)
    assert "Collections.Generic" in opens_gap.text

    math_module = next(c for c in chunks if c.kind == "module" and c.name == "Math")
    assert math_module.qualified_name == "Demo.Math"
    assert "let add" in math_module.text  # full body, nested content included

    inner_module = next(c for c in chunks if c.kind == "module" and c.name == "Inner")
    assert inner_module.qualified_name == "Demo.Math.Inner"

    add_fn = next(c for c in chunks if c.kind == "function" and c.name == "add")
    assert add_fn.qualified_name == "Demo.Math.add"

    helper_fn = next(c for c in chunks if c.kind == "function" and c.name == "helper")
    assert helper_fn.qualified_name == "Demo.Math.Inner.helper"

    pi_val = next(c for c in chunks if c.kind == "value" and c.name == "pi")
    assert pi_val.qualified_name == "Demo.Math.pi"

    shape_type = next(c for c in chunks if c.kind == "type" and c.name == "Shape")
    assert shape_type.qualified_name == "Demo.Math.Shape"
    assert "Circle" in shape_type.text and "Square" in shape_type.text

    point_type = next(c for c in chunks if c.kind == "type" and c.name == "Point")
    assert point_type.qualified_name == "Demo.Point"  # top-level, not nested in Math

    ishape_type = next(c for c in chunks if c.kind == "type" and c.name == "IShape")
    assert ishape_type.qualified_name == "Demo.IShape"

    top_fn = next(c for c in chunks if c.kind == "function" and c.name == "topLevelFn")
    assert top_fn.qualified_name == "Demo.topLevelFn"

    top_val = next(c for c in chunks if c.kind == "value" and c.name == "topLevelVal")
    assert top_val.qualified_name == "Demo.topLevelVal"


TOP_OF_FILE_MODULE_SOURCE = """module Demo.Math

open System

let add x y = x + y

let pi = 3.14159
"""


def test_top_of_file_dotted_module():
    chunker = FSharpChunker()
    source_bytes = TOP_OF_FILE_MODULE_SOURCE.encode("utf-8")
    chunks = chunker.chunk(source_bytes, Path("Math.fs"))

    total_lines = _line_count(TOP_OF_FILE_MODULE_SOURCE)
    for c in chunks:
        _assert_sane_lines(c, total_lines)
        _assert_exact_slice(c, source_bytes)
    _assert_no_gap_overlaps(chunks)

    module_chunk = next(c for c in chunks if c.kind == "module")
    assert module_chunk.name == "Demo.Math"
    assert module_chunk.qualified_name == "Demo.Math"
    # the whole-file module chunk includes its nested declarations too.
    assert "let add" in module_chunk.text

    add_fn = next(c for c in chunks if c.kind == "function" and c.name == "add")
    assert add_fn.qualified_name == "Demo.Math.add"

    pi_val = next(c for c in chunks if c.kind == "value" and c.name == "pi")
    assert pi_val.qualified_name == "Demo.Math.pi"

    opens_gap = next(c for c in chunks if c.kind == "module_level")
    assert "open System" in opens_gap.text


FSX_SOURCE = """open System

printfn "hello"

let x = 1 + 1

let addOne y = y + 1
"""


def test_fsx_script_becomes_module_level():
    chunker = FSharpChunker()
    source_bytes = FSX_SOURCE.encode("utf-8")
    chunks = chunker.chunk(source_bytes, Path("script.fsx"))

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.kind == "module_level"
    _assert_exact_slice(chunk, source_bytes)
    _assert_sane_lines(chunk, _line_count(FSX_SOURCE))
    assert chunk.text == FSX_SOURCE
    assert "addOne" in chunk.text


FSI_SOURCE = """namespace Demo

module Math =
    val add: x: int -> y: int -> int
    val pi: float
    type Shape =
        | Circle of float
        | Square of float
"""


def test_fsi_signature_file_uses_signature_grammar():
    chunker = FSharpChunker()
    source_bytes = FSI_SOURCE.encode("utf-8")
    chunks = chunker.chunk(source_bytes, Path("Math.fsi"))

    assert chunks, "expected chunks from the fsharp_signature grammar"
    total_lines = _line_count(FSI_SOURCE)
    for c in chunks:
        _assert_sane_lines(c, total_lines)
        _assert_exact_slice(c, source_bytes)
    _assert_no_gap_overlaps(chunks)

    module_chunk = next(c for c in chunks if c.kind == "module")
    assert module_chunk.qualified_name == "Demo.Math"

    # function-vs-value in .fsi is decided from the `val` type signature
    # shape (arrow with a named argument => function; bare type => value).
    add_val = next(c for c in chunks if c.name == "add")
    assert add_val.kind == "function"
    assert add_val.qualified_name == "Demo.Math.add"

    pi_val = next(c for c in chunks if c.name == "pi")
    assert pi_val.kind == "value"
    assert pi_val.qualified_name == "Demo.Math.pi"

    shape_type = next(c for c in chunks if c.kind == "type" and c.name == "Shape")
    assert shape_type.qualified_name == "Demo.Math.Shape"


UNION_RECORD_SOURCE = """module Shapes

type Color =
    | Red
    | Green
    | Blue

type Point = { X: float; Y: float }

type Circle(radius: float) =
    member this.Area = System.Math.PI * radius * radius
"""


def test_union_record_and_class_types():
    chunker = FSharpChunker()
    source_bytes = UNION_RECORD_SOURCE.encode("utf-8")
    chunks = chunker.chunk(source_bytes, Path("Shapes.fs"))

    total_lines = _line_count(UNION_RECORD_SOURCE)
    for c in chunks:
        _assert_sane_lines(c, total_lines)
        _assert_exact_slice(c, source_bytes)
    _assert_no_gap_overlaps(chunks)

    type_names = {c.name for c in chunks if c.kind == "type"}
    assert {"Color", "Point", "Circle"} <= type_names

    color = next(c for c in chunks if c.kind == "type" and c.name == "Color")
    assert "Red" in color.text and "Green" in color.text and "Blue" in color.text

    circle = next(c for c in chunks if c.kind == "type" and c.name == "Circle")
    assert "member this.Area" in circle.text  # members stay embedded (flat model)


NESTED_MODULE_SOURCE = """namespace Outer

module Level1 =
    module Level2 =
        let deep x = x * 2

        type Inner = { A: int }
"""


def test_deeply_nested_modules_qualify_correctly():
    chunker = FSharpChunker()
    source_bytes = NESTED_MODULE_SOURCE.encode("utf-8")
    chunks = chunker.chunk(source_bytes, Path("Nested.fs"))

    for c in chunks:
        _assert_exact_slice(c, source_bytes)
    _assert_no_gap_overlaps(chunks)

    level1 = next(c for c in chunks if c.kind == "module" and c.name == "Level1")
    assert level1.qualified_name == "Outer.Level1"

    level2 = next(c for c in chunks if c.kind == "module" and c.name == "Level2")
    assert level2.qualified_name == "Outer.Level1.Level2"

    deep_fn = next(c for c in chunks if c.kind == "function" and c.name == "deep")
    assert deep_fn.qualified_name == "Outer.Level1.Level2.deep"

    inner_type = next(c for c in chunks if c.kind == "type" and c.name == "Inner")
    assert inner_type.qualified_name == "Outer.Level1.Level2.Inner"
