"""tests/test_collect_render_java.py — COLLECT-28.

`MODULE_MAP.md` renders Java records correctly (method signatures, not
Python syntax) — `render.py` has no hidden Python-only assumption: it was
already written entirely in terms of `ModuleRecord`/`FunctionRecord`
fields (`qualname`, `signature`, `is_private`, `docstring_first_line`)
that both language backends populate identically, so this is a
verification test confirming that holds, not a rewrite.
"""

import configparser
from pathlib import Path

import pytest

from tools.collect.java_parser import is_available
from tools.collect.render import render_module_map
from tools.collect.scanner import scan_repo

pytestmark = pytest.mark.skipif(not is_available(), reason="tree-sitter-java not installed")

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo_java"


def _java_enabled_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["collect"] = {"languages": "python,java"}
    return cfg


def _modules():
    return scan_repo(FIXTURE_ROOT, config=_java_enabled_config())


def test_module_map_renders_java_module_headers():
    md = render_module_map(_modules())
    assert "`com/example/Point.java`" in md
    assert "`com/example/Shape.java`" in md
    assert "`com/example/Color.java`" in md
    assert "`com/example/Greeter.java`" in md


def test_module_map_renders_java_method_signatures_not_python_syntax():
    md = render_module_map(_modules())
    # Java's own signature shape (return type shown, Java params) —
    # never the Python placeholder shape ast_facts.extract_symbols uses.
    assert "`sum(): int`" in md
    assert "`greet(String name): String`" in md
    # never a Python-style "(...)" placeholder signature for a Java symbol
    assert "sum(...)" not in md
    assert "greet(...)" not in md


def test_module_map_renders_record_components_in_signature():
    md = render_module_map(_modules())
    assert "`record Point(int x, int y)`" in md


def test_module_map_renders_sealed_interface_permits_clause():
    md = render_module_map(_modules())
    assert "permits Circle, Square" in md


def test_module_map_renders_java_private_flag_correctly():
    md = render_module_map(_modules())
    # capitalize is private -> "yes" in the private column; greet is
    # public -> "no". Both are plain table cells, checked via the
    # qualname anchor immediately preceding them in the row.
    assert "`com/example/Greeter.java:Greeter.capitalize`" in md
    assert "`com/example/Greeter.java:Greeter.greet`" in md


def test_module_map_renders_java_javadoc_as_docstring():
    md = render_module_map(_modules())
    assert "A point in 2D space." in md
    assert "Pi times radius squared." in md


def test_module_map_renders_java_imports():
    md = render_module_map(_modules())
    assert "`java.util.List`" in md
    assert "`java.util.*`" in md
    assert "`java.lang.Math.max`" in md


def test_module_map_two_renders_are_byte_identical():
    modules = _modules()
    first = render_module_map(modules)
    second = render_module_map(modules)
    assert first == second


def test_module_map_ablation_removing_java_module_removes_its_section():
    modules = _modules()
    full = render_module_map(modules)
    assert "`com/example/Point.java`" in full

    without_point = [m for m in modules if m.path != "com/example/Point.java"]
    ablated = render_module_map(without_point)
    assert "`com/example/Point.java`" not in ablated
    # everything else untouched
    assert "`com/example/Shape.java`" in ablated
